"""Offline coverage only: fake credentials, temporary files, and mocked transport."""

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request

import prod_read as reader
import sandbox_read


class ProductionReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = Path(self.directory.name) / "fake.env"
        self.values = {"ACCOUNT_ID": "1234567", "CONSUMER_KEY": "fake-consumer",
                       "CONSUMER_SECRET": "fake-secret&+", "ACCESS_TOKEN": "fake-token",
                       "TOKEN_SECRET": "fake-token-secret"}
        self.write_env()
        self.creds = reader.load_credentials(self.env, "NETSUITE_")

    def write_env(self, prefix="NETSUITE_", values=None, extra=""):
        values = self.values if values is None else values
        self.env.write_text("# Fake credentials only\n\n" + "\n".join(
            f'export {prefix}{key}="{value}" # note' for key, value in values.items()
        ) + "\n" + extra, encoding="utf-8")
        self.env.chmod(0o600)

    def args(self, **kwargs):
        values = dict(command="suiteql", query="SELECT id FROM transaction", limit=100, offset=0)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def response(self, data):
        return io.BytesIO(json.dumps(data).encode())

    def error(self, status, body="", headers=None):
        return HTTPError("https://unused.invalid", status, "mock", headers or {}, io.BytesIO(body.encode()))

    def test_environment_isolation_and_explicit_prefixes(self):
        with patch.dict(os.environ, {"NETSUITE_ACCOUNT_ID": "999", "NETSUITE_ACCESS_TOKEN": "inherited"}):
            self.assertEqual(reader.load_credentials(self.env, "NETSUITE_")["account"], "1234567")
            self.write_env(values={k: v for k, v in self.values.items() if k != "ACCESS_TOKEN"})
            with self.assertRaisesRegex(ValueError, "ACCESS_TOKEN"):
                reader.load_credentials(self.env, "NETSUITE_")
        for prefix in reader.PREFIXES:
            with self.subTest(prefix=prefix):
                values = dict(self.values)
                if prefix == "NS_":
                    values["TOKEN_ID"] = values.pop("ACCESS_TOKEN")
                self.write_env(prefix, values)
                self.assertEqual(reader.load_credentials(self.env, prefix)["token"], "fake-token")
                other = "NS_" if prefix != "NS_" else "NETSUITE_"
                with self.assertRaises(ValueError):
                    reader.load_credentials(self.env, other)
        self.write_env("NS_")
        with self.assertRaisesRegex(ValueError, "NS_TOKEN_ID"):
            reader.load_credentials(self.env, "NS_")

    def test_private_owned_regular_file_required(self):
        for mode in (0o644, 0o640, 0o601, 0o660):
            self.env.chmod(mode)
            with self.assertRaisesRegex(ValueError, "private"):
                reader.load_credentials(self.env, "NETSUITE_")
        self.env.chmod(0o400)
        reader.load_credentials(self.env, "NETSUITE_")
        with patch.object(reader.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "current UID"):
                reader.load_credentials(self.env, "NETSUITE_")
        link = self.env.with_name("link")
        link.symlink_to(self.env)
        with self.assertRaises(OSError):
            reader.load_credentials(link, "NETSUITE_")
        with self.assertRaises((OSError, ValueError)):
            reader.load_credentials(self.directory.name, "NETSUITE_")
        fifo = self.env.with_name("fifo")
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ValueError, "regular"):
            reader.load_credentials(fifo, "NETSUITE_")

    def test_file_parser_literals_and_malformed_assignments(self):
        self.write_env(extra="UNRELATED='$(touch never-executed)'\nEMPTY=\nLITERAL=$HOME\n")
        with patch.object(reader.os, "system", side_effect=AssertionError("Must not execute shell")):
            reader.load_credentials(self.env, "NETSUITE_")
        for extra in ("NETSUITE_ACCESS_TOKEN=duplicate\n", "BROKEN secret-value\n", 'X="unterminated\n',
                      'X="a" "b"\n', "X=bad'quote\n"):
            with self.subTest(extra=extra):
                self.write_env(extra=extra)
                with self.assertRaises(ValueError) as caught:
                    reader.load_credentials(self.env, "NETSUITE_")
                self.assertNotIn("secret-value", str(caught.exception))
        for value in ("", "with space", "tab\there", "nonascii-é"):
            self.write_env(values={**self.values, "ACCESS_TOKEN": value})
            with self.assertRaisesRegex(ValueError, "ACCESS_TOKEN|Malformed"):
                reader.load_credentials(self.env, "NETSUITE_")

    def test_production_account_realm_and_host_refusal(self):
        for account in ("123_SB1", "123_RP", "example.com", "123/evil", "１２３", "123:443"):
            with self.subTest(account=account):
                self.write_env(values={**self.values, "ACCOUNT_ID": account})
                with self.assertRaises(ValueError):
                    reader.load_credentials(self.env, "NETSUITE_")
        for extra in ("NETSUITE_REALM=7654321\n", "NETSUITE_REALM=\n", "NETSUITE_URL=evil.invalid\n"):
            self.write_env(extra=extra)
            with self.assertRaises(ValueError):
                reader.load_credentials(self.env, "NETSUITE_")
        host = "1234567.suitetalk.api.netsuite.com"
        for url in ("", reader.HOST_SUFFIX, host, "https://" + host, "https://" + host + "/"):
            self.write_env(extra=f"NETSUITE_REALM=1234567\nNETSUITE_URL={url}\n")
            reader.load_credentials(self.env, "NETSUITE_")
        for url in ("1234567-sb1.suitetalk.api.netsuite.com", "https://evil.invalid",
                    "https://" + host + "/services/rest", "http://" + host,
                    "https://" + host + ":443", "https://" + host + "?x=1"):
            self.write_env(extra=f"NETSUITE_URL={url}\n")
            with self.assertRaisesRegex(ValueError, "hosts"):
                reader.load_credentials(self.env, "NETSUITE_")

    def test_sandbox_accounts_hosts_and_equivalent_realm(self):
        for account in ("1234567_SB1", "1234567_sb2", "1234567-SB3", "1234567-sb10"):
            host = account.lower().replace("_", "-") + "." + reader.HOST_SUFFIX
            for url in ("", reader.HOST_SUFFIX, host, "https://" + host, "https://" + host + "/"):
                with self.subTest(account=account, url=url):
                    self.write_env(values={**self.values, "ACCOUNT_ID": account},
                                   extra=f"NETSUITE_REALM={account}\nNETSUITE_URL={url}\n")
                    creds = reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")
                    self.assertEqual(creds["account"], account)
                    self.assertEqual(reader.account_host(account, "sandbox"), host)
                    with self.assertRaises(ValueError):
                        reader.load_credentials(self.env, "NETSUITE_")
            for realm in (account.lower(), account.replace("_", "-"), account.upper().replace("-", "_")):
                self.write_env(values={**self.values, "ACCOUNT_ID": account}, extra=f"NETSUITE_REALM={realm}\n")
                creds = reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")
                self.assertEqual(creds["realm"], realm)
                self.assertEqual(creds["account"], account)
            for realm in ("1234567", "", "7654321_SB1", account + "0", "1234567_SB1/evil"):
                self.write_env(values={**self.values, "ACCOUNT_ID": account}, extra=f"NETSUITE_REALM={realm}\n")
                with self.assertRaises(ValueError):
                    reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")
        for url in ("1234567.suitetalk.api.netsuite.com", "https://evil.invalid",
                    "https://1234567-sb1.suitetalk.api.netsuite.com:443",
                    "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest"):
            self.write_env(values={**self.values, "ACCOUNT_ID": "1234567_SB1"}, extra=f"NETSUITE_URL={url}\n")
            with self.assertRaisesRegex(ValueError, "hosts"):
                reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")

    def test_cross_profile_refusal_at_credentials_and_request_boundaries(self):
        profiles = (("sandbox", ("1234567", "123_SB", "123_RP", "１２３_SB1", "123_SB１", "123_SB1/evil")),
                    ("production", ("123_SB1", "123-sb2", "123-sB3")))
        with patch.object(reader, "build_opener") as transport:
            for environment, accounts in profiles:
                for account in accounts:
                    with self.subTest(environment=environment, account=account):
                        self.write_env(values={**self.values, "ACCOUNT_ID": account})
                        with self.assertRaises(ValueError):
                            reader.load_credentials(self.env, "NETSUITE_", environment=environment)
                        for args in (self.args(), self.args(command="record", type="vendor", id="42", expand=False)):
                            with self.assertRaises(ValueError):
                                reader.read_page({**self.creds, "account": account}, args, environment=environment)
            transport.assert_not_called()

    def test_sandbox_inherited_credentials_ignored(self):
        self.write_env(values={**self.values, "ACCOUNT_ID": "1234567_SB1"})
        with patch.dict(os.environ, {"NETSUITE_ACCOUNT_ID": "999", "NETSUITE_ACCESS_TOKEN": "inherited"}):
            creds = reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")
            self.assertEqual(creds["account"], "1234567_SB1")
            self.assertEqual(creds["token"], "fake-token")
            self.write_env(values={k: v for k, v in {**self.values, "ACCOUNT_ID": "1234567_SB1"}.items()
                                   if k != "ACCESS_TOKEN"})
            with self.assertRaisesRegex(ValueError, "ACCESS_TOKEN"):
                reader.load_credentials(self.env, "NETSUITE_", environment="sandbox")

    def test_profile_default_files_and_help(self):
        for entrypoint, environment, filename in ((reader.main, "production", ".env.prod"),
                                                  (sandbox_read.main, "sandbox", ".env.sandbox")):
            with self.subTest(environment=environment):
                with patch.object(reader, "load_credentials", return_value=self.creds) as load, \
                     patch.object(reader, "build_opener", side_effect=AssertionError("Offline only")), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(entrypoint(["check"]), 0)
                load.assert_called_once_with("~/.config/netsuite/" + filename, "NETSUITE_", environment=environment)
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                    entrypoint(["--help"])
                self.assertEqual(caught.exception.code, 0)
                self.assertIn(f"Isolated {environment} reads", output.getvalue())
                self.assertIn(filename, output.getvalue())
                if environment == "sandbox":
                    self.assertNotIn("production", output.getvalue())
        with patch.object(sandbox_read.prod_read, "main", return_value=7) as shared:
            self.assertEqual(sandbox_read.main(["check"]), 7)
        shared.assert_called_once_with(["check"], environment="sandbox")

    def test_sandbox_get_and_suiteql_share_transport_and_signer(self):
        for account in ("1234567_SB1", "1234567-sB2"):
            self.write_env(values={**self.values, "ACCOUNT_ID": account})
            for argv, method, path in ((["record", "vendor", "42", "--expand"], "GET", "/services/rest/record/v1/vendor/42"),
                                       (["suiteql", "SELECT id FROM vendor"], "POST", "/services/rest/query/v1/suiteql")):
                with self.subTest(account=account, method=method):
                    opener = Mock()
                    opener.open.return_value = self.response({"items": [], "hasMore": True})
                    with patch.object(reader, "build_opener", return_value=opener) as build, \
                         patch.object(reader, "auth_header", wraps=reader.auth_header) as sign, \
                         redirect_stdout(io.StringIO()):
                        self.assertEqual(sandbox_read.main(["--env-file", str(self.env)] + argv), 0)
                    opener.open.assert_called_once()
                    request = opener.open.call_args.args[0]
                    self.assertEqual(request.method, method)
                    self.assertEqual(urlsplit(request.full_url).netloc, account.lower().replace("_", "-") + "." + reader.HOST_SUFFIX)
                    self.assertEqual(urlsplit(request.full_url).path, path)
                    self.assertIn(f'realm="{account}"', request.get_header("Authorization"))
                    sign.assert_called_once()
                    self.assertEqual(sign.call_args.args[0]["account"], account)
                    self.assertEqual(build.call_args.args[0].proxies, {})
                    self.assertIsInstance(build.call_args.args[1], reader.NoRedirect)
                    if method == "GET":
                        self.assertIsNone(request.data)
                        self.assertEqual(sign.call_args.args[3], {"expandSubResources": "true"})
                    else:
                        self.assertEqual(json.loads(request.data), {"q": "SELECT id FROM vendor"})
                        self.assertEqual(request.get_header("Prefer"), "transient")

    def test_sandbox_explicit_realm_is_used_without_changing_host(self):
        self.write_env(values={**self.values, "ACCOUNT_ID": "1234567-sb1"},
                       extra="NETSUITE_REALM=1234567_SB1\n")
        opener = Mock()
        opener.open.return_value = self.response({"items": [{"id": "3"}]})
        with patch.object(reader, "build_opener", return_value=opener), redirect_stdout(io.StringIO()):
            self.assertEqual(sandbox_read.main(["--env-file", str(self.env), "suiteql",
                                                "SELECT id FROM subsidiary WHERE id = 3"]), 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).netloc, "1234567-sb1." + reader.HOST_SUFFIX)
        self.assertIn('realm="1234567_SB1"', request.get_header("Authorization"))

    def test_select_literals_and_case_expression(self):
        for query in ("SELECT id FROM transaction", "select 'DELETE; -- /* \" WITH' AS label FROM vendor",
                      "SELECT 'it''s safe' FROM vendor WHERE id IN (SELECT id FROM vendor)",
                      "SELECT CASE WHEN id = 1 THEN 'yes' ELSE 'no' END AS label FROM vendor"):
            with self.subTest(query=query):
                self.assertEqual(reader.validate_select(query), query)

    def test_unsafe_sql_refused(self):
        for query in ("DELETE FROM vendor", "WITH t AS (SELECT id FROM vendor) SELECT * FROM t",
                      "SELECT id FROM vendor;", "SELECT id FROM vendor -- comment",
                      "SELECT /* comment */ id FROM vendor", 'SELECT "DROP" FROM vendor',
                      "SELECT `id` FROM vendor", "SELECT [id] FROM vendor", "SELECT 'unclosed",
                      "SELECT id INTO other FROM vendor", "SELECT id FROM vendor FOR UPDATE",
                      "SELECT q'[x]' FROM vendor", "SELECT 1 EXECUTE evil", "SELECT 1\x00"):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    reader.validate_select(query)
        for keyword in reader.FORBIDDEN_SQL:
            with self.assertRaises(ValueError):
                reader.validate_select("SELECT " + keyword + " FROM vendor")

    def test_path_and_bounds_refused_before_transport(self):
        with patch.object(reader, "build_opener") as transport:
            for record_type, record_id in (("vendor/../", "1"), ("vendor", "1?x=2"),
                                            ("vendor", "１２"), ("vendor", "-1"), ("v_foo", "1")):
                with self.assertRaises(ValueError):
                    reader.read_page(self.creds, self.args(command="record", type=record_type,
                                                           id=record_id, expand=False))
            for limit, offset in ((0, 0), (1001, 0), (1, -1)):
                with self.assertRaises(ValueError):
                    reader.read_page(self.creds, self.args(limit=limit, offset=offset))
            with self.assertRaises(ValueError):
                reader.read_page(self.creds, self.args(query="DROP TABLE vendor"))
            transport.assert_not_called()

    def test_signature_rfc3986_deterministic_query_parameters(self):
        url = "https://1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
        params = {"limit": "100", "offset": "200", "expandSubResources": "true"}
        with patch.object(reader.secrets, "token_hex", return_value="nonce"), patch.object(reader.time, "time", return_value=123):
            header = reader.auth_header(self.creds, "POST", url, params)
        fields = dict((key, unquote(value.strip('"'))) for key, value in
                      (part.split("=", 1) for part in header.removeprefix("OAuth ").split(", ")))
        signature = fields.pop("oauth_signature")
        self.assertEqual(fields.pop("realm"), "1234567")
        self.assertEqual(fields["oauth_nonce"], "nonce")
        self.assertEqual(fields["oauth_timestamp"], "123")
        normalized = "&".join(f"{reader.encode(k)}={reader.encode(v)}" for k, v in
                              sorted({**fields, **params}.items()))
        base = "POST&" + reader.encode(url) + "&" + reader.encode(normalized)
        key = "fake-secret%26%2B&fake-token-secret"
        expected = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(signature, expected)
        self.assertNotIn(self.creds["consumer_secret"], header)

    def test_single_page_fixed_suiteql_endpoint(self):
        data = {"items": [{"id": "1"}], "hasMore": True,
                "links": [{"rel": "next", "href": "https://evil.invalid"}]}
        opener = Mock()
        opener.open.return_value = self.response(data)
        with patch.object(reader, "build_opener", return_value=opener) as build:
            self.assertEqual(reader.read_page(self.creds, self.args()), data)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlsplit(request.full_url).netloc, "1234567.suitetalk.api.netsuite.com")
        self.assertEqual(urlsplit(request.full_url).path, "/services/rest/query/v1/suiteql")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"limit": ["100"], "offset": ["0"]})
        self.assertEqual(json.loads(request.data), {"q": "SELECT id FROM transaction"})
        self.assertEqual(request.get_header("Prefer"), "transient")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 60})
        self.assertIsInstance(build.call_args.args[1], reader.NoRedirect)
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_record_expand_query_is_signed(self):
        opener = Mock()
        opener.open.return_value = self.response({"id": "42"})
        with patch.object(reader, "build_opener", return_value=opener), patch.object(reader, "auth_header", wraps=reader.auth_header) as sign:
            reader.read_page(self.creds, self.args(command="record", type="vendor", id="42", expand=True))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertTrue(request.full_url.endswith("/vendor/42?expandSubResources=true"))
        self.assertEqual(sign.call_args.args[3], {"expandSubResources": "true"})

    def test_redirects_blocked(self):
        for code in (301, 302, 303, 307, 308):
            with self.assertRaises(HTTPError) as caught:
                reader.NoRedirect().redirect_request(Request("https://source.invalid"), io.BytesIO(), code,
                                                     "redirect", {}, "https://evil.invalid")
            caught.exception.close()
        opener = Mock()
        opener.open.side_effect = self.error(302, "fake-secret&+", {"Location": "https://evil.invalid"})
        with patch.object(reader, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                reader.read_page(self.creds, self.args())
        opener.open.assert_called_once()

    def test_retry_fresh_auth_and_capped_delays(self):
        opener = Mock()
        opener.open.side_effect = [self.error(429, headers={"Retry-After": "9999"}),
                                    self.error(401, '{"code":"INVALID_LOGIN"}'), self.response({"items": []})]
        with patch.object(reader, "build_opener", return_value=opener), patch.object(reader.time, "sleep") as sleep, \
             patch.object(reader.secrets, "token_hex", side_effect=["a", "b", "c"]), \
             patch.object(reader.time, "time", side_effect=[100, 101, 102]), redirect_stderr(io.StringIO()):
            self.assertEqual(reader.read_page(self.creds, self.args()), {"items": []})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 1.0])
        headers = [call.args[0].get_header("Authorization") for call in opener.open.call_args_list]
        self.assertEqual(len(set(headers)), 3)
        for index, header in enumerate(headers):
            self.assertIn(f'oauth_timestamp="{100 + index}"', header)
            self.assertIn(f'oauth_nonce="{chr(97 + index)}"', header)

    def test_retry_budget_and_no_other_status_retry(self):
        for status, body, attempts in ((429, "", 3), (401, "INVALID_LOGIN", 3),
                                       (401, "DENIED", 1), (500, "INVALID_LOGIN", 1)):
            with self.subTest(status=status, body=body):
                opener = Mock()
                opener.open.side_effect = [self.error(status, body) for _ in range(attempts)]
                with patch.object(reader, "build_opener", return_value=opener), patch.object(reader.time, "sleep"), \
                     redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                    reader.read_page(self.creds, self.args())
                self.assertEqual(opener.open.call_count, attempts)
        for value, expected in (("-5", 0), ("nan", 0.5), ("inf", 0.5), ("garbage", 0.5),
                                ("Wed, 01 Jan 2020 00:00:00 GMT", 0)):
            self.assertEqual(reader.retry_delay({"Retry-After": value}, 0), expected)

    def test_offline_check_json_and_error_redaction(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(reader, "build_opener", side_effect=AssertionError("Offline only")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(reader.main(["--env-file", str(self.env), "check"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(set(result), {"account", "keys"})
        self.assertEqual(result["account"], "1234567")
        self.assertEqual(stderr.getvalue(), "")
        for value in list(self.values.values())[1:]:
            self.assertNotIn(value, stdout.getvalue())
        for error in (self.error(403, "fake-secret&+ fake-token"), URLError("fake-secret&+")):
            stdout, stderr = io.StringIO(), io.StringIO()
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(reader, "build_opener", return_value=opener), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(reader.main(["--env-file", str(self.env), "suiteql", "SELECT id FROM vendor"]), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn("fake-secret", stderr.getvalue())
            self.assertNotIn("fake-token", stderr.getvalue())
        self.assertEqual(reader.redact("fake-secret&+ fake-secret%26%2B", self.creds), "[redacted] [redacted]")


if __name__ == "__main__":
    unittest.main()
