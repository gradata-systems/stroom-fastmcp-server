# Stroom FastMCP server

MCP server that lets a chat client or agent take a raw data sample and build working Stroom
content for it: a feed, an event-logging translation pipeline, and an Elasticsearch indexing
pipeline, stepped and verified before anything is promoted. See [docs/DESIGN.md](docs/DESIGN.md).

Status: scaffold. Tools so far: `find_documents`, `get_document`, `describe_pipeline` (read-only).

## Running

```
cp .env.example .env   # fill in
uv run python main.py
```

Tests: `uv run pytest`.

## Local Stroom for development

`dev/stroom` runs Stroom v7.13 and MySQL in Docker, bound to localhost. Destructive tests run
here, never against a shared instance.

```
cd dev/stroom && ./init-env.sh && docker compose up -d
```

The Phase 0 spike (`spike/phase0.py`) proves the risky Stroom APIs against it; results are in
[spike/FINDINGS.md](spike/FINDINGS.md).
