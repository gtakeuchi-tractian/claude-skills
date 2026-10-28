"""Async SuiteQL client with OAuth 1.0 HMAC-SHA256 authentication.

Reference clone from a working production integration (originally the
tractian-procurement "deck" backend, ``app/netsuite/suiteql_client.py``).
The repo's pydantic-settings seam (``app.core.config.get_settings``) was
replaced with lazy ``os.environ`` reads — importing this module never fails;
a call without the required env vars raises RuntimeError naming what's missing.

Dependency: httpx.

Env vars (NETSUITE_* naming convention):
  NETSUITE_ACCOUNT_ID        account id (sandbox form ends _SB<digits>)
  NETSUITE_CONSUMER_KEY      OAuth integration consumer key
  NETSUITE_CONSUMER_SECRET   OAuth integration consumer secret
  NETSUITE_ACCESS_TOKEN      TBA token id
  NETSUITE_TOKEN_SECRET      TBA token secret
  NETSUITE_REALM             optional; defaults to NETSUITE_ACCOUNT_ID
  NETSUITE_URL               optional host suffix, default "suitetalk.api.netsuite.com"
  NETSUITE_MAX_CONCURRENCY   optional process-wide in-flight cap, default 4
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import random
import re
import time
import urllib.parse
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

_90_DAYS = timedelta(days=90)
_PAGE_SIZE = 1000
SUITE_QL_PATH = "services/rest/query/v1/suiteql"
RECORD_API_PATH = "services/rest/record/v1"

# Retry policy for NetSuite's account-level concurrency governance. The budget
# is shared with external consumers whose load we cannot see, so even our
# serialized request can receive 429. NetSuite also intermittently returns
# ``401 INVALID_LOGIN`` to requests with credentials that worked seconds
# earlier during a bursting cache warm. Capped exponential backoff absorbs
# both specific transient responses; any other 401 remains actionable.
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_BASE_S = 0.5

# Process-wide in-flight cap for ALL NetSuite HTTP requests; created lazily per
# event loop (asyncio primitives are loop-bound, pytest spawns one per test).
_suiteql_gate: asyncio.Semaphore | None = None
_suiteql_gate_loop: asyncio.AbstractEventLoop | None = None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def _account_id() -> str:
    account_id = _env("NETSUITE_ACCOUNT_ID")
    if not account_id:
        raise RuntimeError("NetSuite not configured: NETSUITE_ACCOUNT_ID is empty")
    return account_id


def _host_suffix() -> str:
    return _env("NETSUITE_URL", "suitetalk.api.netsuite.com") or "suitetalk.api.netsuite.com"


def _oauth_credentials() -> dict[str, str]:
    names = (
        "NETSUITE_CONSUMER_KEY",
        "NETSUITE_CONSUMER_SECRET",
        "NETSUITE_ACCESS_TOKEN",
        "NETSUITE_TOKEN_SECRET",
    )
    missing = [name for name in names if not _env(name)]
    if missing:
        raise RuntimeError(f"NetSuite not configured: missing {', '.join(missing)}")
    return {
        "consumer_key": _env("NETSUITE_CONSUMER_KEY") or "",
        "consumer_secret": _env("NETSUITE_CONSUMER_SECRET") or "",
        "access_token": _env("NETSUITE_ACCESS_TOKEN") or "",
        "token_secret": _env("NETSUITE_TOKEN_SECRET") or "",
        "realm": _env("NETSUITE_REALM") or _account_id(),
    }


def _get_netsuite_gate() -> asyncio.Semaphore:
    global _suiteql_gate, _suiteql_gate_loop
    loop = asyncio.get_running_loop()
    if _suiteql_gate is None or _suiteql_gate_loop is not loop:
        max_concurrency = int(_env("NETSUITE_MAX_CONCURRENCY", "4") or "4")
        _suiteql_gate = asyncio.Semaphore(max(1, max_concurrency))
        _suiteql_gate_loop = loop
    return _suiteql_gate


def _percent_encode(value: str) -> str:
    return (
        urllib.parse.quote(str(value), safe="")
        .replace("!", "%21")
        .replace("*", "%2A")
        .replace("'", "%27")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _build_auth_header(method: str, url: str, query_params: dict[str, str]) -> str:
    creds = _oauth_credentials()
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))

    oauth_params = {
        "oauth_consumer_key": creds["consumer_key"],
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": timestamp,
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }

    all_params = {**oauth_params, **{k: str(v) for k, v in query_params.items()}}
    param_string = "&".join(
        f"{_percent_encode(k)}={_percent_encode(all_params[k])}" for k in sorted(all_params.keys())
    )
    base_string = "&".join(
        [
            method.upper(),
            _percent_encode(url),
            _percent_encode(param_string),
        ]
    )
    signing_key = (
        f"{_percent_encode(creds['consumer_secret'])}"
        f"&{_percent_encode(creds['token_secret'])}"
    )
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()
    ).decode()

    header_entries = [f'realm="{creds["realm"]}"']
    for key, value in oauth_params.items():
        header_entries.append(f'{_percent_encode(key)}="{_percent_encode(value)}"')
    header_entries.append(f'oauth_signature="{_percent_encode(signature)}"')

    return f"OAuth {', '.join(header_entries)}"


async def _post_with_backoff(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Globally concurrency-capped POST with transient 429/INVALID_LOGIN
    absorb. Honor ``Retry-After`` when present, else exponential backoff with
    jitter. Gate is released between retries so queued callers progress."""
    resp: httpx.Response | None = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        async with _get_netsuite_gate():
            resp = await client.post(url, **kwargs)
        is_retryable = resp.status_code == 429 or (
            resp.status_code == 401 and "INVALID_LOGIN" in resp.text
        )
        if not is_retryable or attempt == _RETRY_MAX_ATTEMPTS - 1:
            return resp
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = _RETRY_BACKOFF_BASE_S * 2**attempt
        else:
            delay = _RETRY_BACKOFF_BASE_S * 2**attempt
        await asyncio.sleep(delay + random.uniform(0, 0.25))
    assert resp is not None  # loop always returns or sets resp
    return resp


def _get_next_url(links: list[dict]) -> str | None:
    for link in links:
        if link.get("rel") == "next":
            return link.get("href")
    return None


def _parse_url(full_url: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlparse(full_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    return base, params


async def run_suiteql(query: str) -> list[dict[str, Any]]:
    """Execute a SuiteQL query and return all rows (paginated)."""
    base_url = f"https://{_account_id()}.{_host_suffix()}/{SUITE_QL_PATH}"
    all_items: list[dict[str, Any]] = []
    next_url: str | None = None
    is_first = True

    async with httpx.AsyncClient(timeout=60.0) as client:
        while is_first or next_url:
            is_first = False

            if next_url:
                sign_url, q_params = _parse_url(next_url)
            else:
                sign_url = base_url
                q_params = {"limit": str(_PAGE_SIZE)}

            auth = _build_auth_header("POST", sign_url, q_params)
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth,
                "Prefer": "transient",
            }

            resp = await _post_with_backoff(
                client,
                sign_url,
                headers=headers,
                json={"q": query},
                params=q_params or None,
            )
            if not resp.is_success:
                raise RuntimeError(f"SuiteQL {resp.status_code}: {resp.text[:500]}")
            data = resp.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                item.pop("links", None)
            all_items.extend(items)

            next_url = _get_next_url(data.get("links", []))

    return all_items


async def fetch_ns_record_list(record_type: str) -> list[dict[str, Any]]:
    """Fetch all records of a NS custom type via GET /record/v1/{type} (paginated)."""
    base_url = (
        f"https://{_account_id()}.{_host_suffix()}/{RECORD_API_PATH}/{record_type}"
    )
    all_items: list[dict[str, Any]] = []
    offset = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            q_params = {"limit": str(_PAGE_SIZE), "offset": str(offset)}
            auth = _build_auth_header("GET", base_url, q_params)
            headers = {"Authorization": auth, "Content-Type": "application/json"}

            async with _get_netsuite_gate():
                resp = await client.get(base_url, headers=headers, params=q_params)
            if not resp.is_success:
                raise RuntimeError(
                    f"NS Record API {record_type} {resp.status_code}: {resp.text[:500]}"
                )

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                item.pop("links", None)
            all_items.extend(items)

            if len(items) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    return all_items


async def fetch_ns_record(
    record_type: str, record_id: str, *, expand_sub_resources: bool = False
) -> dict[str, Any]:
    """Fetch a single NS record by id via ``GET /record/v1/{type}/{id}``.

    The Record API correctly resolves subrecord fields (e.g. a vendor's
    default billing address country) where SuiteQL's dot-notation on
    ``addressBookAddress.<field>`` does not (confirmed broken on the source
    account — always echoes the address's internal id).
    """
    base_url = (
        f"https://{_account_id()}.{_host_suffix()}"
        f"/{RECORD_API_PATH}/{record_type}/{record_id}"
    )
    params = {"expandSubResources": "true"} if expand_sub_resources else {}
    auth = _build_auth_header("GET", base_url, params)
    headers = {"Authorization": auth, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        async with _get_netsuite_gate():
            resp = await client.get(base_url, headers=headers, params=params or None)
        if not resp.is_success:
            raise RuntimeError(
                f"NS Record API {record_type}/{record_id} {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()


async def fetch_inbound_delivery() -> list[dict[str, Any]]:
    """Fetch inbound delivery records from Netsuite (last 90 days) via SuiteQL."""
    cutoff = (datetime.now(UTC) - _90_DAYS).strftime("%Y-%m-%d")
    query = f"""
        SELECT
            t.id AS transaction_id,
            t.tranid AS document_number,
            TO_CHAR(t.trandate, 'YYYY-MM-DD') AS transaction_date,
            BUILTIN.DF(t.status) AS transaction_status,
            v.id AS vendor_id,
            t.custbody_brl_tran_t_access_key AS access_key,
            BUILTIN.DF(LOWER(t.custbody_brl_tran_t_natureza)) AS transaction_nature
        FROM transaction t
        LEFT JOIN vendor v ON t.entity = v.id
        WHERE t.type = 'CustPrc'
          AND TO_CHAR(t.trandate, 'YYYY-MM-DD') >= '{cutoff}'
        ORDER BY t.id ASC
    """
    return await run_suiteql(query)


async def fetch_vendor_by_cnpj(cnpj: str) -> list[dict[str, Any]]:
    """Look up an active NetSuite vendor by federal tax registration (CNPJ).

    Quirk (live-verified 400 NOT_EXPOSED): ``vendor.defaultaddress`` is
    not exposed to SuiteQL, so the default billing address block is sourced
    from the default-billing addressbook entry instead. This join selects the
    plain text column ``vendorAddressbookEntityAddress.addrtext`` — distinct
    from the subrecord dot-notation on ``addressBookAddress.<field>`` that is
    separately confirmed broken on the source account (it always echoes the
    address's internal id). LEFT JOINs keep vendors without a default billing
    entry (address comes back absent, by design).
    """
    # CNPJ is interpolated into the WHERE clause (SuiteQL has no bind
    # parameters on this path); embedded quotes are escaped by doubling.
    safe_cnpj = cnpj.strip().replace("'", "''")
    query = f"""
        SELECT
            v.id AS vendor_id,
            v.entityid AS entity_id,
            v.companyname AS vendor_name,
            ea.addrtext AS default_billing_address
        FROM vendor v
        LEFT JOIN vendorAddressbook va
            ON va.entity = v.id AND va.defaultbilling = 'T'
        LEFT JOIN vendorAddressbookEntityAddress ea
            ON ea.nkey = va.addressbookaddress
        WHERE v.isinactive = 'F'
          AND custentity_brl_entity_t_fed_tax_reg = '{safe_cnpj}'
        ORDER BY v.entityid ASC
    """
    return await run_suiteql(query)


async def fetch_vendor_default_billing_address(ns_vendor_id: str) -> list[dict[str, Any]]:
    """Fetch a NetSuite vendor's default-billing address text.

    The join shape is copied from ``fetch_vendor_by_cnpj`` (the live-verified
    400 NOT_EXPOSED quirk on ``vendor.defaultaddress`` applies account-wide);
    the address text is a SELECTED column — never an interpolated input.

    LEFT JOINs keep vendors without a default-billing entry (the address comes
    back absent, by design). No OFFSET/FETCH: SuiteQL silently ignores them;
    pagination is run_suiteql's cursor links instead.
    """
    # Internal id is interpolated into the WHERE clause — hard-guard to
    # digits-only, never accept caller-supplied SQL there.
    vendor_id = str(ns_vendor_id).strip()
    if not vendor_id.isdigit():
        raise ValueError(
            "fetch_vendor_default_billing_address expects a numeric NetSuite"
            f" internal id, got: {ns_vendor_id!r}"
        )
    query = f"""
        SELECT
            ea.addrtext AS default_billing_address
        FROM vendor v
        LEFT JOIN vendorAddressbook va
            ON va.entity = v.id AND va.defaultbilling = 'T'
        LEFT JOIN vendorAddressbookEntityAddress ea
            ON ea.nkey = va.addressbookaddress
        WHERE v.id = {vendor_id}
    """
    return await run_suiteql(query)


async def fetch_cfop_record_by_id(record_id: str) -> dict[str, Any] | None:
    """Fetch one NetSuite CFOP custom record by internal id.

    The internal id is interpolated into the WHERE clause — hard-guarded to
    digits-only BEFORE interpolation: a hostile id raises, never interpolates.
    Returns the record row (``id`` + ``name``) or ``None`` when the id matches
    nothing. No OFFSET/FETCH: SuiteQL silently ignores them.
    """
    safe_id = str(record_id).strip()
    if not safe_id.isdigit():
        raise ValueError(
            f"fetch_cfop_record_by_id expects a numeric NetSuite internal id, got: {record_id!r}"
        )
    query = f"""
        SELECT
            id,
            name
        FROM CUSTOMRECORD_FTEBR_CFOP
        WHERE id = {safe_id}
    """
    rows = await run_suiteql(query)
    return rows[0] if rows else None


async def fetch_cfop_records() -> list[dict[str, Any]]:
    """Fetch the full NetSuite CFOP custom-record set."""
    query = """
        SELECT
            id,
            name
        FROM CUSTOMRECORD_FTEBR_CFOP
        ORDER BY id
    """
    return await run_suiteql(query)


async def fetch_item_expense_accounts(ns_item_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Fetch the expense account of each given NetSuite item.

    Each item carries its own expense account, named/numbered by the account
    join (``item i LEFT JOIN account a ON a.id = i.expenseaccount``).

    BATCHED: one SuiteQL call regardless of list size — ids are interpolated
    as one ``IN`` list, each hard-guarded to digits-only before interpolation.

    A selected id that matches no item row simply returns no row; a row whose
    ``expenseaccount`` is null ships ``expense_account_id: null``.
    """
    safe_ids: list[str] = []
    for ns_item_id in ns_item_ids:
        # Internal id is interpolated into the WHERE clause — hard-guard to
        # digits-only, never accept caller-supplied SQL there.
        item_id = str(ns_item_id).strip()
        if not item_id.isdigit():
            raise ValueError(
                "fetch_item_expense_accounts expects numeric NetSuite internal"
                f" ids, got: {ns_item_id!r}"
            )
        safe_ids.append(item_id)
    if not safe_ids:
        # No selected item: nothing to fetch, no query to run (an empty IN list
        # is not valid SQL).
        return []

    id_list = ", ".join(safe_ids)
    query = f"""
        SELECT
            i.id AS ns_item_id,
            i.expenseaccount AS expense_account_id,
            a.acctname AS expense_account_name,
            a.acctnumber AS expense_account_number
        FROM item i
        LEFT JOIN account a
            ON a.id = i.expenseaccount
        WHERE i.id IN ({id_list})
        ORDER BY i.id
    """
    return await run_suiteql(query)


async def fetch_expense_accounts() -> list[dict[str, Any]]:
    """Fetch the full NetSuite expense-account catalog.

    NO account-type filter and NO inactive filter: a discount account may be
    accountType "Other Income" and an ``accttype = 'Exp'`` filter would
    wrongly exclude it from the picking surface.
    """
    query = """
        SELECT
            id,
            acctname,
            acctnumber
        FROM account
        ORDER BY acctnumber
    """
    return await run_suiteql(query)


async def fetch_vendor_po_lines(ns_vendor_id: str) -> list[dict[str, Any]]:
    """Fetch a NetSuite vendor's open purchase-order lines.

    ``po_line_id`` (``tl.id``) is the line's NATIVE transaction line id — the
    value NetSuite validates ``orderLine`` against. It MUST never be replaced
    by ``linesequencenumber`` (production incident, PO 861761: native 14 at
    sequence 1); the sequence number is therefore not projected at all and
    appears only in ``ORDER BY`` for deterministic oldest-first consumption.

    Quirk (isolated probes 2026-09-11): ``transactionLine.description``
    hard-fails ANY select on the source account with ``UNEXPECTED_ERROR`` —
    the line Description text lives in ``tl.memo`` instead, aliased here.

    No OFFSET/FETCH: SuiteQL silently ignores them; pagination is
    run_suiteql's cursor links instead.
    """
    # Internal id is interpolated into the WHERE clause — hard-guard to
    # digits-only, never accept caller-supplied SQL there.
    vendor_id = str(ns_vendor_id).strip()
    if not vendor_id.isdigit():
        raise ValueError(
            f"fetch_vendor_po_lines expects a numeric NetSuite internal id, got: {ns_vendor_id!r}"
        )
    query = f"""
        SELECT
            t.id AS transaction_id,
            t.tranid AS po_number,
            v.companyname AS vendor_name,
            tl.id AS po_line_id,
            tl.item AS item_internal_id,
            i.itemid AS item_sku,
            i.displayname AS item_display_name,
            tl.memo AS description,
            tl.quantity AS quantity,
            tl.quantityshiprecv AS quantity_shiprecv,
            tl.rate AS rate,
            tl.location AS location_id
        FROM transaction t
        INNER JOIN transactionLine tl ON t.id = tl.transaction
        INNER JOIN vendor v ON t.entity = v.id
        INNER JOIN item i ON tl.item = i.id
        WHERE t.type = 'PurchOrd'
          AND t.entity = {vendor_id}
          AND t.status NOT IN ('G', 'H')
        ORDER BY tl.linesequencenumber ASC
    """
    return await run_suiteql(query)


async def fetch_item_by_exact_code(code: str) -> list[dict[str, Any]]:
    """Look up a NetSuite item by its exact ``itemid``.

    Escaping: the code is trimmed and embedded single quotes are escaped by
    doubling before interpolation (SuiteQL has no bind parameters here), and
    the comparison ``UPPER()``s BOTH sides — NetSuite itemids are not
    guaranteed uppercase in the account, and a lowercase-stored catalog row
    must not read as "not recognized".
    """
    safe_code = code.strip().replace("'", "''")
    query = f"""
        SELECT
            i.id,
            i.itemid,
            i.purchasedescription
        FROM item i
        WHERE UPPER(i.itemid) = UPPER('{safe_code}')
    """
    rows = await run_suiteql(query)
    return [
        {
            "ns_item_id": str(row.get("id")),
            "item_code": row.get("itemid"),
            "description": row.get("purchasedescription"),
        }
        for row in rows
    ]


# Digits-only reduction for NCM values — the rule local catalogs match on.
_NCM_DIGITS = re.compile(r"\D")


def _safe_ncm_member(raw: str) -> str:
    """Reduce one NCM list member to its digits-only matching key, hard-guarded.

    Every member is first reduced to digits-only, then hard-guarded digits-only
    post-normalization, and finally quote-doubled before interpolation. A
    hostile or degenerate member therefore never reaches the query text.
    """
    normalized = _NCM_DIGITS.sub("", raw or "")
    if not normalized.isdigit():
        raise ValueError(f"fetch_items_by_ncm expects digits-normalized NCM codes, got: {raw!r}")
    return normalized.replace("'", "''")


async def fetch_items_by_ncm(ncm_codes: list[str]) -> list[dict[str, Any]]:
    """Look up NetSuite items by NCM through the custom item-code record.

    The item joined to ``CUSTOMRECORD_FTE_ITEMCODE`` on the item's NCM-code
    link, filtered by the NCM list, ordered by ``itemid``. Every member goes
    through ``_safe_ncm_member`` (digits-only normalization + hard guard +
    quote doubling) before interpolation.
    """
    safe_members = [_safe_ncm_member(code) for code in ncm_codes]
    if not safe_members:
        raise ValueError("fetch_items_by_ncm expects a non-empty NCM list")
    in_list = ", ".join(f"'{member}'" for member in safe_members)
    query = f"""
        SELECT
            i.id,
            i.itemid,
            i.purchasedescription AS description,
            ncmList.custrecord_fte_itemcode_t_code AS ncm_code
        FROM item i
        INNER JOIN CUSTOMRECORD_FTE_ITEMCODE ncmList
            ON ncmList.id = i.custitem_fte_item_l_itemcode
        WHERE ncmList.custrecord_fte_itemcode_t_code IN ({in_list})
        ORDER BY i.itemid
    """
    rows = await run_suiteql(query)
    return [
        {
            "ns_item_id": str(row.get("id")),
            "item_code": row.get("itemid"),
            "description": row.get("description"),
            "ncm_code": row.get("ncm_code"),
        }
        for row in rows
    ]


async def fetch_items_by_service_code(code: str) -> list[dict[str, Any]]:
    """Look up the NetSuite service item carrying a 4-digit NFS-e service code.

    Only items whose ``itemid`` is ``SERV-`` followed by the derived code
    match, via the 1-based ``SUBSTR(itemid,6,4)`` window. The code is trimmed
    and quote-doubled before interpolation. An empty code returns [] without
    querying (a query for a literal ``SERV-`` itemid is never useful).
    """
    safe_code = code.strip().replace("'", "''")
    if not safe_code:
        return []
    query = f"""
        SELECT
            i.id,
            i.itemid,
            i.purchasedescription AS description,
            SUBSTR(itemid,6,4) AS ncm_code
        FROM item i
        WHERE itemid LIKE 'SERV-%'
          AND SUBSTR(itemid,6,4) = '{safe_code}'
    """
    rows = await run_suiteql(query)
    return [
        {
            "ns_item_id": str(row.get("id")),
            "item_code": row.get("itemid"),
            "description": row.get("description"),
            "ncm_code": row.get("ncm_code"),
        }
        for row in rows
    ]


async def fetch_vendor_bills() -> list[dict[str, Any]]:
    """Fetch vendor bills from Netsuite (last 90 days) via SuiteQL."""
    cutoff = (datetime.now(UTC) - _90_DAYS).strftime("%Y-%m-%d")
    query = f"""
        SELECT
            t.id AS transaction_id,
            t.tranid AS invoice_number,
            TO_CHAR(t.trandate, 'YYYY-MM-DD') AS transaction_date,
            BUILTIN.DF(t.status) AS transaction_status,
            v.companyname AS vendor_name,
            t.custbody_brl_tran_t_access_key AS access_key,
            t.otherrefnum AS po_number
        FROM transaction t
        LEFT JOIN vendor v ON t.entity = v.id
        WHERE t.type = 'VendBill'
          AND TO_CHAR(t.trandate, 'YYYY-MM-DD') >= '{cutoff}'
        ORDER BY t.id ASC
    """
    return await run_suiteql(query)
