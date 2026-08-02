"""
Cloud Function equivalent: pulls every `status: active` feed in feeds.yaml with
feedparser, dedupes against previously seen article URLs (hash-based, per the
README), and writes raw article JSON.

Local dev (GCS_RAW_BUCKET unset): reads/writes data/raw/ and data/cache/ under
the repo, same as any other script here.

Deployed (GCS_RAW_BUCKET set): Cloud Functions/Cloud Run containers have a
read-only filesystem outside of /tmp, so there's no repo-relative data/ to
write to. Output JSON goes straight to /tmp (ephemeral scratch, immediately
uploaded to GCS) and the seen-URL dedup cache lives as a small JSON blob in the
same GCS bucket instead of a local file — /tmp isn't guaranteed to persist
between invocations, but the bucket is.

Run directly: `python -m etl.ingest_feeds`
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yaml
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_feeds")

# USE_LOCAL_FEEDS reroutes ingestion at etl/mock_rss_server.py (see that file
# and etl/feeds.local.yaml) to generate more volume than the real active feeds
# provide, for local testing only — never set this in a deployed environment.
USE_LOCAL_FEEDS = os.environ.get("USE_LOCAL_FEEDS", "").lower() in ("1", "true", "yes")
FEEDS_YAML = Path(__file__).resolve().parent / ("feeds.local.yaml" if USE_LOCAL_FEEDS else "feeds.yaml")
GCS_RAW_BUCKET = os.environ.get("GCS_RAW_BUCKET")  # presence of this also signals "deployed" for path selection
RAW_DIR = Path(tempfile.gettempdir()) if GCS_RAW_BUCKET else Path(__file__).resolve().parent.parent / "data" / "raw"
SEEN_URLS_PATH = Path(__file__).resolve().parent.parent / "data" / "cache" / "seen_article_urls.json"
SEEN_URLS_GCS_BLOB = "_state/seen_article_urls.json"
STALE_AFTER_CONSECUTIVE_EMPTY = 5  # matches the README's "N consecutive empty/failed fetches" health check


def load_feeds() -> list[dict]:
    with open(FEEDS_YAML) as f:
        feeds = yaml.safe_load(f)
    return [f for f in feeds if f.get("status") == "active"]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def load_seen() -> dict:
    if GCS_RAW_BUCKET:
        from google.cloud import storage
        blob = storage.Client().bucket(GCS_RAW_BUCKET).blob(SEEN_URLS_GCS_BLOB)
        return json.loads(blob.download_as_text()) if blob.exists() else {}
    if SEEN_URLS_PATH.exists():
        return json.loads(SEEN_URLS_PATH.read_text())
    return {}


def save_seen(seen: dict) -> None:
    if GCS_RAW_BUCKET:
        from google.cloud import storage
        blob = storage.Client().bucket(GCS_RAW_BUCKET).blob(SEEN_URLS_GCS_BLOB)
        blob.upload_from_string(json.dumps(seen), content_type="application/json")
        return
    SEEN_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_URLS_PATH.write_text(json.dumps(seen))


def fetch_feed(feed_cfg: dict) -> tuple[list[dict], bool]:
    """Returns (articles, healthy). healthy=False on parse error or zero entries
    (matches the README's feed health check: flag after repeated empty fetches)."""
    try:
        parsed = feedparser.parse(feed_cfg["url"])
    except Exception as e:
        log.warning("Feed fetch failed for %s: %s", feed_cfg["name"], e)
        return [], False

    if getattr(parsed, "bozo", 0) and not parsed.entries:
        log.warning("Feed unparseable for %s (bozo=%s)", feed_cfg["name"], parsed.get("bozo_exception"))
        return [], False

    articles = []
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not url:
            continue
        published = entry.get("published", "") or entry.get("updated", "")
        articles.append({
            "url": url,
            "title": entry.get("title", "").strip(),
            "published": published,
            "source": feed_cfg["name"],
            "category": feed_cfg["category"],
            "raw_text": entry.get("summary", "") or entry.get("description", ""),
        })
    return articles, True


def run_ingest() -> dict:
    if USE_LOCAL_FEEDS:
        log.info("USE_LOCAL_FEEDS=true — reading %s (etl/mock_rss_server.py must be running on :9000)", FEEDS_YAML.name)
    feeds = load_feeds()
    seen = load_seen()
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    all_new: list[dict] = []
    feed_health: dict[str, str] = {}

    for feed_cfg in feeds:
        articles, healthy = fetch_feed(feed_cfg)
        feed_health[feed_cfg["name"]] = "healthy" if healthy else "unhealthy"

        new_for_feed = 0
        for art in articles:
            h = url_hash(art["url"])
            if h in seen:
                continue
            seen[h] = run_ts
            all_new.append(art)
            new_for_feed += 1
        log.info("%-28s %3d entries, %3d new", feed_cfg["name"], len(articles), new_for_feed)
        time.sleep(0.3)  # be polite to shared free-tier endpoints

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"articles_{run_ts}.json"
    out_path.write_text(json.dumps(all_new, indent=2))
    save_seen(seen)

    if GCS_RAW_BUCKET:
        _upload_to_gcs(out_path, run_ts)

    log.info("Run %s: %d new articles across %d active feeds -> %s", run_ts, len(all_new), len(feeds), out_path)
    log.info("Feed health: %s", feed_health)
    return {"run_ts": run_ts, "new_articles": len(all_new), "feed_health": feed_health, "out_path": str(out_path)}


def _upload_to_gcs(local_path: Path, run_ts: str) -> None:
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(GCS_RAW_BUCKET)
    blob = bucket.blob(f"raw/articles_{run_ts}.json")
    blob.upload_from_filename(str(local_path))
    log.info("Uploaded to gs://%s/%s", GCS_RAW_BUCKET, blob.name)


if __name__ == "__main__":
    run_ingest()
