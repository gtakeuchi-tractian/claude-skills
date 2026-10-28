#!/usr/bin/env python3
"""Isolated production reads. SQL filtering is not a substitute for a read-only role."""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PREFIXES = ("NETSUITE_", "NETSUITE_PROD_", "NS_")
HOST_SUFFIX = "suitetalk.api.netsuite.com"
FORBIDDEN_SQL = set("WITH INSERT UPDATE DELETE MERGE UPSERT CREATE ALTER DROP TRUNCATE "
                    "GRANT REVOKE CALL EXEC EXECUTE BEGIN DECLARE PROCEDURE FUNCTION "
                    "COMMIT ROLLBACK SAVEPOINT INTO LOCK FOR SET USE REPLACE ANALYZE "
                    "COMMENT RENAME ATTACH DETACH PRAGMA VACUUM".split())


def account_host(account, environment):
    """Validate the profile boundary and derive a host without changing the OAuth realm."""
    if environment == "production":
        if not re.fullmatch(r"[0-9]+", account):
            raise ValueError("Production ACCOUNT_ID must contain only ASCII digits; sandbox accounts are refused")
    elif environment == "sandbox":
        if not re.fullmatch(r"[0-9]+[_-]sb[0-9]+", account, re.IGNORECASE | re.ASCII):
            raise ValueError("Sandbox ACCOUNT_ID must be numeric with an _SBn or -sbn suffix; production accounts are refused")
    else:
        raise ValueError("Unsupported NetSuite environment")
    return f'{account.lower().replace("_", "-")}.{HOST_SUFFIX}'


def load_credentials(filename, prefix, environment="production"):
    """Read exactly one private, owned regular file; never consult environment variables."""
    if prefix not in PREFIXES:
        raise ValueError("Unsupported credential prefix")
    fd = os.open(Path(filename).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Credential file must be regular, owned by current UID, and private (0600 or 0400)")
        values = {}
        for number, raw in enumerate(source, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if not match or match[1] in values:
                raise ValueError(f"Malformed or duplicate assignment on line {number}")
            key, value = match.groups()
            if value.startswith(("'", '"')):
                quoted = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
                if not quoted or quoted[1] in quoted[2]:
                    raise ValueError(f"Malformed quoted value on line {number}")
                value = quoted[2]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                if any(c in value for c in "'\""):
                    raise ValueError(f"Malformed value on line {number}")
            values[key] = value
    names = dict(account="ACCOUNT_ID", consumer_key="CONSUMER_KEY",
                 consumer_secret="CONSUMER_SECRET", token="TOKEN_ID" if prefix == "NS_" else "ACCESS_TOKEN",
                 token_secret="TOKEN_SECRET")
    creds = {}
    for field, suffix in names.items():
        key = prefix + suffix
        value = values.get(key, "")
        if not value or not re.fullmatch(r"[!-~]+", value):
            raise ValueError(f"Missing or malformed required key {key}")
        creds[field] = value
    derived_host = account_host(creds["account"], environment)
    realm = values.get(prefix + "REALM", creds["account"])
    if account_host(realm, environment) != derived_host:
        raise ValueError("REALM must identify the same account as ACCOUNT_ID")
    creds["realm"] = realm
    if values.get(prefix + "URL", "") not in (
        "", HOST_SUFFIX, derived_host, "https://" + derived_host, "https://" + derived_host + "/"
    ):
        raise ValueError("Custom SuiteTalk hosts are refused")
    creds["keys"] = [prefix + suffix for suffix in names.values()]
    return creds


def validate_select(query):
    """Conservative single-statement filter; only ordinary SQL string literals supported."""
    outside = []
    pos = 0
    while pos < len(query):
        char = query[pos]
        if char == "'":
            if pos and query[pos - 1] in "qQ":
                raise ValueError("Alternative SQL quoting is not supported")
            pos += 1
            while pos < len(query):
                if query[pos] == "'":
                    pos += 1
                    if pos < len(query) and query[pos] == "'":
                        pos += 1
                        continue
                    break
                pos += 1
            else:
                raise ValueError("Unterminated SQL string literal")
            outside.append(" ")
            continue
        if char in '\"`;[]' or query[pos:pos + 2] in ("--", "/*", "*/"):
            raise ValueError("SQL comments, semicolons, and quoted identifiers are refused")
        if not re.fullmatch(r"[A-Za-z0-9_\s.,()<>=!+*/%|\-]", char, flags=re.ASCII):
            raise ValueError("Unsupported SQL syntax")
        outside.append(char)
        pos += 1
    sql = "".join(outside)
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)
    if not re.match(r"\s*SELECT\b", sql, re.IGNORECASE) or not words or any(
        word.upper() in FORBIDDEN_SQL for word in words
    ):
        raise ValueError("Only a single read-only SELECT is allowed")
    return query


def encode(value):
    return quote(str(value), safe="~-._")


def auth_header(creds, method, url, params):
    # Same HMAC-SHA256 scheme as the skill reference, but sort encoded pairs
    # (RFC 5849), and generate authentication anew for every attempt.
    oauth = dict(oauth_consumer_key=creds["consumer_key"], oauth_token=creds["token"],
                 oauth_nonce=secrets.token_hex(16), oauth_timestamp=str(int(time.time())),
                 oauth_signature_method="HMAC-SHA256", oauth_version="1.0")
    pairs = sorted((encode(k), encode(v)) for k, v in list(oauth.items()) + list(params.items()))
    normalized = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join(encode(value) for value in (method, url, normalized))
    key = encode(creds["consumer_secret"]) + "&" + encode(creds["token_secret"])
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()).decode()
    return "OAuth " + ", ".join(f'{encode(k)}="{encode(v)}"' for k, v in
                               [("realm", creds.get("realm", creds["account"]))] + list(oauth.items()))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def redact(message, creds):
    for value in sorted({v for v in creds.values() if isinstance(v, str)}, key=len, reverse=True):
        for form in (value, encode(value)):
            message = message.replace(form, "[redacted]")
    return message


def retry_delay(headers, attempt):
    fallback = 0.5 * 2 ** attempt
    value = headers.get("Retry-After")
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = fallback
    return min(5.0, max(0.0, delay)) if math.isfinite(delay) else fallback


def read_page(creds, args, environment="production"):
    host = "https://" + account_host(creds["account"], environment)
    if args.command == "record":
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", args.type) or not re.fullmatch(r"[0-9]+", args.id):
            raise ValueError("Record type must be alphanumeric and ID must contain only ASCII digits")
        url = f"{host}/services/rest/record/v1/{args.type}/{args.id}"
        method, body = "GET", None
        params = {"expandSubResources": "true"} if args.expand else {}
    elif args.command == "suiteql":
        if not 1 <= args.limit <= 1000 or args.offset < 0:
            raise ValueError("Limit must be 1..1000 and offset nonnegative")
        url = host + "/services/rest/query/v1/suiteql"
        method, body = "POST", json.dumps({"q": validate_select(args.query)}).encode()
        params = {"limit": str(args.limit), "offset": str(args.offset)}
    else:
        raise ValueError("Unsupported read command")
    # No inherited proxy configuration; no cookies or automatic pagination.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for attempt in range(3):
        headers = {"Authorization": auth_header(creds, method, url, params),
                   "Content-Type": "application/json", "Accept": "application/json"}
        if method == "POST":
            headers["Prefer"] = "transient"
        request = Request(url + ("?" + urlencode(params) if params else ""),
                          data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                payload = error.read().decode("utf-8", errors="replace")
            retry = error.code == 429 or (error.code == 401 and re.search(r"\bINVALID_LOGIN\b", payload))
            if not retry or attempt == 2:
                # Never echo server bodies, URLs, headers, or transport exceptions:
                # any of these could contain raw or encoded credential values.
                raise ValueError(f"NetSuite HTTP {error.code}; response details withheld") from None
            print(redact(f"Transient HTTP {error.code}; retrying ({attempt + 2}/3)", creds), file=sys.stderr)
            time.sleep(retry_delay(error.headers, attempt))
        except (URLError, OSError):
            raise ValueError("NetSuite transport failed; details withheld") from None
        except (ValueError, UnicodeError):
            raise ValueError("NetSuite returned invalid JSON; details withheld") from None


def main(argv=None, environment="production"):
    if environment not in ("production", "sandbox"):
        raise ValueError("Unsupported NetSuite environment")
    parser = argparse.ArgumentParser(description=(
        f"Isolated {environment} reads. SQL filtering is not a substitute for a read-only role."), epilog=(
        "SuiteQL returns one page unchanged, including hasMore. For further pages, "
        "rerun with --offset increased by --limit; never follow response URLs. "
        "Use a read-only NetSuite role: SELECT validation is defense-in-depth only."))
    default_file = ".env.prod" if environment == "production" else ".env.sandbox"
    parser.add_argument("--env-file", default=f"~/.config/netsuite/{default_file}",
                        help=f"Private {environment} credential file (default: %(default)s)")
    parser.add_argument("--credential-prefix", choices=PREFIXES, default="NETSUITE_")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Offline validation; report safe key names and account only")
    record = commands.add_parser("record", help="Read one record with GET")
    record.add_argument("type", metavar="TYPE")
    record.add_argument("id", metavar="ID")
    record.add_argument("--expand", action="store_true")
    sql = commands.add_parser("suiteql", help="Read one SELECT page; retain hasMore, rerun with --offset for more",
                              epilog="Single page only. Increase --offset by --limit for the next page. Role must be read-only.")
    sql.add_argument("query", metavar="QUERY")
    sql.add_argument("--limit", type=int, default=100)
    sql.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    creds = {}
    try:
        creds = load_credentials(args.env_file, args.credential_prefix, environment=environment)
        result = ({"account": creds["account"], "keys": creds["keys"]} if args.command == "check"
                  else read_page(creds, args, environment=environment))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, UnicodeError):
        print("Cannot read credential file; details withheld", file=sys.stderr)
    except ValueError as error:
        # Our validation messages contain only fixed text, line numbers, safe
        # key names, or HTTP status codes, never supplied values or response text.
        print(redact(str(error), creds), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
