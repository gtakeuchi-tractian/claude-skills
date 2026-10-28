# NetSuite Python reference client

Dependency: `httpx` (async). Everything else is stdlib.

Reference clones from a working production integration (originally the
tractian-procurement "deck" backend). The repo's pydantic-settings config seam
was replaced with lazy `os.environ` reads — importing never fails, calling
without env raises `RuntimeError` naming the missing vars.

## Files

- `suiteql_client.py` — OAuth 1.0a TBA signer (query-params-in-signature
  included), `run_suiteql` (cursor pagination, 429/INVALID_LOGIN backoff,
  process-wide concurrency gate), `fetch_ns_record_list` / `fetch_ns_record`
  (Record API), plus domain queries (vendor by CNPJ, PO lines, items by
  NCM/service code, CFOP, expense accounts, vendor bills).
- `queries.py` — pure SuiteQL string constants (subsidiaries, departments,
  classes, items, UOM, vendors + extended, terms, incoterms, payment methods,
  locations, POs, vendor bills) with account-verified quirk comments.

## Env vars

`NETSUITE_ACCOUNT_ID`, `NETSUITE_CONSUMER_KEY`, `NETSUITE_CONSUMER_SECRET`,
`NETSUITE_ACCESS_TOKEN`, `NETSUITE_TOKEN_SECRET`, `NETSUITE_REALM` (defaults
to account id), `NETSUITE_URL` (default `suitetalk.api.netsuite.com`),
`NETSUITE_MAX_CONCURRENCY` (default 4).

## Usage

```python
import asyncio
from suiteql_client import run_suiteql

rows = asyncio.run(run_suiteql("SELECT id FROM subsidiary WHERE id = 3"))
```

Note: the source repo is read-only against NetSuite — no Vendor Bill/Sales
Order write payloads exist here. For write payload lessons see the skill's
SKILL.md (Vendor Bill payload constraints section).
