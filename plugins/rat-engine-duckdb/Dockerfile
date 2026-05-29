# rat-engine-duckdb — DuckDB compute engine (engine/v1) for RAT's decoupled
# data architecture (ADR-024). Two-stage: build (with a C toolchain for the
# PyIceberg avro extension, which has no cp314 wheel yet) → slim runtime.
FROM python:3.14-slim AS builder
WORKDIR /build
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential \
  && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV ENGINE_PORT=50081
EXPOSE 50081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50081'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_engine_duckdb"]
