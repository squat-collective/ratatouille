# Build stage — only production dependencies
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir uv
COPY pyproject.toml .
COPY . .
RUN uv pip install --system --no-cache .

# Runtime stage
FROM python:3.12-slim
LABEL org.opencontainers.image.title="rat-query"
LABEL org.opencontainers.image.description="RAT query service (DuckDB read-only sidecar)"
LABEL org.opencontainers.image.source="https://github.com/squat-collective/rat"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="RAT Data"
RUN useradd --create-home query
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY src/ /app/src/
USER query
EXPOSE 50051
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=15s \
  CMD ["python", "-c", "import grpc; ch=grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=2)"]
ENTRYPOINT ["python", "-m", "rat_query"]
