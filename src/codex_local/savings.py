"""Token accounting for replayed local responses.

A cancelled or reset stream makes Codex re-send a turn the local model has
already answered. Codex Local replays the stored response instead of running
inference again; this module reports exactly what that avoided.
"""

from __future__ import annotations

from typing import Any


def replay_savings_fields(usage: Any) -> dict[str, int]:
    """Return exact tokens avoided by replaying a completed local response.

    A replay avoids both the cached response's prompt processing and generation.
    Missing counters stay missing rather than being estimated.
    """
    if not isinstance(usage, dict):
        return {}
    fields: dict[str, int] = {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        fields["replay_saved_input_tokens"] = max(0, input_tokens)
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        fields["replay_saved_output_tokens"] = max(0, output_tokens)
    if fields:
        fields["replay_saved_tokens"] = sum(fields.values())
    return fields
