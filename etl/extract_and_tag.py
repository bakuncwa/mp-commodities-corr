"""
Cloud Function equivalent: tags the raw article JSON written by ingest_feeds.py
in two passes, so the feed never depends on LLM quota:

1. Every article is tagged immediately by etl/rule_tagger.py (deterministic
   dictionaries, no API call) and written with tagger='rules'.
2. Gemini then refines queued articles newest-first, BATCH_SIZE articles per
   request (free tier: ~20 requests/day/model), overwriting them with
   tagger='llm'. 503 "high demand" responses are retried with exponential
   backoff and then sent to GEMINI_FALLBACK_MODEL, which has its own quota.
   Articles the LLM can't reach today stay rule-tagged and queued.

The LLM extracts: category first (monetary_policy vs commodity_stocks), then
country/sector/policy-subtype/sentiment, plus which transmission mechanism(s)
the article's content discusses.

GEMINI_API_KEY (see .env.example) enables pass 2; without it, pass 1 still runs
and the run logs that LLM refinement was skipped.

Run directly against the newest raw file: `python -m etl.extract_and_tag`
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from common.db import get_store
from etl.rule_tagger import tag as rule_tag

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract_and_tag")

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
# K_SERVICE is set automatically by Cloud Run/Cloud Functions gen2 — used here
# the same way GCS_RAW_BUCKET signals "deployed" in ingest_feeds.py, since
# only /tmp is writable outside local dev.
_DEPLOYED = bool(os.environ.get("K_SERVICE"))
DEADLETTER_PATH = (
    Path(tempfile.gettempdir()) / "extract_deadletter.jsonl" if _DEPLOYED
    else Path(__file__).resolve().parent.parent / "data" / "cache" / "extract_deadletter.jsonl"
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")  # a Google-maintained alias, not a dated snapshot — avoids repeating the gemini-2.5-flash-lite deprecation this replaced

GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")

# The Gemini free tier is 20 generate_content calls/day per model, shared with
# analysis/interpret.py's on-demand interpretations. Capped below that to leave
# headroom; every attempt (retries included) counts against it. With batching,
# 15 calls refine up to 15 × BATCH_SIZE = 300 articles/day.
DAILY_EXTRACTION_BUDGET = int(os.environ.get("DAILY_EXTRACTION_BUDGET", "15"))
BATCH_SIZE = int(os.environ.get("EXTRACTION_BATCH_SIZE", "20"))
RETRY_DELAYS_S = (10,)  # one backoff retry on 503 UNAVAILABLE, then switch to the fallback model
MIN_SECONDS_BETWEEN_CALLS = 13  # free tier allows ~5 requests/minute
# Stop starting new LLM batches after this long; the Cloud Function times out at
# 540 s, and rows are written per batch, so stopping early loses nothing.
TIME_LIMIT_S = int(os.environ.get("EXTRACTION_TIME_LIMIT_S", "420"))

SECTOR_IDS = ["copper", "lithium", "nickel", "rare_earths", "cobalt", "semiconductors"]
POLICY_SUBTYPES = ["interest_rate", "market_regulation", "export_control", "capital_controls"]
MECHANISM_IDS = ["cost_of_carry", "usd_value", "investment_substitution", "extraction_incentives"]
# Forward-looking signal for the predictive classifier in notebooks/02_correlation_regression.ipynb:
# does this article's own framing read as tightening (contractionary) or
# easing (expansionary) monetary conditions, independent of whether a rate
# move has actually happened yet. Only meaningful for monetary_policy articles.
POLICY_STANCES = ["contractionary", "expansionary", "neutral"]

EXTRACTION_PROMPT = """You are tagging a news article for a monetary-policy / commodity-markets tracker.

Read the article title and summary below and return structured JSON:

1. article_category: "monetary_policy" if the article is primarily about a central bank
   decision, rate policy, market regulation, export controls, or capital controls.
   "commodity_stocks" if it's primarily about a commodity/sector's market performance,
   a mining or semiconductor company, or production/supply news, not a policy decision.
2. country_codes: ISO 3166-1 alpha-2 codes for the country/countries the article is
   substantively about. Empty list if none is identifiable.
3. sector_tags: which of {sectors} the article concerns. Empty list if
   article_category is monetary_policy and no specific commodity is named, or if no
   listed sector applies.
4. policy_subtype: which of {subtypes} apply. Only populate when
   article_category is monetary_policy. Empty list otherwise.
5. sentiment_score: float from -1.0 (very negative for the named sector/country) to
   +1.0 (very positive). 0.0 if neutral/unclear.
6. sentiment_target: short string naming what the sentiment is about (e.g.
   "copper_exporters", "lithium_producers"). Empty string if unclear.
7. mechanisms: which of these transmission-mechanism concepts the article's own
   content actually touches on (not whether they're true, just whether the article
   discusses them) — {mechanisms}. cost_of_carry = inventory/storage/holding-cost
   discussion. usd_value = dollar strength/weakness affecting commodity pricing.
   investment_substitution = capital moving between commodities and bonds/yield assets.
   extraction_incentives = production/extraction rate decisions tied to financing cost.
   Empty list if none apply.
8. policy_stance: one of {stances}. Only populate when article_category is
   monetary_policy; use "neutral" otherwise. "contractionary" = the article's own
   framing points toward tightening (rate hikes, reduced liquidity, stricter
   capital controls, hawkish language). "expansionary" = points toward easing
   (rate cuts, stimulus, relaxed controls, dovish language). "neutral" = holds
   steady, mixed signals, or not clearly one direction. Judge from what the
   article itself says, not from outside knowledge of what "should" happen.

Title: {title}
Summary: {summary}
"""


class Extraction(BaseModel):
    article_category: str
    country_codes: list[str] = Field(default_factory=list)
    sector_tags: list[str] = Field(default_factory=list)
    policy_subtype: list[str] = Field(default_factory=list)
    sentiment_score: float = 0.0
    sentiment_target: str = ""
    mechanisms: list[str] = Field(default_factory=list)
    policy_stance: str = "neutral"


class BatchExtraction(Extraction):
    idx: int


def _client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def _published_utc(value: str | None) -> datetime | None:
    """RSS dates arrive as RFC-822 ("Thu, 16 Jul 2026 18:00:00 GMT") or ISO-8601;
    both become an aware UTC datetime (None if unparseable)."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _batch_prompt(articles: list[dict]) -> str:
    rules = EXTRACTION_PROMPT.split("Title: {title}")[0].format(
        sectors=SECTOR_IDS, subtypes=POLICY_SUBTYPES, mechanisms=MECHANISM_IDS, stances=POLICY_STANCES,
    )
    items = [{"idx": i, "title": a["title"], "summary": (a.get("raw_text") or "")[:1000]} for i, a in enumerate(articles)]
    return (rules + "Apply the rules above to EACH article below independently. Return a JSON array "
            "with exactly one object per article, echoing its idx.\n\nArticles:\n" + json.dumps(items, ensure_ascii=False))


def classify_batch(client, model: str, articles: list[dict]) -> dict[int, Extraction]:
    """One generate_content call for up to BATCH_SIZE articles, constrained to
    a list[BatchExtraction] schema. Returns {index in `articles`: Extraction}."""
    from google.genai import types
    response = client.models.generate_content(
        model=model,
        contents=_batch_prompt(articles),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[BatchExtraction],
            temperature=0.1,
        ),
    )
    parsed = [BatchExtraction.model_validate(o) for o in json.loads(response.text)]
    return {p.idx: Extraction(**p.model_dump(exclude={"idx"})) for p in parsed if 0 <= p.idx < len(articles)}


def classify_and_tag(client, title: str, summary: str) -> Extraction:
    """Single-article form, kept for notebooks/ad-hoc use."""
    out = classify_batch(client, GEMINI_MODEL, [{"title": title, "raw_text": summary}])
    return out[0]


def _latest_raw_file() -> Path | None:
    files = sorted(glob.glob(str(RAW_DIR / "articles_*.json")))
    return Path(files[-1]) if files else None


def _article_id(art: dict) -> str:
    """Stable id (SHA-256 of the URL) for both fact_article and the queue, so an
    LLM re-tag overwrites the rule-tagged row instead of duplicating it."""
    return hashlib.sha256(art["url"].encode()).hexdigest()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_used_today(store) -> int:
    df = store.query_df(
        "SELECT calls_used FROM dim_extraction_budget WHERE date_key = '%s'" % _today_utc()
    )
    return int(df["calls_used"].iloc[0]) if len(df) else 0


def _record_budget_used(store, count: int) -> None:
    import pandas as pd
    store.upsert_df(
        "dim_extraction_budget",
        pd.DataFrame([{"date_key": _today_utc(), "calls_used": count}]),
        ["date_key"],
    )


def _load_queue(store) -> list[dict]:
    df = store.query_df("SELECT article_id, payload FROM fact_extraction_queue ORDER BY queued_at")
    return [{"_queue_id": row.article_id, **json.loads(row.payload)} for row in df.itertuples()]


def _enqueue(store, articles: list[dict]) -> None:
    if not articles:
        return
    import pandas as pd
    now = datetime.now(timezone.utc)
    rows = [{"article_id": _article_id(a), "payload": json.dumps(a), "queued_at": now} for a in articles]
    store.upsert_df("fact_extraction_queue", pd.DataFrame(rows), ["article_id"])


def _id_list(ids) -> str:
    return ", ".join(f"'{i}'" for i in ids)  # SHA-256 hex digests only, never free text


def _dequeue(store, article_ids: list[str]) -> None:
    if article_ids:
        store.execute(f"DELETE FROM fact_extraction_queue WHERE article_id IN ({_id_list(article_ids)})")


def _write_tagged(store, tagged: list[tuple[dict, Extraction]], tagger: str) -> int:
    """Upsert articles + their bridge rows. Existing bridge rows for these ids
    are deleted first, so a re-tag replaces (not adds to) country/sector tags."""
    import pandas as pd
    if not tagged:
        return 0
    sources, articles, countries, sectors = {}, [], [], []
    valid_sectors = set(SECTOR_IDS)
    for art, ex in tagged:
        article_id = _article_id(art)
        source_id = art["source"].lower().replace(" ", "_")
        sources[source_id] = {"source_id": source_id, "source_name": art["source"],
                              "source_type": art["category"], "home_country_code": None}
        published = _published_utc(art.get("published"))
        articles.append({
            "article_id": article_id,
            "date_key": published.strftime("%Y-%m-%d") if published else None,
            "source_id": source_id,
            "article_category": ex.article_category,
            "policy_subtype": ",".join(ex.policy_subtype),
            "sentiment_score": ex.sentiment_score,
            "sentiment_target": ex.sentiment_target,
            "source_url": art["url"],
            "title": art["title"],
            "published": published,
            "mech": ",".join(ex.mechanisms),
            "policy_stance": ex.policy_stance,
            "tagger": tagger,
        })
        countries += [{"article_id": article_id, "country_code": cc} for cc in dict.fromkeys(ex.country_codes)]
        sectors += [{"article_id": article_id, "sector_id": sid} for sid in dict.fromkeys(ex.sector_tags) if sid in valid_sectors]

    ids = [a["article_id"] for a in articles]
    store.upsert_df("dim_source", pd.DataFrame(sources.values()), ["source_id"])
    store.upsert_df("fact_article", pd.DataFrame(articles), ["article_id"])
    for table in ("bridge_article_country", "bridge_article_sector"):
        store.execute(f"DELETE FROM {table} WHERE article_id IN ({_id_list(ids)})")
    if countries:
        store.upsert_df("bridge_article_country", pd.DataFrame(countries), ["article_id", "country_code"])
    if sectors:
        store.upsert_df("bridge_article_sector", pd.DataFrame(sectors), ["article_id", "sector_id"])
    return len(articles)


def _rule_tag(art: dict) -> Extraction:
    return Extraction(**rule_tag(art["title"], art.get("raw_text") or "", art.get("category", ""), art.get("source", "")))


def _call_with_fallback(client, batch: list[dict], spend, models: list[str]) -> dict[int, Extraction] | None:
    """Tries `models` in order with a backoff retry on 503; 429 (daily quota)
    skips straight to the next model. A model that succeeds moves to the front
    of `models`, so later batches in the run skip a model that is overloaded.
    `spend()` records one budget unit per attempt, False once it is gone."""
    for model in list(models):
        for attempt, delay in enumerate((0, *RETRY_DELAYS_S)):
            if not spend():
                return None
            time.sleep(delay)
            try:
                result = classify_batch(client, model, batch)
                models.remove(model)
                models.insert(0, model)
                return result
            except Exception as e:  # google.genai raises ServerError/ClientError; schema errors are ValueError
                msg = str(e)
                log.warning("Gemini %s attempt %d failed for %d articles: %s", model, attempt + 1, len(batch), msg[:160])
                with open(DEADLETTER_PATH, "a") as f:
                    f.write(json.dumps({"model": model, "batch_urls": [a["url"] for a in batch], "error": msg[:500]}) + "\n")
                if "503" in msg or "UNAVAILABLE" in msg:
                    continue  # transient overload: back off and retry this model
                break  # 429 quota / 4xx / malformed output: try the next model
    return None


def run_extraction(raw_path: Path | None = None, gcs_bucket: str | None = None, gcs_object: str | None = None) -> dict:
    """Local dev: defaults to the most recent data/raw/articles_*.json.
    Deployed (Cloud Storage trigger): gcs_bucket/gcs_object identify exactly
    which object just landed — required in production since the ingest and
    extract Cloud Functions don't share local disk between invocations."""
    if gcs_bucket and gcs_object:
        # The Storage trigger fires on every object finalized in the raw-cache
        # bucket, not just new article batches — ingest_feeds.py also rewrites
        # its dedup cache (_state/seen_article_urls.json, a {hash: timestamp}
        # object) in that same bucket, which would otherwise crash this
        # function (iterating a dict yields its string keys, not articles).
        if not gcs_object.startswith("raw/"):
            log.info("Ignoring non-article object gs://%s/%s", gcs_bucket, gcs_object)
            return {"rules": 0, "llm": 0}
        from google.cloud import storage
        blob = storage.Client().bucket(gcs_bucket).blob(gcs_object)
        articles = json.loads(blob.download_as_text())
        if not isinstance(articles, list):
            log.warning("gs://%s/%s is not an article list (got %s) — skipping", gcs_bucket, gcs_object, type(articles).__name__)
            return {"rules": 0, "llm": 0}
        source_label = f"gs://{gcs_bucket}/{gcs_object}"
    else:
        raw_path = raw_path or _latest_raw_file()
        if raw_path is None:
            log.warning("No raw article files found in %s", RAW_DIR)
            return {"rules": 0, "llm": 0}
        articles = json.loads(raw_path.read_text())
        source_label = raw_path.name

    store = get_store()
    DEADLETTER_PATH.parent.mkdir(parents=True, exist_ok=True)

    # --- Pass 1: rule-tag everything not yet in fact_article (new articles,
    # plus any backlog from before this pass existed). Zero API cost. ---
    backlog = _load_queue(store)
    queued_urls = {a["url"] for a in backlog}
    new = [a for a in {a["url"]: a for a in articles}.values() if a["url"] not in queued_urls]
    existing = set(store.query_df("SELECT article_id FROM fact_article")["article_id"])
    untagged = [a for a in backlog + new if _article_id(a) not in existing]
    rules_written = _write_tagged(store, [(a, _rule_tag(a)) for a in untagged], "rules")
    _enqueue(store, [a for a in new if _article_id(a) not in existing])

    # --- Pass 2: LLM refinement, newest first, in batches. ---
    client = _client()
    pending = sorted(_load_queue(store), key=lambda a: a.get("published") or "", reverse=True)
    budget_used = _budget_used_today(store)

    def spend() -> bool:
        nonlocal budget_used
        if budget_used >= DAILY_EXTRACTION_BUDGET:
            return False
        budget_used += 1
        _record_budget_used(store, budget_used)  # persisted per attempt: a crash must never hide a billed call
        return True

    llm_written = 0
    started = time.monotonic()
    models = list(dict.fromkeys([GEMINI_MODEL, GEMINI_FALLBACK_MODEL]))
    if client is None:
        log.warning("GEMINI_API_KEY not set — skipping LLM refinement; %d articles stay rule-tagged", len(pending))
    else:
        for start in range(0, len(pending), BATCH_SIZE):
            if time.monotonic() - started > TIME_LIMIT_S:
                break
            batch = pending[start:start + BATCH_SIZE]
            result = _call_with_fallback(client, batch, spend, models)
            if result is None:
                break  # budget exhausted or both models unavailable — rest stay queued
            refined = [(a, result[i]) for i, a in enumerate(batch) if i in result]
            llm_written += _write_tagged(store, refined, "llm")
            _dequeue(store, [a["_queue_id"] for a, _ in refined])
            time.sleep(MIN_SECONDS_BETWEEN_CALLS)

    remaining = len(pending) - llm_written
    log.info(
        "%s: %d rule-tagged, %d LLM-refined, %d awaiting LLM — %d/%d daily calls used",
        source_label, rules_written, llm_written, remaining, budget_used, DAILY_EXTRACTION_BUDGET,
    )
    return {"rules": rules_written, "llm": llm_written, "queued": remaining}


if __name__ == "__main__":
    run_extraction()
