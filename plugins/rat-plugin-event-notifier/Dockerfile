# Example RAT platform plugin.
#
# The platform module (a local `replace` dependency) is supplied as a NAMED
# build context, so the build does not depend on the repo-root .dockerignore.
# Build from the repo root:
#
#   docker build \
#     --build-context platform=platform \
#     --build-context sdk=sdk-go \
#     -f examples/rat-plugin-event-notifier/Dockerfile \
#     -t rat-plugin-event-notifier examples/rat-plugin-event-notifier
#
# Or just run `make build` from this directory.
FROM golang:1.26-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS builder
WORKDIR /build

# The platform module supplies the generated plugin/v1 ConnectRPC stubs.
COPY --from=platform . /platform/
COPY --from=sdk . /sdk-go/
COPY . .

RUN go mod tidy && CGO_ENABLED=0 go build -ldflags="-s -w" -o /event-notifier .

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN addgroup -S rat && adduser -S -G rat rat
COPY --from=builder /event-notifier /event-notifier
USER rat
EXPOSE 50090
ENTRYPOINT ["/event-notifier"]
