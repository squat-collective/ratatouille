# rat-storage-s3 — S3 storage axis (storage/v1) for RAT's decoupled data
# architecture (ADR-024). Pulls in the shared rat-protos package via the
# `protos` build context (see Makefile docker-build target). Two-stage:
# build → slim runtime.
FROM python:3.14-slim AS builder
WORKDIR /build
COPY --from=protos . /tmp/rat-protos
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir /tmp/rat-protos boto3 \
  && /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV STORAGE_PORT=50083
EXPOSE 50083
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50083'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_storage_s3"]
