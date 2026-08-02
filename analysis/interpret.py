"""
Generates a plain-language interpretation of a specific /api/correlation
result via Gemini — dynamic, not the fixed JS template that used to hardcode
"holding FDI net inflows constant" regardless of which control actually got
used. Every call is cached in fact_interpretation keyed on the exact inputs
(country/sector/rate_basis/target/control + the stat values themselves,
rounded), so re-viewing the same result never re-calls Gemini — important
given this key's free-tier project has only a ~20 requests/day provisional
quota (see README).

If Gemini is unavailable (no key, quota exceeded, network error), this raises
rather than silently returning a fabricated-sounding string; api/main.py
catches that and the frontend falls back to its own client-side, rule-based
interpretation so the panel never breaks even when the quota's out.

Run standalone: `python -m analysis.interpret CL copper` (uses the country's
current best result to demo one call).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from common.db import get_store

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("interpret")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

PROMPT_TEMPLATE = """You are writing a short, plain-language interpretation of one \
statistical result for a public data dashboard. Use ONLY the numbers given below — \
do not invent additional facts, causes, or economic mechanisms not implied by them.

Country: {country_name}
Sector: {sector_label}
Rate basis: {rate_basis} policy rate
Target variable: {target_label}
Control variable used (empirically selected, not fixed): {control_label}
Pearson r (raw): {pearson_r:+.2f}
Partial r (control applied): {partial_r:+.2f}
p-value: {p_value:.3f}
R-squared: {r_squared:.2f}
Best-fit lag: {lag} month(s)

Write 3-4 short sentences covering, in this order: (1) the direction and \
conventional strength band of the relationship (below 0.3 weak, 0.3-0.5 \
moderate, above 0.5 strong) from the partial r, (2) what the p-value means in \
plain terms (below 0.05 = unlikely due to chance; at/above = consistent with \
chance), (3) what the R-squared means as a percentage of variation explained, \
(4) what the lag means (immediate vs. delayed effect). Plain prose, no bullet \
points, no markdown, no preamble like "Here is the interpretation.\""""


def _cache_key(country: str, sector: str, rate_basis: str, target: str, control: str | None,
                pearson_r: float, partial_r: float, p_value: float, r_squared: float, lag: int) -> str:
    raw = f"{country}|{sector}|{rate_basis}|{target}|{control}|{pearson_r:.3f}|{partial_r:.3f}|{p_value:.3f}|{r_squared:.3f}|{lag}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _call_gemini(prompt: str) -> str:
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set — cannot generate a dynamic interpretation.")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text


def get_or_generate_interpretation(
    country_code: str, country_name: str, sector_id: str, sector_label: str,
    rate_basis: str, target: str, target_label: str, control: str | None, control_label: str,
    pearson_r: float, partial_r: float, p_value: float, r_squared: float, lag: int,
) -> dict:
    """Returns {"text": str, "source": "cache"|"llm"}. Raises if no cached
    entry exists and the live Gemini call also fails — callers should catch
    and fall back to a non-LLM interpretation rather than surfacing a 500."""
    store = get_store()
    key = _cache_key(country_code, sector_id, rate_basis, target, control, pearson_r, partial_r, p_value, r_squared, lag)

    cached = store.query_df(f"SELECT interpretation_text FROM fact_interpretation WHERE cache_key = '{key}'")
    if not cached.empty:
        return {"text": cached.iloc[0]["interpretation_text"], "source": "cache"}

    prompt = PROMPT_TEMPLATE.format(
        country_name=country_name, sector_label=sector_label, rate_basis=rate_basis,
        target_label=target_label, control_label=control_label,
        pearson_r=pearson_r, partial_r=partial_r, p_value=p_value, r_squared=r_squared, lag=lag,
    )
    text = _call_gemini(prompt)

    import pandas as pd
    row = pd.DataFrame([{
        "cache_key": key, "country_code": country_code, "sector_id": sector_id,
        "rate_basis": rate_basis, "target_variable": target, "control_used": control or "",
        "interpretation_text": text, "model": GEMINI_MODEL, "source": "llm",
        "created_at": datetime.now(timezone.utc),
    }])
    store.upsert_df("fact_interpretation", row, ["cache_key"])
    log.info("Generated + cached interpretation for %s/%s (%s)", country_code, sector_id, key)
    return {"text": text, "source": "llm"}


if __name__ == "__main__":
    import sys
    from analysis.correlation_regression import build_country_panel, run_one

    country, sector = sys.argv[1], sys.argv[2]
    store = get_store()
    panel = build_country_panel(store, country, sector)
    result = run_one(store, country, sector, "nominal", "equity_valuation", panel)
    if not result:
        print("No result for this pair yet.")
    else:
        out = get_or_generate_interpretation(
            country, country, sector, sector, "nominal", "equity_valuation", "equity valuation",
            result["control_set"] or None, result["control_set"] or "none",
            result["pearson_r"], result["partial_r"], result["p_value"], result["r_squared"], result["lag"],
        )
        print(out)
