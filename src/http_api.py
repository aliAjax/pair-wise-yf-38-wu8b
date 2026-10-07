import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    Actor,
)


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def create_handler(service, rules, static_dir, federation=None, merger=None, ledgers=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularPython/1.0"

        def log_message(self, format, *args):
            return

        def _send(self, status, payload):
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _actor(self):
            return Actor.from_headers(self.headers)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValidationError("request body must be valid JSON")
            if not isinstance(value, dict):
                raise ValidationError("request body must be a JSON object")
            return value

        def _fail(self, exc):
            if isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, (ConflictError, InvalidTransition)):
                status = 409
            elif isinstance(exc, ValidationError):
                status = 400
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._send(status, {"error": str(exc), "type": type(exc).__name__})

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.path == "/health":
                    return self._send(200, service.health())
                if parsed.path == "/":
                    index = os.path.join(static_dir, "index.html")
                    with open(index, "r", encoding="utf-8") as handle:
                        return self._send_html(200, handle.read())
                if parts == ["api", "audit"]:
                    return self._send(200, {"items": service.audit_log()})
                if len(parts) == 5 and parts[:3] == ["api", "federation", "ledgers"] and parts[4] == "grants":
                    ledger = ledgers.get(parts[3])
                    return self._send(200, {"items": ledger.list_grants()})
                if len(parts) == 5 and parts[:3] == ["api", "federation", "ledgers"] and parts[4] == "ops":
                    ledger = ledgers.get(parts[3])
                    return self._send(200, {"items": ledger.list_pending_ops()})
                if parts == ["api", "federation", "reconciliation"]:
                    query = parse_qs(parsed.query)
                    institution_id = query.get("institution", [None])[0]
                    return self._send(
                        200,
                        {"items": service.repository.list_reconciliation(institution_id)},
                    )
                if len(parts) == 4 and parts[:3] == ["api", "federation", "merges"]:
                    merge = service.repository.get_merge(parts[3])
                    if not merge:
                        raise NotFoundError("merge not found: " + parts[3])
                    merge["items"] = service.repository.list_merge_items(parts[3])
                    return self._send(200, merge)
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    return self._send(200, service.get(parts[2]))
                if len(parts) >= 2 and parts[0] == "api":
                    if parts[1] == "entities":
                        raise NotFoundError("not found")
                    if len(parts) == 3:
                        return self._send(200, service.get(parts[2]))
                    query = parse_qs(parsed.query)
                    status = query.get("status", [None])[0]
                    return self._send(
                        200,
                        {"items": service.list(parts[1], status=status)},
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                actor = self._actor()
                if (len(parts) == 7 and parts[:3] == ["api", "federation", "ledgers"]
                        and parts[6] == "register"):
                    ledger = ledgers.get(parts[3])
                    body = self._body()
                    grant_id = body.get("grant_id")
                    if not grant_id:
                        raise ValidationError("grant_id is required")
                    master = service.get(grant_id)
                    policy_version = int(master["data"].get("policy_version", 1))
                    return self._send(
                        201,
                        ledger.register_grant(
                            grant_id, data=master["data"], policy_version=policy_version
                        ),
                    )
                if (len(parts) == 7 and parts[:3] == ["api", "federation", "ledgers"]
                        and parts[6] == "activate"):
                    ledger = ledgers.get(parts[3])
                    body = self._body()
                    op_id = ledger.activate(parts[5], body.get("starts_at"), body.get("expires_at"))
                    return self._send(202, {"op_id": op_id, "state": "pending"})
                if (len(parts) == 7 and parts[:3] == ["api", "federation", "ledgers"]
                        and parts[6] == "revoke"):
                    ledger = ledgers.get(parts[3])
                    body = self._body()
                    op_id = ledger.revoke(parts[5], body.get("reason"))
                    return self._send(202, {"op_id": op_id, "state": "pending"})
                if (len(parts) == 5 and parts[:3] == ["api", "federation", "merges"]
                        and parts[4] != "run"):
                    merge = service.repository.get_merge(parts[4])
                    if not merge:
                        raise NotFoundError("merge not found: " + parts[4])
                    merge["items"] = service.repository.list_merge_items(parts[4])
                    return self._send(200, merge)
                if (len(parts) == 5 and parts[:3] == ["api", "federation", "ledgers"]
                        and parts[4] == "reconcile"):
                    ledger = ledgers.get(parts[3])
                    results = federation.reconcile_ledger(ledger)
                    return self._send(200, {"items": results})
                if parts == ["api", "federation", "merges"]:
                    body = self._body()
                    merge = merger.start_merge(
                        actor,
                        body.get("from_institution"),
                        body.get("to_institution"),
                        body.get("merge_id"),
                    )
                    return self._send(201, merge)
                if (len(parts) == 5 and parts[:3] == ["api", "federation", "merges"]
                        and parts[4] == "run"):
                    body = self._body()
                    merge, migrated = merger.run_merge(
                        actor, parts[3], body.get("limit")
                    )
                    return self._send(200, {"merge": merge, "migrated": migrated})
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    data = body.pop("data", body)
                    expected = body.pop("expected_version", None)
                    return self._send(
                        200,
                        service.transition(actor, parts[2], action, data, expected),
                    )
                if len(parts) == 4 and parts[0] == "api" and parts[3] == "actions":
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    return self._send(
                        200,
                        service.transition(
                            actor,
                            parts[2],
                            action,
                            body.pop("data", body),
                            body.pop("expected_version", None),
                        ),
                    )
                if len(parts) == 5 and parts[0] == "api" and parts[4] == "actions":
                    return self._send(
                        200,
                        service.transition(actor, parts[2], parts[3], self._body(), None),
                    )
                if len(parts) == 2 and parts[0] == "api":
                    body = self._body()
                    idem = self.headers.get("Idempotency-Key")
                    return self._send(
                        201,
                        service.create(actor, parts[1], body, idem),
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

    return Handler


def create_server(host, port, service, rules, static_dir, federation=None, merger=None, ledgers=None):
    handler = create_handler(service, rules, static_dir, federation, merger, ledgers)
    return ThreadingHTTPServer((host, int(port)), handler)
