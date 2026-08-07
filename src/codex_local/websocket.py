from __future__ import annotations

import copy
from collections import OrderedDict
import json
import uuid
from pathlib import Path
from typing import Any, Iterable

_MAX_RETAINED_RESPONSES = 64
_MAX_RETAINED_BYTES = 32 * 1024 * 1024
_MAX_ROUTE_AFFINITIES = 512


class SSEEventDecoder:
    """Incrementally decode JSON payloads from an SSE byte stream."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        events: list[bytes] = []
        while (idx := self._buffer.find(b"\n\n")) >= 0:
            block, self._buffer = self._buffer[:idx], self._buffer[idx + 2 :]
            data = b"\n".join(
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith(b"data:")
            )
            if data and data != b"[DONE]":
                events.append(data)
        return events

    def finish(self) -> list[bytes]:
        return self.feed(b"\n\n")


class ResponsesWebSocketState:
    """Expand Codex's incremental WebSocket requests for stateless local HTTP."""

    def __init__(
        self,
        *,
        spill_dir: Path | None = None,
        route_affinity: OrderedDict[str, bool] | None = None,
    ) -> None:
        self._responses: dict[str, dict[str, Any]] = {}
        self._retained_bytes = 0
        self._last_inference_route_local: bool | None = None
        # Shared by every socket owned by one interceptor process. Response IDs
        # identify a conversation chain even after Codex reconnects its socket.
        self._route_affinity = (
            route_affinity if route_affinity is not None else OrderedDict()
        )
        self._spill_dir = spill_dir
        if spill_dir is not None:
            spill_dir.mkdir(parents=True, exist_ok=True)

    @property
    def last_inference_route_local(self) -> bool | None:
        """Route affinity for maintenance requests on this Codex socket."""
        return self._last_inference_route_local

    def inference_route(self, payload: Any) -> bool | None:
        """Resolve route affinity from the referenced conversation response."""
        previous_id = (
            payload.get("previous_response_id")
            if isinstance(payload, dict)
            else None
        )
        if isinstance(previous_id, str) and previous_id in self._route_affinity:
            route = self._route_affinity.pop(previous_id)
            self._route_affinity[previous_id] = route
            return route
        return self._last_inference_route_local

    def note_inference_route(self, *, local: bool) -> None:
        """Remember only real turns; callers intentionally exclude prewarms."""
        self._last_inference_route_local = bool(local)

    def bind_response_route(self, response_id: str | None, *, local: bool) -> None:
        """Bind a completed response to its route for future socket reconnects."""
        if not isinstance(response_id, str) or not response_id:
            return
        self._route_affinity.pop(response_id, None)
        self._route_affinity[response_id] = bool(local)
        while len(self._route_affinity) > _MAX_ROUTE_AFFINITIES:
            self._route_affinity.popitem(last=False)

    @staticmethod
    def is_response_create(payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("type") == "response.create"

    @staticmethod
    def is_prewarm(payload: Any) -> bool:
        return ResponsesWebSocketState.is_response_create(payload) and payload.get(
            "generate"
        ) is False

    def acknowledge_prewarm(self, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        response_id = "resp_local_warm_" + uuid.uuid4().hex
        request = self._http_request(payload)
        # `request` is a fresh deep copy owned by this entry, so the context
        # can share its input items instead of copying them a second time.
        input_items = request.get("input")
        context = list(input_items) if isinstance(input_items, list) else []
        self._store(response_id, request, context)
        usage = {
            "input_tokens": 0,
            "input_tokens_details": None,
            "output_tokens": 0,
            "output_tokens_details": None,
            "total_tokens": 0,
        }
        return response_id, [
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.completed",
                "response": {"id": response_id, "usage": usage},
            },
        ]

    def expand(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._http_request(payload)
        previous_id = payload.get("previous_response_id")
        previous = self._responses.get(previous_id) if isinstance(previous_id, str) else None
        if previous is None:
            # An entry evicted by the retention caps may have been spilled to
            # disk; reload it so a later turn does not lose the conversation and
            # force a full-context re-send.
            previous = (
                self._load_spilled(previous_id) if isinstance(previous_id, str) else None
            )
        if previous is None:
            return request

        # The retained request's "input" duplicates its "context", so skip it
        # here and deep-copy the context once below. Values from `request`
        # are fresh deep copies owned by this call and can move across as-is.
        expanded = {
            key: copy.deepcopy(value)
            for key, value in previous["request"].items()
            if key != "input"
        }
        for key, value in request.items():
            if key != "input":
                expanded[key] = value
        prior_context = previous.get("context")
        incremental = request.get("input")
        if isinstance(prior_context, list) and isinstance(incremental, list):
            # Retained items are deep-copied so later caller mutations of the
            # expanded request can never corrupt the stored conversation.
            expanded["input"] = copy.deepcopy(prior_context) + incremental
        else:
            expanded["input"] = incremental
        return expanded

    def remember(
        self,
        request: dict[str, Any],
        events: Iterable[dict[str, Any]],
        *,
        route_local: bool | None = None,
    ) -> str | None:
        response_id: str | None = None
        completed_output: list[Any] | None = None
        done_output: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "response.output_item.done" and "item" in event:
                done_output.append(copy.deepcopy(event["item"]))
            if event.get("type") == "response.completed":
                response = event.get("response")
                candidate = response.get("id") if isinstance(response, dict) else None
                if isinstance(candidate, str) and candidate:
                    response_id = candidate
                output = response.get("output") if isinstance(response, dict) else None
                if isinstance(output, list):
                    completed_output = copy.deepcopy(output)
        if response_id is None:
            return None
        # Some OpenAI-compatible servers emit only response.completed and omit
        # response.output_item.done entirely. Treat completed.response.output as
        # authoritative, supplementing it with any richer done items that are not
        # already present. Otherwise the next function_call_output is reconstructed
        # without its function_call and local chat templates reject the history.
        output_items = _merge_response_output(done_output, completed_output or [])
        stored_request = copy.deepcopy(request)
        input_items = stored_request.get("input")
        # The stored request and its context share the same input items: the
        # single deep copy above serves both, and expand() copies retained
        # state again before handing it back to callers.
        context = list(input_items) if isinstance(input_items, list) else []
        context.extend(output_items)
        self._store(
            response_id, stored_request, context, extra_size=_estimate_size(output_items)
        )
        if route_local is not None:
            self.bind_response_route(response_id, local=route_local)
        return response_id

    def _store(
        self,
        response_id: str,
        request: dict[str, Any],
        context: list[Any],
        *,
        extra_size: int = 0,
    ) -> None:
        previous = self._responses.pop(response_id, None)
        if previous is not None:
            self._retained_bytes -= previous.get("size", 0)
        size = _estimate_size(request) + extra_size
        self._responses[response_id] = {
            "request": request,
            "context": context,
            "size": size,
        }
        self._retained_bytes += size
        # Cap retention by count and by estimated serialized bytes. The newest
        # entry is never evicted, even if it alone exceeds the byte ceiling.
        while len(self._responses) > _MAX_RETAINED_RESPONSES or (
            self._retained_bytes > _MAX_RETAINED_BYTES and len(self._responses) > 1
        ):
            oldest = next(iter(self._responses))
            entry = self._responses.pop(oldest, None)
            if entry is not None:
                self._retained_bytes -= entry.get("size", 0)
                self._spill(oldest, entry)

    def _spill(self, response_id: str, entry: dict[str, Any]) -> None:
        """Persist an evicted conversation entry to disk instead of dropping it.

        The retention caps are in-memory ceilings; a spilled entry is reloaded
        by expand() when a later turn references it, so history survives a long
        conversation without unbounded memory growth.
        """
        if self._spill_dir is None:
            return
        try:
            path = self._spill_dir / (response_id.replace("/", "_") + ".json")
            path.write_text(
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            pass

    def _load_spilled(self, response_id: str) -> dict[str, Any] | None:
        if self._spill_dir is None:
            return None
        try:
            path = self._spill_dir / (response_id.replace("/", "_") + ".json")
            if not path.is_file():
                return None
            decoded = json.loads(path.read_text(encoding="utf-8"))
            return decoded if isinstance(decoded, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _http_request(payload: dict[str, Any]) -> dict[str, Any]:
        request = copy.deepcopy(payload)
        for key in ("type", "generate", "client_metadata", "previous_response_id"):
            request.pop(key, None)
        request["stream"] = True
        return request


def _estimate_size(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return 0


def _merge_response_output(done: list[Any], completed: list[Any]) -> list[Any]:
    merged: list[Any] = []
    positions: dict[str, int] = {}

    def key(item: Any) -> str:
        if isinstance(item, dict):
            item_type = item.get("type")
            call_id = item.get("call_id")
            if (
                item_type
                in {
                    "function_call",
                    "custom_tool_call",
                    "computer_call",
                    "tool_search_call",
                }
                and isinstance(call_id, str)
                and call_id
            ):
                return f"call:{item_type}:{call_id}"
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                return "id:" + item_id
            if isinstance(call_id, str) and call_id:
                return f"call:{item_type}:{call_id}"
        try:
            return "json:" + json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return "repr:" + repr(item)

    # Completed output is the final ordered response. Done events can contain
    # fuller per-item fields, so replace matching completed items with those.
    for item in completed:
        item_key = key(item)
        positions[item_key] = len(merged)
        merged.append(copy.deepcopy(item))
    for item in done:
        item_key = key(item)
        position = positions.get(item_key)
        if position is None:
            positions[item_key] = len(merged)
            merged.append(copy.deepcopy(item))
        else:
            merged[position] = copy.deepcopy(item)
    return merged


def decode_json_events(payloads: Iterable[bytes]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return events
