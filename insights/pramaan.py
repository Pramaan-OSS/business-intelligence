"""All outside service calls go through this server-side module.

One client, keyed by ``PRAMAAN_API_KEY`` (optional ``PRAMAAN_BASE_URL``).
Standard library only. Mutating calls are never retried here; every caller
passes its own idempotency key. The strict in-memory fake is selected with
``PRAMAAN_FAKE=1`` and has no network transport.
"""

from dataclasses import dataclass
from typing import Literal
import copy
import json
import math
import os
import time
from email.utils import parseaddr
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

Feature = Literal[
    "alerts_email", "alerts_telegram", "alerts_notify", "alerts_workflow",
    "ask_your_data", "connected_sources",
]


def usable_contact_address(value):
    """Setup placeholders are not configured contacts or reply destinations."""
    if not isinstance(value, str):
        return False
    address = parseaddr(value)[1]
    if address.count("@") != 1:
        return False
    domain = address.rsplit("@", 1)[1].lower()
    return bool(domain and domain not in {"example.com", "example.net", "example.org", "localhost"}
                and not domain.endswith((".test", ".invalid", ".localhost")))


class CapabilityUnavailable(RuntimeError):
    """A stable application error safe to show without request data or secrets."""

    code = "capability_unavailable"

    def __init__(self, feature: Feature):
        self.feature = feature
        messages = {
            "alerts_email": "Email sending is not available yet.",
            "alerts_telegram": "Channel alerts are not available yet. Use the Email channel.",
            "alerts_notify": "In-app notifications are not available yet.",
            "alerts_workflow": "Workflow delivery is not available yet.",
            "ask_your_data": "Drafting help is not available yet.",
            "connected_sources": "Connected sources are not available yet.",
        }
        super().__init__(messages[feature])


@dataclass(frozen=True)
class FeatureAvailability:
    feature: Feature
    enabled: bool
    reason: str


class PramaanError(RuntimeError):
    """Normalized error: never expose an upstream body, provider or secret."""

    def __init__(self, code="unavailable", *, uncertain=False, job_id=None):
        self.code = code
        self.uncertain = uncertain
        self.job_id = job_id
        super().__init__(
            "The action may already have completed. Check its status before trying again."
            if uncertain else "Pramaan could not complete this request."
        )


MISSING_KEY_MESSAGE = "Add PRAMAAN_API_KEY to .env"


class MissingPramaanKey(PramaanError):
    http_status_code = 422

    def __init__(self):
        super().__init__("missing_api_key")
        self.args = (MISSING_KEY_MESSAGE,)


class InvalidGenerationRequest(PramaanError):
    http_status_code = 422

    def __init__(self, label, path, reason):
        super().__init__("invalid_request")
        self.args = (f"{label} schema at {path}: {reason}",)


class IncompleteGeneration(PramaanError):
    http_status_code = 422

    def __init__(self):
        super().__init__("incomplete_reply")
        self.args = ("The draft is incomplete because it reached the reply limit. Retry with a larger reply limit.",)


class GenerationConfigurationError(PramaanError):
    http_status_code = 422

    def __init__(self):
        super().__init__("reply_limit_configuration")
        self.args = ("The reply limit is too small to produce a draft. Ask your administrator to increase the reply limit before retrying.",)


class PramaanNotAvailable(PramaanError):
    http_status_code = 503

    def __init__(self, capability):
        super().__init__("not_available_yet")
        if capability.startswith("email."):
            surface = "Email sending"
        elif capability.startswith("social."):
            surface = "Channel alerts"
        elif capability.startswith("notify."):
            surface = "In-app notifications"
        elif capability.startswith("workflow."):
            surface = "Workflow delivery"
        elif capability.startswith("llm."):
            surface = "Drafting help"
        elif capability.startswith("apps."):
            surface = "Connected sources"
        elif capability.startswith("web."):
            surface = "Page reads"
        else:
            surface = "This feature"
        self.args = (f"{surface} is not available yet.",)


class ConnectionRequired(PramaanError):
    """The capability needs the owner's connected account, and none is connected."""

    http_status_code = 422

    def __init__(self):
        super().__init__("connection_required")
        self.args = (
            "This feature needs a connected account. Set up its delivery target in Settings first.",
        )


def generation_result(capability, inputs, result):
    if capability in {"llm.complete"} and result.get("finishReason") == "length":
        if inputs.get("jsonSchema"):
            raise IncompleteGeneration()
    return result


SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "anyOf", "$defs", "$ref", "title", "description", "default", "examples",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
    "minItems", "maxItems"})
SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
SCHEMA_BOUNDS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems"})
UNAVAILABLE_CODES = frozenset({"upstream_unavailable", "not_available_yet", "not_available",
    "capability_unavailable", "not_implemented", "unsupported_capability"})


def _schema_path(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _schema_json(value, label, path="#", stack=None, depth=0):
    stack = set() if stack is None else stack
    if depth > 32:
        raise InvalidGenerationRequest(label, path, "JSON value is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if not isinstance(value, (dict, list)) or id(value) in stack:
        raise InvalidGenerationRequest(label, path, "must contain finite JSON values without cycles")
    stack.add(id(value))
    for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
        if not isinstance(key, (str, int)) or isinstance(value, dict) and not isinstance(key, str):
            raise InvalidGenerationRequest(label, path, "property names must be strings")
        _schema_json(item, label, _schema_path(path, key), stack, depth + 1)
    stack.remove(id(value))


def validate_json_schema(value, label="Response"):
    """Validate the platform's bounded subset, including acyclic local references."""
    if not isinstance(value, dict):
        raise InvalidGenerationRequest(label, "#", "root must be a JSON Schema object")
    _schema_json(value, label)
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (ValueError, TypeError, OverflowError):
        raise InvalidGenerationRequest(label, "#", "must be serializable JSON") from None
    if size > 65536:
        raise InvalidGenerationRequest(label, "#", "exceeds 64 KB")
    nodes = set()
    count = 0

    def children(node, path):
        for key in ("properties", "$defs"):
            for name, child in node.get(key, {}).items():
                yield child, _schema_path(_schema_path(path, key), name)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                yield node[key], _schema_path(path, key)
        for index, child in enumerate(node.get("anyOf", [])):
            yield child, f"{path}/anyOf/{index}"

    def walk(node, path, depth):
        nonlocal count
        def fail(key, reason):
            raise InvalidGenerationRequest(label, _schema_path(path, key) if key else path, reason)
        if not isinstance(node, dict):
            fail("", "must be a schema object")
        if depth > 16:
            fail("", "exceeds depth 16")
        count += 1
        if count > 256:
            fail("", "exceeds 256 schema nodes")
        nodes.add(id(node))
        for key, item in node.items():
            if key not in SCHEMA_KEYS:
                fail(key, "unsupported keyword " + key)
            if key == "type":
                items = item if isinstance(item, list) else [item]
                if not items or any(not isinstance(t, str) or t not in SCHEMA_TYPES for t in items) or len(set(items)) != len(items):
                    fail(key, "invalid type")
            if key in {"properties", "$defs", "items"} and not isinstance(item, dict):
                fail(key, "must be an object")
            if key == "required" and (not isinstance(item, list) or any(not isinstance(t, str) for t in item) or len(set(item)) != len(item)):
                fail(key, "must be distinct property names")
            if key == "additionalProperties" and not isinstance(item, (bool, dict)):
                fail(key, "must be boolean or a schema")
            if key in {"anyOf", "enum"} and (not isinstance(item, list) or not 1 <= len(item) <= (16 if key == "anyOf" else 256)):
                fail(key, "exceeds branch or value limit")
            if key in {"title", "description", "$ref"} and not isinstance(item, str):
                fail(key, "must be a string")
            if key == "examples" and not isinstance(item, list):
                fail(key, "must be an array")
            if key in SCHEMA_BOUNDS and (isinstance(item, bool) or not isinstance(item, (int, float)) or isinstance(item, float) and not math.isfinite(item) or key in {"minLength", "maxLength", "minItems", "maxItems"} and (not isinstance(item, int) or item < 0)):
                fail(key, "invalid bound")
        for child, child_path in children(node, path):
            walk(child, child_path, depth + 1)

    walk(value, "#", 0)
    expanded = 0

    def references(node, path, stack, depth):
        nonlocal expanded
        expanded += 1
        if id(node) in stack or depth > 32 or expanded > 4096:
            raise InvalidGenerationRequest(label, path, "recursive or excessive reference expansion")
        next_stack = stack | {id(node)}
        if "$ref" in node:
            ref, target = node["$ref"], value
            import re
            if not ref.startswith("#/") or re.search(r"~(?![01])", ref):
                raise InvalidGenerationRequest(label, path + "/$ref", "only local #/... references are allowed")
            for key in ref[2:].split("/"):
                key = key.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or key not in target:
                    raise InvalidGenerationRequest(label, path + "/$ref", "reference does not resolve")
                target = target[key]
            if not isinstance(target, dict) or id(target) not in nodes:
                raise InvalidGenerationRequest(label, path + "/$ref", "reference must resolve to a schema")
            references(target, path + "/$ref", next_stack, depth + 1)
        for child, child_path in children(node, path):
            references(child, child_path, next_stack, depth + 1)

    references(value, "#", set(), 0)
    return value


def normalise_json_schema(value, label="Response"):
    """Strip only refused annotations; unexpressible constraints are rejected."""
    if not isinstance(value, dict):
        raise InvalidGenerationRequest(label, "#", "root must be a JSON Schema object")
    _schema_json(value, label)
    def convert(node, path, depth=0):
        if not isinstance(node, dict) or depth > 16:
            raise InvalidGenerationRequest(label, path, "must be a schema object within depth 16")
        result = {}
        for key, item in node.items():
            if key in {"$schema", "format"}:
                continue
            if key not in SCHEMA_KEYS:
                raise InvalidGenerationRequest(label, _schema_path(path, key), "unsupported keyword " + key)
            if key in {"properties", "$defs"} and isinstance(item, dict):
                result[key] = {name: convert(child, _schema_path(_schema_path(path, key), name), depth + 1) for name, child in item.items()}
            elif key in {"items", "additionalProperties"} and isinstance(item, dict):
                result[key] = convert(item, _schema_path(path, key), depth + 1)
            elif key == "anyOf" and isinstance(item, list):
                result[key] = [convert(child, f"{path}/anyOf/{index}", depth + 1) for index, child in enumerate(item)]
            else:
                result[key] = copy.deepcopy(item)
        return result
    return validate_json_schema(convert(value, "#"), label)


def generation_request(inputs, *, normalise=True):
    """One boundary for chat/completion messages, token limits and schemas."""
    if not isinstance(inputs, dict) or "prompt" in inputs or not isinstance(inputs.get("messages"), list) or not inputs["messages"]:
        raise InvalidGenerationRequest("Generation", "#/messages", "send nonempty messages instead of prompt")
    for index, message in enumerate(inputs["messages"]):
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"} or not isinstance(message.get("content"), str):
            raise InvalidGenerationRequest("Generation", f"#/messages/{index}", "must be a message with a supported role and text")
    request = dict(inputs)
    maximum = inputs.get("maxTokens", 1024) if normalise else inputs.get("maxTokens")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 64:
        raise InvalidGenerationRequest("Generation", "#/maxTokens", "must be an integer of 64 or more")
    request["maxTokens"] = maximum
    validate = normalise_json_schema if normalise else validate_json_schema
    if "jsonSchema" in inputs:
        request["jsonSchema"] = validate(inputs["jsonSchema"], "Response")
    for optional in ("model", "temperature"):
        if optional in inputs and inputs[optional] is None:
            request.pop(optional)
    return request


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PramaanClient:
    """Single server-side HTTP client. Mutating requests are never retried."""

    CAPABILITIES = frozenset({
        "email.send", "email.status", "llm.complete",
        "notify.message.send",
        "social.connect.telegram.code.create", "social.connect.telegram.code.check",
        "social.connect.telegram.chat.connect", "social.accounts.list",
        "social.post.publish",
        "workflow.webhook.call",
        "apps.catalog.search", "apps.connection.start", "apps.connections.list",
        "apps.request.read", "apps.action.execute",
        "web.read",
    })
    ACTIONS = frozenset({
        "email.send", "notify.message.send", "social.post.publish",
        "workflow.webhook.call",
        "social.connect.telegram.code.create", "social.connect.telegram.chat.connect",
        "apps.connection.start", "apps.action.execute",
    })

    def __init__(self, *, api_key=None, base_url=None, transport=None, sleep=time.sleep):
        self._api_key = api_key if api_key is not None else os.environ.get("PRAMAAN_API_KEY", "")
        self._base_url = (base_url or os.environ.get("PRAMAAN_BASE_URL") or "https://api.pramaan.dev").rstrip("/")
        parsed = urlparse(self._base_url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "fake-pramaan"}
        ):
            raise PramaanError("invalid_configuration")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PramaanError("invalid_configuration")
        self._transport = transport or (self._fake if os.environ.get("PRAMAAN_FAKE") == "1" else self._http)
        self._sleep = sleep

    @property
    def configured(self):
        return bool(self._api_key.strip())

    def require_configured(self):
        if not self.configured:
            raise MissingPramaanKey()

    @staticmethod
    def _fake(method, url, headers, body):
        # Explicit local fixture mode has no network transport or outside effects.
        path = urlparse(url).path.rsplit("/", 1)[-1]
        if method == "POST" and path == "email.send":
            return 200, {"status": "queued", "ref": "fixture-email-0001"}
        if method == "POST" and path == "email.status":
            return 200, {"status": "queued", "updatedAt": "2026-01-01T00:00:00Z"}
        if method == "POST" and path == "llm.complete":
            return 200, {"text": "SELECT 1 AS one", "finishReason": "stop",
                         "usage": {"inputTokens": 3, "outputTokens": 4}}
        if method == "POST" and path == "notify.message.send":
            return 200, {"accepted": True, "state": "accepted", "ref": "fixture-notify-0001"}
        if method == "POST" and path == "social.connect.telegram.code.create":
            return 200, {"code": "fixture-code", "expiresAt": "2026-01-01T00:15:00Z",
                         "expiresIn": 900, "botUsername": "fixture_bot",
                         "instructions": ["Add the bot to the channel", "Send it the code"]}
        if method == "POST" and path == "social.connect.telegram.code.check":
            status = (body or {}).get("code") == "fixture-code" and "connected" or "pending"
            result = {"status": status}
            if status == "connected":
                result.update(accountId="fixture-account", chatTitle="Fixture channel")
            return 200, result
        if method == "POST" and path == "social.connect.telegram.chat.connect":
            return 200, {"accountId": "fixture-account", "platform": "telegram",
                         "handle": "fixture_channel", "displayName": "Fixture channel",
                         "chatType": "channel"}
        if method == "POST" and path == "social.accounts.list":
            return 200, {"results": [{"accountId": "fixture-account", "platform": "telegram",
                                      "handle": "fixture_channel", "displayName": "Fixture channel"}]}
        if method == "POST" and path == "social.post.publish":
            if (body or {}).get("accountIds") != ["fixture-account"]:
                return 409, {"error": {"code": "connection_required", "effect": "none"}}
            return 200, {"status": "published", "ref": "fixture-post-0001"}
        if method == "POST" and path == "workflow.webhook.call":
            return 202, {"job_id": "fixture-job-0001", "status": "running"}
        if method == "GET" and urlparse(url).path.startswith("/v1/jobs/"):
            return 200, {"job_id": path, "capability": "workflow.webhook.call",
                         "status": "done", "result": {"status": "succeeded"},
                         "created_at": "2026-01-01T00:00:00Z",
                         "updated_at": "2026-01-01T00:00:00Z"}
        if method == "POST" and path == "web.read":
            return 200, {"url": (body or {}).get("url", ""), "title": "Fixture page",
                         "text": "Fixture text", "truncated": False}
        if method == "POST" and urlparse(url).path == "/v1/files":
            return 201, {"fileId": "file_fixture00000000000000000000", "kind": "document",
                         "origin": "upload", "contentType": headers.get("Content-Type"),
                         "sizeBytes": len(body) if isinstance(body, (bytes, bytearray)) else 0}
        if method == "POST" and path == "apps.connections.list":
            return 200, {"results": [{"app": "fixture_sheets", "status": "active"}]}
        if method == "POST" and path == "apps.request.read":
            return 200, {"status": 200, "body": {"rows": [{"name": "one", "amount": 10.5},
                                                          {"name": "two", "amount": 3}]},
                         "headers": {}, "untrusted": True, "truncated": False}
        if method == "POST" and path == "apps.action.execute":
            return 200, {"status": "succeeded", "output": {"rows": [{"name": "one", "amount": 10.5}]}}
        if method == "POST" and path == "apps.catalog.search":
            return 200, {"results": [{"app": "fixture_sheets", "action": "FIXTURE_READ_ROWS", "readOnly": True,
                                      "description": "Spreadsheet reads"}], "nextCursor": ""}
        if method == "POST" and path == "apps.connection.start":
            return 200, {"connectUrl": "https://fake-pramaan/connect/fixture",
                         "expiresAt": "2026-01-01T00:10:00Z"}
        return 503, {"error": {"code": "upstream_unavailable", "retryable": False}}

    def _http(self, method, url, headers, body):
        request = Request(url, data=body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None),
                          headers=headers, method=method)
        try:
            with build_opener(NoRedirects()).open(request, timeout=30) as response:
                raw = response.read(16_000_001)
                if len(raw) > 16_000_000:
                    raise PramaanError("invalid_response")
                return response.status, json.loads(raw)
        except HTTPError as error:
            # Read only bounded, recognized error codes; never surface the upstream body.
            try:
                payload = json.loads(error.read(16_001))
                code = payload.get("error") if isinstance(payload, dict) else None
                message = code.get("message", "") if isinstance(code, dict) else (payload.get("message", "") if isinstance(payload, dict) else "")
                if isinstance(code, dict):
                    code = code.get("code")
                if not code and isinstance(payload, dict):
                    code = payload.get("code")
                if error.code == 400 and code == "invalid_request" and isinstance(message, str) and "maxtokens" in message.lower():
                    return 400, {"error": {"code": "invalid_request", "message": "maxTokens"}}
            except (ValueError, OSError):
                code = None
            return error.code, {"error": code if isinstance(code, str) and code in UNAVAILABLE_CODES else None}
        except (URLError, OSError, ValueError):
            raise PramaanError("unavailable", uncertain=method == "POST") from None

    def _request(self, method, path, body=None, *, idempotency_key=None, capability=None):
        self.require_configured()
        headers = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            status, result = self._transport(method, self._base_url + path, headers, body)
        except PramaanError:
            raise
        except Exception:
            raise PramaanError("unavailable", uncertain=method == "POST") from None
        if status >= 400:
            remote_code = result.get("error") if isinstance(result, dict) else None
            if isinstance(remote_code, dict):
                remote_code = remote_code.get("code")
            if not remote_code and isinstance(result, dict):
                remote_code = result.get("code")
            remote_error = result.get("error") if isinstance(result, dict) else None
            remote_message = remote_error.get("message", "") if isinstance(remote_error, dict) else (result.get("message", "") if isinstance(result, dict) else "")
            if status == 400 and capability == "llm.complete" and remote_code == "invalid_request" and isinstance(remote_message, str) and "maxtokens" in remote_message.lower():
                raise GenerationConfigurationError()
            if isinstance(remote_code, str) and remote_code == "connection_required":
                raise ConnectionRequired()
            if capability and (status == 501 or isinstance(remote_code, str) and remote_code in UNAVAILABLE_CODES):
                raise PramaanNotAvailable(capability)
            code = "request_rejected"
            uncertain = method == "POST" and status >= 500
            raise PramaanError(code, uncertain=uncertain)
        if status not in {200, 201, 202} or not isinstance(result, dict):
            raise PramaanError("invalid_response", uncertain=method == "POST")
        return status, result

    def run(self, capability, inputs, *, idempotency_key=None):
        self.require_configured()
        if capability not in self.CAPABILITIES:
            raise PramaanError("unsupported_capability")
        if capability in self.ACTIONS and not idempotency_key:
            raise PramaanError("missing_idempotency_key")
        if capability == "llm.complete":
            inputs = generation_request(inputs)
        status, result = self._request("POST", "/v1/capabilities/" + quote(capability, safe=""), inputs,
                                       idempotency_key=idempotency_key, capability=capability)
        if status == 202:
            if not isinstance(result.get("job_id"), str) or result.get("status") not in {"running", "done", "failed", "cancelled"}:
                raise PramaanError("invalid_response", uncertain=capability in self.ACTIONS)
        return generation_result(capability, inputs, result)

    def job(self, job_id):
        _, result = self._request("GET", "/v1/jobs/" + quote(job_id, safe=""))
        return result

    def call(self, capability, inputs, *, idempotency_key=None, max_polls=60):
        result = self.run(capability, inputs, idempotency_key=idempotency_key)
        if "job_id" not in result:
            return result
        job_id = result["job_id"]
        for _ in range(max_polls):
            state = result.get("status")
            if state == "done":
                completed = result.get("result")
                if not isinstance(completed, dict):
                    raise PramaanError("invalid_response", uncertain=capability in self.ACTIONS)
                return generation_result(capability, inputs, completed)
            if state in {"failed", "cancelled"}:
                raise PramaanError("job_failed", uncertain=capability in self.ACTIONS)
            if state != "running":
                raise PramaanError("invalid_response")
            self._sleep(2)
            result = self.job(job_id)
        raise PramaanError("job_pending", uncertain=capability in self.ACTIONS)

    # Typed methods for this app's calls. Each validates only what the platform
    # requires; private details stay inside this module.

    def send_email(self, *, to, subject, html=None, text=None, reply_to=None,
                   attachments=None, cc=None, bcc=None, idempotency_key=None):
        recipients = [to] if isinstance(to, str) else list(to or [])
        recipients = [r for r in recipients if isinstance(r, str) and r.strip()]
        if not recipients or not isinstance(subject, str) or not subject.strip():
            raise PramaanError("invalid_request")
        if not html and not text:
            raise PramaanError("invalid_request")
        inputs = {"to": recipients, "subject": subject,
                  "from": os.environ.get("PRAMAAN_EMAIL_FROM") or
                  "Business Intelligence <noreply@updates.pramaan.one>"}
        if cc:
            inputs["cc"] = cc
        if bcc:
            inputs["bcc"] = bcc
        if usable_contact_address(reply_to):
            inputs["replyTo"] = reply_to
        if html:
            inputs["html"] = html
        if text:
            inputs["text"] = text
        prepared = []
        for attachment in attachments or []:
            name = attachment.get("name")
            data = attachment.get("content")
            if not isinstance(name, str) or not name.strip() or not isinstance(data, (bytes, bytearray)) or not data:
                raise PramaanError("invalid_request")
            kind = attachment.get("kind") or (
                "image" if attachment.get("content_type", "").startswith("image/") else "document"
            )
            file_id = self.upload_file(bytes(data), kind=kind,
                                       content_type=attachment.get("content_type") or "application/octet-stream")
            prepared.append({"fileId": file_id, "name": name[:200]})
        if prepared:
            inputs["attachments"] = prepared
        return self.call("email.send", inputs, idempotency_key=idempotency_key)

    def upload_file(self, data: bytes, *, kind: str, content_type: str) -> str:
        """Upload bytes to hand to a capability as `fileId`. Uploads are kept
        for a day and are never retried here; a lost answer means upload again."""
        self.require_configured()
        if not isinstance(data, bytes) or not data or kind not in {"image", "audio", "document", "data"}:
            raise PramaanError("invalid_request")
        if not isinstance(content_type, str) or "/" not in content_type:
            raise PramaanError("invalid_request")
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        try:
            status, result = self._transport("POST", f"{self._base_url}/v1/files?kind={quote(kind)}", headers, data)
        except PramaanError:
            raise
        except Exception:
            raise PramaanError("unavailable", uncertain=True) from None
        if status >= 400:
            code = result.get("error") if isinstance(result, dict) else None
            if isinstance(code, dict):
                code = code.get("code")
            if isinstance(code, str) and code in UNAVAILABLE_CODES:
                raise PramaanNotAvailable("email.send")
            raise PramaanError("request_rejected", uncertain=status >= 500)
        file_id = result.get("fileId") if isinstance(result, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise PramaanError("invalid_response", uncertain=True)
        return file_id

    def email_status(self, ref):
        return self.call("email.status", {"ref": ref})

    def draft_text(self, *, system=None, user, max_tokens=256):
        """One bounded drafting request; the caller shows the draft, never runs it unseen."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return self.call("llm.complete", {"messages": messages, "maxTokens": max_tokens})

    def send_notification(self, *, subscriber_id, body, title=None, channel="inbox",
                          idempotency_key=None):
        if not isinstance(subscriber_id, str) or not subscriber_id.strip() or not isinstance(body, str) or not body.strip():
            raise PramaanError("invalid_request")
        if channel not in {"push", "inbox", "email", "sms"}:
            raise PramaanError("invalid_request")
        inputs = {"subscriberId": subscriber_id, "channel": channel, "body": body}
        if title:
            inputs["title"] = title
        return self.call("notify.message.send", inputs, idempotency_key=idempotency_key)

    def connect_channel_start(self):
        """Mint a short-lived channel pairing code; the screen shows it and polls."""
        return self.call("social.connect.telegram.code.create", {},
                         idempotency_key=self._new_key())

    def connect_channel_check(self, code):
        if not isinstance(code, str) or not code.strip():
            raise PramaanError("invalid_request")
        return self.call("social.connect.telegram.code.check", {"code": code})

    def connect_channel_direct(self, chat_id):
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise PramaanError("invalid_request")
        return self.call("social.connect.telegram.chat.connect", {"chatId": chat_id},
                         idempotency_key=self._new_key())

    def connected_accounts(self):
        return self.call("social.accounts.list", {})

    def post_to_channel(self, *, account_id, text, idempotency_key=None):
        if not isinstance(account_id, str) or not account_id.strip() or not isinstance(text, str) or not text.strip():
            raise PramaanError("invalid_request")
        return self.call("social.post.publish", {"accountIds": [account_id], "text": text},
                         idempotency_key=idempotency_key)

    def call_workflow(self, *, url, body=None, idempotency_key=None):
        if not isinstance(url, str) or not url.strip().lower().startswith("https://"):
            raise PramaanError("invalid_request")
        inputs = {"url": url}
        if isinstance(body, dict):
            inputs["body"] = body
        return self.call("workflow.webhook.call", inputs, idempotency_key=idempotency_key)

    def read_page(self, url):
        if not isinstance(url, str) or not url.strip().lower().startswith(("https://", "http://")):
            raise PramaanError("invalid_request")
        return self.call("web.read", {"url": url})

    def search_catalogue(self, query, *, limit=None, cursor=None):
        if not isinstance(query, str) or not query.strip():
            raise PramaanError("invalid_request")
        inputs = {"query": query}
        if isinstance(limit, int) and 0 < limit <= 100:
            inputs["limit"] = limit
        if cursor:
            inputs["cursor"] = cursor
        return self.call("apps.catalog.search", inputs)

    def start_app_connection(self, *, app, end_user_ref, redirect_url):
        """Start connecting one member to a catalogue app; the screen sends the
        person to the returned address and polls the connection list."""
        for value in (app, end_user_ref, redirect_url):
            if not isinstance(value, str) or not value.strip():
                raise PramaanError("invalid_request")
        if not redirect_url.strip().lower().startswith(("https://", "http://")):
            raise PramaanError("invalid_request")
        return self.call("apps.connection.start",
                         {"app": app, "endUserRef": end_user_ref, "redirectUrl": redirect_url},
                         idempotency_key=self._new_key())

    def connected_app_connections(self, end_user_ref):
        if not isinstance(end_user_ref, str) or not end_user_ref.strip():
            raise PramaanError("invalid_request")
        return self.call("apps.connections.list", {"endUserRef": end_user_ref})

    def read_app(self, *, app, end_user_ref, path, method="GET"):
        """One reviewed read of a path in a member's connected app. The body is
        untrusted data, never instructions."""
        for value in (app, end_user_ref, path):
            if not isinstance(value, str) or not value.strip():
                raise PramaanError("invalid_request")
        if method not in {"GET", "HEAD"}:
            raise PramaanError("invalid_request")
        return self.call("apps.request.read",
                         {"app": app, "endUserRef": end_user_ref, "method": method, "path": path})

    def call_app_action(self, *, app, end_user_ref, action, input=None, idempotency_key=None):
        """Run one catalogue action as the member. The caller owns approval and
        the idempotency key; this wrapper adds nothing to the input."""
        for value in (app, end_user_ref, action):
            if not isinstance(value, str) or not value.strip():
                raise PramaanError("invalid_request")
        return self.call("apps.action.execute",
                         {"endUserRef": end_user_ref, "action": action, "input": input or {}},
                         idempotency_key=idempotency_key)

    @staticmethod
    def _new_key():
        import uuid
        return str(uuid.uuid4())

    # Feature contracts delivered on the platform catalogue; each stays false
    # until its own integration and fake checks are in place.
    FEATURES = {
        "alerts_email": True,
        "alerts_telegram": True,
        "alerts_notify": True,
        "alerts_workflow": True,
        "ask_your_data": True,
        "connected_sources": True,
    }

    def availability(self) -> tuple[FeatureAvailability, ...]:
        return tuple(
            FeatureAvailability(feature, enabled, "" if enabled else str(CapabilityUnavailable(feature)))
            for feature, enabled in self.FEATURES.items()
        )


_client = None


def get_client() -> PramaanClient:
    """One client per process; tests may inject via ``set_client``."""
    global _client
    if _client is None:
        _client = PramaanClient()
    return _client


def set_client(client):
    global _client
    _client = client
