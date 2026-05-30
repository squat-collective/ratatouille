# rat-catalog-ducklake — DuckLake catalog/v1 service for RAT's decoupled
# data architecture (ADR-024). Pulls in the shared rat-protos package via
# the `protos` build context (see Makefile docker-build target).
FROM python:3.14-slim AS builder
WORKDIR /build
COPY --from=protos . /tmp/rat-protos
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir /tmp/rat-protos \
  && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV CATALOG_PORT=50082
EXPOSE 50082
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50082'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_catalog_ducklake"]
