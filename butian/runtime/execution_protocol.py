"""Authenticated loopback execution messages; no credential values in errors."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from typing import Any

PROTOCOL_VERSION = 1
# Two 4 MiB output streams can expand sixfold when JSON escapes controls.
# Leave bounded envelope room; enforce the limit on the signed wire message.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sign_message(key: bytes, executor_id: str, instance_id: str, action: str,
                 payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    if len(key) < 32:
        raise ValueError("executor_key_invalid")
    message = {"version": PROTOCOL_VERSION, "executor_id": executor_id,
               "instance_id": instance_id, "action": action, "payload": payload,
               "timestamp": time.time() if now is None else now,
               "nonce": secrets.token_hex(32)}
    encoded = canonical(message)
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("executor_message_too_large")
    message["signature"] = hmac.new(key, encoded, hashlib.sha256).hexdigest()
    if len(canonical(message)) > MAX_MESSAGE_BYTES:
        raise ValueError("executor_message_too_large")
    return message


class MessageVerifier:
    """Persist replay protection across reconnects and verifier restarts."""

    def __init__(self, db: sqlite3.Connection, key: bytes, executor_id: str,
                 instance_id: str, *, window: float = 30):
        if len(key) < 32 or not executor_id or not instance_id or not math.isfinite(window) or window <= 0:
            raise ValueError("executor_verifier_invalid")
        self.db, self._key = db, key
        self.executor_id, self.instance_id, self.window = executor_id, instance_id, window
        db.execute("CREATE TABLE IF NOT EXISTS executor_nonces (instance_id TEXT NOT NULL, nonce TEXT NOT NULL, received_at REAL NOT NULL, PRIMARY KEY(instance_id,nonce))")

    def verify(self, message: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise ValueError("executor_message_invalid")
        unsigned = {k: v for k, v in message.items() if k != "signature"}
        if set(unsigned) != {"version", "executor_id", "instance_id", "action", "payload", "timestamp", "nonce"}:
            raise ValueError("executor_message_invalid")
        try:
            encoded = canonical(unsigned)
        except (TypeError, ValueError) as exc:
            raise ValueError("executor_message_invalid") from exc
        if len(canonical(message)) > MAX_MESSAGE_BYTES:
            raise ValueError("executor_message_too_large")
        signature = message.get("signature")
        expected = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError("executor_authentication_failed")
        if unsigned["version"] != PROTOCOL_VERSION:
            raise ValueError("executor_protocol_unsupported")
        if unsigned["executor_id"] != self.executor_id or unsigned["instance_id"] != self.instance_id:
            raise ValueError("executor_identity_mismatch")
        timestamp = unsigned["timestamp"]
        current = time.time() if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp) or abs(current - timestamp) > self.window:
            raise ValueError("executor_message_expired")
        nonce = unsigned["nonce"]
        if not isinstance(nonce, str) or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
            raise ValueError("executor_nonce_invalid")
        if not isinstance(unsigned["payload"], dict) or unsigned["action"] not in {"capabilities", "start", "inspect", "events", "cancel", "renew", "revoke", "authorize"}:
            raise ValueError("executor_action_invalid")
        try:
            self.db.execute("INSERT INTO executor_nonces VALUES (?,?,?)", (self.instance_id, nonce, current))
        except sqlite3.IntegrityError as exc:
            raise ValueError("executor_message_replayed") from exc
        return unsigned
