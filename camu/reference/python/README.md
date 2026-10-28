# CAMU Python reference client

Deps: `httpx` (origin also used structlog — replaced with stdlib logging here).

Config via env vars: `CAMU_API_KEY` (required; missing → `CamuNotConfiguredError`
at call time), `CAMU_API_URL` (optional; defaults to
`https://api.service.camu.com.br`).

Minimal usage — fetch one NF-e by its 44-digit access key:

```python
import asyncio
from camu_client import fetch_nfe_by_key

doc = asyncio.run(fetch_nfe_by_key("3525...44 digits..."))
print(doc["issuer_name"], doc["total_nfe"])
```

Also exported: `fetch_nfes` (60-day list, both pagination styles),
`fetch_nfse_nacional_by_key`, `fetch_pdf_by_key` (DANFE/DANFSE bytes),
`parse_nfe_document`, `parse_nfse_nacional_document`, raw-payload re-walkers
(`issuer_uf_from_raw_payload`, `icms_tot_from_raw_payload`,
`nfe_item_vprod_values_from_raw_payload`, `raw_nfe_payload_by_key`), and typed
errors (`CamuNotFoundError`, `CamuUpstreamError`, `CamuStaleError`). 5-minute
cache with stale-on-upstream-failure. Cloned from the tractian-procurement
backend; the fake-provider fixture tier was removed.
