# Vendored schema — sources & attribution

This directory vendors the Closebot API schema so Closewire has a stable, offline
**oracle** to generate its client from (`scripts/codegen.py`) and to diff live drift
against (`scripts/fetch_spec.py`). No secrets are present — the OpenAPI spec is public.

## Attribution

The four files below are vendored **verbatim** from
[**Bleupreneur/closebot-agency-toolkit**](https://github.com/Bleupreneur/closebot-agency-toolkit),
licensed **MIT** (Copyright © 2026 Bleupreneur). MIT permits redistribution with
attribution; that upstream license notice is preserved via this file. Closewire itself is
also MIT (see repo `LICENSE`).

## Vendored files

Fetched **2026-07-22** from branch `main` (raw origin
`https://raw.githubusercontent.com/Bleupreneur/closebot-agency-toolkit/main/schema/`):

| File | Size (bytes) | SHA-256 | What it is |
| --- | --- | --- | --- |
| `openapi.json` | 261527 | `8cb9e3f450cb3c71860269c8a500d6e5c9cee8108d6cd32bf35eb4185f764887` | Real Closebot OpenAPI 3.0.1 spec — 91 paths / 126 operations / 17 tags. The codegen oracle. |
| `endpoints.index.json` | 32896 | `e646c779285aff31d9586c611f51bfcf13027026a91c09deccbc1f5f14e3af88` | Flat 1:1 operation list (127 entries = 126 REST + 1 live). Supplies stable operation `id`s (the raw Swagger omits `operationId` on 125/126 ops) → future MCP tool names. |
| `node-descriptors.json` | 42707 | `c33f6cb9e9d3bd1a067e9d4de6c7fac903b349a948973a96bc3382a95282c375` | Job-Flow node catalog (27 node types + account tools). **Not** in the Swagger; needed for bot flow editing (later phases). |
| `live-message.json` | 5314 | `927dc20a3749d18d202a46826d363ac1dd7d59652e36054bceba913825f9962d` | Hand-built spec for the runtime `POST https://api.closebot.ai/message` endpoint, absent from the REST Swagger. |

## Upstream / authoritative origins (for `fetch_spec.py`)

The **live** Closebot Swagger (used to detect drift, written to `openapi.live.json`,
git-ignored) is pulled directly from Closebot, not from the toolkit:

- Primary: `https://megastream25-api.closebot.com/swagger/v1/swagger.json`
- Mirror: `https://api.closebot.com/swagger/v1/swagger.json`
- Swagger UI: `https://api.closebot.com/swagger/index.html`

> ⚠️ **Do not** use `https://developers.closebot.com/api-reference/openapi.json` — it
> returns a Mintlify "Plant Store" placeholder, **not** the real API (see `RESEARCH.md`).

## Regenerating

```bash
python scripts/fetch_spec.py   # refresh openapi.live.json + print drift vs vendored
python scripts/codegen.py      # regenerate closewire_client/endpoints/ + OPERATIONS.md
```

Vendored files are kept verbatim; refresh them only by re-downloading from the toolkit and
updating the sizes/hashes above.
