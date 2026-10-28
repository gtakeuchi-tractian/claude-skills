# CAMU TypeScript reference client

Zero npm dependencies — global `fetch`, Node 18+.

Config via env vars: `CAMU_API_URL` (base URL; unset → all fetchers return null),
`CAMU_API_KEY` (sent as `X-API-Key` header).

Exports from `camu-server.ts`: `fetchNfeByAccessKey`, `fetchNfseNacionalByKey`
(optional `pdfUrlBase` opt), `fetchTenantCompanies`, `fetchNfeListPage`,
`fetchNfseListPage`. Types in `camu-types.ts` (`CamuNfeRawData`,
`CamuNfseNacionalRawData`, `CamuDfeListPage`, list params, `CamuTenantCompany`).

Policy: null on any failure (never throws), 5-minute in-process cache including
negative results, envelope unwrapping (`content`, `companies`).

Cloned from a working production integration (originally the mint repo); the
mock-fixture tier was removed for this standalone clone.
