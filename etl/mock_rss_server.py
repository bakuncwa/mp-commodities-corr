"""
Local-only synthetic RSS server, used purely to generate more ingestion volume
than the ~11 real active feeds in feeds.yaml provide — useful for exercising
the pipeline (dedup, extraction, correlation) at a higher article count than
real feeds currently give, without hammering real news sites. Every request
generates a fresh batch of randomized-but-plausible headlines, so re-running
ingestion against this server behaves like a live feed that keeps publishing.

Two of the seven simulated sources are tagged world_bank_group and mnc_bank —
categories the README's feeds.yaml documents as `needs_research` because no
working public RSS endpoint was found for them. The simulated versions let the
rest of the pipeline (classification, country/sector tagging, correlation) be
exercised against those categories even though real feeds for them aren't wired
up yet.

Run: `python -m etl.mock_rss_server` (serves on :9000), then point ingestion at
it with `USE_LOCAL_FEEDS=true python -m etl.ingest_feeds` (reads
etl/feeds.local.yaml instead of etl/feeds.yaml — see that file's comment).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI(title="mock-rss-server (local data simulation only)")

COUNTRIES = ["Chile", "Peru", "Zambia", "DR Congo", "Australia", "Indonesia", "China",
             "Taiwan", "South Korea", "Japan", "United States", "United Kingdom", "Germany", "India"]
SECTORS = ["copper", "lithium", "nickel", "rare earths", "cobalt", "semiconductors"]
POLICY_VERBS = ["holds", "raises", "cuts", "signals a gradual path for", "reviews"]
POLICY_TOPICS = ["interest rates", "export controls", "capital controls", "market regulation", "reserve requirements"]
STOCK_VERBS = ["climbs on", "slips on", "holds steady amid", "rallies after", "eases following"]
STOCK_REASONS = ["rate outlook", "quarterly earnings", "a supply disruption", "shifting demand forecasts", "new export quota news"]
SUMMARY_TAILS = [
    "Analysts weighed the impact on financing costs for producers.",
    "The move reflects ongoing efforts to manage currency stability.",
    "Market participants had largely priced in the decision.",
    "Supply chain watchers flagged downstream effects on manufacturers.",
    "The announcement follows weeks of speculation among traders.",
]

SOURCES = {
    "sim-central-bank-1": "central_bank",
    "sim-central-bank-2": "central_bank",
    "sim-world-bank": "world_bank_group",
    "sim-mnc-bank-1": "mnc_bank",
    "sim-commodity-press-1": "commodity_press",
    "sim-commodity-press-2": "commodity_press",
    "sim-semiconductor-press-1": "semiconductor_press",
}


def random_policy_headline() -> str:
    return f"{random.choice(COUNTRIES)} central bank {random.choice(POLICY_VERBS)} {random.choice(POLICY_TOPICS)}"


def random_stock_headline() -> str:
    return f"{random.choice(SECTORS).title()} sector valuation {random.choice(STOCK_VERBS)} {random.choice(STOCK_REASONS)}"


def make_rss(source_name: str, category: str, n: int) -> str:
    now = datetime.now(timezone.utc)
    is_policy = category in ("central_bank", "world_bank_group", "mnc_bank")
    items = []
    for i in range(n):
        title = random_policy_headline() if is_policy else random_stock_headline()
        pub = (now - timedelta(hours=random.randint(0, 72))).strftime("%a, %d %b %Y %H:%M:%S +0000")
        link = f"https://simulated.local/{source_name}/{i}-{random.randint(100000, 999999)}"
        summary = f"{title}. {random.choice(SUMMARY_TAILS)}"
        items.append(f"""
        <item>
          <title>{title}</title>
          <link>{link}</link>
          <guid>{link}</guid>
          <pubDate>{pub}</pubDate>
          <description>{summary}</description>
        </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>{source_name} (simulated)</title>
  <link>https://simulated.local/{source_name}</link>
  <description>Synthetic feed for local volume testing — not real news</description>
  {''.join(items)}
</channel></rss>"""


@app.get("/feed/{slug}")
def feed(slug: str, n: int = 25):
    category = SOURCES.get(slug)
    if not category:
        return Response(status_code=404, content=f"Unknown simulated feed {slug!r}. Known: {list(SOURCES)}")
    return Response(content=make_rss(slug, category, n), media_type="application/rss+xml")


@app.get("/")
def index():
    return {"available_feeds": list(SOURCES.keys()), "usage": "/feed/<slug>?n=<article_count>"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
