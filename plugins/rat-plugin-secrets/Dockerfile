# RAT secrets plugin. Build from the repo root:
#
#   docker build \
#     --build-context platform=platform \
#     --build-context sdk=sdk-go \
#     -f examples/rat-plugin-secrets/Dockerfile \
#     -t rat-plugin-secrets examples/rat-plugin-secrets
#
# Or `make build` from this directory.
FROM golang:1.26-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS builder
WORKDIR /build

COPY --from=platform . /platform/
COPY --from=sdk . /sdk-go/
COPY . .

RUN go mod tidy && CGO_ENABLED=0 go build -ldflags="-s -w" -o /secrets .

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN addgroup -S rat && adduser -S -G rat rat \
    && mkdir -p /data \
    && chown -R rat:rat /data
COPY --from=builder /secrets /secrets
USER rat
VOLUME ["/data"]
EXPOSE 50099
ENTRYPOINT ["/secrets"]
