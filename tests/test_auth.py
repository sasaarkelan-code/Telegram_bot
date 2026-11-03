from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from smm.auth import AUTH, SCOPE, Auth, AuthError


class HostIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = Auth(self.root)
        self.host_path = self.root / "host.json"

    def test_new_host_is_persisted_uuid4_uri(self):
        value = self.auth.host_id()
        self.assertTrue(value.startswith("urn:uuid:"))
        self.assertEqual(uuid.UUID(value).version, 4)
        self.assertEqual(json.loads(self.host_path.read_text())["id"], value)
        self.assertEqual(Auth(self.root).host_id(), value)

    def test_legacy_uuid_migrates_without_losing_settings(self):
        legacy = str(uuid.uuid4())
        self.host_path.write_text(json.dumps({"id": legacy, "other": "keep"}))
        settings = self.root / "config.json"
        settings.write_bytes(b'{"test_setting":"keep"}')
        self.auth.path.write_bytes(b'{"test_credentials":"keep"}')
        self.assertEqual(self.auth.host_id(), "urn:uuid:" + legacy)
        self.assertEqual(json.loads(self.host_path.read_text()),
                         {"id": "urn:uuid:" + legacy, "other": "keep"})
        self.assertEqual(settings.read_bytes(), b'{"test_setting":"keep"}')
        self.assertEqual(self.auth.path.read_bytes(), b'{"test_credentials":"keep"}')

    def test_saved_host_uris_are_reused_without_rewriting(self):
        for value in (uuid.uuid4().urn, "did:key:zTestHost",
                      "urn:ietf:params:oauth:jwk-thumbprint:sha-256:test-host"):
            with self.subTest(value=value):
                original = json.dumps({"id": value, "other": "keep"}, indent=4)
                self.host_path.write_text(original)
                with patch("smm.auth.atomic_json") as write:
                    self.assertEqual(self.auth.host_id(), value)
                    write.assert_not_called()
                self.assertEqual(self.host_path.read_text(), original)

    def test_broken_host_is_reported_without_erasing_it(self):
        for raw in ('{', '{}', '[]', '{"id":null}', '{"id":[]}',
                    '{"id":"not-a-host"}', '{"id":"urn:uuid:invalid"}'):
            with self.subTest(raw=raw):
                self.host_path.write_text(raw)
                with self.assertRaises(AuthError):
                    self.auth.host_id()
                self.assertEqual(self.host_path.read_text(), raw)

    def simulate_login(self, expected_host=None, returning=False):
        captured = {}
        servers = []
        if returning:
            self.auth.path.write_text(json.dumps({"client_id": "issued-test-client", "sub": "test-user"}))

        class FakeServer:
            def __init__(self, address, handler):
                self.server_port = 1455
                self.handler = handler
                self.closed = False
                servers.append(self)

            def handle_request(self):
                handler = self.handler.__new__(self.handler)
                handler.path = "/auth/callback?" + urllib.parse.urlencode({
                    "state": captured["state"][0], "code": "test-code",
                    "client_id": "issued-test-client"})
                handler.wfile = io.BytesIO()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.do_GET()

            def server_close(self):
                self.closed = True

        def open_browser(url):
            captured.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
            value = captured["ext_agent_host_id"][0]
            self.assertTrue(value.startswith("urn:uuid:"))
            self.assertEqual(uuid.UUID(value).version, 4)
            if expected_host:
                self.assertEqual(value, expected_host)
            self.assertEqual(json.loads(self.host_path.read_text())["id"], value)
            self.assertEqual(captured["client_id"], ["issued-test-client" if returning else "dynamic_agent_client"])
            self.assertEqual(captured["scope"], [SCOPE])
            if returning:
                self.assertNotIn("agent_name_hint", captured)
            else:
                self.assertEqual(captured["agent_name_hint"], ["SMM Manager Local"])
            return True

        tokens = {"access_token": "test-access", "refresh_token": "test-refresh",
                  "id_token": "test-id-token", "scope": SCOPE, "expires_in": 3600}

        def fake_api(url, *args, **kwargs):
            if url == AUTH + "/api/accounts/oauth/token":
                self.assertEqual(args[0]["client_id"], "issued-test-client")
                self.assertEqual(args[0]["redirect_uri"], captured["redirect_uri"][0])
                return tokens
            if url == AUTH + "/jwks":
                return {"keys": []}
            self.fail("Unexpected network request in login test")

        models = [{"slug": "test-model", "name": "Test"}]
        with patch("smm.auth.HTTPServer", FakeServer), \
                patch("smm.auth.webbrowser.open", side_effect=open_browser), \
                patch("smm.auth.api_json", side_effect=fake_api), \
                patch("smm.auth.validate_id_token", return_value={"sub": "test-user"}), \
                patch.object(self.auth, "discovery", return_value={"issuer": AUTH, "jwks_uri": AUTH + "/jwks"}), \
                patch.object(self.auth, "models", return_value=models), \
                patch("builtins.print"):
            self.assertEqual(self.auth.login(), models)
        self.assertTrue(servers[0].closed)
        self.assertEqual(self.auth.saved()["client_id"], "issued-test-client")
        return captured["ext_agent_host_id"][0]

    def test_first_login_sends_persisted_host_uri(self):
        self.simulate_login()

    def test_login_migrates_legacy_host_before_opening_browser(self):
        legacy = str(uuid.uuid4())
        self.host_path.write_text(json.dumps({"id": legacy}))
        self.simulate_login(expected_host="urn:uuid:" + legacy)

    def test_returning_login_keeps_host_and_client(self):
        value = uuid.uuid4().urn
        self.host_path.write_text(json.dumps({"id": value}))
        self.simulate_login(expected_host=value, returning=True)


if __name__ == "__main__":
    unittest.main()
