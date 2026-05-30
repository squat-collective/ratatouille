# rat-engine-duckdb — DuckDB compute engine (engine/v1) for RAT's decoupled
# data architecture (ADR-024). The engine itself is format-agnostic — formats
# come from rat-format-* plugins bundled in here at build time and discovered
# at runtime via the `rat_engine_duckdb.format_adapters` entry-point group.
#
# Build contexts (see Makefile docker-build target):
#   - protos          → proto/sdk-python (shared rat-protos package)
#   - format_iceberg  → plugins/rat-format-iceberg
#   - format_ducklake → plugins/rat-format-ducklake
#
# Two-stage: build (with a C toolchain for the PyIceberg avro extension —
# pulled in by rat-format-iceberg, no cp314 wheel yet) → slim runtime.
FROM python:3.14-slim AS builder
WORKDIR /build
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential \
  && rm -rf /var/lib/apt/lists/*
COPY --from=protos . /tmp/rat-protos
COPY --from=format_iceberg . /tmp/rat-format-iceberg
COPY --from=format_ducklake . /tmp/rat-format-ducklake
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir /tmp/rat-protos \
  && /opt/venv/bin/pip install --no-cache-dir . \
  && /opt/venv/bin/pip install --no-cache-dir /tmp/rat-format-iceberg /tmp/rat-format-ducklake

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV ENGINE_PORT=50081
EXPOSE 50081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50081'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_engine_duckdb"]
