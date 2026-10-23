"""Contract checks for the single Pramaan client, against the in-module fake
and injected transports. No live call, no frappe import, no network."""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

MODULE_PATH = Path(__file__).resolve().parents[1] / "pramaan.py"
SPEC = importlib.util.spec_from_file_location("pramaan", MODULE_PATH)
pramaan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pramaan)


class RecordingTransport:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.respond(method, url, headers, body)


def client(transport, key="test-key"):
    return pramaan.PramaanClient(api_key=key, transport=transport)


class FakeClientTests(unittest.TestCase):
    def setUp(self):
        os.environ["PRAMAAN_FAKE"] = "1"
        self.c = pramaan.PramaanClient(api_key="test-key")

    def tearDown(self):
        os.environ.pop("PRAMAAN_FAKE", None)

    # @feature pramaan.client-contract
    def test_email_fixture_and_key_required(self):
        out = self.c.send_email(to="a@example.com", subject="s", html="<p>x</p>", idempotency_key="k")
        self.assertEqual(out["status"], "queued")
        with self.assertRaises(pramaan.MissingPramaanKey):
            pramaan.PramaanClient(api_key="").send_email(to="a@example.com", subject="s", text="x", idempotency_key="k")

    def test_single_sender_default_and_override(self):
        from unittest.mock import patch
        transport = RecordingTransport(pramaan.PramaanClient._fake)
        c = client(transport)
        with patch.dict(os.environ, {"PRAMAAN_EMAIL_FROM": ""}):
            c.send_email(to="reader@example.test", subject="Report", text="Ready", idempotency_key="sender-default")
        self.assertEqual(transport.calls[-1][3]["from"], "Business Intelligence <noreply@updates.pramaan.one>")
        self.assertNotIn("replyTo", transport.calls[-1][3])
        with patch.dict(os.environ, {"PRAMAAN_EMAIL_FROM": "Reports <reports@owner.test>"}):
            c.send_email(to="reader@example.test", subject="Report", text="Ready", idempotency_key="sender-owner")
        self.assertEqual(transport.calls[-1][3]["from"], "Reports <reports@owner.test>")

    def test_placeholder_reply_addresses_are_omitted(self):
        transport = RecordingTransport(pramaan.PramaanClient._fake)
        c = client(transport)
        for address in [None, "Administrator", "admin@example.com", "Owner <owner@example.org>", "owner@local.test"]:
            c.send_email(to="reader@example.test", subject="Report", text="Ready", reply_to=address, idempotency_key="reply")
            self.assertNotIn("replyTo", transport.calls[-1][3])
        c.send_email(to="reader@example.test", subject="Report", text="Ready", reply_to="owner@unit-mail.company", idempotency_key="reply-configured")
        self.assertEqual(transport.calls[-1][3]["replyTo"], "owner@unit-mail.company")

    # @feature pramaan.client-contract
    def test_draft_contract(self):
        out = self.c.draft_text(user="make sql")
        self.assertEqual(out["finishReason"], "stop")
        self.assertEqual(out["usage"], {"inputTokens": 3, "outputTokens": 4})
        with self.assertRaises(pramaan.InvalidGenerationRequest):
            self.c.draft_text(user="x", max_tokens=63)
        with self.assertRaises(pramaan.InvalidGenerationRequest):
            self.c.call("llm.complete", {"prompt": "x"})

    # @feature pramaan.client-contract
    def test_channel_connect_and_post(self):
        start = self.c.connect_channel_start()
        self.assertEqual(start["code"], "fixture-code")
        paired = self.c.connect_channel_check("fixture-code")
        self.assertEqual(paired["status"], "connected")
        self.assertEqual(paired["accountId"], "fixture-account")
        self.assertEqual(self.c.connect_channel_check("other")["status"], "pending")
        accounts = self.c.connected_accounts()["results"]
        out = self.c.post_to_channel(account_id=accounts[0]["accountId"], text="alert", idempotency_key="k")
        self.assertEqual(out["status"], "published")

    # @feature pramaan.client-contract
    def test_notify_workflow_page_catalogue(self):
        out = self.c.send_notification(subscriber_id="u1", body="done", idempotency_key="k")
        self.assertTrue(out["accepted"])
        done = self.c.call_workflow(url="https://example.com/hook", body={"a": 1}, idempotency_key="k")
        self.assertEqual(done["status"], "succeeded")
        page = self.c.read_page("https://example.com")
        self.assertFalse(page["truncated"])
        self.assertEqual(self.c.search_catalogue("sheets")["results"][0]["app"], "fixture_sheets")

    def test_connected_read_contract_and_attachment_upload(self):
        transport = RecordingTransport(pramaan.PramaanClient._fake)
        c = client(transport)
        c.start_app_connection(app="fixture_sheets", end_user_ref="member1", redirect_url="http://localhost/return")
        self.assertNotIn("access", transport.calls[-1][3])
        self.assertEqual(c.connected_app_connections("member1")["results"][0]["status"], "active")
        for method in ["POST", "PATCH", "PUT", "DELETE"]:
            with self.assertRaises(pramaan.PramaanError):
                c.read_app(app="fixture_sheets", end_user_ref="member1", path="/records", method=method)
        c.send_email(to="reader@example.com", subject="Report", text="Attached", attachments=[
            {"name": "report.csv", "content": b"name,amount\nA,1", "content_type": "text/csv"}
        ], idempotency_key="report1")
        self.assertIsInstance(transport.calls[-2][3], bytes)
        self.assertEqual(transport.calls[-1][3]["attachments"][0]["name"], "report.csv")
        self.assertIn("fileId", transport.calls[-1][3]["attachments"][0])

    def test_http_upload_sends_raw_bytes(self):
        from unittest.mock import MagicMock, patch
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 201
        response.read.return_value = b'{"fileId":"file_fixture"}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(pramaan, "build_opener", return_value=opener):
            pramaan.PramaanClient._http(None, "POST", "https://example.com/v1/files", {"Content-Type": "text/csv"}, b"a,b")
        self.assertEqual(opener.open.call_args.args[0].data, b"a,b")

    # @feature pramaan.client-contract
    def test_actions_require_idempotency_key(self):
        with self.assertRaises(pramaan.PramaanError) as ctx:
            self.c.post_to_channel(account_id="a", text="x")
        self.assertEqual(ctx.exception.code, "missing_idempotency_key")

    # @feature pramaan.client-contract
    def test_plain_unavailable_message(self):
        """An unconnected account is refused with one plain setup message."""
        with self.assertRaises(pramaan.ConnectionRequired) as ctx:
            self.c.post_to_channel(account_id="a", text="x", idempotency_key="no-such-connection")
        self.assertIn("Set up its delivery target in Settings", str(ctx.exception))

    # @feature pramaan.client-contract
    def test_unavailable_envelope_is_plain(self):
        def respond(method, url, headers, body):
            return 503, {"error": {"code": "upstream_unavailable", "retryable": False}}
        c = pramaan.PramaanClient(api_key="test-key", transport=respond)
        with self.assertRaises(pramaan.PramaanNotAvailable) as ctx:
            c.send_notification(subscriber_id="u", body="b", idempotency_key="k")
        self.assertEqual(str(ctx.exception), "In-app notifications is not available yet.")

    # @feature pramaan.client-contract
    def test_connection_required_is_plain(self):
        with self.assertRaises(pramaan.ConnectionRequired) as ctx:
            self.c.post_to_channel(account_id="never-connected", text="x", idempotency_key="k")
        self.assertIn("Set up its delivery target in Settings", str(ctx.exception))


class TransportTests(unittest.TestCase):
    def ok(self, method, url, headers, body):
        return 200, {"status": "queued"}

    # @feature pramaan.client-contract
    def test_idempotency_header_and_allowlist(self):
        t = RecordingTransport(self.ok)
        c = client(t)
        c.send_email(to="a@example.com", subject="s", text="x", idempotency_key="abc")
        self.assertEqual(t.calls[0][2]["Idempotency-Key"], "abc")
        self.assertIn("Bearer test-key", t.calls[0][2]["Authorization"])
        with self.assertRaises(pramaan.PramaanError) as ctx:
            c.run("voice.call.start-configured", {})
        self.assertEqual(ctx.exception.code, "unsupported_capability")

    # @feature pramaan.client-contract
    def test_generation_payload_bounded(self):
        t = RecordingTransport(self.ok)
        c = client(t)
        c.draft_text(system="s", user="u")
        body = t.calls[0][3]
        self.assertEqual(body["maxTokens"], 256)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertNotIn("temperature", body)

    # @feature pramaan.client-contract
    def test_schema_subset_normalised(self):
        t = RecordingTransport(self.ok)
        c = client(t)
        c.call("llm.complete", {"messages": [{"role": "user", "content": "x"}], "maxTokens": 64,
                                "jsonSchema": {"type": "object", "$schema": "https://json-schema.org/draft-07/schema",
                                               "properties": {"a": {"type": "string", "format": "date-time"}}}})
        schema = t.calls[0][3]["jsonSchema"]
        self.assertNotIn("$schema", schema)
        self.assertNotIn("format", schema["properties"]["a"])
        for bad in ({"oneOf": []}, {"pattern": "^a$"}):
            with self.assertRaises(pramaan.InvalidGenerationRequest):
                c.call("llm.complete", {"messages": [{"role": "user", "content": "x"}], "maxTokens": 64, "jsonSchema": bad})

    # @feature pramaan.client-contract
    def test_max_tokens_refusal_is_configuration_error(self):
        def respond(method, url, headers, body):
            json.dumps({"error": {"code": "invalid_request", "message": "maxTokens must be at least 1"}}).encode()
            raise HTTPError(url, 400, "Bad Request", {}, None)
        def http_error_surface(method, url, headers, body):
            return 400, {"error": {"code": "invalid_request", "message": "maxTokens too small"}}
        c = client(http_error_surface)
        with self.assertRaises(pramaan.GenerationConfigurationError):
            c.draft_text(user="x")

    # @feature pramaan.client-contract
    def test_unavailable_is_plain(self):
        c = client(lambda *a: (501, {}))
        with self.assertRaises(pramaan.PramaanNotAvailable) as ctx:
            c.send_email(to="a@example.com", subject="s", text="x", idempotency_key="k")
        self.assertEqual(str(ctx.exception), "Email sending is not available yet.")

    # @feature pramaan.client-contract
    def test_post_500_is_uncertain_and_never_retried(self):
        attempts = []
        def respond(method, url, headers, body):
            attempts.append(url)
            return 500, {}
        c = client(respond)
        with self.assertRaises(pramaan.PramaanError) as ctx:
            c.send_email(to="a@example.com", subject="s", text="x", idempotency_key="k")
        self.assertTrue(ctx.exception.uncertain)
        self.assertEqual(len(attempts), 1)

    # @feature pramaan.client-contract
    def test_rate_limit_surfaces_without_retry(self):
        attempts = []
        def respond(method, url, headers, body):
            attempts.append(url)
            return 429, {}
        c = client(respond)
        with self.assertRaises(pramaan.PramaanError) as ctx:
            c.send_email(to="a@example.com", subject="s", text="x", idempotency_key="k")
        self.assertEqual(ctx.exception.code, "request_rejected")
        self.assertEqual(len(attempts), 1)

    # @feature pramaan.client-contract
    def test_base_url_restrictions(self):
        with self.assertRaises(pramaan.PramaanError):
            pramaan.PramaanClient(api_key="k", base_url="http://evil.example.com")
        with self.assertRaises(pramaan.PramaanError):
            pramaan.PramaanClient(api_key="k", base_url="https://user:pass@api.pramaan.dev")

    # @feature pramaan.client-contract
    def test_job_polling_for_async_action(self):
        calls = {"n": 0}
        def respond(method, url, headers, body):
            calls["n"] += 1
            path = urlparse(url).path
            if path.endswith("workflow.webhook.call"):
                return 202, {"job_id": "j1", "status": "running"}
            return 200, {"job_id": "j1", "capability": "workflow.webhook.call", "status": "done",
                         "result": {"status": "succeeded"}, "created_at": "t", "updated_at": "t"}
        c = client(respond)
        out = c.call_workflow(url="https://example.com/hook", body={}, idempotency_key="k")
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
