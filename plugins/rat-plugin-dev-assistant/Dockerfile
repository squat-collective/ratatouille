# Example RAT dev-assistant plugin — a platform + portal plugin.
#
# The portal UI bundle is hand-written and build-free, so there is no
# front-end build step — just Go.
#
# Build from the repo root (the platform module is a named build context):
#
#   docker build \
#     --build-context platform=platform \
#     --build-context sdk=sdk-go \
#     -f examples/rat-plugin-dev-assistant/Dockerfile \
#     -t rat-plugin-dev-assistant examples/rat-plugin-dev-assistant
#
# Or just run `make build` from this directory.
FROM golang:1.27-alpine@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc AS builder
WORKDIR /build

COPY --from=platform . /platform/
COPY --from=sdk . /sdk-go/
COPY . .

RUN go mod tidy && CGO_ENABLED=0 go build -ldflags="-s -w" -o /dev-assistant .

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN addgroup -S rat && adduser -S -G rat rat
COPY --from=builder /dev-assistant /dev-assistant
USER rat
EXPOSE 50095
ENTRYPOINT ["/dev-assistant"]
