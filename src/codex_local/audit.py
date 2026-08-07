from __future__ import annotations

import base64
import binascii
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable, cast


AUDIT_SCHEMA_VERSION = 2
SENSITIVE_AUDIT_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
}


class AuditRecorder:
    """Owner-only, content-addressed ledger for observable inference traffic."""

    def __init__(
        self,
        root: Path,
        session_id: str,
        *,
        include_credentials: bool = False,
        request_bodies_redacted: bool = False,
    ) -> None:
        self.session_id = session_id
        self.include_credentials = include_credentials
        self.request_bodies_redacted = request_bodies_redacted
        self.root = root.expanduser().resolve() / session_id
        self.blob_dir = self.root / "blobs"
        self.events_path = self.root / "events.jsonl"
        self.manifest_path = self.root / "manifest.json"
        self._lock = threading.Lock()
        self._sequence = 0
        self._previous_hash: str | None = None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blob_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.blob_dir, 0o700)
        self.events_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.events_path, 0o600)
        self._restore_chain()
        if not self.manifest_path.exists():
            self._write_manifest()

    def record_bytes(
        self,
        event: str,
        data: bytes | bytearray | memoryview | str,
        **fields: Any,
    ) -> dict[str, Any]:
        raw = _as_bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        blob_path = self.blob_dir / f"{digest}.bin"
        with self._lock:
            if not blob_path.exists():
                temporary = self.blob_dir / f".{digest}.{os.getpid()}.tmp"
                with temporary.open("wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                try:
                    temporary.replace(blob_path)
                finally:
                    temporary.unlink(missing_ok=True)
                os.chmod(blob_path, 0o600)
            self._sequence += 1
            payload = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "sequence": self._sequence,
                "time": time.time(),
                "time_ns": time.time_ns(),
                "event": event,
                "session_id": self.session_id,
                "byte_length": len(raw),
                "sha256": digest,
                "blob": f"blobs/{digest}.bin",
                "previous_event_sha256": self._previous_hash,
                **_json_safe(fields),
            }
            event_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
            payload["event_sha256"] = event_hash
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.events_path, 0o600)
            self._previous_hash = event_hash
            return payload

    def record_json(self, event: str, value: Any, **fields: Any) -> dict[str, Any]:
        return self.record_bytes(
            event,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            content_type="application/json",
            **fields,
        )

    def _restore_chain(self) -> None:
        try:
            last = self.events_path.read_text(encoding="utf-8").splitlines()[-1]
        except (IndexError, OSError):
            return
        try:
            decoded = json.loads(last)
        except json.JSONDecodeError:
            return
        sequence = decoded.get("sequence")
        event_hash = decoded.get("event_sha256")
        if isinstance(sequence, int) and sequence >= 0:
            self._sequence = sequence
        if isinstance(event_hash, str) and event_hash:
            self._previous_hash = event_hash

    def _write_manifest(self) -> None:
        manifest = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": time.time(),
            "capture_scope": "observable inference traffic",
            "capture_boundary": (
                "decrypted application data exposed to the interceptor; "
                "local inference is excluded"
            ),
            "body_storage": (
                "hosted request bodies are replaced by structural redaction records; "
                "response bodies remain byte-exact content-addressed blobs"
                if self.request_bodies_redacted
                else "byte-exact content-addressed blobs"
            ),
            "http_fidelity": (
                "payload bytes plus headers and transport metadata exposed by mitmproxy; "
                "TLS records and HTTP/2 header compression are not exposed"
            ),
            "websocket_fidelity": (
                "complete text/binary application messages exposed by mitmproxy; "
                "TLS records, masking keys, and compression framing are not exposed"
            ),
            "credential_header_values": (
                "included; archive contains reusable account credentials"
                if self.include_credentials
                else "excluded"
            ),
            "reasoning_capture": (
                "hosted request reasoning configuration is redacted; reasoning "
                "disclosed in provider responses is retained"
                if self.request_bodies_redacted
                else (
                    "all reasoning material actually disclosed on the intercepted "
                    "transport is retained; undisclosed provider-internal reasoning "
                    "cannot be reconstructed from client traffic"
                )
            ),
            "reasoning_summaries": "captured when present",
            "disclosed_reasoning_text": "captured byte-for-byte when present",
            "encrypted_reasoning_items": (
                "captured byte-for-byte when present and classified structurally; "
                "ciphertext remains opaque without the provider-held key"
            ),
            "tamper_evidence": (
                "SHA-256 event chain and blob digests; self-verifying, not externally anchored"
            ),
        }
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.manifest_path)
        os.chmod(self.manifest_path, 0o600)


def redact_cloud_request_body(data: bytes | str) -> bytes:
    """Replace a hosted request body with content-free structural telemetry."""
    raw = _as_bytes(data)
    payloads = _decoded_json_payloads(raw)
    payload = next((item for item in payloads if isinstance(item, dict)), None)
    structure: dict[str, Any] = {
        "schema_version": 1,
        "request_body_redacted": True,
        "original_byte_length": len(raw),
        "encoding": "structural_metadata_only",
    }
    if payload is not None:
        for key in ("type", "model", "stream", "generate"):
            value = payload.get(key)
            if isinstance(value, (str, bool)):
                structure[key] = value
        structure["has_previous_response_id"] = isinstance(
            payload.get("previous_response_id"), str
        )
        for key in ("input", "tools", "context_management"):
            value = payload.get(key)
            if isinstance(value, list):
                structure[f"{key}_item_count"] = len(value)
            elif value is not None:
                structure[f"has_{key}"] = True
    return json.dumps(
        structure,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def safe_audit_headers(
    headers: Any,
    *,
    include_credentials: bool = False,
) -> list[dict[str, str]]:
    """Return header order/names with optional reusable credentials."""
    result: list[dict[str, str]] = []
    for name, value in _header_items(headers):
        normalized = str(name).lower()
        sensitive = (
            normalized in SENSITIVE_AUDIT_HEADERS
            or normalized.startswith("x-openai-")
            or "token" in normalized
            or "secret" in normalized
            or "api-key" in normalized
        )
        result.append(
            {
                "name": str(name),
                "value": (
                    "<credential-excluded>"
                    if sensitive and not include_credentials
                    else str(value)
                ),
            }
        )
    return result


def audit_header_bytes(headers: Any, *, include_credentials: bool = False) -> bytes:
    lines = []
    for item in safe_audit_headers(
        headers,
        include_credentials=include_credentials,
    ):
        lines.append(f"{item['name']}: {item['value']}\r\n")
    return "".join(lines).encode("utf-8", errors="surrogateescape")


def is_cloud_auxiliary_inference_request(
    method: str,
    host: str,
    path: str,
) -> bool:
    """Identify hosted inference endpoints outside the Codex Responses route."""
    clean_path = str(path).split("?", 1)[0]
    return (
        str(method).upper() == "POST"
        and str(host).lower() in {"chatgpt.com", "chat.openai.com"}
        and clean_path.startswith("/backend-api/codex/images/")
    )


def inspect_fernet_token(value: str) -> dict[str, Any]:
    """Classify a token by Fernet's public envelope shape without decrypting it."""
    result: dict[str, Any] = {
        "fernet_shaped": False,
        "authenticated": False,
    }
    if not isinstance(value, str) or not value:
        return result
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return result
    # Fernet is: version(1), timestamp(8), IV(16), AES-CBC ciphertext
    # (one or more 16-byte blocks), and HMAC-SHA256(32).
    if (
        len(decoded) < 73
        or decoded[0] != 0x80
        or (len(decoded) - 57) % 16 != 0
    ):
        return result
    timestamp = int.from_bytes(decoded[1:9], "big")
    result.update(
        {
            "fernet_shaped": True,
            "version": decoded[0],
            "timestamp": timestamp,
            "ciphertext_bytes": len(decoded) - 57,
            "hmac_bytes": 32,
            # Envelope recognition does not authenticate or decrypt the token.
            "authenticated": False,
        }
    )
    return result


def analyze_reasoning_evidence(data: bytes | str) -> dict[str, int]:
    """Count observable reasoning evidence in JSON or SSE application payloads."""
    counters: Counter[str] = Counter()
    for payload in _decoded_json_payloads(_as_bytes(data)):
        _scan_reasoning_value(payload, counters)
    return {
        "request_reasoning_configurations": counters[
            "request_reasoning_configurations"
        ],
        "reasoning_summaries": counters["reasoning_summaries"],
        "disclosed_reasoning_text": counters["disclosed_reasoning_text"],
        "encrypted_reasoning_items": counters["encrypted_reasoning_items"],
        "fernet_shaped_encrypted_items": counters[
            "fernet_shaped_encrypted_items"
        ],
        "reasoning_token_reports": counters["reasoning_token_reports"],
        "reasoning_tokens_reported": counters["reasoning_tokens_reported"],
        "terminal_response_events": counters["terminal_response_events"],
    }


def verify_audit_session(path: Path) -> dict[str, Any]:
    session = path.expanduser().resolve()
    events_path = session / "events.jsonl"
    previous: str | None = None
    count = 0
    total_bytes = 0
    unique_blobs: set[str] = set()
    errors: list[str] = []
    warnings: list[str] = []
    event_counts: Counter[str] = Counter()
    direction_counts: Counter[str] = Counter()
    reasoning_evidence: Counter[str] = Counter()
    _check_private_mode(session, 0o700, "session directory", errors)
    _check_private_mode(events_path, 0o600, "events ledger", errors)
    _check_private_mode(session / "manifest.json", 0o600, "manifest", errors)
    for line_number, line in enumerate(
        events_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON ({exc})")
            continue
        claimed = event.pop("event_sha256", None)
        actual = hashlib.sha256(_canonical_json(event)).hexdigest()
        if claimed != actual:
            errors.append(f"line {line_number}: event hash mismatch")
        if event.get("previous_event_sha256") != previous:
            errors.append(f"line {line_number}: chain link mismatch")
        previous = claimed if isinstance(claimed, str) else None
        event_name = event.get("event")
        if isinstance(event_name, str):
            event_counts[event_name] += 1
        direction = event.get("direction")
        if isinstance(direction, str):
            direction_counts[direction] += 1
        blob = event.get("blob")
        digest = event.get("sha256")
        if isinstance(blob, str) and isinstance(digest, str):
            blob_path = session / blob
            try:
                raw = blob_path.read_bytes()
            except OSError as exc:
                errors.append(f"line {line_number}: blob unavailable ({exc})")
            else:
                if hashlib.sha256(raw).hexdigest() != digest:
                    errors.append(f"line {line_number}: blob hash mismatch")
                if len(raw) != event.get("byte_length"):
                    errors.append(f"line {line_number}: blob length mismatch")
                _check_private_mode(blob_path, 0o600, "blob", errors)
                unique_blobs.add(digest)
                for key, value in analyze_reasoning_evidence(raw).items():
                    reasoning_evidence[key] += value
        count += 1
        byte_length = event.get("byte_length")
        if isinstance(byte_length, int):
            total_bytes += byte_length
    all_blobs = {
        item.stem
        for item in (session / "blobs").glob("*.bin")
        if item.is_file()
    }
    orphan_blobs = sorted(all_blobs - unique_blobs)
    if orphan_blobs:
        warnings.append(
            f"{len(orphan_blobs)} unreferenced blob(s), possibly from an interrupted write"
        )
    return {
        "ok": not errors,
        "session": str(session),
        "events": count,
        "referenced_bytes": total_bytes,
        "unique_blobs": len(unique_blobs),
        "event_counts": dict(sorted(event_counts.items())),
        "direction_counts": dict(sorted(direction_counts.items())),
        "reasoning_evidence": dict(sorted(reasoning_evidence.items())),
        "orphan_blobs": len(orphan_blobs),
        "errors": errors,
        "warnings": warnings,
    }


def _check_private_mode(
    path: Path,
    expected: int,
    label: str,
    errors: list[str],
) -> None:
    try:
        actual = path.stat().st_mode & 0o777
    except OSError as exc:
        errors.append(f"{label} unavailable ({exc})")
        return
    if actual != expected:
        errors.append(
            f"{label} permissions are {actual:04o}; expected {expected:04o}"
        )

def _decoded_json_payloads(raw: bytes) -> list[Any]:
    payloads: list[Any] = []
    try:
        payloads.append(json.loads(raw))
        return payloads
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            continue
        candidate = stripped[5:].strip()
        if not candidate or candidate == b"[DONE]":
            continue
        try:
            payloads.append(json.loads(candidate))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return payloads


def _scan_reasoning_value(value: Any, counters: Counter[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _scan_reasoning_value(item, counters)
        return
    if not isinstance(value, dict):
        return
    item_type = value.get("type")
    if isinstance(item_type, str):
        if item_type.startswith("response.reasoning_summary"):
            counters["reasoning_summaries"] += 1
        if item_type in {"summary_text", "reasoning_summary_text"}:
            counters["reasoning_summaries"] += 1
        if item_type.startswith("response.reasoning_text") or item_type in {
            "reasoning_text",
            "raw_reasoning_text",
        }:
            counters["disclosed_reasoning_text"] += 1
        if item_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        }:
            counters["terminal_response_events"] += 1
    reasoning = value.get("reasoning")
    if isinstance(reasoning, dict):
        counters["request_reasoning_configurations"] += 1
    summary = value.get("summary")
    if isinstance(summary, (str, list)) and summary:
        counters["reasoning_summaries"] += 1
    raw_content = value.get("raw_content")
    if isinstance(raw_content, str) and raw_content:
        counters["disclosed_reasoning_text"] += 1
    encrypted = value.get("encrypted_content")
    if isinstance(encrypted, str) and encrypted:
        counters["encrypted_reasoning_items"] += 1
        if inspect_fernet_token(encrypted)["fernet_shaped"]:
            counters["fernet_shaped_encrypted_items"] += 1
    for key in ("reasoning_tokens", "reasoning_output_tokens"):
        token_count = value.get(key)
        if isinstance(token_count, int) and not isinstance(token_count, bool):
            counters["reasoning_token_reports"] += 1
            counters["reasoning_tokens_reported"] += token_count
    for item in value.values():
        _scan_reasoning_value(item, counters)


def _header_items(headers: Any) -> Iterable[tuple[Any, Any]]:
    if headers is None:
        return []
    items = getattr(headers, "items", None)
    if callable(items):
        try:
            return list(cast(Iterable[tuple[Any, Any]], items(multi=True)))
        except TypeError:
            return list(cast(Iterable[tuple[Any, Any]], items()))
    if isinstance(headers, dict):
        return list(headers.items())
    return list(headers)


def _as_bytes(value: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
