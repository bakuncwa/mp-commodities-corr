"""
Separate Cloud Run service (own container from api/main.py) so openpyxl and
reportlab — the heavier export dependencies — don't bloat the main
request-serving image. Exports exactly what's currently filtered, not a full
unfiltered dump.

Run: `uvicorn api.export:app --reload --port 8001`
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

from common.db import get_store
from common.seed import COUNTRIES, COUNTRY_SECTOR_PAIRS
from common.schema import SECTORS

app = FastAPI(title="mp-commodities-corr export service", version="0.1.0")

VALID_COUNTRIES = {c[0] for c in COUNTRIES}
VALID_SECTORS = {s[0] for s in SECTORS}


def _fetch_correlation_df(country: str, sector: str, rate_basis: str, controls: list[str]) -> pd.DataFrame:
    store = get_store()
    control_set = ",".join(sorted(controls))
    df = store.query_df(
        f"""
        SELECT * FROM fact_correlation_result
        WHERE country_code = '{country}' AND sector_id = '{sector}' AND rate_basis = '{rate_basis}'
        ORDER BY computed_at DESC LIMIT 1
        """
    )
    return df


@app.get("/api/export")
def export(
    scope: str = Query(..., pattern="^(correlation|articles)$"),
    format: str = Query(..., pattern="^(csv|xlsx|pdf)$"),
    country: str | None = None,
    sector: str | None = None,
    rate_basis: str = "nominal",
    controls: list[str] = Query(default=["fdi_net_inflow"]),
    start_date: date | None = None,
    end_date: date | None = None,
):
    if country and country not in VALID_COUNTRIES:
        raise HTTPException(422, f"Invalid country {country!r}")
    if sector and sector not in VALID_SECTORS:
        raise HTTPException(422, f"Invalid sector {sector!r}")

    store = get_store()
    if scope == "correlation":
        if not country or not sector:
            raise HTTPException(422, "country and sector are required for scope=correlation")
        df = _fetch_correlation_df(country, sector, rate_basis, controls)
        filename_base = f"correlation_{country}_{sector}_{rate_basis}"
    else:
        where = []
        if country:
            where.append(f"bc.country_code = '{country}'")
        if start_date:
            where.append(f"a.date_key >= '{start_date.isoformat()}'")
        if end_date:
            where.append(f"a.date_key <= '{end_date.isoformat()}'")
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        join = "JOIN bridge_article_country bc ON a.article_id = bc.article_id" if country else ""
        df = store.query_df(f"""
            SELECT a.title, a.article_category, a.sentiment_score, a.source_url, a.published
            FROM fact_article a {join} {where_clause} ORDER BY a.published DESC
        """)
        filename_base = f"articles_{country or 'all'}"

    if df.empty:
        raise HTTPException(404, "No data matches the requested filters")

    if format == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.csv"},
        )

    if format == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="export")
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.xlsx"},
        )

    # pdf
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("mp-commodities-corr — export snapshot", styles["Title"]),
        Paragraph(f"Scope: {scope} · Filters: country={country} sector={sector} rate_basis={rate_basis} controls={controls}", styles["Normal"]),
        Paragraph(f"Generated: {datetime.now(timezone.utc).isoformat()}", styles["Normal"]),
        Spacer(1, 12),
    ]
    table_data = [list(df.columns)] + df.astype(str).values.tolist()
    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c08a4e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        "Correlation does not establish causation. This snapshot reflects the filters active "
        "at generation time and will not change retroactively.", styles["Italic"],
    ))
    doc.build(elements)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename_base}.pdf"},
    )


@app.get("/health")
def healthz():
    # Not /healthz — see api/main.py's healthz() comment.
    return {"status": "ok"}
