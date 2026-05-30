# rat-protos

Generated gRPC + protobuf stubs for the RAT data platform.

One shared package, consumed by every Python service that talks RAT's gRPC contracts
(`runner`, `query`, and every plugin in `plugins/`). Replaces the per-service
`<svc>/gen/` directory we used to vendor in each consumer (a real waste + drift
risk).

## Layout

`src/` contains the top-level proto packages exactly as the codegen emits them:

```
src/
├── catalog/v1/   ← catalog/v1 service contracts
├── common/v1/    ← shared types (TableRef, TableDescriptor, CatalogDescriptor, …)
├── engine/v1/
├── storage/v1/
├── query/v1/
├── runner/v1/
└── …
```

Each is a PEP 420 namespace package — `from catalog.v1 import catalog_pb2` works
once `src/` is on `sys.path`.

## Regenerating

`make proto` at the repo root. Output lands here (see `proto/buf.gen.yaml`).

## Versioning

Pinned to `buf.build/protocolbuffers/python:v33.0` so the gencode targets the
protobuf 6.33.x Python runtime that ships from PyPI. Bump in lockstep with the
protobuf dep in consumers when upgrading.
