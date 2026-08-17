# RAT lineage plugin — three-stage build:
#   1. node:20 builds the UI bundle (esbuild → dist/bundle.js)
#   2. golang:1.24 builds the Go binary (embeds the UI via go:embed)
#   3. alpine:3.21 runtime
#
# Build from the repo root so the platform module is available as a
# named context:
#
#   docker build \
#     --build-context platform=platform \
#     --build-context sdk=sdk-go \
#     -f examples/rat-plugin-lineage/Dockerfile \
#     -t rat-plugin-lineage examples/rat-plugin-lineage
#
# Or `make build` from this directory.
FROM node:26-alpine@sha256:aadf416b2cdce311a8811ba3f0608a61b77dbf997500e2eafe781b51f6a0b019 AS ui-builder
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install --no-audit --no-fund --silent || npm install --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM golang:1.26-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS go-builder
WORKDIR /build
COPY --from=platform . /platform/
COPY --from=sdk . /sdk-go/
COPY . .
# Pull the built UI bundle into the Go build context so go:embed picks
# it up. ui/dist/bundle.js is the only file we need at this stage.
COPY --from=ui-builder /ui/dist/bundle.js ./ui/dist/bundle.js
RUN go mod tidy && CGO_ENABLED=0 go build -ldflags="-s -w" -o /lineage .

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
RUN addgroup -S rat && adduser -S -G rat rat
COPY --from=go-builder /lineage /lineage
USER rat
EXPOSE 50098
ENTRYPOINT ["/lineage"]
