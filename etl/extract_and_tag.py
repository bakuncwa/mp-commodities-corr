"""
Cloud Function equivalent: takes the raw article JSON written by ingest_feeds.py
and calls the Gemini API once per article for structured extraction — category
first (monetary_policy vs commodity_stocks), then country/sector/policy-subtype/
sentiment, plus which transmission mechanism(s) the article's content actually
discusses (used later to compute the RSS-evidence percentages the frontend
shows per mechanism).

Requires GEMINI_API_KEY (see .env.example). Without it, run_extraction() raises
immediately rather than silently no-op'ing, since a missing key is a setup
error, not an expected runtime condition — the README's fallback behaviors are
about bad *data* (schema-invalid LLM output), not a missing credential.

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

from common.db import get_store, new_id

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

# The Gemini free tier is 20 generate_content calls/day total for this model,
# shared with analysis/interpret.py's on-demand interpretations — observed
# directly as 429 RESOURCE_EXHAUSTED in production once ingest_feeds ran
# hourly. Capped below that (not at 20) to leave headroom for interpret calls
# and manual testing; anything that doesn't fit today's budget is queued in
# fact_extraction_queue rather than force-failed.
DAILY_EXTRACTION_BUDGET = int(os.environ.get("DAILY_EXTRACTION_BUDGET", "15"))

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


def _client():
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (see .env.example) — required to run extraction.")
    return genai.Client(api_key=api_key)


def classify_and_tag(client, title: str, summary: str) -> Extraction:
    from google.genai import types
    prompt = EXTRACTION_PROMPT.format(
        sectors=SECTOR_IDS, subtypes=POLICY_SUBTYPES, mechanisms=MECHANISM_IDS, stances=POLICY_STANCES,
        title=title, summary=summary[:2000],
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Extraction,
            temperature=0.1,
        ),
    )
    return Extraction.model_validate_json(response.text)


def _latest_raw_file() -> Path | None:
    files = sorted(glob.glob(str(RAW_DIR / "articles_*.json")))
    return Path(files[-1]) if files else None


def _article_id(art: dict) -> str:
    """Stable id for queue dedup — same article seen twice (already-queued vs.
    freshly re-ingested) collapses to one queue row rather than two."""
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


def _dequeue(store, article_ids: list[str]) -> None:
    if not article_ids:
        return
    ids = ", ".join(f"'{i}'" for i in article_ids)
    store.execute(f"DELETE FROM fact_extraction_queue WHERE article_id IN ({ids})")


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
            return {"processed": 0, "failed": 0}
        from google.cloud import storage
        blob = storage.Client().bucket(gcs_bucket).blob(gcs_object)
        articles = json.loads(blob.download_as_text())
        if not isinstance(articles, list):
            log.warning("gs://%s/%s is not an article list (got %s) — skipping", gcs_bucket, gcs_object, type(articles).__name__)
            return {"processed": 0, "failed": 0}
        source_label = f"gs://{gcs_bucket}/{gcs_object}"
    else:
        raw_path = raw_path or _latest_raw_file()
        if raw_path is None:
            log.warning("No raw article files found in %s", RAW_DIR)
            return {"processed": 0, "failed": 0}
        articles = json.loads(raw_path.read_text())
        source_label = raw_path.name

    store = get_store()

    # Backlog first (oldest first — FIFO), then this run's newly-arrived
    # articles, deduped by URL so an article already queued from an earlier
    # run doesn't get processed twice just because it showed up again.
    backlog = _load_queue(store)
    seen_urls = {a["url"] for a in backlog}
    candidates = backlog + [a for a in articles if a["url"] not in seen_urls]

    budget_used = _budget_used_today(store)
    remaining = max(0, DAILY_EXTRACTION_BUDGET - budget_used)

    if remaining == 0:
        _enqueue(store, [a for a in candidates if "_queue_id" not in a])
        log.info(
            "Daily extraction budget (%d) already used for %s — queued %d article(s) for a later run",
            DAILY_EXTRACTION_BUDGET, _today_utc(), len(candidates),
        )
        return {"processed": 0, "failed": 0, "queued": len(candidates)}

    to_process, to_queue = candidates[:remaining], candidates[remaining:]
    if to_queue:
        log.info("Processing %d of %d article(s) this run — queuing %d for later (daily budget)", len(to_process), len(candidates), len(to_queue))
        _enqueue(store, [a for a in to_queue if "_queue_id" not in a])

    client = _client()
    source_rows, article_rows, bridge_country_rows, bridge_sector_rows = [], [], [], []
    failed = 0
    processed_queue_ids = []

    for art in to_process:
        if "_queue_id" in art:
            processed_queue_ids.append(art["_queue_id"])
        try:
            extraction = classify_and_tag(client, art["title"], art["raw_text"])
        except Exception as e:
            failed += 1
            DEADLETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(DEADLETTER_PATH, "a") as f:
                f.write(json.dumps({"article": art, "error": str(e)}) + "\n")
            log.warning("Extraction failed for %s: %s", art["url"][:80], e)
            continue
        finally:
            budget_used += 1  # counts the attempt either way — Google bills a failed call same as a successful one
            _record_budget_used(store, budget_used)  # persisted per-attempt, not batched at the end — a downstream crash (e.g. the dim_source/BigQuery bug this replaced) must never leave an already-billed Gemini call invisible to the budget tracker

        article_id = new_id()
        source_id = art["source"].lower().replace(" ", "_")
        source_rows.append({
            "source_id": source_id, "source_name": art["source"],
            "source_type": art["category"], "home_country_code": None,
        })
        article_rows.append({
            "article_id": article_id,
            "date_key": (art["published"] or "")[:10],
            "source_id": source_id,
            "article_category": extraction.article_category,
            "policy_subtype": ",".join(extraction.policy_subtype),
            "sentiment_score": extraction.sentiment_score,
            "sentiment_target": extraction.sentiment_target,
            "source_url": art["url"],
            "title": art["title"],
            "published": art["published"] or None,
            "mech": ",".join(extraction.mechanisms),
            "policy_stance": extraction.policy_stance,
        })
        for cc in extraction.country_codes:
            bridge_country_rows.append({"article_id": article_id, "country_code": cc})
        for sec in extraction.sector_tags:
            bridge_sector_rows.append({"article_id": article_id, "sector_id": sec})

        time.sleep(13)  # free tier is 5 requests/minute (observed in production 429s) — 0.2s was ~60x too fast to actually respect that

    import pandas as pd
    if source_rows:
        store.upsert_df("dim_source", pd.DataFrame(source_rows).drop_duplicates("source_id"), ["source_id"])
    if article_rows:
        store.upsert_df("fact_article", pd.DataFrame(article_rows), ["article_id"])
    if bridge_country_rows:
        store.upsert_df("bridge_article_country", pd.DataFrame(bridge_country_rows), ["article_id", "country_code"])
    if bridge_sector_rows:
        store.upsert_df("bridge_article_sector", pd.DataFrame(bridge_sector_rows), ["article_id", "sector_id"])

    _dequeue(store, processed_queue_ids)

    log.info(
        "Processed %d articles (%d failed) from %s — %d/%d daily budget used, %d queued for later",
        len(article_rows), failed, source_label, budget_used, DAILY_EXTRACTION_BUDGET, len(to_queue),
    )
    return {"processed": len(article_rows), "failed": failed, "queued": len(to_queue)}


if __name__ == "__main__":
    run_extraction()
