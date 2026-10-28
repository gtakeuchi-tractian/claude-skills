# NetSuite TypeScript reference client

Zero npm deps — Node 18+ builtins (`node:crypto`, global `fetch`) + `tsx` to run.

Reference clones from a working production integration (originally the mint
repo). Do not add repo-specific imports here; copy these files into your
project and adapt there.

## Files

- `env.ts` — env accessors: `getNsCredentials()` (validates 5 creds),
  `suiteTalkHost(accountId)` (lowercase, `_`→`-`), `assertSandboxOnlyInDev()`
  guardrail. Reads `NS_*` env vars.
- `netsuiteAuth.ts` — `buildNsAuthHeader(method, urlPath, queryParams?)`,
  OAuth 1.0a TBA HMAC-SHA256 signer. Imports `./env`.
- `suiteql-probe.ts` — sandbox-guarded single-query probe; prints raw response.
- `live-lib.ts` — `loadEnvLocal()`, `suiteql(q)`, `runAll(checks)` harness.

## Env vars

`NS_ACCOUNT_ID`, `NS_REALM` (defaults to account id), `NS_CONSUMER_KEY`,
`NS_CONSUMER_SECRET`, `NS_TOKEN_ID`, `NS_TOKEN_SECRET`.

## Usage

```bash
# probe loads .env.sandbox.local from cwd (override: NS_ENV_FILE=.env.local)
pnpm exec tsx suiteql-probe.ts "SELECT id FROM subsidiary WHERE id = 3"
```

The probe hard-aborts unless `NS_ACCOUNT_ID` ends in `_SB<digits>` (sandbox
only). `env.ts` additionally refuses non-sandbox ids when
`NODE_ENV=development` unless `NS_ALLOW_PROD_LOCAL=true`.
