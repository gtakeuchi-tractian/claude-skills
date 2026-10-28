---
name: camu-api
description: >
  How to call the CAMU API correctly — Brazilian electronic fiscal documents
  (DFe: NF-e, CTe, CT-e OS, NFSe Nacional): x-api-key auth, base URLs, every
  endpoint, the two pagination styles, wire shapes, and ready-to-use client
  implementations (TypeScript + Python) in reference/. Use when a task
  mentions "CAMU", "camu-api", chave de acesso / access key, DANFE/DANFSE/DACTE
  PDF, SPED EFD, manifestação, or before writing any curl/script/code that
  hits api.service.camu.com.br or api.staging.camu.com.br.
allowed-tools: Bash, Read, Write, Edit
updated: 2026-10-05
---

# CAMU API — usage reference

CAMU stores Brazilian electronic fiscal documents (DFes) per tenant and serves
them over REST. Every resource is keyed by the **chave de acesso** (access key).
Full OpenAPI 3.1 spec: `camu-api-1.json` in this skill folder; canonical live
copy `https://api.service.camu.com.br/openapi.yaml`.

## Auth & environments

- Every request sends header `x-api-key: <key>` (case-insensitive). **Never**
  `Authorization: Bearer` — the spec (`securitySchemes.ApiKeyAuth`) and every
  working client send `x-api-key`/`X-API-Key`.
- Production `https://api.service.camu.com.br` · Staging `https://api.staging.camu.com.br`.
- The key selects the tenant: cross-tenant reads return 404. Errors are always
  `{"error": "..."}` with 400/401/404/500. Rate limit 100 req/s, burst 500.
- The API key is **server-only** — never ship it to a browser; serve PDFs/docs
  through a server-side proxy that attaches the key.

## Client implementations (this skill folder)

Two working clients cloned from production integrations, config seam replaced
with env vars (`CAMU_API_URL`, `CAMU_API_KEY`):

| File | Lang | Notes |
|---|---|---|
| `reference/ts/camu-server.ts` | TypeScript, zero npm deps | Null-on-failure fetchers + 5-min in-process cache (negative results cached — documents are immutable). Types in `reference/ts/camu-types.ts`. |
| `reference/python/camu_client.py` | Python (httpx) | Typed errors (`CamuNotConfiguredError`/`NotFound`/`Upstream`/`Stale`), 5-min cache with stale-on-upstream-failure, parsers for NF-e and NFSe Nacional, both pagination styles. |

Adopt the same shape when writing a new consumer: server-only key,
null-on-failure (or typed-error) policy, immutable-document caching.

## Endpoints

`documentType` ∈ `nfe` · `cte` · `cte-os` · `nfse`.

| # | Call | Purpose |
|---|---|---|
| 1 | `GET /{documentType}` | List summaries. Filters: `document_status` (CSV of `captured,in_review,approved,errors,canceled`), `manifestation_status` (CSV, NFe only: `acknowledgement_of_issuance,confirmation_of_operation,operation_unknown,operation_not_performed,executing`), `issue_date_start`/`issue_date_end` (`YYYY-MM-DD`), `sort=issue_date|captured_at:(asc|desc)` (default desc), `fields=` (see Wire shapes). Offset-paginated. |
| 2 | `GET /{documentType}/{documentKey}` | Full document: `{documentType, documentKey, documentNumber, capturedAt, content, xmlContent}`. `content` = parsed XML as JSON. |
| 3 | `GET /{documentType}/{documentKey}/xml` | Original signed XML (`application/xml`) — signatures + authorization protocol included. |
| 4 | `GET /{documentType}/{documentKey}/pdf` | DANFE / DACTE / DACTE-OS / DANFSE as a **binary PDF stream**. |
| 5 | `GET /{documentType}/{documentKey}/events` | All events for one document — camelCase, no pagination, full type history (CTe delivery/MDF-e, NFSe cancellations). |
| 6 | `GET /{documentType}/events` | Tenant-wide event feed — cursor-paginated, **snake_case**, allowlisted types only (NFe: `Cancel`, `Approved`, `ValidationFailed`; `event_type` filter also accepts `Captured`, `Emitted`). Filters: `source=camu|sefaz`, `tax_id` (CSV CNPJs — 400 if any isn't in the tenant), `limit` (1–100, default 50). |
| 7 | `GET /{documentType}/events/{eventId}` | One event by UUID — **nfe only** today. |
| 8 | `POST /{documentType}/events/{eventId}/ack` | Ack processing; idempotent. Recommended for webhook consumers, optional when polling (track your own checkpoint). |
| 9 | `GET /companies` | All tenant companies, no pagination: `{id, name, taxId}` (taxId = 14-digit CNPJ, no formatting). |
| 10 | `POST /files/upload` | Ingest XMLs (NFe/CTe/NFSe Nacional/CC-e). multipart/form-data, field `file` or `files`; ≤5MB/file; `application/xml`/`text/xml`. Processing is async; per-file `filesDetails[]`: `isDfe`, `companyFound`, `alreadyExists`, `documentKey`, `storagePath`, `cceOutcome`. |
| 11 | `GET /sped/efd` | SPED EFD-ICMS/IPI as pipe-delimited `text/plain`. Query: `company_id` (UUID), `start_date`, `end_date`, optional `cod_fin=0|1` (0=Original, 1=Substitution). Built from NFe Entrada (inbound) perspective. |
| 12 | webhook `event` | 🚧 Not implemented on Camu's side yet. Payload `WebhookPayload` (snake_case, UUIDv7 `id` for idempotency), `X-Camu-Signature` header, exponential-backoff retries (1m/5m/30m/2h). |

## Wire shapes

- **List item (`DfeSummary`)** — fields: `documentType`, `documentKey`,
  `documentNumber`, `documentSeries`, `emissionDate`, `manifestationStatus`,
  `documentStatus`, `erpDocumentStatus`, `content`, `xmlContent`. Default
  returns all **except `xmlContent`** — it is opt-in via `fields=` and slows
  the response (fetched from storage per document). Pagination fields
  `nextOffset`/`nextUrl` are **omitted** on the last page (absence = done).
- **`documentStatus`** ∈ `captured | in_review | approved | errors | canceled`
  (same vocabulary as the `document_status` filter).
- **NF-e `content`** — `nfeProc.NFe.infNFe.*` tree (vendor at
  `infNFe.emit.CNPJ`, NCMs per item).
- **NFSe Nacional `content`** — raw DPS layout, **not** ABRASF:
  `content.NFSe.infNFSe.DPS.infDPS.*` (prest/toma CNPJs,
  `serv.cServ.cTribNac`, `valores.vLiq`, `dhEmi`). `documentNumber` is `null`
  on NFSe Nacional.
- **Events** — per-document (#5): `eventType`/`eventTypeCode`/`eventCategory`
  camelCase. Unified (#6): `event_type`/`event_type_code`/`event_category`
  snake_case. `eventCategory` ∈ `document|canceled|manifestation|delivery|correction|validation|other`.
  Validation-event content is an **array** of
  `{lineNumber, classification, validations[]}`; other event contents are objects.
- NFe tpEvento cheat-sheet: `110110` Correction (CCe) · `110111` Cancel ·
  `210200` ConfirmOperation · `210210` AcknowledgeReceipt · `210220`
  UnknownOperation · `210240` OperationNotPerformed.

## Two pagination styles — don't mix them

| Endpoint family | Style | Has-more signal |
|---|---|---|
| `GET /{documentType}` (documents) | `limit` (1–100, default 100) + `offset`; follow `nextOffset`/`nextUrl` | fields omitted on last page |
| `GET /{documentType}/events` (unified feed) | `limit` (1–100, default 50) + `cursor` (UUIDv7, time-ordered) | `has_more` bool + `next_cursor` (null when done) |

## Gotchas

1. **Key lengths in the wild**: NF-e = 44 digits; NFSe Nacional = **50 digits**
   (spec prose saying 44 is stale). Municipal NFS-e `codigo_verificador`
   (short, variable length) **does not exist in CAMU at all** — live-verified
   2026-09: 10/10 sampled codes 404. CAMU's NFSe scope is NFSe Nacional only.
2. **Two casings**: document + per-document-event payloads are camelCase;
   unified events and webhooks are snake_case.
3. **PDF is binary**, despite spec prose claiming "Base64-encoded PDF string"
   — stream `upstream.body` straight through. Never base64-decode.
4. **Direct browser/PDF links 401**: `x-api-key` can't ride a plain `<a href>`.
   Serve PDFs through a server-side proxy that attaches the key.
5. **Documents are immutable** once issued → safe to cache (reference clients
   cache 5 min in-process).
6. **Failure policy**: recommended pattern is null-on-failure (TS client) or
   typed errors with stale-cache fallback (Python client). A 404 on
   `/nfse/{key}` means "CAMU doesn't hold it" → fall back to another source
   if you have one. Never throw raw on CAMU errors.
7. **cTribNac → LC-116 service code**: use the **first 4 digits** (`140101` →
   `SERV-1401`). `slice(-4)` is the ABRASF/`CodigoItemLei116` rule and is wrong
   for the DPS shape.
8. **Uploads**: company matching gates processing (NFe: dest → transp → emit;
   CTe: toma → emit; NFSe Nacional: toma → prest). `companyFound=false` =
   stored but not processed. A CC-e needs its parent NFe already in the tenant
   (`cceOutcome: parent_not_found` otherwise); duplicates short-circuit with
   `alreadyExists: true`. The spec's `files` tag blurb claiming NFSe isn't
   uploadable is stale — the endpoint doc, `nfse-documents/` storage path and
   NFSe matching rules are newer.
9. **Unified-event allowlist**: `GET /{documentType}/events` returns only
   allowlisted types, and only NFe has entries today. Use per-document events
   (#5) for full history.
10. **`tax_id` filter**: every CNPJ must belong to the tenant, else 400.

## Quick curls

```bash
export CAMU_API_URL=https://api.service.camu.com.br
export CAMU_API_KEY=...   # server-side only

KEY=11250202916265003770550010002236881280492015   # 44-digit NF-e key

# Full NF-e
curl -s -H "x-api-key: $CAMU_API_KEY" "$CAMU_API_URL/nfe/$KEY" | jq '.content'

# Newest 20 NF-e, minimal fields
curl -s -H "x-api-key: $CAMU_API_KEY" \
  "$CAMU_API_URL/nfe?fields=documentType,documentKey,documentNumber,documentStatus&sort=issue_date:desc&limit=20" | jq .

# NFSe Nacional (50-digit key) — DPS shape
curl -s -H "x-api-key: $CAMU_API_KEY" "$CAMU_API_URL/nfse/<50-digit-key>" \
  | jq '.content.NFSe.infNFSe.DPS.infDPS'

# DANFE PDF → file
curl -s -H "x-api-key: $CAMU_API_KEY" "$CAMU_API_URL/nfe/$KEY/pdf" -o danfe.pdf

# Tenant companies (map CNPJ → company_id for SPED)
curl -s -H "x-api-key: $CAMU_API_KEY" "$CAMU_API_URL/companies" | jq .
```

## Key-length dispatch rule (multi-source consumers)

When an app accepts a user-typed document identifier:

- 44 digits → NF-e → CAMU `GET /nfe/{key}`
- 50 digits → NFSe Nacional → CAMU `GET /nfse/{key}`
- anything else (municipal `codigo_verificador`, CNPJ search) → **not CAMU**
  (the `/nfse` list has no CNPJ filter; municipal NFS-e isn't in CAMU at all)
