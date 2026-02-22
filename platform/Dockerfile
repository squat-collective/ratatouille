# Build stage
FROM golang:1.24-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w -extldflags '-static'" -o /ratd ./cmd/ratd

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
