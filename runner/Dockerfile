# Build stage — only production dependencies
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS builder
WORKDIR /build
# C toolchain for source builds: pyiceberg ships no cp314 wheel yet, so its
# avro `decoder_fast` C extension is compiled from sdist here. Builder-stage
# only — the slim runtime stage just copies the resulting .venv, so the
# compiler never lands in the shipped image. Drop this once pyiceberg
# publishes cp314 wheels.
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
# Copy the lockfile + pyproject so uv can install from the frozen set
# BEFORE we copy the source — keeps the dep layer cacheable across source changes.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
# Shared rat-protos package (ADR-024 cleanup C) — supplied via `--build-context
# protos=proto/sdk-python` in the Makefile. Installed into the uv-managed venv
# AFTER uv sync so it doesn't have to be a uv-tracked dependency (it's a pure
# proto-stubs library — no version drift risk).
COPY --from=protos . /tmp/rat-protos
# `uv sync --locked` asserts that uv.lock is in sync with pyproject.toml and
# fails the build if the lockfile is stale (drift between the two files).
# Note: `--frozen` would SILENTLY skip lock resolution; `--locked` is the
# correct strict-mode flag for reproducible CI/Docker builds.
# `--no-dev` skips dev-only deps for the production image.
# `--no-editable` builds rat_runner as a regular wheel into the venv's
# site-packages — without this, an editable install leaves a .pth file
# pointing at /build/src/rat_runner which no longer exists in the runtime
# stage (we don't COPY the source into /app anymore).
RUN uv sync --locked --no-dev --no-editable \
  && uv pip install --no-cache-dir --python /build/.venv/bin/python /tmp/rat-protos

# Runtime stage
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97
LABEL org.opencontainers.image.title="rat-runner"
LABEL org.opencontainers.image.description="RAT pipeline runner (Python execution engine)"
LABEL org.opencontainers.image.source="https://github.com/squat-collective/rat"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="RAT Data"
RUN useradd --create-home runner
WORKDIR /app
# Copy the uv-managed virtualenv from the builder stage.
# The rat_runner package is installed inside the venv (not editable),
# so we don't need to copy /app/src separately.
COPY --from=builder /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER runner
EXPOSE 50052
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=15s \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50052'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_runner"]
