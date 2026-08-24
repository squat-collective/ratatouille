# Build stage
FROM golang:1.27-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .

# Version metadata injected by the release pipeline. Defaults keep local
# `docker build` honest ("dev") — the git tag is the single source of truth,
# passed through .github/workflows/release.yml → --build-arg.
ARG VERSION=dev
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -extldflags '-static' \
      -X github.com/rat-data/rat/platform/internal/api.Version=${VERSION} \
      -X github.com/rat-data/rat/platform/internal/api.GitCommit=${GIT_COMMIT} \
      -X github.com/rat-data/rat/platform/internal/api.BuildTime=${BUILD_TIME}" \
    -o /ratd ./cmd/ratd

# Runtime stage — scratch for minimal attack surface (~15-20 MB final image).
# No shell or utilities — healthcheck uses the built-in `ratd healthcheck` command.
FROM scratch
LABEL org.opencontainers.image.title="ratd"
LABEL org.opencontainers.image.description="RAT platform API server (ratd)"
LABEL org.opencontainers.image.source="https://github.com/squat-collective/rat"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="RAT Data"
COPY --from=builder /etc/passwd /etc/passwd
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /ratd /ratd
USER 1001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=5 --start-period=5s \
  CMD ["/ratd", "healthcheck"]
ENTRYPOINT ["/ratd"]
