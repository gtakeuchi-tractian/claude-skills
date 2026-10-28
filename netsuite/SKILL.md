---
name: netsuite
description: >
  How to authenticate and call NetSuite (SuiteTalk REST + SuiteQL) with OAuth
  1.0a TBA: signing rules (query params in the signature), the two hosts,
  SuiteQL protocol, isolated production and sandbox reads from external credentials,
  sandbox setup + guardrails, ready-to-use client code
  (TypeScript + Python) in reference/, and account-verified gotchas. Use when
  a task mentions "NetSuite", "SuiteQL", "SuiteTalk", "TBA", "OAuth TBA",
  "Vendor Bill", "NS_ACCOUNT_ID", "NETSUITE_ACCOUNT_ID", "sandbox setup",
  or before writing any script/code that reads/writes NetSuite data.
allowed-tools: Bash, Read, Write, Edit
updated: 2026-10-09
---

# NetSuite — usage reference + sandbox setup

Auth is **OAuth 1.0a TBA, HMAC-SHA256** — never Basic/Bearer. TBA tokens don't
expire; a sudden 401/403 after months usually means the token's role lost
permissions, not key rotation.

## Two hosts — don't confuse

| Host | Serves |
|---|---|
| `<account>.suitetalk.api.netsuite.com` | REST record API `/services/rest/record/v1/...` + SuiteQL `/services/rest/query/v1/suiteql` |
| `<account>.restlets.api.netsuite.com` | RESTlets `/app/site/hosting/restlet.nl?script=X&deploy=Y` |

Host rule: lowercase, `_` → `-` — sandbox `123456_SB1` → `123456-sb1.suitetalk.api.netsuite.com`.

## Auth: OAuth 1.0a TBA

- Signature base string = `METHOD & percentEncode(baseUrl-without-query) & percentEncode(sorted params)`; signing key = `percentEncode(consumerSecret) & percentEncode(tokenSecret)`; HMAC-SHA256.
- **Request query params MUST join the signature parameter set** — NetSuite 401s when the URL has a query absent from the base string (RFC 5849 §3.4.1.3). This applies to `?expandSubResources=true`, pagination `limit`/`offset`, and SuiteQL next-page URLs.
- Percent-encoding is RFC 3986 strict: `! * ' ( )` must be explicitly escaped (`encodeURIComponent`/`urllib.parse.quote` leave them alone).
- Header shape: `OAuth realm="...", oauth_consumer_key="...", oauth_token="...", oauth_signature_method="HMAC-SHA256", oauth_timestamp="...", oauth_nonce="...", oauth_version="1.0", oauth_signature="..."`.
- Realm defaults to the account id.

## Reference clients (this skill folder)

Working client code cloned from two production integrations, config seams
replaced with plain env vars. Copy into your project and adapt — don't import
across repos.

| File | Lang | What you get |
|---|---|---|
| `scripts/prod_read.py` | Python, stdlib only | Isolated production record GET and SELECT-only SuiteQL; private external credential file, no inherited credentials |
| `scripts/sandbox_read.py` | Python, stdlib only | Sandbox-only entrypoint sharing the same read transport; external sandbox credentials, production accounts refused |
| `reference/ts/netsuiteAuth.ts` + `reference/ts/env.ts` | TypeScript, zero npm deps | `buildNsAuthHeader(method, urlPath, queryParams?)`, `getNsCredentials()`, `suiteTalkHost()`, `assertSandboxOnlyInDev()` guardrail |
| `reference/ts/suiteql-probe.ts` | TypeScript | Sandbox-guarded single-query probe, prints raw response incl. `o:errorDetails` |
| `reference/ts/live-lib.ts` | TypeScript | `loadEnvLocal()`, `suiteql(q)`, `runAll(checks)` serial live-check harness |
| `reference/python/suiteql_client.py` | Python (httpx) | Full async client: signer, 429/INVALID_LOGIN backoff, process-wide concurrency gate, cursor pagination, Record API, domain queries |
| `reference/python/queries.py` | Python | Pure SuiteQL string catalog (subsidiaries, vendors, terms, POs, bills…) with quirk comments |

## Env var naming — two conventions in the wild

Semantic credential set is identical; names differ per repo. Both reference
clients read their own convention:

| Semantic | `NS_*` convention (TS client) | `NETSUITE_*` convention (Python client) |
|---|---|---|
| Account id | `NS_ACCOUNT_ID` | `NETSUITE_ACCOUNT_ID` |
| Realm (defaults to account id) | `NS_REALM` | `NETSUITE_REALM` |
| Consumer pair | `NS_CONSUMER_KEY` / `NS_CONSUMER_SECRET` | `NETSUITE_CONSUMER_KEY` / `NETSUITE_CONSUMER_SECRET` |
| TBA token pair | `NS_TOKEN_ID` / `NS_TOKEN_SECRET` | `NETSUITE_ACCESS_TOKEN` / `NETSUITE_TOKEN_SECRET` |
| Host suffix override | — (helper `suiteTalkHost()`) | `NETSUITE_URL` (default `suitetalk.api.netsuite.com`) |
| Concurrency cap | — | `NETSUITE_MAX_CONCURRENCY` (default 4) |
| Prod-local escape hatch | `NS_ALLOW_PROD_LOCAL` | — |

Known local repos: **mint** (`~/mint`) uses `NS_*`; **tractian-procurement**
(`~/tractian-procurement`, Python backend) uses `NETSUITE_*`. When writing new
code, match the repo you're in.

## SuiteQL protocol

- `POST <suitetalk-host>/services/rest/query/v1/suiteql`, `Content-Type: application/json`, body `{"q": "SELECT ..."}`, header **`prefer: transient`** (avoids locks).
- Pagination two ways: `limit`/`offset` query params (remember: they join the signature), or follow `links[rel="next"]` hrefs — re-sign each page URL with its query params. The Python client's `run_suiteql` does cursor pagination; 1000/page is the practical page size.
- **SuiteQL silently ignores OFFSET/FETCH** inside the query text — paginate via the protocol, not the SQL.
- Ad-hoc read-only probe (TS, sandbox-guarded):

  ```bash
  cd <dir-with-env-file> && pnpm exec tsx <skill>/reference/ts/suiteql-probe.ts "SELECT id, tranid FROM transaction WHERE rownum < 5"
  ```

## Separation of concerns: Deck versus standalone tooling

These are independent consumers, not one shared environment:

- **Deck application:** each checkout loads its own `backend/.env` containing
  sandbox `NETSUITE_*` credentials. Keep this unchanged. Deck must not load the
  skill's external profiles or import skill scripts. Production credentials do
  not belong in Deck `.env` files or its process environment.
- **Standalone production investigations:** the skill's `scripts/prod_read.py`
  loads only `~/.config/netsuite/.env.prod`.
- **Standalone sandbox investigations:** the skill's `scripts/sandbox_read.py`
  loads only `~/.config/netsuite/.env.sandbox`.

Use the application connection for testing Deck submissions; use the standalone
reader for inspecting NetSuite directly. Never edit Deck's configuration as a
side effect of a skill investigation, discover repo `.env` files, merge profiles,
or source external profiles into a shared shell. Identical sandbox credentials
in Deck and the external sandbox file are acceptable. Separate sandbox tokens
are optional for independent auditing and revocation.

Selecting a profile authorizes no writes. Both standalone readers are read-only;
any separate write procedure requires explicit per-action authorization naming
its environment and exact target. Never weaken the production reader's account
guard to access sandbox; use the sandbox entrypoint instead.

## Isolated production reads (preferred agent path)

Use `scripts/prod_read.py` for deliberate production reads. It runs independently
of Deck, uses Python's standard library, and never loads repository `.env` files
or inherited credential variables. Default credential file:
`/Users/haruo/.config/netsuite/.env.prod` (portable default: `~/.config/netsuite/.env.prod`).
Never `source` this file into a shell launching Deck. Never copy production tokens
into Deck, sandbox profiles, source files, or worktrees.

### Credential and authorization boundary

- Production token must belong to a dedicated role with **View-only** permissions
  for required records plus the SuiteTalk/SuiteAnalytics access needed for reads.
  No write-capable RESTlets. File isolation does not change token permissions.
- The CLI cannot prove the role is read-only. Its `check` command is offline
  validation, not a role-permission audit or a live authentication check. Never
  test permissions by attempting
  a production write.
- Reads must stay within the user's requested investigation. Production writes
  require explicit per-action authorization and are not supported by this CLI.
- Do not read the credential file into tool output. Use `check` to validate it
  without printing secrets. Do not print auth headers or use shell tracing.
- Credential files must be owned by the current user, with no group/other access;
  recommended file mode `600`, parent directory mode `700`.
- Uses only the selected prefix from the explicit file. Default `NETSUITE_`;
  `--credential-prefix NETSUITE_PROD_` supports migrated Deck production keys;
  `--credential-prefix NS_` supports mint naming. No fallback or merging between
  prefixes, files, or process environments. Realm defaults to the account ID.
- Required keys: account ID, consumer key/secret, access token/token secret using
  the naming table above. For `NETSUITE_PROD_`, prepend `PROD_` after `NETSUITE_`
  (for example `NETSUITE_PROD_ACCESS_TOKEN`). No shell interpolation is performed.
- Requires a numeric production account ID; rejects sandbox accounts. Host is
  derived from that account, HTTPS only. Host overrides and redirects are refused.

### Commands (run from any directory)

Offline validation, safe account/key-name output only:

```bash
python3 ~/.claude/skills/netsuite/scripts/prod_read.py check
```

If the external file retains Deck's `NETSUITE_PROD_*` names:

```bash
python3 ~/.claude/skills/netsuite/scripts/prod_read.py --credential-prefix NETSUITE_PROD_ check
```

Use the same prefix option on each subsequent command. An alternative credential
file must be passed explicitly with `--env-file /absolute/path/to/private.env`.
Global options precede the subcommand.

Read the known production vendor bill (JSON on stdout):

```bash
python3 ~/.claude/skills/netsuite/scripts/prod_read.py record vendorBill 992479 --expand
```

Read a small SuiteQL result:

```bash
python3 ~/.claude/skills/netsuite/scripts/prod_read.py suiteql 'SELECT id, tranid FROM transaction WHERE id = 992479' --limit 10
```

The only network operations are record **GET** and a single-statement **SELECT**
via **POST** to the exact SuiteQL endpoint with `Prefer: transient`. SuiteQL POST
is a read, not a record mutation. No generic HTTP command, RESTlets, record
POST/PATCH/DELETE, arbitrary hosts, or write operations are exposed. Conservative
SQL validation rejects comments, semicolons, quoted identifiers, and mutation
keywords outside string literals; it is defense in depth, not a replacement for
NetSuite's read-only role.

SuiteQL returns **one page**, including `hasMore`. Default limit is 100;
maximum `--limit` is 1000. If `hasMore` is true, request the next page explicitly
with `--offset` advanced by the limit. Keep reads serial; narrow queries first.
Record `--expand` is not a guarantee that every large sublist is fully paginated.
OAuth signs all URL query parameters and is regenerated on retries. Retries are
bounded for 429 and 401 `INVALID_LOGIN`; other errors stop the command.

JSON may contain confidential business data. Prefer narrow fields/results. Before
persisting an export, choose an approved private destination outside Git, verify
it is not an existing file, and restrict its permissions. Never publish record
exports to GitHub, Slack, or another service without explicit authorization.

Offline regression tests (outside Deck; no credentials or network required):

```bash
python3 -m unittest discover -s ~/.claude/skills/netsuite/scripts -p 'test_*.py'
```

## Isolated sandbox reads (preferred agent path)

Use `scripts/sandbox_read.py` for sandbox investigations independent of Deck.
Default credential file: `~/.config/netsuite/.env.sandbox`. Uses `NETSUITE_*` by
default; pass `--credential-prefix NS_` for mint names. Global options precede
the subcommand, just as for the production reader.

```bash
# Offline credential validation; no secrets printed, no network calls
python3 ~/.claude/skills/netsuite/scripts/sandbox_read.py check

# Small sandbox read; does not use Deck's connection
python3 ~/.claude/skills/netsuite/scripts/sandbox_read.py suiteql 'SELECT id FROM subsidiary WHERE id = 3' --limit 10

# Explicit alternative private file, when necessary
python3 ~/.claude/skills/netsuite/scripts/sandbox_read.py --env-file /absolute/path/to/private-sandbox.env check
```

`record TYPE ID [--expand]` is also available; supply an actual sandbox record ID,
not a production ID copied without verification. The sandbox reader accepts
numeric accounts ending `_SB<digits>` or `-sb<digits>` case-insensitively and
refuses production accounts. Host normalization is lowercase with `_` replaced
by `-`; OAuth realm uses the explicit `NETSUITE_REALM` (or selected-prefix
`REALM`) verbatim, falling back to the account value when omitted. Account and
realm must identify the same sandbox after host normalization, not match
literally. For example, `NETSUITE_ACCOUNT_ID=9403772-sb1` with
`NETSUITE_REALM=9403772_SB1` is valid and was verified live on 2026-10-09.
Different account numbers, sandbox slots, or production/sandbox profiles remain
refused. Host-form account values do not necessarily work as OAuth realms;
use the account's canonical NetSuite realm spelling.

Both readers share the same signer and transport, private-file permission checks,
SELECT-only validation, fixed hosts, blocked redirects, bounded retries, explicit
single-page pagination, and safe diagnostics. Neither offers write commands or
loads inherited credentials. Credential file mode must exclude group/other access
(recommended `600`; parent `700`). Neither performs role-permission verification.
Do not run live probes merely because a credential file was added; run them only
within a requested investigation.

## Vendor Bill payload lessons (write path)

Learned on account 9403772 (subsidiary Brazil id 3 — add `subsidiary = 3` to
BR transaction queries). Ids below are THAT account's — treat as examples of
the *kind* of fixed constants a single-subsidiary integration bakes in, not as
values to copy.

- **`orderLine` = the PO line's native `transactionLine.id`, NOT `linesequencenumber`** — ids survive PO line edits/deletes while sequence numbers get compacted (PO 861761: native id 14 at sequence 1; `orderLine: 1` 400s with "item and purchase order combination that does not exist"). Select `tl.id as polineid`.
- **Field names can be misspelled in the production schema** (e.g. `catergory` for `DocumentLineAdditionalExpenses`) — renaming to the "correct" spelling breaks every line at submit with no type error. Verify against the live account before fixing "typos" in payload code.
- Dates: send bare `YYYY-MM-DD`; never `new Date("YYYY-MM-DD")` — single-arg form parses as UTC and rolls back a day in negative-offset timezones.
- Reference fields reject empty values (`{id: ""}`) — omit the key entirely.
- REST record API POST returns **204 with no body** — the new record id comes from the `Location` header.
- `expense.items` lines require an explicit `account` reference — NetSuite does not infer it from the category.

## Gotchas

1. Query params in the URL must be part of the OAuth signature, else 401.
2. SuiteQL column names are case-insensitive; string **values** are case-sensitive.
3. Fields can be hidden from `SELECT *` / `NOT_EXPOSED` and may error only when rows match — select rare fields explicitly by name.
4. `vendor.defaultaddress` is NOT_EXPOSED to SuiteQL (400); source the default billing address via the `vendorAddressbook`/`vendorAddressbookEntityAddress` join (`ea.addrtext`) or the Record API.
5. SuiteQL subrecord dot-notation on `addressBookAddress.<field>` is broken on account 9403772 — always echoes the address's internal id. Use the Record API (`GET /record/v1/vendor/{id}?expandSubResources=true`) for resolved subrecord fields.
6. `transactionLine.description` hard-fails ANY select on account 9403772 with `UNEXPECTED_ERROR` — the line description lives in `transactionLine.memo`.
7. Rate limits: serialize SuiteQL calls; never `Promise.all` large bursts (429). NetSuite also intermittently returns `401 INVALID_LOGIN` to valid credentials under burst — retry both with capped exponential backoff + jitter, honor `Retry-After` (see Python client `_post_with_backoff`).
8. `trandate` is `YYYY-MM-DD` already in account timezone — compare as strings; don't round-trip through `new Date()`.
9. `BUILTIN.DF(field)` resolves List/Record fields to display text (status, terms, incoterms, payment methods) without a join.
10. Custom record types in FROM/JOIN want uppercase (`CUSTOMRECORD_*`); lowercase can 400 on sandbox.
11. Sandbox account ids end `_SB<digits>`.
12. Never sign NetSuite requests client-side; long-lived TBA credentials are not revocable session tokens.

---

# Sandbox dev setup

Goal: local dev talks to the NetSuite **sandbox** account (`…_SB<n>`), reads
verified live, writes blocked or deliberately gated.

## Hard rules

- **Never print secret values** (consumer/token keys+secrets) into the transcript. Inspect env files key-names-only, or assert properties (non-empty, `_SB` suffix) without echoing.
- **Never commit secrets.** `.env*.local` are gitignored — keep real creds inside them, never in code, docs, or git history.
- Sandbox creds only in sandbox env files. Production creds belong only in a dedicated prod env file used for deliberate prod-local runs — never paste prod pairs into sandbox files.

## Step 1 — obtain sandbox TBA credentials

The agent **cannot fetch these** — they are shown once by the NetSuite SANDBOX UI:

1. Log into the sandbox UI (host must read `…-sb1.app.netsuite.com`) — creating creds on the prod UI makes a prod pair, which guardrails will rightly reject.
2. Setup > Integration > Manage Integrations → integration record with "Token Based Authentication" checked → one-time **consumer key + secret**.
3. Setup > Users/Roles > Access Tokens > New → same integration, your user, a TBA-enabled role (e.g. Administrator) → one-time **token id + secret**.
4. Copy the account id verbatim in `_SB<n>` form (e.g. `9403772_SB1`); hosts are normalized automatically.

Ask the user to save the five values directly into a private local credential file,
not into chat. Do not invent, guess, read them into tool output, or echo them.

## Step 2 — write the env file(s)

For Deck, keep sandbox credentials in each checkout's own `backend/.env` using
`NETSUITE_*`. For standalone skill tooling, keep them independently in
`~/.config/netsuite/.env.sandbox`; never synchronize either file automatically.
Use the naming table above. Account id **must end `_SB<digits>`** (the standalone
reader also accepts normalized `-sb<digits>`). Set realm to the canonical NetSuite
account ID, typically `9403772_SB1`, even when the standalone account value uses
host form `9403772-sb1`. Both must identify the same account and sandbox slot.
Keep any prod escape-hatch var (`NS_ALLOW_PROD_LOCAL`) false/unset.

## Step 3 — static validation (key-names-only)

Assert per file, without printing values:

- required vars present and non-empty (account id, realm, consumer pair, token pair)
- account id matches `[0-9]+_SB[0-9]+$`
- realm identifies the same account and sandbox slot (when set); standalone readers compare normalized hosts while preserving the configured realm for authentication
- no mock-mode/prod-bypass flag is `true`

Validate against the repo's `.env.example`, never against another machine's env file — real-world files carry harmless legacy leftovers. Extra keys are not failures; missing required keys are.

## Step 4 — live verification (read-only)

Preferred standalone probe (external sandbox file only; no repository dependencies):

```bash
python3 ~/.claude/skills/netsuite/scripts/sandbox_read.py suiteql 'SELECT id FROM subsidiary WHERE id = 3' --limit 10
```

Legacy TS probe remains a reference implementation, not the default investigation
path: it uses `NS_*`, discovers `.env.sandbox.local` unless `NS_ENV_FILE` is set,
and may inherit process credentials. Do not use it to bypass isolated readers.
Application integration checks use the app's own connection and repository
Makefile workflow; do not load external skill profiles into Deck.

Interpreting output:

- `200` + expected row → env file is good. Reading a known subsidiary id also sanity-checks you're on the right account, not just authenticated.
- `401` → wrong consumer/token pair or realm mismatch (re-check Step 1).
- `403` after previously working → the token's role lost permissions, not key rotation.
- "Not sandbox …; abort." → a prod account id was filled in; fix the file, do **not** bypass.

## Step 5 — guardrails are features

Two guardrail patterns worth keeping in any integration (both ship in the
reference clients):

1. **Sandbox-only in dev** — refuse to sign when the account id lacks `_SB<digits>` and `NODE_ENV=development` (`assertSandboxOnlyInDev` in `reference/ts/env.ts`).
2. **Submit-gate isolation** — if writes go through an external gate/service, remember the write lands in whichever account THE GATE's credentials point at, independent of the app's sandbox creds. End-to-end sandbox writes need a sandbox-configured gate; otherwise reads work and submit stays blocked — that's the correct sandbox state.

## Done criteria

- Step 4 probe returns the expected row from the sandbox, and
- the app boots and a real lookup returns live sandbox data.
