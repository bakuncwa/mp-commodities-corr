# Slim multi-stage build — keeps this image well clear of the Artifact Registry
# free-tier's 0.5GB ceiling (see README's Deployment & Container Hosting Cost Notes).
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
COPY api ./api
ENV DB_BACKEND=bigquery
EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
