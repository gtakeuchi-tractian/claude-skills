"""Async Camu REST API client for NF-e / NFSe Nacional documents.

Reference clone of a working production integration (originally the
tractian-procurement backend's app/fiscal/camu_client.py), made repo-agnostic:
config reads CAMU_API_KEY / CAMU_API_URL from os.environ instead of a Settings
object, and structlog was replaced with stdlib logging. Deps: httpx only.

The origin repo also carried a fake-provider fixture mode
(app.fiscal.fake_provider, gated by CAMU_FAKE_PROVIDER_ENABLED) for offline
dev — removed here; this client always talks to the real CAMU API.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

CAMU_BASE_URL = "https://api.service.camu.com.br"

NFE_KEY_LEN = 44
NFSE_NACIONAL_KEY_LEN = 50
_DOC_TTL_SECONDS = 300.0


class CamuError(Exception):
    """Base for typed Camu client failures (never let bare httpx errors reach routers)."""


class CamuNotConfiguredError(CamuError):
    """CAMU_API_KEY is empty."""


class CamuNotFoundError(CamuError):
    """Camu does not hold the requested document."""


class CamuUpstreamError(CamuError):
    """Camu unreachable or answered with an unusable response."""


class CamuStaleError(CamuError):
    """Camu failed while only an expired cached copy exists; carries its timestamp."""

    def __init__(self, cached_at: datetime) -> None:
        super().__init__("Camu unavailable; only stale cached data exists")
        self.cached_at = cached_at


def _api_key() -> str:
    key = os.environ.get("CAMU_API_KEY", "")
    if not key:
        raise CamuNotConfiguredError("Camu API not configured: CAMU_API_KEY is empty")
    return key


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key()}


def _base_url() -> str:
    return os.environ.get("CAMU_API_URL") or CAMU_BASE_URL


def _parse_doc(raw: dict[str, Any]) -> dict[str, Any]:
    content = raw.get("content") or {}
    nfe_proc = content.get("nfeProc") or {}
    nfe = nfe_proc.get("NFe") or {}
    inf_nfe = nfe.get("infNFe") or {}
    emit = inf_nfe.get("emit") or {}
    ide = inf_nfe.get("ide") or {}
    total = (inf_nfe.get("total") or {}).get("ICMSTot") or {}
    doc = {
        "document_key": raw.get("documentKey"),
        "document_number": raw.get("documentNumber"),
        "document_type": raw.get("documentType"),
        "emission_date": raw.get("emissionDate"),
        "issuer_cnpj": emit.get("CNPJ"),
        "issuer_name": emit.get("xNome"),
        "nature_operation": ide.get("natOp"),
        "total_nfe": total.get("vNF"),
    }
    return {k: v for k, v in doc.items() if v is not None}


_60_DAYS = timedelta(days=60)


async def fetch_nfes() -> list[dict[str, Any]]:
    """Fetch NF-e documents from Camu (last 60 rolling calendar days).

    Walks BOTH upstream pagination styles: envelope `nextUrl` first, then
    envelope `nextOffset`, then bare list/offset arithmetic as a fallback.
    """
    api_key = _api_key()

    cutoff = (datetime.now(UTC) - _60_DAYS).date()
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    results: list[dict[str, Any]] = []
    limit = 100
    offset = 0
    next_url: str | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            if next_url:
                resp = await client.get(next_url, headers=headers)
            else:
                resp = await client.get(
                    f"{_base_url()}/nfe",
                    headers=headers,
                    params={"limit": limit, "offset": offset},
                )
            resp.raise_for_status()
            body = resp.json()

            page: list[dict[str, Any]] = (
                body if isinstance(body, list) else body.get("documents", body.get("items", []))
            )

            if not page:
                break

            for raw in page:
                parsed = _parse_doc(raw)
                try:
                    doc_date = datetime.fromisoformat(str(parsed["emission_date"])).date()
                except (ValueError, KeyError):
                    continue
                if doc_date >= cutoff:
                    results.append(parsed)

            if isinstance(body, dict):
                next_url = body.get("nextUrl")
                if next_url:
                    continue
                next_offset = body.get("nextOffset")
                if next_offset is not None:
                    offset = int(next_offset)
                    continue

            if len(page) < limit:
                break
            offset += limit

    return results


async def _get(path: str) -> httpx.Response:
    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{_base_url()}{path}", headers=headers)
    except httpx.HTTPError as exc:
        raise CamuUpstreamError(f"Camu request failed: {exc.__class__.__name__}") from exc
    if resp.status_code == 404:
        raise CamuNotFoundError(path)
    if resp.status_code >= 400:
        raise CamuUpstreamError(f"Camu answered {resp.status_code}")
    return resp


def _as_list(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [n for n in node if isinstance(n, dict)]
    return [node] if isinstance(node, dict) else []


def _ncm_dotted(ncm: str) -> str:
    digits = "".join(ch for ch in ncm if ch.isdigit())
    return f"{digits[:4]}.{digits[4:6]}.{digits[6:8]}" if len(digits) == 8 else ncm


def parse_nfe_document(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a Camu `GET /nfe/{key}` envelope into the fields fiscal review reads."""
    inf = (((raw.get("content") or {}).get("nfeProc") or {}).get("NFe") or {}).get("infNFe") or {}
    emit = inf.get("emit") or {}
    ide = inf.get("ide") or {}
    icms = (inf.get("total") or {}).get("ICMSTot") or {}
    lines = []
    for det in _as_list(inf.get("det")):
        prod = det.get("prod") or {}
        ncm = prod.get("NCM")
        lines.append(
            {
                "description": prod.get("xProd"),
                "ncm": _ncm_dotted(str(ncm)) if ncm is not None else None,
                "quantity": prod.get("qCom"),
                "unit_value": prod.get("vUnCom"),
                "amount": prod.get("vProd"),
                "x_ped": None if prod.get("xPed") is None else str(prod["xPed"]),
            }
        )
    inf_adic = (inf.get("infAdic") or {}).get("infCpl")
    return {
        "kind": "nfe",
        "document_key": str(raw.get("documentKey") or ""),
        "document_number": None
        if raw.get("documentNumber") is None
        else str(raw["documentNumber"]),
        "emission_date": raw.get("emissionDate") or ide.get("dhEmi"),
        "series": None if ide.get("serie") is None else str(ide["serie"]),
        "issuer_cnpj": None if emit.get("CNPJ") is None else str(emit["CNPJ"]),
        "issuer_name": emit.get("xNome"),
        "issuer_uf": (emit.get("enderEmit") or {}).get("UF"),
        "recipient_cnpj": None
        if (inf.get("dest") or {}).get("CNPJ") is None
        else str(inf["dest"]["CNPJ"]),
        "ncms": sorted({ln["ncm"] for ln in lines if ln["ncm"]}),
        "lines": lines,
        "icms_tot": icms,
        "total_nfe": icms.get("vNF"),
        "x_ped": sorted({ln["x_ped"] for ln in lines if ln["x_ped"]}),
        "inf_adic": inf_adic,
        "xml": raw.get("xmlContent"),
    }


def issuer_uf_from_raw_payload(raw: dict[str, Any]) -> str | None:
    """Emitter UF read straight from the retained raw CAMU envelope.

    Callers typically persist the raw envelope, not this module's projection,
    so the UF read re-walks the same
    ``content.nfeProc.NFe.infNFe.emit.enderEmit.UF`` path ``parse_nfe_document``
    projects. Pure: no I/O, never writes the snapshot.
    """
    inf = (((raw.get("content") or {}).get("nfeProc") or {}).get("NFe") or {}).get("infNFe") or {}
    uf = ((inf.get("emit") or {}).get("enderEmit") or {}).get("UF")
    if isinstance(uf, str) and uf.strip():
        return uf.strip()
    return None


def icms_tot_from_raw_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """The NF-e ICMSTot block read straight from the retained raw CAMU envelope.

    ``{}`` when absent: an ICMSTot-less payload is an honest empty, never a
    fabricated total. Pure: no I/O, never writes the snapshot.
    """
    inf = (((raw.get("content") or {}).get("nfeProc") or {}).get("NFe") or {}).get("infNFe") or {}
    icms_tot = (inf.get("total") or {}).get("ICMSTot") or {}
    return icms_tot if isinstance(icms_tot, dict) else {}


def nfe_item_vprod_values_from_raw_payload(raw: dict[str, Any]) -> list[Any]:
    """The ordered NF-e ``det[].prod.vProd`` values read straight from the
    retained raw CAMU envelope.

    In document (XML) order, one value per det line. A single-det payload is
    normalized to a one-element list (the module's own ``_as_list`` walk); a
    det line without a ``prod.vProd`` keeps its POSITION as ``None`` so the
    caller's alignment issues stay one-based. Pure: no I/O, never writes the
    snapshot.
    """
    inf = (((raw.get("content") or {}).get("nfeProc") or {}).get("NFe") or {}).get("infNFe") or {}
    values: list[Any] = []
    for det in _as_list(inf.get("det")):
        prod = det.get("prod")
        values.append(prod.get("vProd") if isinstance(prod, dict) else None)
    return values


def parse_nfse_nacional_document(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Project a Camu `GET /nfse/{key}` envelope (DPS/infNFSe shape)."""
    inf = (((raw.get("content") or {}).get("NFSe") or {}).get("infNFSe")) or {}
    dps = (inf.get("DPS") or {}).get("infDPS") or {}
    prest = dps.get("prest") or {}
    toma = dps.get("toma") or {}
    serv = (dps.get("serv") or {}).get("cServ") or {}
    valores = inf.get("valores") or {}
    cnpj = prest.get("CNPJ") or key[6:20]
    return {
        "kind": "nfse_nacional",
        "document_key": key,
        "document_number": None if inf.get("nNFSe") is None else str(inf["nNFSe"]),
        "emission_date": dps.get("dhEmi"),
        "series": None if inf.get("serieNFSe") is None else str(inf["serieNFSe"]),
        "issuer_cnpj": None if cnpj is None else str(cnpj),
        "issuer_name": prest.get("xNome"),
        "recipient_cnpj": None if toma.get("CNPJ") is None else str(toma["CNPJ"]),
        "recipient_name": toma.get("xNome"),
        "service_code": serv.get("cTribNac"),
        "service_description": serv.get("xDescServ"),
        "net_value": valores.get("vLiq"),
        "xml": raw.get("xmlContent"),
    }


_doc_cache: dict[str, tuple[float, datetime, dict[str, Any]]] = {}
_raw_doc_cache: dict[str, dict[str, Any]] = {}


def clear_document_cache() -> None:
    _doc_cache.clear()
    _raw_doc_cache.clear()


async def _cached_document(cache_key: str, path: str, parse: Any) -> dict[str, Any]:
    now = time.monotonic()
    hit = _doc_cache.get(cache_key)
    if hit and now - hit[0] < _DOC_TTL_SECONDS:
        return hit[2]
    try:
        resp = await _get(path)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CamuUpstreamError("Camu returned non-JSON body") from exc
        if not isinstance(body, dict):
            raise CamuUpstreamError("Camu returned unexpected payload")
        doc = parse(body)
        if cache_key.startswith("nfe:"):
            _raw_doc_cache[cache_key] = body
    except (CamuUpstreamError, CamuNotConfiguredError):
        if hit:
            raise CamuStaleError(hit[1]) from None
        raise
    _doc_cache[cache_key] = (now, datetime.now(UTC), doc)
    return doc


async def fetch_nfe_by_key(key: str) -> dict[str, Any]:
    """Fetch and parse one NF-e by its 44-digit access key."""
    return await _cached_document(f"nfe:{key}", f"/nfe/{key}", parse_nfe_document)


def raw_nfe_payload_by_key(key: str) -> dict[str, Any] | None:
    """Return the source CAMU envelope paired with the current cached NF-e."""
    return _raw_doc_cache.get(f"nfe:{key}")


async def fetch_nfse_nacional_by_key(key: str) -> dict[str, Any]:
    """Fetch and parse one NFSe Nacional by its 50-digit access key."""

    def _parse(raw: dict[str, Any]) -> dict[str, Any]:
        if not ((raw.get("content") or {}).get("NFSe")):
            raise CamuUpstreamError("Camu returned a non-NFSe Nacional payload")
        return parse_nfse_nacional_document(raw, key)

    return await _cached_document(f"nfse:{key}", f"/nfse/{key}", _parse)


async def fetch_pdf_by_key(key: str) -> bytes:
    """Fetch the DANFE (44 digits) or DANFSE (50 digits) PDF bytes."""
    if len(key) == NFE_KEY_LEN:
        path = f"/nfe/{key}/pdf"
    elif len(key) == NFSE_NACIONAL_KEY_LEN:
        path = f"/nfse/{key}/pdf"
    else:
        raise ValueError("key must be 44 (NF-e) or 50 (NFSe Nacional) digits")
    return (await _get(path)).content
