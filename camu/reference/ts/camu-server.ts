/**
 * Cached fetches from the CAMU fiscal-documents API.
 *
 * Reference clone of a working production integration (originally the mint
 * repo's src/lib/billing/camu.server.ts) with the mock-fixture tier removed —
 * this module always talks to the real CAMU API.
 *
 * Two document-kind arms:
 *  - NF-e (44-digit access key) — `fetchNfeByAccessKey`.
 *  - NFSe Nacional (50-digit numeric chave de acesso) — `fetchNfseNacionalByKey`.
 *    Covers ONLY documents CAMU's NFSe Nacional integration holds; municipal
 *    (short-code) NFS-e has no CAMU path.
 *
 * Plus two browse arms: `fetchTenantCompanies` (GET /companies) and
 * `fetchNfeListPage` / `fetchNfseListPage` (GET /nfe, GET /nfse).
 *
 * Config: CAMU_API_URL and CAMU_API_KEY from process.env. Unset CAMU_API_URL
 * → every fetcher returns null (null-on-any-failure policy — callers treat
 * null as "unverifiable"/"tenant unknown"/"exhausted", never throw).
 *
 * Zero npm deps; global fetch; Node 18+.
 */
import type {
  CamuDfeListPage,
  CamuNfeListParams,
  CamuNfeRawData,
  CamuNfseListParams,
  CamuNfseNacionalRawData,
  CamuTenantCompany,
} from "./camu-types";

function getCamuConfig(): { apiUrl: string | undefined; apiKey: string | undefined } {
  return {
    apiUrl: process.env.CAMU_API_URL,
    apiKey: process.env.CAMU_API_KEY,
  };
}

const CACHE_TTL_MS = 5 * 60 * 1000; // CAMU documents are immutable once issued — safe to cache
const nfeCache = new Map<string, { at: number; data: CamuNfeRawData | null }>();
const nfseNacionalCache = new Map<string, { at: number; data: CamuNfseNacionalRawData | null }>();
const tenantCompaniesCache = new Map<string, { at: number; data: CamuTenantCompany[] | null }>();
const nfeListPageCache = new Map<string, { at: number; data: CamuDfeListPage | null }>();
// The NFS-e arm owns its cache Map (never shared with the nfe arm's): same TTL
// semantics, disjoint keyspaces.
const nfseListPageCache = new Map<string, { at: number; data: CamuDfeListPage | null }>();

function camuHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const { apiKey } = getCamuConfig();
  if (apiKey) headers["X-API-Key"] = apiKey;
  return headers;
}

async function fetchNfeByAccessKeyUncached(accessKey: string): Promise<CamuNfeRawData | null> {
  const camuUrl = getCamuConfig().apiUrl;
  if (!camuUrl) return null;

  try {
    const res = await fetch(`${camuUrl}/nfe/${accessKey}`, {
      method: "GET",
      headers: camuHeaders(),
    });
    if (!res.ok) return null;
    return (await res.json()) as CamuNfeRawData;
  } catch {
    return null;
  }
}

/**
 * NF-e only. Returns null on any failure (network, 4xx/5xx, unset CAMU_API_URL)
 * — callers treat null as "unverifiable", never throw.
 */
export async function fetchNfeByAccessKey(accessKey: string): Promise<CamuNfeRawData | null> {
  const cached = nfeCache.get(accessKey);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  const data = await fetchNfeByAccessKeyUncached(accessKey);
  nfeCache.set(accessKey, { at: Date.now(), data });
  return data;
}

// ---------------------------------------------------------------------------
// NFSe Nacional arm
// ---------------------------------------------------------------------------

/**
 * Live `GET /nfse/{documentKey}` envelope (2026-09-09, prod CAMU): the
 * document payload sits under `content` (top-level also carries
 * `documentType`, `documentNumber` — null on NFSe —, `documentKey`,
 * `xmlContent` etc.; only `content` is consumed).
 */
interface CamuNfseNacionalEnvelope {
  documentKey: string;
  content?: Omit<CamuNfseNacionalRawData, "docKind" | "pdfUrl" | "documentKey">;
}

/** Options for `fetchNfseNacionalByKey`. */
export interface FetchNfseNacionalOptions {
  /**
   * Base of a PDF URL to inject as the result's `pdfUrl` field (CAMU serves
   * the DANFSE PDF at `/nfse/{key}/pdf`; a proxy base lets callers hand their
   * UI a ready link). The final value is `${pdfUrlBase}?key=${key}` — e.g.
   * pass `/api/camu/pdf` for a same-origin proxy route. Absent → no pdfUrl
   * is injected.
   */
  pdfUrlBase?: string;
}

async function fetchNfseNacionalByKeyUncached(
  key: string,
  opts?: FetchNfseNacionalOptions,
): Promise<CamuNfseNacionalRawData | null> {
  const camuUrl = getCamuConfig().apiUrl;
  if (!camuUrl) return null;

  try {
    const res = await fetch(`${camuUrl}/nfse/${key}`, { method: "GET", headers: camuHeaders() });
    if (!res.ok) return null;
    const envelope = (await res.json()) as CamuNfseNacionalEnvelope;
    if (!envelope.content?.NFSe) return null; // defensive: not an NFSe Nacional payload
    return {
      docKind: "nfseNacional",
      documentKey: key,
      ...(opts?.pdfUrlBase ? { pdfUrl: `${opts.pdfUrlBase}?key=${key}` } : {}),
      ...envelope.content,
    };
  } catch {
    return null;
  }
}

/**
 * NFSe Nacional only, keyed by the 50-digit numeric chave de acesso.
 * Returns null on any failure (network, 4xx/5xx — a 404 here means "CAMU
 * does not hold this document" — config gap) — callers treat null as
 * "not CAMU-resolvable", never throw.
 */
export async function fetchNfseNacionalByKey(
  key: string,
  opts?: FetchNfseNacionalOptions,
): Promise<CamuNfseNacionalRawData | null> {
  const cacheKey = `${key}|${opts?.pdfUrlBase ?? ""}`;
  const cached = nfseNacionalCache.get(cacheKey);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  const data = await fetchNfseNacionalByKeyUncached(key, opts);
  nfseNacionalCache.set(cacheKey, { at: Date.now(), data });
  return data;
}

// ---------------------------------------------------------------------------
// Browse arms (tenant companies + summary lists)
// ---------------------------------------------------------------------------

async function fetchTenantCompaniesUncached(): Promise<CamuTenantCompany[] | null> {
  const camuUrl = getCamuConfig().apiUrl;
  if (!camuUrl) return null;

  try {
    const res = await fetch(`${camuUrl}/companies`, { method: "GET", headers: camuHeaders() });
    if (!res.ok) return null;
    // OpenAPI-verified envelope: `{ companies: CamuTenantCompany[] }` — a bare
    // cast would feed the envelope itself to callers at runtime.
    const body = (await res.json()) as { companies?: CamuTenantCompany[] };
    return Array.isArray(body.companies) ? body.companies : null;
  } catch {
    return null;
  }
}

/**
 * All tenant companies belonging to the CAMU tenant. Null on any failure
 * (network, 4xx/5xx, unset CAMU_API_URL) — callers treat null as
 * "tenant unknown", never throw.
 */
export async function fetchTenantCompanies(): Promise<CamuTenantCompany[] | null> {
  const cached = tenantCompaniesCache.get("all");
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  const data = await fetchTenantCompaniesUncached();
  tenantCompaniesCache.set("all", { at: Date.now(), data });
  return data;
}

function listQuery(params: CamuNfeListParams | CamuNfseListParams): URLSearchParams {
  const query = new URLSearchParams();
  query.set("limit", String(params.limit ?? 100));
  query.set("offset", String(params.offset ?? 0));
  if (params.document_status) query.set("document_status", params.document_status);
  if (params.manifestation_status) query.set("manifestation_status", params.manifestation_status);
  if (params.issue_date_start) query.set("issue_date_start", params.issue_date_start);
  if (params.issue_date_end) query.set("issue_date_end", params.issue_date_end);
  if (params.sort) query.set("sort", params.sort);
  return query;
}

function listCacheKey(params: CamuNfeListParams | CamuNfseListParams): string {
  return [
    params.limit ?? 100,
    params.offset ?? 0,
    params.document_status ?? "",
    params.manifestation_status ?? "",
    params.issue_date_start ?? "",
    params.issue_date_end ?? "",
    params.sort ?? "issue_date:desc",
  ].join("|");
}

async function fetchNfeListPageUncached(
  params: CamuNfeListParams,
): Promise<CamuDfeListPage | null> {
  const camuUrl = getCamuConfig().apiUrl;
  if (!camuUrl) return null;

  try {
    const res = await fetch(`${camuUrl}/nfe?${listQuery(params).toString()}`, {
      method: "GET",
      headers: camuHeaders(),
    });
    if (!res.ok) return null;
    return (await res.json()) as CamuDfeListPage;
  } catch {
    return null;
  }
}

/**
 * One upstream page of CAMU NF-e summaries.
 * Null on any failure — callers treat a null first page as a 500 and null
 * refill pages as graceful exhaustion, never throw.
 */
export async function fetchNfeListPage(params: CamuNfeListParams): Promise<CamuDfeListPage | null> {
  const key = listCacheKey(params);
  const cached = nfeListPageCache.get(key);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  const data = await fetchNfeListPageUncached(params);
  nfeListPageCache.set(key, { at: Date.now(), data });
  return data;
}

async function fetchNfseListPageUncached(
  params: CamuNfseListParams,
): Promise<CamuDfeListPage | null> {
  const camuUrl = getCamuConfig().apiUrl;
  if (!camuUrl) return null;

  // Identical param serialization to fetchNfeListPageUncached.
  try {
    const res = await fetch(`${camuUrl}/nfse?${listQuery(params).toString()}`, {
      method: "GET",
      headers: camuHeaders(),
    });
    if (!res.ok) return null;
    return (await res.json()) as CamuDfeListPage;
  } catch {
    return null;
  }
}

/**
 * One upstream page of CAMU NFS-e summaries — the sibling of `fetchNfeListPage`
 * with its OWN per-param-key cache Map (never shared with the nfe arm). Null
 * on any failure — callers treat a null first page as a 500 and null refill
 * pages as graceful exhaustion, never throw.
 */
export async function fetchNfseListPage(
  params: CamuNfseListParams,
): Promise<CamuDfeListPage | null> {
  const key = listCacheKey(params);
  const cached = nfseListPageCache.get(key);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.data;

  const data = await fetchNfseListPageUncached(params);
  nfseListPageCache.set(key, { at: Date.now(), data });
  return data;
}
