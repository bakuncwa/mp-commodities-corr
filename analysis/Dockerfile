# Cloud Run Job (not a Service) — runs correlation_regression.py to completion
# on the Cloud Scheduler cadence set in infra/scheduler.tf, then exits.
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY common ./common
COPY analysis ./analysis
ENV DB_BACKEND=bigquery
CMD ["python", "-m", "analysis.correlation_regression"]
