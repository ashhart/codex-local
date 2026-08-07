"""Codex Local — run Codex on your own local model.

Codex Local starts the Codex app or CLI behind a process-scoped HTTPS proxy that
recognises exactly one model slot. Requests for that slot are served by a
private endpoint you already run; every other request, including the ones that
carry Projects, plugins, automations and account state, reaches its original
destination unchanged. Codex configuration is never edited.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
