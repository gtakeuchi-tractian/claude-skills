"""SuiteQL query strings for common NetSuite sources.

Reference clone (verbatim) from the tractian-procurement "deck" backend
(``app/sources/queries.py``). Pure string constants + a 90-day cutoff helper.
Comments document account-verified quirks — keep them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _cutoff_90d() -> str:
    return (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")


QUERY_SUBSIDIARIES = """
    SELECT id, name, country, isinactive
    FROM subsidiary
    ORDER BY id ASC
"""

QUERY_COST_CENTERS = """
    SELECT id, name, fullname, parent AS parent_id, isinactive
    FROM department
    ORDER BY id ASC
"""

QUERY_CLASSES = """
    SELECT id, name, fullname, parent AS parent_id, isinactive
    FROM classification
    ORDER BY id ASC
"""

QUERY_ITEMS = """
    SELECT id, itemid, displayname, isinactive
    FROM item
    WHERE ROWNUM <= 1000
"""

QUERY_UOM = """
    SELECT id, name, isinactive
    FROM unitstype
    ORDER BY id ASC
"""

QUERY_VENDORS = """
    SELECT id, companyname AS name, entityid AS entity_id
    FROM vendor
    WHERE isinactive = 'F'
    ORDER BY companyname ASC
"""

# Sandbox-verified 2026-09-18 — per-vendor payment term / incoterm / payment
# method, resolved via ``BUILTIN.DF`` directly on the vendor's own
# scalar/List-type fields (no join needed).
# ``terms``/``incoterm`` are NetSuite's standard vendor body fields;
# ``custentity_nsl_atb_payment_form_ls`` is the real payment-method field
# (entity-scoped List/Record → ``customrecord_nsl_atb_paymentform``),
# confirmed via ``GET /record/v1/vendor/{id}?expandSubResources=true`` —
# rejecting the previously-suspected ``custbody_nsl_atb_payment_form_pay_nu``
# (transaction-scoped, wrong record). Coverage in sandbox: terms ~66% of
# active vendors, payment method ~72%, incoterm ~0.04% (Incoterms feature is
# barely adopted upstream — not a bug).
#
# Country is intentionally NOT in this query: the community-standard
# ``vendorAddressBook``/``address`` join is confirmed broken on this account —
# SuiteQL's subrecord dot-notation on ``addressBookAddress.<field>`` always
# returns the address's internal id, never the resolved subfield, reproduced
# across multiple vendors and multiple subfields (country, city, state, zip
# all echoed the same id). Country is instead backfilled by a throttled
# per-vendor Record-API job because that endpoint resolves the subrecord
# correctly.
QUERY_VENDORS_EXTENDED = """
    SELECT
        id,
        companyname AS name,
        entityid AS entity_id,
        BUILTIN.DF(terms) AS payment_term,
        BUILTIN.DF(incoterm) AS incoterm,
        BUILTIN.DF(custentity_nsl_atb_payment_form_ls) AS payment_method
    FROM vendor
    WHERE isinactive = 'F'
    ORDER BY companyname ASC
"""

QUERY_NS_TERMS = """
    SELECT id, name, isinactive
    FROM term
    WHERE isinactive = 'F'
    ORDER BY name ASC
"""

QUERY_NS_INCOTERMS = """
    SELECT id, name, isinactive
    FROM incoterm
    WHERE isinactive = 'F'
    ORDER BY name ASC
"""

# Sandbox-verified 2026-09-18 — real payment-method catalog. The vendor-side
# field ``custentity_nsl_atb_payment_form_ls`` is a List/Record reference into
# this custom record type (confirmed via NS metadata-catalog + live sandbox
# query), NOT the transaction-scoped ``custbody_nsl_atb_payment_form_pay_nu``.
QUERY_NS_PAYMENT_METHODS: str | None = """
    SELECT id, name, isinactive
    FROM customrecord_nsl_atb_paymentform
    WHERE isinactive = 'F'
    ORDER BY name ASC
"""

QUERY_LOCATIONS = """
    SELECT id, name, fullname, parent AS parent_id, isinactive
    FROM location
    ORDER BY id ASC
"""


def query_purchase_orders() -> str:
    return f"""
    SELECT
        t.id AS transaction_id,
        t.tranid AS po_number,
        TO_CHAR(t.trandate, 'YYYY-MM-DD') AS transaction_date,
        BUILTIN.DF(t.status) AS transaction_status,
        v.companyname AS vendor_name,
        t.foreigntotal AS total_amount
    FROM transaction t
    LEFT JOIN vendor v ON t.entity = v.id
    WHERE t.type = 'PurchOrd'
      AND TO_CHAR(t.trandate, 'YYYY-MM-DD') >= '{_cutoff_90d()}'
    ORDER BY t.id ASC
"""


def query_vendor_bills() -> str:
    return f"""
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
      AND TO_CHAR(t.trandate, 'YYYY-MM-DD') >= '{_cutoff_90d()}'
    ORDER BY t.id ASC
"""
