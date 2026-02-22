# 🐀 RAT — Anyone Can Data

> *A self-hostable data platform. Write SQL, run pipelines, query your data — all from a web IDE.*

[![CI](https://github.com/squat-collective/rat/actions/workflows/ci.yml/badge.svg)](https://github.com/squat-collective/rat/actions/workflows/ci.yml)
[![Docs](https://github.com/squat-collective/rat/actions/workflows/docs.yml/badge.svg)](https://squat-collective.github.io/rat/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

RAT is an open-source data platform built for people who know SQL. Community Edition is free, self-hostable, and runs with a single `docker compose up`.

Part of [Le Squat](https://squat-collective.github.io/website/) — an underground builders collective.

## Quick Start

```bash
git clone https://github.com/squat-collective/rat.git
cd rat
make up
```

Open [http://localhost:3000](http://localhost:3000) — you're running.

## Architecture

RAT runs as 7 containers:

| Service | Language | Role |
|---------|----------|------|
| **ratd** | Go | API server, scheduling, auth, plugins |
| **runner** | Python | Pipeline execution (DuckDB + Iceberg) |
| **ratq** | Python | Interactive DuckDB queries (read-only) |
| **portal** | Next.js | Web IDE — the only user interface |
| **postgres** | — | Platform state |
| **minio** | — | S3-compatible object storage |
| **nessie** | — | Git-like Iceberg catalog |

```
Portal → ratd (REST) → runner/ratq (gRPC) → DuckDB → Iceberg → MinIO
```

## Features

- **Medallion architecture** — Bronze → Silver → Gold data layers
- **SQL + Python pipelines** — Write transforms in the language you know
- **Git-like isolation** — Each run gets its own Nessie branch
- **Incremental processing** — Watermark-based incremental loads
- **Built-in quality tests** — Not-null, unique, accepted values, custom SQL
- **Cron scheduling** — 5-field cron expressions with catch-up
- **Web IDE** — CodeMirror editor, query console, DAG visualization

## Development

```bash
make help             # show all targets
make up               # start all services
make test             # run all tests (Go + Python + TS)
make lint             # lint all code
make dev-portal       # hot-reload portal
make dev-ratd         # hot-reload platform
```

See [CLAUDE.md](CLAUDE.md) for full development guidelines.

## Documentation

📖 **[squat-collective.github.io/rat](https://squat-collective.github.io/rat/)**

## License

Apache-2.0 — see [LICENSE](LICENSE).

---

Built underground by [Le Squat](https://squat-collective.github.io/website/) 🐀
