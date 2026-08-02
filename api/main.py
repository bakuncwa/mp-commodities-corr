"""
FastAPI app: serves the choropleth's per-country aggregate, on-demand
correlation results (computed synchronously via Pingouin when the requested
window/controls don't match a cached batch result), and tagged articles.

Every query-string value that reaches SQL is validated against a fixed
allowlist (known country codes, sector ids, enum values) before being used —
not parameterized bind variables, because the two backends (DuckDB dev vs.
BigQuery prod) use different placeholder syntax ($name vs @name) and this
project's queries are small, config-driven lookups rather than free-text
search. Rejecting anything outside the allowlist closes the injection surface
the same way parameterization would, at the actual system boundary (the HTTP
request), which is the point in the pipeline that matters.

Run: `uvicorn api.main:app --reload --port 8000`
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from common.db import get_store
from common.schema import SECTORS
from common.seed import COUNTRIES, COUNTRY_SECTOR_PAIRS
from analysis.correlation_regression import RATE_INDICATORS, MIN_OBS, build_country_panel, run_one, CANDIDATE_CONTROLS
from analysis.interpret import get_or_generate_interpretation

app = FastAPI(title="mp-commodities-corr API", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
)

VALID_COUNTRIES = {c[0] for c in COUNTRIES}
VALID_SECTORS = {s[0] for s in SECTORS}
VALID_PAIRS = set(COUNTRY_SECTOR_PAIRS)
VALID_RATE_BASIS = {"nominal", "real"}
VALID_TARGETS = {"equity_valuation", "commodity_price"}


def _require(value: str, allowed: set, field: str) -> str:
    if value not in allowed:
        raise HTTPException(status_code=422, detail=f"Invalid {field}: {value!r}. Must be one of {sorted(allowed)}")
    return value


@app.get("/api/countries")
def get_countries(start_date: date | None = None, end_date: date | None = None):
    """Per-country aggregate policy-activity score for the choropleth: article
    count in the window, tagged by country via bridge_article_country."""
    store = get_store()
    where = []
    if start_date:
        where.append(f"a.date_key >= '{start_date.isoformat()}'")
    if end_date:
        where.append(f"a.date_key <= '{end_date.isoformat()}'")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT bc.country_code, COUNT(*) AS article_count,
               AVG(a.sentiment_score) AS avg_sentiment
        FROM fact_article a
        JOIN bridge_article_country bc ON a.article_id = bc.article_id
        {where_clause}
        GROUP BY bc.country_code
    """
    df = store.query_df(sql)
    return df.to_dict(orient="records")


@app.get("/api/correlation")
def get_correlation(
    country: str = Query(...),
    sector: str = Query(...),
    rate_basis: str = Query("nominal"),
    target: str = Query("equity_valuation"),
    control: str | None = Query(None, description="Explicit control override; omit to let the backend pick empirically"),
    start_date: date | None = None,
    end_date: date | None = None,
):
    _require(country, VALID_COUNTRIES, "country")
    _require(sector, VALID_SECTORS, "sector")
    _require(rate_basis, VALID_RATE_BASIS, "rate_basis")
    _require(target, VALID_TARGETS, "target")
    if control is not None:
        _require(control, set(CANDIDATE_CONTROLS), "control")
    if (country, sector) not in VALID_PAIRS:
        raise HTTPException(status_code=404, detail=f"{country}/{sector} is not a configured analysis pair")

    store = get_store()
    panel = build_country_panel(store, country, sector)
    if panel.empty:
        raise HTTPException(status_code=404, detail="Not enough joined data for this window yet")
    if start_date:
        panel = panel[panel["date_key"] >= start_date.strftime("%Y-%m")]
    if end_date:
        panel = panel[panel["date_key"] <= end_date.strftime("%Y-%m")]
    if panel.empty or len(panel) < MIN_OBS:
        raise HTTPException(status_code=404, detail="Not enough joined data for this window yet")

    rate_col = RATE_INDICATORS[rate_basis]
    target_col = "equity_valuation_change" if target == "equity_valuation" else "commodity_price_change"
    if rate_col not in panel.columns or target_col not in panel.columns:
        raise HTTPException(status_code=404, detail="Required series not available for this country/sector/basis combination")

    # Reuses the exact same dynamic-control-selection path as the batch job
    # (analysis/correlation_regression.run_one) — no separate logic to keep in
    # sync. `control` lets a caller pin a specific one instead (e.g. for a
    # side-by-side comparison), skipping the empirical search.
    best = run_one(store, country, sector, rate_basis, target, panel, controls=[control] if control else None)
    if best is None:
        raise HTTPException(status_code=404, detail="No lag produced enough overlapping observations")

    def _clean(value):
        """Panel values are pandas/numpy floats, which can be NaN — not valid JSON.
        The frontend already treats None as "n/a" for these two fields."""
        if value is None or pd.isna(value):
            return None
        return float(value)

    latest = panel.iloc[-1]
    return {
        "country": country, "sector": sector, "rate_basis": rate_basis, "target": target,
        "control": best["control_set"] or None,
        "lag": best["lag"], "pearson_r": best["pearson_r"], "partial_r": best["partial_r"],
        "p_value": best["p_value"], "r_squared": best["r_squared"],
        "rate_change_latest": None if rate_col not in panel.columns else _clean(latest.get(rate_col)),
        "target_change_latest": _clean(latest.get(target_col)),
    }


@app.get("/api/interpret")
def get_interpretation(
    country: str = Query(...),
    sector: str = Query(...),
    rate_basis: str = Query("nominal"),
    target: str = Query("equity_valuation"),
    control: str | None = Query(None),
    pearson_r: float = Query(...),
    partial_r: float = Query(...),
    p_value: float = Query(...),
    r_squared: float = Query(...),
    lag: int = Query(...),
):
    """Dynamic, Gemini-generated plain-language interpretation of an already-
    computed /api/correlation result — the caller passes the stats back so this
    endpoint never recomputes them, just explains them. Cached in
    fact_interpretation keyed on the exact inputs, so repeat views of the same
    result never re-call Gemini (see analysis/interpret.py on why that matters
    given this key's ~20 requests/day quota). Returns 503 rather than a fake
    interpretation when Gemini is unavailable — the frontend falls back to its
    own client-side rule-based text in that case, so the panel never breaks."""
    _require(country, VALID_COUNTRIES, "country")
    _require(sector, VALID_SECTORS, "sector")
    _require(rate_basis, VALID_RATE_BASIS, "rate_basis")
    _require(target, VALID_TARGETS, "target")
    if control is not None:
        _require(control, set(CANDIDATE_CONTROLS), "control")

    country_name = next((c[1] for c in COUNTRIES if c[0] == country), country)
    sector_label = next((s[1] for s in SECTORS if s[0] == sector), sector)
    target_label = f"{sector_label} sector valuation" if target == "equity_valuation" else f"{sector_label} spot price"
    control_label = control.replace("_", " ") if control else "none — raw correlation only"

    try:
        result = get_or_generate_interpretation(
            country, country_name, sector, sector_label, rate_basis, target, target_label,
            control, control_label, pearson_r, partial_r, p_value, r_squared, lag,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Dynamic interpretation unavailable: {e}")
    return result


@app.get("/api/articles")
def get_articles(
    country: str | None = None,
    sector: str | None = None,
    article_category: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
):
    if country:
        _require(country, VALID_COUNTRIES, "country")
    if sector:
        _require(sector, VALID_SECTORS, "sector")
    if article_category:
        _require(article_category, {"monetary_policy", "commodity_stocks"}, "article_category")
    limit = max(1, min(limit, 200))

    store = get_store()
    joins, where = [], []
    if country:
        joins.append("JOIN bridge_article_country bc ON a.article_id = bc.article_id")
        where.append(f"bc.country_code = '{country}'")
    if sector:
        joins.append("JOIN bridge_article_sector bs ON a.article_id = bs.article_id")
        where.append(f"bs.sector_id = '{sector}'")
    if article_category:
        where.append(f"a.article_category = '{article_category}'")
    if start_date:
        where.append(f"a.date_key >= '{start_date.isoformat()}'")
    if end_date:
        where.append(f"a.date_key <= '{end_date.isoformat()}'")

    join_clause = " ".join(joins)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT a.article_id, a.title, a.source_url, a.source_id, a.article_category,
               a.policy_subtype, a.sentiment_score, a.sentiment_target, a.mech, a.published
        FROM fact_article a
        {join_clause}
        {where_clause}
        ORDER BY a.published DESC
        LIMIT {limit}
    """
    df = store.query_df(sql)
    return df.to_dict(orient="records")


@app.get("/api/sectors")
def get_sectors():
    return [{"sector_id": s[0], "sector_name": s[1], "sector_category": s[2]} for s in SECTORS]


@app.get("/api/pairs")
def get_pairs():
    """Which country/sector combinations actually have a configured analysis pair — the frontend
    uses this to know which map markers should be clickable/queryable."""
    return [{"country": c, "sector": s} for c, s in COUNTRY_SECTOR_PAIRS]


@app.get("/health")
def healthz():
    # Not /healthz: Google's front-end layer intercepts that exact path with its
    # own 404 before it ever reaches the container, even on Cloud Run's default
    # *.run.app domain (a legacy App Engine health-check reservation).
    return {"status": "ok"}
