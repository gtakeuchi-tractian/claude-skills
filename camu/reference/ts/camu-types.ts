/**
 * CAMU API wire types, trimmed to what camu-server.ts consumes.
 * Cloned from the mint repo (src/types/camu.ts, src/types/billing.ts,
 * src/types/billing-nfse.ts) with mint-only fields (NetSuite annotations,
 * response envelopes) removed.
 */

/** Tenant company row from CAMU `GET /companies` ({id, name, taxId}). */
export interface CamuTenantCompany {
  id: string;
  name: string;
  taxId: string; // 14-digit CNPJ, no formatting
}

/**
 * CAMU list item (`DfeSummary`), live-verified 2026-09-11. `content` is the
 * parsed-XML `nfeProc.NFe.infNFe.*` tree (may be null on documents not yet
 * approved); `xmlContent` is opt-in upstream and is never requested here.
 */
export interface CamuDfeSummary {
  documentType: string;
  documentKey: string;
  documentNumber: string | null;
  documentSeries: string | null;
  emissionDate: string;
  manifestationStatus: string | null;
  documentStatus: string;
  erpDocumentStatus: string | null;
  content: unknown | null;
}

/** Live `GET /nfe` (and `/nfse`) envelope slice; nextOffset absent on last page. */
export interface CamuDfeListPage {
  documents: CamuDfeSummary[];
  nextOffset?: number;
}

export interface CamuNfeListParams {
  limit?: number;
  offset?: number;
  document_status?: string;
  manifestation_status?: string;
  issue_date_start?: string;
  issue_date_end?: string;
  sort?: "issue_date:asc" | "issue_date:desc" | "captured_at:asc" | "captured_at:desc";
}

/**
 * Query params of `GET /nfse` — a distinct sibling of `CamuNfeListParams`
 * with identical field types/optionality (the origin contract forbids merging
 * the siblings' data models).
 */
export interface CamuNfseListParams {
  limit?: number;
  offset?: number;
  document_status?: string;
  manifestation_status?: string;
  issue_date_start?: string;
  issue_date_end?: string;
  sort?: "issue_date:asc" | "issue_date:desc" | "captured_at:asc" | "captured_at:desc";
}

// ---------------------------------------------------------------------------
// NF-e raw document shape (GET /nfe/{key})
// ---------------------------------------------------------------------------

export interface NfeProduct {
  NCM?: string;
  CFOP?: string | number;
  /** NF line order number vendors key from the buyer's PO. */
  xPed?: string | number;
  qCom?: string | number;
  vUnCom?: string | number;
  /** Product description. */
  xProd?: string;
  /** Pre-calculated NF-e item amount. */
  vProd?: string | number;
  [key: string]: unknown;
}

export interface NfeDetLine {
  prod?: NfeProduct;
  [key: string]: unknown;
}

export interface NfeIcmsTot {
  vFrete?: string | number;
  vSeg?: string | number;
  vOutro?: string | number;
  vIPI?: string | number;
  vST?: string | number;
  vDesc?: string | number;
  vProd?: string | number;
  vNF?: string | number;
  [key: string]: unknown;
}

export interface NfeInfNFe {
  emit: {
    CNPJ?: string | number;
    /** Emitter address block — `UF` feeds UF/CFOP pipelines. */
    enderEmit?: { UF?: string | number; [key: string]: unknown };
    [key: string]: unknown;
  };
  dest?: { CNPJ?: string | number; [key: string]: unknown };
  ide: { serie?: string | number; dhEmi?: string; [key: string]: unknown };
  det: NfeDetLine | NfeDetLine[];
  total?: { ICMSTot?: NfeIcmsTot; [key: string]: unknown };
  [key: string]: unknown;
}

export interface CamuNfeRawData {
  documentKey: string | number; // 44-digit NF-e access key
  documentNumber: string | number;
  emissionDate?: string;
  content: {
    nfeProc: {
      NFe: {
        infNFe: NfeInfNFe;
      };
    };
  };
}

// ---------------------------------------------------------------------------
// NFSe Nacional raw document shape (GET /nfse/{key} — content envelope)
// ---------------------------------------------------------------------------

/**
 * Live-verified shape of CAMU `GET /nfse/{documentKey}`'s `content` payload.
 * This is the raw DPS/NFSe Nacional layout as returned by CAMU — it is NOT
 * normalized into ABRASF. `documentKey` is a 50-digit numeric chave de acesso
 * (live-verified `^[0-9]{50}$`), the single lookup key exposed by CAMU's NFSe
 * endpoints.
 *
 * Example content tree (live-verified 2026-09-09, values redacted):
 *
 *   content = {
 *     NFSe: {
 *       infNFSe: {
 *         cStat: "100";
 *         nNFSe: "725";
 *         valores: { vBC: "12000.00"; vLiq: "12000.00"; vServ: "12000.00"; ... };
 *         DPS: {
 *           infDPS: {
 *             dhEmi: "2025-06-11T15:30:03-03:00";
 *             nDPS: "725";
 *             prest: { CNPJ, fone, email, regTrib };
 *             toma: { CNPJ, xNome, end, ... };
 *             serv: { cServ: { cNBS, cTribNac: "140101", xDescServ }, ... };
 *             valores?: { vServPrest: { vServ } };  // layout 1.01 nests gross here
 *           };
 *         };
 *       };
 *     };
 *   }
 */
export interface CamuNfseNacionalRawData {
  docKind: "nfseNacional";
  /**
   * Optional View-PDF link. The origin repo injects a server-side PDF proxy
   * URL here; this clone injects it only when the caller passes
   * `opts.pdfUrlBase` to `fetchNfseNacionalByKey`.
   */
  pdfUrl?: string;
  /**
   * The 50-digit numeric chave de acesso CAMU addressed this document by.
   * Extractors can derive prestador CNPJ (digits 8-21 of the key) and
   * prestador UF (digits 1-2) without depending on the (sometimes absent)
   * `prest.CNPJ` body field.
   */
  documentKey: string;
  /** Live `content.NFSe.infNFSe` node. Only typed fields are used downstream. */
  NFSe: {
    infNFSe: {
      /** Document number — consistently present (envelope `documentNumber` is null on NFSe). */
      nNFSe: string;
      /** Document series — not consistently carried; skipped when absent. */
      serieNFSe?: string;
      DPS: {
        infDPS: {
          /** DPS emission datetime — present on all sampled documents (primary date source; dhProc is fallback only). */
          dhEmi?: string;
          /** DPS number, e.g. "725" — semantically equivalent to ABRASF `NumeroRPS`. */
          nDPS?: string;
          prest: {
            /** May be absent in edge cases — fallback: documentKey slice(7, 21). */
            CNPJ?: string;
            [key: string]: unknown;
          };
          toma?: {
            CNPJ?: string;
            xNome?: string;
            end?: unknown;
            [key: string]: unknown;
          };
          serv?: {
            cServ: {
              /**
               * 6-digit national taxation code (`Código de Tributação
               * Nacional`), e.g. "140101" = LC-116 14.01 + subitem 01. The
               * LC-116 service code is its FIRST 4 digits ("1401") —
               * `slice(-4)` is NOT the LC-116 code.
               */
              cTribNac: string;
              cNBS?: string;
              xDescServ?: string;
              [key: string]: unknown;
            };
            /** 7-digit IBGE code of the UF+município where the service took place. */
            locPrest?: { cLocPrestacao?: string };
            [key: string]: unknown;
          };
          /** DPS-level service values (string decimals, all optional). */
          valores?: {
            vBC?: string;
            vServ?: string;
            vLiq?: string;
            vServPrest?: { vServ?: string; vReceb?: string };
            [key: string]: unknown;
          };
          [key: string]: unknown;
        };
      };
      /** infNFSe-level service values (string decimals, all optional). vLiq/vReceb are never the gross value. */
      valores?: {
        vBC?: string;
        vServ?: string;
        vLiq?: string;
        vServPrest?: { vServ?: string; vReceb?: string };
        [key: string]: unknown;
      };
      /** Processing instant — fallback date source only (dhEmi > dhProc). */
      dhProc?: string;
      [key: string]: unknown;
    };
  };
}
