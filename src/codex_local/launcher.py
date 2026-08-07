"""Launch Codex behind the Codex Local proxy.

The launcher picks a model, starts mitmproxy with the Codex Local addon, and hands
the Codex app or CLI a process-scoped proxy and CA environment. Nothing outside
that child process is modified: no Codex config, no system proxy, no keychain.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen

from .paths import config_file as _default_config_file
from .paths import ensure_private_dir, legacy_config_paths
from .paths import runtime_dir as _default_runtime_dir
from .routing import (
    codex_thread_url,
    format_interceptor_event,
    interceptor_status,
    list_opencode_interceptor_choices,
    list_pi_interceptor_choices,
    load_opencode_selection,
    load_pi_selection,
    pi_model_context_window,
    select_lowest_visible_codex_model,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
ADDON_PATH = PACKAGE_ROOT / "addon.py"
MENU_SOURCE_PATH = PACKAGE_ROOT / "menubar" / "CodexLocalMenu.swift"
DEFAULT_RUNTIME_DIR = _default_runtime_dir()
LAST_SELECTION_FILE = "last-selection.json"
OMLX_SETTINGS_PATH = Path.home() / ".omlx" / "settings.json"
DEFAULT_MODELS_PATH = _default_config_file()
# Codex ranks its own models; Codex Local claims the lowest-ranked one visible in
# the app, because that is the slot a user is least likely to want for hosted
# work. The name comes from Codex's own catalogue, which Codex Local caches the
# first time Codex fetches it through the proxy. This constant is only the
# bootstrap guess for the run before any catalogue has been seen, and is
# overridden by --local-slot or config `routing.local_slot`.
BOOTSTRAP_LOCAL_SLOT = "gpt-5.3-codex-spark"
DEFAULT_LOCAL_LABEL = "Local"
MODEL_CATALOG_CACHE_FILE = "codex-local-model-catalog.json"
CODEX_LAB_FEATURES = (
    "multi_agent_v2",
    "concurrent_reasoning_summaries",
    "executor_capability_discovery",
    "default_mode_request_user_input",
    "terminal_visualization_instructions",
    "prevent_idle_sleep",
    "runtime_metrics",
)
CODEX_LAB_CONFIG_OVERRIDES = (
    "show_raw_agent_reasoning=true",
    "hide_agent_reasoning=false",
    'model_reasoning_summary="detailed"',
    "suppress_unstable_features_warning=true",
)
TOOL_CANARY_CACHE_TTL_SECONDS = 24 * 60 * 60
MODEL_SOURCES = ("catalog", "pi", "opencode", "omlx")
DEFAULT_MODEL_IDLE_UNLOAD_SECONDS = float(
    os.environ.get("CODEX_LOCAL_MODEL_IDLE_UNLOAD_SECONDS", str(15 * 60))
)


@dataclass(frozen=True)
class LocalSelection:
    provider: str
    server: str
    model: str
    base_url: str
    api_key: str | None
    auth_header: bool = True
    backend: str = "openai-responses"
    command: str | None = None

    @property
    def responses_url(self) -> str:
        return self.base_url.rstrip("/") + "/responses"


def _local_label_for_selection(
    selection: LocalSelection,
    *,
    configured_label: str | None = None,
) -> str:
    """Build the label Codex shows for the claimed slot.

    Codex renders the slot name in a narrow space, so a long model id is
    truncated rather than allowed to push the rest of the line out of view.
    """
    if configured_label and configured_label != DEFAULT_LOCAL_LABEL:
        return configured_label
    server = selection.server
    model = Path(selection.model).name
    if len(model) > 24:
        model = model[:23].rstrip("-_. ") + "…"
    return f"Local · {server} · {model}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Codex through a process-scoped HTTPS interceptor without editing "
            "Codex config, provider identity, plugins, projects, or automations."
        )
    )
    sub = parser.add_subparsers(dest="command")
    interactive = sub.add_parser("interactive")
    interactive.add_argument("--models-path", default=str(DEFAULT_MODELS_PATH))
    interactive.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    interactive.add_argument(
        "--project",
        help="Directory to open in Codex. Defaults to the current directory.",
    )
    interactive.add_argument("--verbose", action="store_true")
    sub.add_parser("doctor")
    config_command = sub.add_parser("config")
    config_command.add_argument("--models-path", default=str(DEFAULT_MODELS_PATH))
    config_command.add_argument(
        "--init",
        action="store_true",
        help="Write a starting configuration file if none exists.",
    )
    status = sub.add_parser("status")
    status.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    attest = sub.add_parser("attest-desktop")
    attest.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))

    for name in ("plan", "serve", "exec", "app", "cli"):
        command = sub.add_parser(name)
        command.add_argument("--server")
        command.add_argument("--model", required=True)
        command.add_argument(
            "--source",
            choices=MODEL_SOURCES,
            default="catalog",
            help=(
                "Resolve the model from Codex Local's own configuration, or from "
                "Pi or OpenCode configuration."
            ),
        )
        command.add_argument("--project", default=".")
        command.add_argument("--listen-port", type=int, default=8877)
        command.add_argument("--models-path", default=str(DEFAULT_MODELS_PATH))
        command.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
        command.add_argument("--local-slot")
        command.add_argument("--local-label")
        command.add_argument("--local-slot-auto", action="store_true", help=argparse.SUPPRESS)
    sub.choices["exec"].add_argument(
        "prompt", nargs=argparse.REMAINDER, help="Prompt after an optional -- separator"
    )
    sub.choices["app"].add_argument(
        "--allow-running-app",
        action="store_true",
        help="Launch a second app instance even when ChatGPT/Codex is already running.",
    )
    sub.choices["app"].add_argument(
        "--no-attestation",
        action="store_true",
        help="Skip the desktop acceptance instructions and post-exit checklist.",
    )
    sub.choices["app"].add_argument(
        "--no-menubar",
        action="store_true",
        help="Do not start the optional macOS menu-bar status controller.",
    )
    sub.choices["app"].add_argument(
        "--lab-mode",
        "--god-mode",
        dest="lab_mode",
        action="store_true",
        help=(
            "Enable process-local Codex lab features and detailed reasoning summaries "
            "without editing config.toml."
        ),
    )
    sub.choices["cli"].add_argument(
        "--plain",
        dest="lab_mode",
        action="store_false",
        help="Disable process-local Codex lab features for this CLI launch.",
    )
    sub.choices["cli"].set_defaults(lab_mode=True)
    for name in ("serve", "exec", "app", "cli"):
        sub.choices[name].add_argument(
            "--live",
            action="store_true",
            help="Show a privacy-safe live request dashboard.",
        )
        sub.choices[name].add_argument(
            "--skip-warmup",
            action="store_true",
            help=argparse.SUPPRESS,
        )
        sub.choices[name].add_argument(
            "--warm-model",
            action="append",
            default=[],
            help=(
                "Also warm this model concurrently; it must be configured on "
                "the selected model's server. May be repeated."
            ),
        )
        sub.choices[name].add_argument(
            "--idle-unload-seconds",
            type=float,
            default=DEFAULT_MODEL_IDLE_UNLOAD_SECONDS,
            help="Unload the selected oMLX model after this idle period; 0 disables.",
        )
        sub.choices[name].add_argument(
            "--verbose",
            action="store_true",
            help="Print each privacy-safe intercepted request instead of the quiet display.",
        )

    args = parser.parse_args()
    if args.command in {None, "interactive"}:
        return _interactive(
            models_path=getattr(args, "models_path", str(DEFAULT_MODELS_PATH)),
            runtime_dir=getattr(args, "runtime_dir", str(DEFAULT_RUNTIME_DIR)),
            project_dir=getattr(args, "project", None),
            verbose=getattr(args, "verbose", False),
        )
    if args.command == "doctor":
        return _doctor()
    if args.command == "config":
        return _config_command(
            Path(args.models_path).expanduser().resolve(),
            initialise=args.init,
        )
    if args.command == "status":
        print(json.dumps(interceptor_status(runtime_dir=args.runtime_dir), indent=2))
        return 0
    if args.command == "attest-desktop":
        return _collect_desktop_attestation(
            Path(args.runtime_dir).expanduser().resolve()
        )
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    routing = _load_hybrid_routing(Path(args.models_path).expanduser().resolve())
    local_slot = args.local_slot or _resolve_local_slot(
        runtime_dir, configured_slot=routing.get("local_slot")
    )
    local_slot_auto = args.local_slot_auto or (
        args.local_slot is None
        and routing.get("local_slot", "auto").lower() in {"auto", "lowest"}
    )
    selection = _load_selection(
        args.model,
        server=args.server,
        source=args.source,
        models_path=args.models_path,
    )
    local_label = args.local_label or _local_label_for_selection(
        selection,
        configured_label=routing.get("display_name"),
    )
    plan = _build_omlx_plan(
        project=args.project,
        server=args.server,
        model=args.model,
        source=args.source,
        listen_port=args.listen_port,
        models_path=args.models_path,
        local_slot=local_slot,
        local_label=local_label,
    )
    if args.command == "plan":
        print(json.dumps(plan, indent=2))
        return 0

    session_id = f"interceptor-{uuid.uuid4().hex[:12]}"
    codex_config_path = Path.home() / ".codex" / "config.toml"
    codex_config_sha256_before = _sha256_file(codex_config_path)
    _write_session_receipt(
        runtime_dir,
        {
            "schema_version": 1,
            "session_id": session_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "phase": "proxy_starting",
            "command": args.command,
            "provider": plan["provider"],
            "source": plan["source"],
            "model": plan["model"],
            "project": plan["project"],
            "listen_host": plan["listen_host"],
            "listen_port": plan["listen_port"],
            "preserves_provider_identity": True,
            "passes_through_non_inference_requests": True,
            "local_slot": local_slot,
            "local_label": local_label,
            "warm_models": list(getattr(args, "warm_model", [])),
            "idle_unload_seconds": max(0.0, args.idle_unload_seconds),
            "codex_config_path": str(codex_config_path),
            "codex_config_sha256_before": codex_config_sha256_before,
        },
    )
    warmup_selections = _resolve_warmup_selections(
        selection,
        getattr(args, "warm_model", []),
        source=args.source,
        models_path=args.models_path,
    )
    warmups = (
        []
        if getattr(args, "skip_warmup", False)
        else _start_model_warmups(warmup_selections, runtime_dir=runtime_dir)
    )
    try:
        proxy = _start_proxy(
            selection=selection,
            listen_port=args.listen_port,
            runtime_dir=runtime_dir,
            session_id=session_id,
            local_slot=local_slot,
            local_label=local_label,
            local_slot_auto=local_slot_auto,
            idle_unload_seconds=max(0.0, args.idle_unload_seconds),
        )
    except Exception as exc:
        _update_session_receipt(
            runtime_dir,
            phase="proxy_start_failed",
            error_type=type(exc).__name__,
        )
        raise
    _update_session_receipt(
        runtime_dir,
        phase="proxy_ready",
        proxy_pid=proxy.pid,
    )
    dashboard = None
    menu_process = None
    control_loop = None
    idle_unloaders = [
        ModelIdleUnloader(
            selection=managed_selection,
            status_path=runtime_dir / "status.jsonl",
            session_id=session_id,
            idle_seconds=max(0.0, args.idle_unload_seconds),
            busy_threads=warmups,
        )
        for managed_selection in warmup_selections
    ]
    for idle_unloader in idle_unloaders:
        idle_unloader.start()
    if _should_start_dashboard(
        command=args.command,
        live=getattr(args, "live", False),
    ):
        dashboard = LiveDashboard(
            status_path=runtime_dir / "status.jsonl",
            session_id=session_id,
            provider=str(plan["provider"]),
            server=str(plan["server"]),
            model=str(plan["model"]),
            project=str(plan["project"]),
            command=args.command,
            listen_port=int(str(plan["listen_port"])),
            local_slot=local_slot,
            local_label=local_label,
            verbose=getattr(args, "verbose", False),
        )
        dashboard.start()
    if args.command == "app" and not getattr(args, "no_menubar", False):
        menu_process = _start_menu_bar(runtime_dir)
        control_loop = LocalControlLoop(selection=selection, runtime_dir=runtime_dir)
        control_loop.start()
    try:
        cert_path = runtime_dir / "mitmproxy" / "mitmproxy-ca-cert.pem"
        child_env = _codex_environment(args.listen_port, cert_path)
        if args.command == "serve":
            _update_session_receipt(runtime_dir, phase="proxy_serving")
            print(
                json.dumps(
                    {
                        **plan,
                        "session_id": session_id,
                        "proxy_pid": proxy.pid,
                        "ca_certificate": str(cert_path),
                        "status_path": str(runtime_dir / "status.jsonl"),
                        "ready": True,
                    },
                    indent=2,
                ),
                flush=True,
            )
            exit_code = _wait_for_proxy(proxy)
            _record_exit(runtime_dir, phase="proxy_exited", exit_code=exit_code)
            return exit_code
        if args.command == "exec":
            prompt = list(args.prompt)
            if prompt and prompt[0] == "--":
                prompt = prompt[1:]
            if not prompt:
                raise ValueError("exec requires a prompt after --")
            codex = _required_command("codex")
            command = [
                codex,
                "exec",
                "--skip-git-repo-check",
                "-C",
                str(Path(args.project).expanduser().resolve()),
                " ".join(prompt),
            ]
            _update_session_receipt(runtime_dir, phase="cli_running")
            exit_code = subprocess.run(command, env=child_env, check=False).returncode
            _record_exit(runtime_dir, phase="cli_exited", exit_code=exit_code)
            return exit_code
        if args.command == "cli":
            codex = _codex_cli_executable(
                runtime_dir=runtime_dir,
                lab_mode=args.lab_mode,
            )
            command = [
                codex,
                "-C",
                str(Path(args.project).expanduser().resolve()),
            ]
            _update_session_receipt(
                runtime_dir,
                phase="tui_running",
                codex_lab_mode=args.lab_mode,
                codex_lab_features=(list(CODEX_LAB_FEATURES) if args.lab_mode else []),
            )
            exit_code = subprocess.run(command, env=child_env, check=False).returncode
            _record_exit(runtime_dir, phase="tui_exited", exit_code=exit_code)
            return exit_code
        if args.command == "app":
            return _launch_app(
                project=args.project,
                child_env=child_env,
                allow_running=args.allow_running_app,
                runtime_dir=runtime_dir,
                collect_attestation=not args.no_attestation,
                lab_mode=args.lab_mode,
            )
        raise AssertionError(f"unhandled command {args.command}")
    finally:
        for warmup in warmups:
            warmup.join(timeout=0.1)
        for idle_unloader in idle_unloaders:
            idle_unloader.stop()
        if dashboard is not None:
            dashboard.stop()
        if control_loop is not None:
            control_loop.stop()
        if menu_process is not None:
            _terminate(menu_process)
        _terminate(proxy)
        codex_config_sha256_after = _sha256_file(codex_config_path)
        _update_session_receipt(
            runtime_dir,
            proxy_stopped_at=time.time(),
            codex_config_sha256_after=codex_config_sha256_after,
            codex_config_unchanged=(
                codex_config_sha256_before == codex_config_sha256_after
            ),
        )


class LiveDashboard:
    SPINNER = ("⣋", "⣙", "⣹", "⣸", "⣼", "⣴", "⣦", "⣧", "⣇", "⣏")

    def __init__(
        self,
        *,
        status_path: Path,
        session_id: str,
        provider: str,
        server: str,
        model: str,
        project: str,
        command: str,
        listen_port: int,
        local_slot: str,
        local_label: str,
        verbose: bool = False,
    ) -> None:
        self.status_path = status_path
        self.session_id = session_id
        self.provider = provider
        self.server = server
        self.model = model
        self.project = project
        self.command = command
        self.listen_port = listen_port
        self.local_slot = local_slot
        self.local_label = local_label
        self.verbose = verbose
        self.tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.requests = 0
        self.responses = 0
        self.local_requests = 0
        self.local_responses = 0
        self.remote_requests = 0
        self.remote_responses = 0
        self.route = "ready"
        self.activity = "idle"
        self.activity_since = time.time()
        self.local_active_requests = 0
        self.remote_active_requests = 0
        self.last_error: str | None = None
        self.frame = 0
        self.last_first_byte_ms: int | None = None
        self.last_first_visible_ms: int | None = None
        self.last_total_ms: int | None = None
        self.last_connect_ms: int | None = None
        self.connection_reused: bool | None = None
        self.last_cache_hit_percent: float | None = None
        self.last_cached_tokens: int | None = None
        self.replay_saved_tokens = 0
        self.resident = False
        self.prefix_cached = False
        self.performance_warning: str | None = None
        self._warmup_mtime_ns: int | None = None

    def start(self) -> None:
        print("", file=sys.stderr)
        print(
            f"◈ CODEX LOCAL  Select {self.local_slot} in Codex  →  "
            f"{self.server_name} · {self.model}",
            file=sys.stderr,
        )
        print(
            "  Other Codex models remain on OpenAI. Codex config is untouched.",
            file=sys.stderr,
            flush=True,
        )
        self._write_state()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        self._write_state()
        if self.tty and not self.verbose:
            print("\r\033[2K", end="", file=sys.stderr)
        print(
            f"○ STOPPED  local {self.local_responses}/{self.local_requests}  "
            f"OpenAI {self.remote_responses}/{self.remote_requests}",
            file=sys.stderr,
            flush=True,
        )

    @property
    def server_name(self) -> str:
        return self.server

    def _render(self) -> None:
        if self.verbose or not self.tty:
            return
        if self.activity in {"local_receiving", "local_generating"}:
            glyph = self.SPINNER[self.frame % len(self.SPINNER)]
            self.frame += 1
        elif self.activity == "local_success":
            glyph = "◆"
        elif self.activity in {"local_error", "remote_error"}:
            glyph = "!"
        elif self.activity == "remote":
            glyph = "○"
        else:
            glyph = "◇"
        state = {
            "local_receiving": "routed request received",
            "local_generating": "selected model generating",
            "local_success": "routed response delivered",
            "local_error": "routed request failed",
            "remote": "OpenAI pass-through active",
            "remote_error": "OpenAI request failed",
            "idle": "ready for routed requests",
        }.get(self.activity, self.route)
        print(f"\r\033[2K{self._status_line(glyph, state)}", end="", file=sys.stderr, flush=True)

    def _status_line(self, glyph: str, state: str) -> str:
        """One line that always fits the terminal.

        A line wider than the window wraps, and then `\\r` only returns to the
        start of the last visual row while `\\033[2K` clears only that row, so
        every redraw leaves its remainder behind and scrolls the terminal away.
        The request counts are what the line is for, so they are never dropped;
        everything else is added only while there is room.
        """
        # Leave the final column free so the cursor itself cannot wrap. No
        # lower floor: a floor wider than the terminal reintroduces the wrap
        # this method exists to prevent.
        width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
        line = (
            f"{glyph} local {self.local_responses}/{self.local_requests}"
            f"  OpenAI {self.remote_responses}/{self.remote_requests}"
        )

        extras: list[str] = [state]
        if self.performance_warning:
            extras.append(f"! {self.performance_warning.replace('_', ' ')}")
        extras.append(self.model)
        if self.last_first_byte_ms is not None:
            extras.append(f"first {self.last_first_byte_ms}ms")
        if self.last_total_ms is not None:
            extras.append(f"total {self.last_total_ms}ms")
        if self.last_cache_hit_percent is not None:
            extras.append(f"cache {self.last_cache_hit_percent:g}%")
        if self.replay_saved_tokens:
            extras.append(f"replay saved {self.replay_saved_tokens:,} tok")
        readiness = [
            label
            for label, active in (("resident", self.resident), ("prefix cached", self.prefix_cached))
            if active
        ]
        if readiness:
            extras.append(" · ".join(readiness))

        for extra in extras:
            candidate = f"{line}  {extra}"
            if len(candidate) > width:
                # Stop at the first thing that does not fit rather than
                # skipping it, so the line does not reshuffle between frames.
                break
            line = candidate
        return line[:width]

    def _write_state(self) -> None:
        path = self.status_path.parent / "dashboard.json"
        temporary = path.with_suffix(".tmp")
        payload = {
            "session_id": self.session_id,
            "status": "active" if not self.stop_event.is_set() else "stopped",
            "server": self.server,
            "model": self.model,
            "local_slot": self.local_slot,
            "local_label": self.local_label,
            "route": self.route,
            "activity": self.activity,
            "activity_since": self.activity_since,
            "local_active_requests": self.local_active_requests,
            "remote_active_requests": self.remote_active_requests,
            "local_requests": self.local_requests,
            "local_responses": self.local_responses,
            "remote_requests": self.remote_requests,
            "remote_responses": self.remote_responses,
            "first_byte_ms": self.last_first_byte_ms,
            "first_visible_ms": self.last_first_visible_ms,
            "total_ms": self.last_total_ms,
            "connect_ms": self.last_connect_ms,
            "connection_reused": self.connection_reused,
            "cache_hit_percent": self.last_cache_hit_percent,
            "cached_tokens": self.last_cached_tokens,
            "replay_saved_tokens": self.replay_saved_tokens,
            "resident": self.resident,
            "prefix_cached": self.prefix_cached,
            "performance_warning": self.performance_warning,
            "last_error": self.last_error,
            "updated_at": time.time(),
        }
        temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _set_activity(self, activity: str, route: str) -> None:
        self.activity = activity
        self.activity_since = time.time()
        self.route = route

    def _expire_activity(self) -> bool:
        age = time.time() - self.activity_since
        if self.local_active_requests > 0:
            return False
        if self.activity == "local_success" and age >= 3:
            self._set_activity("idle", "local ready")
            return True
        if self.activity in {"remote", "remote_error"} and age >= 2:
            self._set_activity("idle", "ready")
            return True
        return False

    def _sync_warmup_status(self) -> bool:
        path = self.status_path.parent / "warmup.json"
        try:
            mtime_ns = path.stat().st_mtime_ns
            if mtime_ns == self._warmup_mtime_ns:
                return False
            self._warmup_mtime_ns = mtime_ns
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or payload.get("model") != self.model:
            return False
        status = payload.get("status")
        if status == "ok":
            self.resident = True
            if not self.local_active_requests:
                self._set_activity(
                    "idle",
                    "resident · prefix cached" if self.prefix_cached else "resident",
                )
        elif status == "failed":
            self.resident = False
            self.performance_warning = "warmup_failed"
        return True

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_name = event.get("event")
        if event_name in {
            "request_passthrough",
            "inference_routed",
            "websocket_forced_to_http",
            "remote_inference_routed",
        }:
            self.requests += 1
        if event_name in {
            "response_passthrough",
            "response_passthrough_error",
            "inference_completed",
            "inference_stream_closed",
            "inference_error",
            "remote_inference_completed",
            "remote_inference_error",
        }:
            self.responses += 1
        if event_name == "inference_routed":
            self.local_requests += 1
            self.local_active_requests += 1
            self.last_error = None
            self.performance_warning = None
            self._set_activity("local_receiving", "local request received")
        elif event_name in {"inference_completed", "inference_stream_closed"}:
            self.local_responses += 1
            self.local_active_requests = max(0, self.local_active_requests - 1)
            self.last_error = None
            first_byte = event.get("first_byte_ms")
            first_visible = event.get("first_visible_ms")
            total = event.get("duration_ms")
            connect_ms = event.get("connect_ms")
            self.last_first_byte_ms = (
                first_byte if isinstance(first_byte, int) else None
            )
            self.last_first_visible_ms = (
                first_visible if isinstance(first_visible, int) else None
            )
            self.last_total_ms = total if isinstance(total, int) else None
            self.last_connect_ms = connect_ms if isinstance(connect_ms, int) else None
            reused = event.get("connection_reused")
            self.connection_reused = reused if isinstance(reused, bool) else None
            cache_hit = event.get("cache_hit_percent")
            self.last_cache_hit_percent = (
                float(cache_hit) if isinstance(cache_hit, (int, float)) else None
            )
            cached_tokens = event.get("cached_tokens")
            self.last_cached_tokens = (
                cached_tokens if isinstance(cached_tokens, int) else None
            )
            self.resident = True
            if self.local_active_requests:
                self._set_activity("local_generating", "local model generating")
            else:
                self._set_activity("local_success", "local response delivered")
        elif event_name == "local_response_replayed":
            self.requests += 1
            self.responses += 1
            self.local_requests += 1
            self.local_responses += 1
            saved = event.get("replay_saved_tokens")
            if isinstance(saved, int):
                self.replay_saved_tokens += max(0, saved)
            self.last_error = None
            self._set_activity("local_success", "local response replayed")
        elif event_name == "inference_first_byte":
            first_byte = event.get("first_byte_ms")
            if isinstance(first_byte, int):
                self.last_first_byte_ms = first_byte
            self.resident = True
            self._set_activity("local_generating", "local model generating")
        elif event_name == "inference_upstream_connected":
            connect_ms = event.get("connect_ms")
            self.last_connect_ms = (
                connect_ms if isinstance(connect_ms, int) else None
            )
            reused = event.get("connection_reused")
            self.connection_reused = reused if isinstance(reused, bool) else None
            self._set_activity("local_generating", "connected · model prefilling")
        elif event_name == "inference_first_visible_event":
            first_visible = event.get("first_visible_ms")
            if isinstance(first_visible, int):
                self.last_first_visible_ms = first_visible
        elif event_name in {
            "prefix_prefill_completed",
            "prefix_prefill_deduplicated",
        }:
            self.prefix_cached = True
            self.resident = True
            if not self.local_active_requests:
                self._set_activity("idle", "resident · prefix cached")
        elif event_name == "prefix_prefill_started":
            if not self.local_active_requests:
                self._set_activity("idle", "prefilling Codex prefix")
        elif event_name == "prefix_prefill_failed":
            self.prefix_cached = False
        elif event_name == "residency_keepalive_completed":
            self.resident = True
            if not self.local_active_requests:
                self._set_activity("idle", "resident · ready")
        elif event_name == "residency_keepalive_failed":
            self.resident = False
        elif (
            event_name == "model_idle_unloaded"
            and event.get("local_model") == self.model
        ):
            self.resident = False
            self.prefix_cached = False
            if not self.local_active_requests:
                self._set_activity("idle", "model unloaded after idle timeout")
        elif (
            event_name == "model_idle_unload_failed"
            and event.get("local_model") == self.model
        ):
            self.performance_warning = "idle_unload_failed"
        elif event_name == "interceptor_ready":
            self.prefix_cached = event.get("prefix_prefill_status") == "cached"
            self.resident = event.get("residency_status") == "resident"
        elif event_name == "performance_warning":
            warning = event.get("warning_code")
            if isinstance(warning, str):
                self.performance_warning = warning
        elif event_name == "local_slot_recovered":
            slot = event.get("local_slot")
            if isinstance(slot, str) and slot:
                self.local_slot = slot
            if not self.local_active_requests:
                self._set_activity("idle", "slot updated")
        elif event_name == "remote_inference_routed":
            self.remote_requests += 1
            self.remote_active_requests += 1
            if not self.local_active_requests:
                self._set_activity("remote", "OpenAI pass-through active")
        elif event_name == "remote_inference_completed":
            self.remote_responses += 1
            self.remote_active_requests = max(0, self.remote_active_requests - 1)
            if not self.local_active_requests:
                self._set_activity("remote", "OpenAI pass-through complete")
        elif event_name == "inference_error":
            self.local_responses += 1
            self.local_active_requests = max(0, self.local_active_requests - 1)
            self.last_error = "local request failed"
            self._set_activity("local_error", "local request failed")
        elif event_name == "remote_inference_error":
            self.remote_responses += 1
            self.remote_active_requests = max(0, self.remote_active_requests - 1)
            if not self.local_active_requests:
                self._set_activity("remote_error", "OpenAI request failed")

    def _run(self) -> None:
        try:
            with self.status_path.open("r", encoding="utf-8") as handle:
                while True:
                    line = handle.readline()
                    if not line:
                        changed = self._sync_warmup_status() or self._expire_activity()
                        if changed:
                            self._write_state()
                        if changed or self.activity in {"local_receiving", "local_generating"}:
                            self._render()
                        try:
                            if os.fstat(handle.fileno()).st_size < handle.tell():
                                handle.seek(0)
                                continue
                        except FileNotFoundError:
                            pass
                        if self.stop_event.wait(0.3):
                            # One last read on the next iteration drains events
                            # written immediately before the child exited.
                            line = handle.readline()
                            if not line:
                                break
                        else:
                            continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("session_id") != self.session_id:
                        continue
                    event_name = event.get("event")
                    self._handle_event(event)
                    formatted = format_interceptor_event(event)
                    if formatted and self.verbose:
                        print(formatted, file=sys.stderr, flush=True)
                    elif formatted and not self.tty and event_name in {
                        "inference_error",
                        "remote_inference_error",
                        "request_rejected",
                    }:
                        print(formatted, file=sys.stderr, flush=True)
                    self._render()
                    self._write_state()
        except FileNotFoundError:
            print("Live event file was not created.", file=sys.stderr, flush=True)


class LocalControlLoop:
    def __init__(self, *, selection: LocalSelection, runtime_dir: Path) -> None:
        self.selection = selection
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / "control.jsonl"
        self.path.write_text("", encoding="utf-8")
        os.chmod(self.path, 0o600)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="codex-local-controls")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            while not self.stop_event.wait(1.0):
                line = handle.readline()
                if not line:
                    continue
                command = "unknown"
                try:
                    command = json.loads(line).get("command")
                    if command == "restart":
                        _unload_model_via_omlx_admin(self.selection)
                        _warm_up_model(self.selection, self.runtime_dir)
                        self._write_result(command, "ok")
                    elif command == "unload":
                        unloaded = _unload_model_via_omlx_admin(self.selection)
                        self._write_result(command, "ok" if unloaded else "unsupported")
                except Exception as exc:
                    self._write_result(str(command), "failed", error_type=type(exc).__name__)

    def _write_result(self, command: str, status: str, *, error_type: str | None = None) -> None:
        payload = {"command": command, "status": status, "updated_at": time.time()}
        if error_type:
            payload["error_type"] = error_type
        _write_warmup_status(self.runtime_dir / "control-status.json", payload)


class ModelIdleUnloader:
    """Unload an oMLX model once no routed turns remain and the timeout expires."""

    _TERMINAL_EVENTS = {
        "inference_completed",
        "inference_stream_closed",
        "inference_error",
    }

    def __init__(
        self,
        *,
        selection,
        status_path: Path,
        session_id: str,
        idle_seconds: float,
        busy_threads: list[threading.Thread] | None = None,
    ) -> None:
        self.selection = selection
        self.status_path = status_path
        self.session_id = session_id
        self.idle_seconds = max(0.0, idle_seconds)
        self.busy_threads = busy_threads or []
        self.poll_seconds = min(30.0, max(0.25, self.idle_seconds / 4))
        self.last_activity = time.monotonic()
        self.active_requests = 0
        self.unloaded = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.idle_seconds > 0 and _supports_omlx_admin(self.selection)

    def start(self) -> None:
        if not self.enabled:
            return
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="codex-local-model-idle-unloader",
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_model = event.get("local_model")
        if isinstance(event_model, str) and event_model != self.selection.model:
            return
        name = event.get("event")
        if name == "inference_routed":
            self.active_requests += 1
            self.last_activity = time.monotonic()
            self.unloaded = False
        elif name in self._TERMINAL_EVENTS:
            self.active_requests = max(0, self.active_requests - 1)
            self.last_activity = time.monotonic()
        elif name == "local_response_replayed":
            self.last_activity = time.monotonic()

    def _maybe_unload(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if any(thread.is_alive() for thread in self.busy_threads):
            self.last_activity = current
            return False
        if (
            not self.enabled
            or self.unloaded
            or self.active_requests
            or current - self.last_activity < self.idle_seconds
        ):
            return False
        unloaded = _unload_model_via_omlx_admin(self.selection)
        self.last_activity = current
        self.unloaded = unloaded
        self._record(
            "model_idle_unloaded" if unloaded else "model_idle_unload_failed",
            idle_seconds=round(self.idle_seconds, 3),
        )
        return unloaded

    def _record(self, event: str, **fields: Any) -> None:
        payload = {
            "time": time.time(),
            "event": event,
            "session_id": self.session_id,
            "local_model": self.selection.model,
            "local_server": getattr(self.selection, "server", None),
            **fields,
        }
        try:
            with self.status_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _run(self) -> None:
        try:
            with self.status_path.open("r", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                while not self.stop_event.wait(self.poll_seconds):
                    while line := handle.readline():
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            isinstance(event, dict)
                            and event.get("session_id") == self.session_id
                        ):
                            self._handle_event(event)
                    self._maybe_unload()
                    try:
                        if self.status_path.stat().st_size < handle.tell():
                            handle.seek(0)
                    except OSError:
                        pass
        except FileNotFoundError:
            return


def _start_menu_bar(runtime_dir: Path) -> subprocess.Popen | None:
    if sys.platform != "darwin" or not MENU_SOURCE_PATH.is_file():
        return None
    xcrun = _which("xcrun")
    if not xcrun:
        return None
    binary_dir = runtime_dir / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = binary_dir / "CodexLocalMenu"
    if not binary.is_file() or binary.stat().st_mtime < MENU_SOURCE_PATH.stat().st_mtime:
        result = subprocess.run(
            [
                xcrun,
                "swiftc",
                str(MENU_SOURCE_PATH),
                "-framework",
                "AppKit",
                "-o",
                str(binary),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print("! Menu-bar controller could not be built; continuing without it.", file=sys.stderr)
            return None
        os.chmod(binary, 0o700)
    return subprocess.Popen(
        [
            str(binary),
            str(runtime_dir / "dashboard.json"),
            str(runtime_dir / "control.jsonl"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _require_mitmproxy() -> None:
    """Fail with the install command rather than a traceback."""
    if _which("mitmdump"):
        return
    hint = (
        "  brew install mitmproxy"
        if sys.platform == "darwin"
        else "  pipx install mitmproxy   (or your distribution's package)"
    )
    raise SystemExit(
        "Codex Local needs mitmproxy to intercept Codex traffic, and mitmdump was "
        "not found on PATH.\n\nInstall it once:\n" + hint
    )


def _resolve_front_end() -> str:
    """Choose which Codex to launch: the desktop app, or the CLI.

    The desktop app is preferred because it is the surface most of Codex Local is
    for, but a machine with only the CLI installed is perfectly usable and
    should not be turned away.
    """
    if _find_app():
        return "app"
    if _which("codex"):
        print("ChatGPT/Codex.app was not found. Launching the Codex CLI instead.\n")
        return "cli"
    raise SystemExit(
        "Codex Local found neither the ChatGPT desktop app nor the `codex` CLI.\n\n"
        "Install one of them:\n"
        "  https://chatgpt.com/download        (desktop app, macOS)\n"
        "  npm install -g @openai/codex        (CLI)"
    )


def _no_models_message(models_config_path: Path) -> str:
    """Explain where Codex Local looked, so an empty menu is actionable."""
    pi_path = Path(
        os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent")
    ) / "models.json"
    opencode_path = (
        Path(os.environ["OPENCODE_CONFIG"]).expanduser()
        if os.environ.get("OPENCODE_CONFIG")
        else Path.home() / ".config" / "opencode" / "opencode.json"
    )
    omlx_path = OMLX_SETTINGS_PATH
    lines = [
        "Codex Local found no local models to offer.",
        "",
        "It looked in:",
        f"  Pi        {pi_path}   {'found' if pi_path.is_file() else 'not found'}",
        f"  OpenCode  {opencode_path}   "
        f"{'found' if opencode_path.is_file() else 'not found'}",
        f"  oMLX      {omlx_path}   "
        f"{'found' if omlx_path.is_file() else 'not found'}",
        f"  Custom    {models_config_path}   "
        f"{'found' if models_config_path.is_file() else 'not found'}",
        "",
        "Only private endpoints are offered: loopback, private LAN, link-local",
        "or .local hosts. A configured model served from a public URL is",
        "deliberately skipped, because sending Codex's conversation there would",
        "not be local inference.",
        "",
        "Install or configure Pi, OpenCode or oMLX and they are picked up",
        "automatically. For any other endpoint, run: codex-local config --init",
    ]
    return "\n".join(lines)


def _interactive(
    *,
    models_path: str,
    runtime_dir: str,
    project_dir: str | None = None,
    verbose: bool = False,
) -> int:
    project_dir = project_dir or str(Path.cwd())
    print("\nCodex Local: run Codex on your own model")
    print(
        "Codex keeps its configuration, Projects, plugins, automations and "
        "account. Only the chosen model slot is served locally.\n"
    )
    _require_mitmproxy()
    mode = _resolve_front_end()

    runtime_path = ensure_private_dir(Path(runtime_dir).expanduser().resolve())
    models_config_path = Path(models_path).expanduser().resolve()
    _prepare_config_file(models_config_path)
    routing = _load_hybrid_routing(models_config_path)
    local_slot = _resolve_local_slot(
        runtime_path, configured_slot=routing.get("local_slot")
    )
    configured_local_label = routing.get("display_name")
    groups = _discover_model_menu(models_path=models_config_path)
    if not groups:
        raise SystemExit(_no_models_message(models_config_path))

    selected = _choose_model_hierarchy(
        groups,
        saved=_load_last_selection(runtime_path),
    )
    # The directory Codex Local was started in is the project. Preferring a
    # remembered one instead means running it inside a second project silently
    # points Codex at the first.
    project = str(Path(project_dir).expanduser().resolve())
    selected_server = selected["server"]
    selected_model = selected["id"]
    selected_source = selected["source"]

    port = _suggest_port(8877)
    selection = _load_selection(
        selected_model,
        server=selected_server,
        source=selected_source,
        models_path=models_config_path,
    )
    local_label = _local_label_for_selection(
        selection,
        configured_label=configured_local_label,
    )
    _print_preflight(
        selection,
        selected_model=selected_model,
        local_slot=local_slot,
        port=port,
        project=project,
        mode=mode,
        slot_confirmed=(runtime_path / MODEL_CATALOG_CACHE_FILE).is_file(),
    )
    warmup = _start_model_warmup(selection, runtime_dir=runtime_path)
    _print_warmup_progress(runtime_path / "warmup.json", selected_model)
    _save_last_selection(
        runtime_path,
        server=selected_server,
        model=selected_model,
        project=project,
        provider=selection.provider,
        source=selected_source,
    )

    print("Launching Codex…")

    # The app has to start fresh: its bundled Codex process inherits the proxy
    # and CA only from the environment it is launched with.
    while mode == "app" and _running_app_pids():
        input(
            "\nChatGPT/Codex is currently running. Quit it normally, "
            "then press Enter to check again (Ctrl-C cancels): "
        )

    command = [
        sys.executable,
        "-m",
        "codex_local",
        mode,
        "--server",
        selected_server,
        "--model",
        selected_model,
        "--source",
        selected_source,
        "--project",
        str(Path(project).expanduser().resolve()),
        "--listen-port",
        str(port),
        "--runtime-dir",
        str(runtime_path),
        "--models-path",
        str(models_config_path),
        "--local-slot",
        local_slot,
        "--local-label",
        local_label,
        "--local-slot-auto",
        "--live",
        "--skip-warmup",
    ]
    if mode == "app":
        # Only the app subcommand accepts these; the CLI enables lab mode by
        # default and has no post-exit checklist.
        command.extend(["--no-attestation", "--lab-mode"])
    if verbose:
        command.append("--verbose")
    child_env = {**os.environ, "PYTHONPATH": _package_pythonpath()}
    try:
        return subprocess.run(command, env=child_env, check=False).returncode
    finally:
        if warmup is not None:
            warmup.join(timeout=0.1)


def _count(total: int, noun: str) -> str:
    """Pluralise a count for the selector, so it never reads "1 models"."""
    return f"{total} {noun}" if total == 1 else f"{total} {noun}s"


def _package_pythonpath() -> str:
    """PYTHONPATH that makes `codex-local` importable in a child process.

    Codex Local is commonly run straight from a checkout, where the package is not
    installed and a bare `-m codex_local` in a child would not resolve.
    """
    parent = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    return f"{parent}{os.pathsep}{existing}" if existing else parent


def _own_catalog_devices(models_path: Path) -> list[dict[str, Any]]:
    """Group Codex Local's own configured servers the same way Pi/OpenCode are.

    Keeping one shape for every source means the selector is always
    source → device → model, whichever configuration the models came from.
    """
    try:
        choices = _list_omlx_model_choices(models_path=models_path)
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
        return []
    devices: dict[str, dict[str, Any]] = {}
    for choice in choices:
        device = devices.setdefault(
            choice["server"], {"provider": choice["server"], "models": []}
        )
        device["models"].append({"id": choice["id"], "name": choice["name"]})
    return sorted(devices.values(), key=lambda item: item["provider"].casefold())


DISCOVERED_SOURCES: tuple[tuple[str, str], ...] = (
    ("pi", "Pi"),
    ("opencode", "OpenCode"),
    ("omlx", "oMLX"),
)


def _source_loaders() -> dict[str, Any]:
    return {
        "pi": list_pi_interceptor_choices,
        "opencode": list_opencode_interceptor_choices,
        "omlx": _omlx_devices,
    }


def _enabled_sources(models_path: Path) -> dict[str, bool]:
    """Which discovered sources are switched on.

    All three are on unless the configuration turns one off, so somebody who
    has never written a config file gets everything they have installed.
    """
    enabled = {name: True for name, _ in DISCOVERED_SOURCES}
    try:
        configured = _read_omlx_models_config(models_path).get("sources")
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
        return enabled
    if not isinstance(configured, dict):
        return enabled
    for name in enabled:
        value = configured.get(name)
        if isinstance(value, bool):
            enabled[name] = value
    return enabled


def _discover_model_menu(*, models_path: Path) -> list[dict[str, Any]]:
    """Return every model Codex Local can offer, grouped by where it came from.

    Pi, OpenCode and oMLX are read from their own configuration, so anyone who
    has one of them installed needs no Codex Local configuration at all. Any of the
    three can be switched off, and custom endpoints from Codex Local's own file are
    offered alongside whatever remains.
    """
    groups: list[dict[str, Any]] = []
    catalog_devices = _own_catalog_devices(models_path)
    if catalog_devices:
        groups.append(
            {
                "source": "catalog",
                "label": "Custom",
                "models": [],
                "devices": catalog_devices,
            }
        )

    enabled = _enabled_sources(models_path)
    loaders = _source_loaders()
    for source, label in DISCOVERED_SOURCES:
        if not enabled.get(source, True):
            continue
        loader = loaders[source]
        try:
            devices = loader()
        except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
            devices = []
        if devices:
            groups.append(
                {
                    "source": source,
                    "label": label,
                    "models": [],
                    "devices": devices,
                }
            )
    return groups


def _model_menu_label(item: dict[str, str]) -> str:
    name = item.get("name") or item["id"]
    return name if name.casefold() == item["id"].casefold() else f"{name}  · {item['id']}"


def _saved_source(saved: dict[str, Any]) -> str | None:
    source = saved.get("source")
    if source in MODEL_SOURCES:
        return str(source)
    return None


def _choose_model_hierarchy(
    groups: list[dict[str, Any]],
    *,
    saved: dict[str, Any] | None = None,
) -> dict[str, str]:
    saved = saved or {}
    last_source = _saved_source(saved)
    while True:
        provider_default = next(
            (
                index
                for index, group in enumerate(groups)
                if group["source"] == last_source
            ),
            0,
        )
        provider_index = _select_menu(
            "Choose a model source",
            [
                (
                    f"{group['label']}  · {_count(len(group['devices']), 'device')}"
                    if group["devices"]
                    else f"{group['label']}  · {_count(len(group['models']), 'model')}"
                )
                for group in groups
            ],
            default_index=provider_default,
        )
        group = groups[provider_index]
        source = group["source"]

        if not group["devices"]:
            models = group["models"]
            model_default = next(
                (
                    index
                    for index, item in enumerate(models)
                    if source == last_source and item["id"] == saved.get("model")
                ),
                0,
            )
            model_index = _select_menu(
                f"Choose a {group['label']} model",
                [_model_menu_label(item) for item in models] + ["← Back"],
                default_index=model_default,
            )
            if model_index == len(models):
                continue
            return {
                "source": source,
                "server": group["server"],
                **models[model_index],
            }

        while True:
            devices = group["devices"]
            device_default = next(
                (
                    index
                    for index, device in enumerate(devices)
                    if source == last_source
                    and device["provider"] == saved.get("server")
                ),
                0,
            )
            device_index = _select_menu(
                f"Choose a {group['label']} device",
                [
                    f"{device['provider']}  · {_count(len(device['models']), 'model')}"
                    for device in devices
                ]
                + ["← Back"],
                default_index=device_default,
            )
            if device_index == len(devices):
                break
            device = devices[device_index]
            models = device["models"]
            model_default = next(
                (
                    index
                    for index, item in enumerate(models)
                    if source == last_source
                    and device["provider"] == saved.get("server")
                    and item["id"] == saved.get("model")
                ),
                0,
            )
            model_index = _select_menu(
                f"Choose a model on {device['provider']}",
                [_model_menu_label(item) for item in models] + ["← Back"],
                default_index=model_default,
            )
            if model_index == len(models):
                continue
            return {
                "source": source,
                "server": device["provider"],
                **models[model_index],
            }


def _select_menu(title: str, options: list[str], *, default_index: int) -> int:
    if not options:
        raise ValueError(f"{title} has no choices")
    if (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() != "dumb"
    ):
        try:
            return _select_menu_tui(
                title,
                options,
                default_index=default_index,
            )
        except (ImportError, OSError):
            pass
    return _select_menu_numbered(title, options, default_index=default_index)


def _select_menu_numbered(
    title: str, options: list[str], *, default_index: int
) -> int:
    print(title + ":")
    for index, option in enumerate(options, start=1):
        suffix = " (default)" if index - 1 == default_index else ""
        print(f"  {index}. {option}{suffix}")
    while True:
        answer = input(f"Select [default {default_index + 1}]: ").strip()
        if not answer:
            return default_index
        try:
            selected = int(answer) - 1
        except ValueError:
            selected = -1
        if 0 <= selected < len(options):
            print("")
            return selected
        print(f"Please enter a number from 1 to {len(options)}.")


def _select_menu_tui(title: str, options: list[str], *, default_index: int) -> int:
    import termios
    import tty

    selected = max(0, min(default_index, len(options) - 1))
    width, height = shutil.get_terminal_size((80, 24))
    page_size = max(4, min(12, height - 7))
    original = termios.tcgetattr(sys.stdin.fileno())
    print(f"{title}  (↑/↓ or j/k, Enter)")
    print("\x1b[?25l", end="", flush=True)
    drawn = 0

    def rewind() -> str:
        # Move back to the first line of the previous frame. Relative moves
        # follow the content when drawing scrolls the screen; an absolute
        # save/restore (\x1b[s/\x1b[u) would land mid-frame after a scroll and
        # leave the old frame behind as a duplicate.
        return f"\x1b[{drawn - 1}A\r" if drawn > 1 else "\r"

    try:
        tty.setraw(sys.stdin.fileno())
        while True:
            start = min(
                max(0, selected - page_size // 2),
                max(0, len(options) - page_size),
            )
            stop = min(len(options), start + page_size)
            lines = []
            if start:
                lines.append(f"  ↑ {start} more")
            for index in range(start, stop):
                marker = "❯" if index == selected else " "
                lines.append(f"{marker} {options[index]}")
            if stop < len(options):
                lines.append(f"  ↓ {len(options) - stop} more")
            # Raw mode disables ONLCR, so join with \r\n and clip to the
            # terminal width: a wrapped line would consume an extra row and
            # desync the rewind count.
            frame = "\r\n".join(line[: max(1, width - 1)] for line in lines)
            print(rewind() + "\x1b[J" + frame if drawn else "\x1b[J" + frame, end="", flush=True)
            drawn = len(lines)

            key = sys.stdin.read(1)
            if key in {"\r", "\n"}:
                return selected
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b":
                suffix = sys.stdin.read(2)
                if suffix == "[A":
                    selected = (selected - 1) % len(options)
                elif suffix == "[B":
                    selected = (selected + 1) % len(options)
            elif key in {"k", "K"}:
                selected = (selected - 1) % len(options)
            elif key in {"j", "J"}:
                selected = (selected + 1) % len(options)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
        print(rewind() + "\x1b[J\x1b[?25h" if drawn else "\x1b[?25h", flush=True)


def _reap_orphaned_proxies(
    *, dry_run: bool = False, protected_pids: set[int] | None = None
) -> list[dict]:
    """Terminate proxies left behind by an earlier session.

    Each launch starts its own proxy. Three of them accumulated over two days
    before anyone noticed, holding ports and roughly 400 MB. A proxy belonging
    to the current session must be passed via protected_pids or the reaper
    kills it as a false orphan.
    """
    protected = (protected_pids or set()) | {os.getpid()}
    targets = (str(ADDON_PATH),)
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            stdout=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    reaped: list[dict] = []
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid in protected or not any(target in command for target in targets):
            continue
        reaped.append({"pid": pid, "kind": "proxy"})
        if dry_run:
            continue
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, signal_number)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
    return reaped


def _local_context_window(
    selection, *, models_path: str | Path | None = None
) -> int | None:
    """The local model's real context window, for Codex's auto-compact limit.

    Codex compacts against the window advertised for the slot it thinks it is
    using, so a local model with a larger window would otherwise be compacted
    long before it is full, and compaction is the most expensive turn of a
    session. Pi's catalogue records the figure per server and model; an explicit
    override wins.
    """
    override = os.environ.get("CODEX_LOCAL_LOCAL_CONTEXT_WINDOW")
    if override:
        try:
            value = int(override)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return pi_model_context_window(
        selection.model, server=selection.server, models_path=models_path
    )


def _omlx_settings(path: Path = OMLX_SETTINGS_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"oMLX settings not found: {path}")
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid oMLX settings: {path}")
    return decoded


def _omlx_base_url(settings: dict | None = None) -> str:
    resolved = settings or _omlx_settings()
    server_value = resolved.get("server")
    server: dict[str, Any] = server_value if isinstance(server_value, dict) else {}
    port = server.get("port", 9000)
    if not isinstance(port, int):
        raise ValueError("oMLX server.port must be an integer")
    return f"http://127.0.0.1:{port}/v1"


def _omlx_api_key(settings: dict | None = None) -> str | None:
    resolved = settings or _omlx_settings()
    auth_value = resolved.get("auth")
    auth: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
    api_key = auth.get("api_key")
    return api_key if isinstance(api_key, str) and api_key else None


def _load_selection(
    model: str,
    *,
    server: str | None = None,
    source: str = "catalog",
    models_path: str | Path | None = None,
) -> LocalSelection:
    if source == "omlx":
        settings = _omlx_settings()
        return LocalSelection(
            provider="oMLX",
            server=server or "oMLX",
            model=model,
            base_url=_omlx_base_url(settings),
            api_key=_omlx_api_key(settings),
            auth_header=True,
        )
    if source == "pi":
        if not server:
            raise ValueError("Pi selections require a configured device")
        selected = load_pi_selection(server, model)
        return LocalSelection(
            provider="Pi",
            server=selected.provider,
            model=selected.model,
            base_url=selected.base_url,
            api_key=selected.api_key,
            auth_header=selected.auth_header,
        )
    if source == "opencode":
        if not server:
            raise ValueError("OpenCode selections require a configured device")
        selected = load_opencode_selection(server, model)
        return LocalSelection(
            provider="OpenCode",
            server=selected.provider,
            model=selected.model,
            base_url=selected.base_url,
            api_key=selected.api_key,
            auth_header=selected.auth_header,
        )
    if source != "catalog":
        raise ValueError(f"unsupported model source: {source}")
    return _load_omlx_selection(model, server=server, models_path=models_path)


def _load_omlx_selection(
    model: str, *, server: str | None = None, models_path: str | Path | None = None
) -> LocalSelection:
    config_path = Path(models_path).expanduser().resolve() if models_path else DEFAULT_MODELS_PATH
    if not config_path.is_file():
        raise ValueError(
            f"no Codex Local configuration exists at {config_path}; models from Pi, "
            "OpenCode and oMLX are discovered automatically and need no file"
        )
    matches = [
        entry
        for entry in _read_omlx_models_config(config_path).get("servers", [])
        if isinstance(entry, dict)
        and (server is None or entry.get("name") == server)
        and any(
            isinstance(item, dict) and item.get("id") == model
            for item in entry.get("models", [])
        )
    ]
    if not matches:
        target = f" on server {server!r}" if server else ""
        raise ValueError(f"inference model {model!r} is not configured{target}")
    if len(matches) > 1:
        names = ", ".join(str(entry.get("name")) for entry in matches)
        raise ValueError(f"oMLX model {model!r} exists on multiple servers; pass --server ({names})")
    entry = matches[0]
    base_url = entry.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(
            f"inference server {entry.get('name')!r} is missing base_url"
        )
    return LocalSelection(
        provider="oMLX",
        server=str(entry["name"]),
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=entry.get("api_key") if isinstance(entry.get("api_key"), str) else None,
        auth_header=bool(entry.get("auth_header", True)),
        backend="openai-responses",
    )


def _list_omlx_model_choices(*, models_path: Path) -> list[dict[str, str]]:
    config = _read_omlx_models_config(models_path)
    choices: list[dict[str, str]] = []
    for server in config.get("servers", []):
        if not isinstance(server, dict):
            continue
        server_name = server.get("name")
        if not isinstance(server_name, str) or not server_name:
            continue
        for item in server.get("models", []):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            item_name = item.get("name")
            choices.append(
                {
                    "server": server_name,
                    "id": item_id,
                    "name": item_name if isinstance(item_name, str) else item_id,
                }
            )
    return sorted(choices, key=lambda item: (item["server"].lower(), item["id"].lower()))


def _read_omlx_models_config(path: Path) -> dict:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid oMLX models config: {path}")
    servers = decoded.get("servers")
    if not isinstance(servers, list):
        raise ValueError(f"oMLX models config must contain a servers array: {path}")
    return decoded


def _load_hybrid_routing(path: Path) -> dict[str, str]:
    try:
        routing = _read_omlx_models_config(path).get("routing")
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(routing, dict):
        return {}
    result: dict[str, str] = {}
    for target, sources in {
        "local_slot": ("local_slot", "localSlot"),
        "display_name": ("display_name", "displayName"),
    }.items():
        value = next((routing.get(key) for key in sources if routing.get(key)), None)
        if isinstance(value, str) and value.strip():
            result[target] = value.strip()
    return result


def _resolve_local_slot(runtime_dir: Path, *, configured_slot: str | None = None) -> str:
    if configured_slot and configured_slot.lower() not in {"auto", "lowest"}:
        return configured_slot
    catalog = _read_json_file(runtime_dir / MODEL_CATALOG_CACHE_FILE)
    selected = select_lowest_visible_codex_model(catalog)
    return selected["slug"] if selected else BOOTSTRAP_LOCAL_SLOT


def _write_omlx_models_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _prepare_config_file(path: Path) -> None:
    """Secure an existing configuration file, or import one if asked to.

    Codex Local never creates this file. Models are discovered from the tools that
    already own that information (Pi, OpenCode, and a local oMLX install), so
    a working setup needs no Codex Local configuration at all. The file exists only
    for people who want to pin a slot or add an endpoint none of those know
    about, and writing an empty one on first run would just be litter.
    """
    if path.is_file():
        os.chmod(path, 0o600)
        return
    for source in legacy_config_paths():
        if not source.is_file():
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        ensure_private_dir(path.parent)
        _write_omlx_models_config(path, payload)
        print(f"Imported configuration from {source}\n  to {path}\n")
        return


# `servers` starts empty on purpose: a placeholder entry would show up in the
# selector as a real, broken choice. The example sits under a key Codex Local
# ignores, so it can be copied into `servers` when it is actually wanted.
CONFIG_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "//sources": "Set any of these to false to stop discovering that tool.",
    "sources": {"pi": True, "opencode": True, "omlx": True},
    "//routing": (
        "local_slot 'auto' claims the lowest-ranked model visible in Codex. "
        "display_name 'Local' auto-generates 'Local · server · model'."
    ),
    "routing": {"local_slot": "auto", "display_name": "Local"},
    "//servers": [
        "Copy this into `servers` for an endpoint Pi, OpenCode and oMLX do not",
        "know about. Only private hosts are accepted: loopback, private LAN,",
        "link-local, or .local. Omit api_key if the endpoint needs none.",
        {
            "name": "My Server",
            "base_url": "http://127.0.0.1:9000/v1",
            "api_key": "",
            "auth_header": True,
            "models": [{"id": "your-model-id", "name": "Your Model"}],
        },
    ],
    "servers": [],
}


def _config_command(models_path: Path, *, initialise: bool) -> int:
    """Show the configuration, or write a starting one on request.

    Codex Local never writes this file on its own: models come from Pi,
    OpenCode and oMLX, so creating it is always something the user asked for.
    """
    if initialise:
        if models_path.is_file():
            print(f"Configuration already exists: {models_path}")
        else:
            ensure_private_dir(models_path.parent)
            _write_omlx_models_config(models_path, CONFIG_TEMPLATE)
            print(f"Wrote a starting configuration to {models_path}")
            print(
                "Edit the `servers` entry with your endpoint, or set any of "
                "`sources` to false to stop discovering it."
            )
        return 0

    enabled = _enabled_sources(models_path)
    print(f"Config file  {models_path}" + ("" if models_path.is_file() else "  (none)"))
    print("")
    print("Sources")
    loaders = _source_loaders()
    for source, label in DISCOVERED_SOURCES:
        if not enabled[source]:
            print(f"  {label:<10} disabled")
            continue
        summary = _source_summary(loaders[source])
        detail = (
            f"{_count(summary['models'], 'model')} on "
            f"{_count(summary['devices'], 'device')}"
            if summary["available"]
            else "enabled, nothing found"
        )
        print(f"  {label:<10} {detail}")
    custom = _own_catalog_devices(models_path)
    custom_models = sum(len(device["models"]) for device in custom)
    print(
        f"  {'Custom':<10} "
        + (
            f"{_count(custom_models, 'model')} on {_count(len(custom), 'device')}"
            if custom
            else "none configured"
        )
    )
    if not models_path.is_file():
        print("")
        print("Run `codex-local config --init` to add custom endpoints or turn a")
        print("source off. You do not need it if Pi, OpenCode or oMLX cover you.")
    return 0


def _omlx_devices() -> list[dict[str, Any]]:
    """Discover a local oMLX install the same way Pi and OpenCode are read.

    oMLX records its endpoint and credential in its own settings file, and the
    live model list comes from the server itself, so there is nothing for the
    user to copy into Codex Local.
    """
    try:
        settings = _omlx_settings()
        selection = LocalSelection(
            provider="oMLX",
            server="oMLX",
            model="__discovery__",
            base_url=_omlx_base_url(settings),
            api_key=_omlx_api_key(settings),
            auth_header=True,
        )
        model_ids = sorted(_fetch_available_model_ids(selection, timeout=2.0))
    except Exception:
        # A missing, unreadable or unreachable oMLX is simply one fewer source.
        return []
    if not model_ids:
        return []
    return [
        {
            "provider": "oMLX",
            "models": [{"id": model_id, "name": model_id} for model_id in model_ids],
        }
    ]


def _load_last_selection(runtime_dir: Path) -> dict:
    path = runtime_dir / LAST_SELECTION_FILE
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _save_last_selection(
    runtime_dir: Path,
    *,
    server: str,
    model: str,
    project: str,
    provider: str = "oMLX",
    source: str = "catalog",
    effort: str | None = None,
) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    path = runtime_dir / LAST_SELECTION_FILE
    path.write_text(
        json.dumps(
            {
                "provider": provider,
                "source": source,
                "server": server,
                "model": model,
                "project": str(Path(project).expanduser().resolve()),
                "updated_at": time.time(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _print_preflight(
    selection: LocalSelection,
    *,
    selected_model: str,
    local_slot: str = BOOTSTRAP_LOCAL_SLOT,
    port: int,
    project: str,
    mode: str = "app",
    slot_confirmed: bool = True,
) -> None:
    print(
        f"Route  {local_slot} in Codex  →  "
        f"{selection.server} · {selected_model}"
    )
    if not slot_confirmed:
        # The real slot names come from Codex's own catalogue, which Codex Local
        # only sees once Codex has fetched it through the proxy.
        print(
            "       (slot confirmed once Codex connects; Codex Local claims the "
            "lowest-ranked visible model)"
        )
    print(f"Project  {Path(project).expanduser().resolve()}")
    if selection.provider == "Pi":
        provider_check = (
            "Pi config",
            (
                Path(
                    os.environ.get(
                        "PI_CODING_AGENT_DIR",
                        Path.home() / ".pi" / "agent",
                    )
                )
                / "models.json"
            ).is_file(),
        )
    elif selection.provider == "OpenCode":
        provider_check = (
            "OpenCode config",
            (
                Path(os.environ["OPENCODE_CONFIG"]).expanduser()
                if os.environ.get("OPENCODE_CONFIG")
                else Path.home() / ".config" / "opencode" / "opencode.json"
            ).is_file(),
        )
    else:
        provider_check = ("oMLX settings", OMLX_SETTINGS_PATH.is_file())
    checks = [
        provider_check,
        ("server reachable", _check_bool(lambda: _check_upstream_reachable(selection.base_url, timeout=1.5))),
        ("model advertised", _check_bool(lambda: _assert_model_advertised(selection, selected_model))),
        ("proxy port free", not _port_is_open(port)),
        ("mitmdump installed", bool(_which("mitmdump"))),
        (
            ("Codex app installed", bool(_find_app()))
            if mode == "app"
            else ("codex CLI installed", bool(_which("codex")))
        ),
        ("traffic-only launch", True),
    ]
    disk = _disk_space_summary(Path(project))
    failed = [label for label, ok in checks if not ok]
    if not disk["ok"]:
        failed.append(f"disk space ({disk['free_human']} free)")
    if failed:
        print("! Preflight: " + ", ".join(failed))
    else:
        print("✓ Preflight ready  · server · model · tools · proxy")
    print("")


def _assert_model_advertised(selection: LocalSelection, model: str) -> None:
    if model not in _fetch_available_model_ids(selection, timeout=3.0):
        raise ValueError(f"upstream does not advertise selected model: {model}")


def _check_bool(fn) -> bool:
    try:
        fn()
        return True
    except Exception:
        return False


def _disk_space_summary(path: Path) -> dict[str, object]:
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    return {
        "ok": usage.free >= 2 * 1024 * 1024 * 1024,
        "free_human": _human_bytes(usage.free),
        "mount": str(path),
    }


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("unreachable")


def _print_warmup_progress(path: Path, model: str, *, timeout: float = 15.0) -> None:
    start = time.monotonic()
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    frames = LiveDashboard.SPINNER
    frame = 0
    status: dict[str, Any] = {}
    mtime_ns: int | None = None
    while time.monotonic() - start < timeout:
        try:
            current_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            current_mtime_ns = None
        if current_mtime_ns != mtime_ns:
            mtime_ns = current_mtime_ns
            status = _read_json_file(path)
        active_phase = status.get("active_phase")
        phase_labels = {
            "model_load": "loading weights",
            "first_token": "priming first token",
            "tool_canary": "checking tools",
        }
        phase_label = (
            phase_labels.get(active_phase, "connecting")
            if isinstance(active_phase, str)
            else "connecting"
        )
        if tty:
            print(
                f"\r\033[2K{frames[frame % len(frames)]} Waking {model} · {phase_label}",
                end="",
                flush=True,
            )
            frame += 1
        if status.get("status") == "ok":
            elapsed = time.monotonic() - start
            prefix = "\r\033[2K" if tty else ""
            tool_text = "tools ready" if status.get("tool_ready") else "tools need repair"
            print(f"{prefix}✓ Model warm in {elapsed:.1f}s · {tool_text}")
            return
        if status.get("status") == "failed":
            prefix = "\r\033[2K" if tty else ""
            hint = status.get("error_hint")
            hint_text = f" · {hint}" if isinstance(hint, str) and hint else ""
            print(
                f"{prefix}! Warm-up failed ({status.get('error_type', 'unknown error')}); "
                f"launching anyway{hint_text}"
            )
            return
        time.sleep(0.25)
    prefix = "\r\033[2K" if tty else ""
    print(f"{prefix}… Model still warming while Codex opens")


def _read_json_file(path: Path) -> dict:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _fetch_available_model_ids(selection, *, timeout: float) -> set[str]:
    models_url = selection.base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if selection.api_key and selection.auth_header:
        headers["Authorization"] = f"Bearer {selection.api_key}"
    request = Request(models_url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return set()
    return {
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    }


def _suggest_port(preferred: int) -> int:
    for port in range(preferred, min(preferred + 100, 65536)):
        if not _port_is_open(port):
            return port
    raise RuntimeError("could not find a free local proxy port")


def _doctor() -> int:
    """Report what Codex Local found, and what it still needs.

    Readiness needs three things: mitmproxy to run the interceptor, somewhere
    to send Codex (the desktop app or the `codex` CLI), and at least one model
    source. Everything else is informational.
    """
    models_path = DEFAULT_MODELS_PATH
    enabled = _enabled_sources(models_path)
    loaders = _source_loaders()
    sources: dict[str, Any] = {}
    for source, _label in DISCOVERED_SOURCES:
        if not enabled[source]:
            sources[source] = {
                "enabled": False,
                "available": False,
                "devices": 0,
                "models": 0,
            }
            continue
        sources[source] = {"enabled": True, **_source_summary(loaders[source])}
    sources["custom"] = {
        "enabled": True,
        **_source_summary(lambda: _own_catalog_devices(models_path)),
    }
    payload: dict[str, Any] = {
        "mitmdump": _which("mitmdump"),
        "codex_cli": _which("codex"),
        "codex_app": _find_app(),
        "addon": str(ADDON_PATH) if ADDON_PATH.is_file() else None,
        "config_path": str(models_path) if models_path.is_file() else None,
        "runtime_dir": str(DEFAULT_RUNTIME_DIR),
        "sources": sources,
        "launch_scope": "traffic_interception_only",
    }
    payload["models_available"] = sum(
        summary["models"] for summary in sources.values()
    )
    payload["ready"] = bool(
        payload["mitmdump"]
        and payload["addon"]
        and (payload["codex_cli"] or payload["codex_app"])
        and payload["models_available"]
    )
    payload["next_steps"] = _doctor_next_steps(payload)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ready"] else 1


def _source_summary(loader) -> dict[str, Any]:
    """Count the devices and models a source offers, without raising."""
    try:
        devices = loader() or []
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        return {"available": False, "devices": 0, "models": 0, "note": str(exc)}
    return {
        "available": bool(devices),
        "devices": len(devices),
        "models": sum(len(device.get("models", [])) for device in devices),
    }


def _doctor_next_steps(payload: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    if not payload["mitmdump"]:
        steps.append(
            "Install mitmproxy: brew install mitmproxy (or pipx install mitmproxy)"
        )
    if not payload["codex_cli"] and not payload["codex_app"]:
        steps.append(
            "Install the Codex CLI or the ChatGPT desktop app. Codex Local needs "
            "one of them to launch."
        )
    if not payload["models_available"]:
        steps.append(
            "No local models found. Configure Pi (~/.pi/agent/models.json), "
            "OpenCode (~/.config/opencode/opencode.json), or add a server to "
            f"{DEFAULT_MODELS_PATH}. Only private endpoints (loopback, LAN, "
            "link-local or .local) are offered."
        )
    return steps


def _build_omlx_plan(
    *,
    project: str,
    server: str | None,
    model: str,
    source: str = "catalog",
    listen_port: int = 8877,
    models_path: str | Path | None = None,
    local_slot: str = BOOTSTRAP_LOCAL_SLOT,
    local_label: str = DEFAULT_LOCAL_LABEL,
) -> dict[str, object]:
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_dir():
        raise FileNotFoundError(f"project directory does not exist: {project_path}")
    if not isinstance(listen_port, int) or not 1024 <= listen_port <= 65535:
        raise ValueError("listen_port must be 1024..65535")
    selection = _load_selection(
        model,
        server=server,
        source=source,
        models_path=models_path,
    )
    if not ADDON_PATH.is_file():
        raise FileNotFoundError(f"Codex Local addon is missing: {ADDON_PATH}")
    codex_config_path = Path.home() / ".codex" / "config.toml"
    return {
        "mode": "transparent_process_proxy",
        "source": source,
        "provider": selection.provider,
        "server": selection.server,
        "model": selection.model,
        "backend": selection.backend,
        "base_url": selection.base_url,
        "project": str(project_path),
        "listen_host": "127.0.0.1",
        "listen_port": listen_port,
        "local_slot": local_slot,
        "local_label": local_label,
        "package_root": str(PACKAGE_ROOT),
        "codex_config_mutation": False,
        "codex_config_path": str(codex_config_path),
        "codex_config_sha256": _sha256_file(codex_config_path),
        "requirements": {
            "mitmdump": bool(_which("mitmdump")),
            "codex": bool(_which("codex")),
        },
    }


def _start_proxy(
    *,
    selection,
    listen_port: int,
    runtime_dir: Path,
    session_id: str,
    local_slot: str | None = None,
    local_label: str = DEFAULT_LOCAL_LABEL,
    local_slot_auto: bool = False,
    protected_pids: set[int] | None = None,
    idle_unload_seconds: float = 0.0,
) -> subprocess.Popen:
    if _port_is_open(listen_port):
        raise OSError(
            f"local proxy port {listen_port} is already in use; choose another port"
        )
    if os.environ.get("CODEX_LOCAL_REAP", "1") not in {"0", "false", "False"}:
        for orphan in _reap_orphaned_proxies(protected_pids=protected_pids):
            print(
                f"  closed orphaned {orphan['kind']} from an earlier session "
                f"(pid {orphan['pid']})"
            )
    mitmdump = _required_command("mitmdump")
    _check_upstream_ready(selection)
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    confdir = runtime_dir / "mitmproxy"
    confdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(confdir, 0o700)
    status_path = runtime_dir / "status.jsonl"
    status_path.touch(mode=0o600, exist_ok=True)
    os.chmod(status_path, 0o600)
    env = os.environ.copy()
    env.update(
        {
            "CODEX_LOCAL_UPSTREAM_URL": selection.responses_url,
            "CODEX_LOCAL_MODEL": selection.model,
            "CODEX_LOCAL_AUTH_HEADER": "1" if selection.auth_header else "0",
            "CODEX_LOCAL_STATUS_PATH": str(status_path),
            "CODEX_LOCAL_SESSION_ID": session_id,
            "CODEX_LOCAL_LOCAL_LABEL": local_label,
            "CODEX_LOCAL_LOCAL_SERVER": selection.server,
            "CODEX_LOCAL_CAPABILITIES_PATH": str(
                _model_capabilities_path(runtime_dir, selection)
            ),
            "CODEX_LOCAL_PREFIX_CACHE_PATH": str(
                _model_prefix_cache_path(runtime_dir, selection)
            ),
            "CODEX_LOCAL_PREFIX_PREFILL": "1",
            # The idle unloader and the residency keepalive pull in opposite
            # directions, so only one of them may be active.
            "CODEX_LOCAL_RESIDENCY_KEEPALIVE": (
                "0"
                if idle_unload_seconds > 0 and _supports_omlx_admin(selection)
                else "1"
            ),
        }
    )
    if local_slot:
        env["CODEX_LOCAL_LOCAL_SLOT"] = local_slot
    env["CODEX_LOCAL_LOCAL_SLOT_AUTO"] = "1" if local_slot_auto else "0"
    if selection.api_key:
        env["CODEX_LOCAL_UPSTREAM_API_KEY"] = selection.api_key
    context_window = _local_context_window(selection)
    if context_window:
        env["CODEX_LOCAL_LOCAL_CONTEXT_WINDOW"] = str(context_window)
    command = [
        mitmdump,
        "--quiet",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(listen_port),
        "--set",
        f"confdir={confdir}",
        "--set",
        "block_global=true",
        "--set",
        "flow_detail=0",
        "--set",
        "console_eventlog_verbosity=error",
        "-s",
        str(ADDON_PATH),
    ]
    proxy = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cert_path = confdir / "mitmproxy-ca-cert.pem"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proxy.poll() is not None:
            error = proxy.stderr.read().strip() if proxy.stderr else ""
            raise RuntimeError(error or f"mitmdump exited with {proxy.returncode}")
        if cert_path.is_file() and _port_is_open(listen_port):
            return proxy
        time.sleep(0.1)
    _terminate(proxy)
    raise TimeoutError("interceptor proxy did not become ready within 20 seconds")


def _check_upstream_ready(selection, *, timeout: float = 3.0) -> None:
    _check_upstream_reachable(selection.base_url, timeout=timeout)
    models_url = selection.base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if selection.api_key and selection.auth_header:
        headers["Authorization"] = f"Bearer {selection.api_key}"
    request = Request(models_url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return
            raise RuntimeError(
                f"local model server returned HTTP {response.status} from {models_url}"
            )
    except HTTPError as exc:
        message = _http_error_message(exc)
        if exc.code in {401, 403}:
            auth_hint = (
                f" Check this server's api_key/auth_header settings in "
                f"{DEFAULT_MODELS_PATH}."
            )
            raise PermissionError(
                f"local model server rejected authentication at {models_url}: "
                f"{message or f'HTTP {exc.code}'}."
                f"{auth_hint}"
            ) from exc
        raise RuntimeError(
            f"local model server returned HTTP {exc.code} from {models_url}: "
            f"{message or 'no error message'}"
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionError(
            f"local model server is not reachable at {selection.base_url} "
            f"({reason}). Start that server, then run the launcher again."
        ) from exc


def _resolve_warmup_selections(
    primary,
    additional_models: list[str],
    *,
    source: str,
    models_path: str | Path | None,
) -> list:
    """Resolve opt-in warmups without allowing accidental cross-server loads."""
    selections = [primary]
    seen = {primary.model}
    for model in additional_models:
        if model in seen:
            continue
        candidate = _load_selection(
            model,
            server=primary.server,
            source=source,
            models_path=models_path,
        )
        if (
            candidate.server != primary.server
            or candidate.base_url.rstrip("/") != primary.base_url.rstrip("/")
        ):
            raise ValueError(
                f"warm model {model!r} does not share server {primary.server!r}"
            )
        selections.append(candidate)
        seen.add(model)
    return selections


def _model_warmup_status_path(runtime_dir: Path, selection) -> Path:
    identity = f"{selection.base_url}\0{selection.model}".encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()[:20]
    return runtime_dir / "warmups" / f"{key}.json"


def _start_model_warmups(selections: list, *, runtime_dir: Path) -> list[threading.Thread]:
    """Start same-server model warmups concurrently with isolated receipts."""
    if not selections:
        return []
    endpoint = selections[0].base_url.rstrip("/")
    if any(selection.base_url.rstrip("/") != endpoint for selection in selections[1:]):
        raise ValueError("concurrent warmups must share one local model server")
    threads: list[threading.Thread] = []
    for index, selection in enumerate(selections):
        status_path = (
            None
            if index == 0
            else _model_warmup_status_path(runtime_dir, selection)
        )
        thread = _start_model_warmup(
            selection,
            runtime_dir=runtime_dir,
            status_path=status_path,
        )
        if thread is not None:
            threads.append(thread)
    return threads


def _start_model_warmup(
    selection,
    *,
    runtime_dir: Path,
    status_path: Path | None = None,
) -> threading.Thread | None:
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    warmup_path = status_path or runtime_dir / "warmup.json"
    warmup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(warmup_path.parent, 0o700)
    _write_warmup_status(
        warmup_path,
        {"status": "starting", "model": selection.model},
    )
    kwargs = {"status_path": status_path} if status_path is not None else {}
    thread = threading.Thread(
        target=_warm_up_model,
        args=(selection, runtime_dir),
        kwargs=kwargs,
        daemon=True,
        name=f"codex-local-model-warmup-{selection.model}",
    )
    thread.start()
    return thread


def _warm_up_model(
    selection,
    runtime_dir: Path,
    *,
    status_path: Path | None = None,
) -> None:
    warmup_path = status_path or runtime_dir / "warmup.json"
    try:
        _set_warmup_phase(warmup_path, "model_load", "running")
        phase_started = time.monotonic()
        loaded = _warm_up_model_via_omlx_admin(selection)
        _set_warmup_phase(
            warmup_path,
            "model_load",
            "ok" if loaded else "unsupported",
            duration_ms=round((time.monotonic() - phase_started) * 1000),
        )
        # Loading weights is not enough: the first Responses request also
        # initializes the tokenizer/grammar path and prompt cache. Always
        # send the tiny ping so the first real Codex turn does not pay
        # that cost.
        _set_warmup_phase(warmup_path, "first_token", "running")
        phase_started = time.monotonic()
        _warm_up_model_via_responses(selection)
        _set_warmup_phase(
            warmup_path,
            "first_token",
            "ok",
            duration_ms=round((time.monotonic() - phase_started) * 1000),
        )
        _set_warmup_phase(warmup_path, "tool_canary", "running")
        phase_started = time.monotonic()
        tool_error = None
        try:
            tool_ready = _run_tool_canary_cached(selection, runtime_dir)
        except Exception as exc:
            tool_ready = False
            tool_error = type(exc).__name__
        _set_warmup_phase(
            warmup_path,
            "tool_canary",
            "ok" if tool_ready else "degraded",
            duration_ms=round((time.monotonic() - phase_started) * 1000),
            error_type=tool_error,
        )
        _write_model_capabilities(
            runtime_dir,
            selection,
            {
                "responses": True,
                "streaming": True,
                "native_function_call": tool_ready,
                "checked_at": time.time(),
            },
        )
        method = "omlx_admin_load+responses_ping" if loaded else "responses_ping"
        current = _read_json_file(warmup_path)
        _write_warmup_status(
            warmup_path,
            {
                **current,
                "status": "ok",
                "model": selection.model,
                "method": method,
                "tool_ready": tool_ready,
                "updated_at": time.time(),
            },
        )
    except Exception as exc:
        # Warm-up is an optimization. Launch should continue even if the local
        # server does not support a preload endpoint or rejects the tiny ping.
        current = _read_json_file(warmup_path)
        _write_warmup_status(
            warmup_path,
            {
                **current,
                "status": "failed",
                "model": getattr(selection, "model", None),
                "error_type": type(exc).__name__,
                **(
                    {"error_hint": hint}
                    if (hint := _warmup_error_hint(exc))
                    else {}
                ),
                "updated_at": time.time(),
            },
        )


def _warm_up_model_via_omlx_admin(selection, *, timeout: float = 30.0) -> bool:
    if not _supports_omlx_admin(selection):
        return False
    parsed = urlparse(selection.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    admin_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/admin/api/models/{quote(selection.model, safe='')}/load",
            "",
            "",
            "",
        )
    )
    headers = {"Authorization": f"Bearer {selection.api_key}"}
    request = Request(admin_url, data=b"", headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except HTTPError as exc:
        exc.close()
        return False
    except (URLError, TimeoutError):
        return False


def _unload_model_via_omlx_admin(selection, *, timeout: float = 15.0) -> bool:
    if not _supports_omlx_admin(selection):
        return False
    parsed = urlparse(selection.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    admin_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/admin/api/models/{quote(selection.model, safe='')}/unload",
            "",
            "",
            "",
        )
    )
    request = Request(
        admin_url,
        data=b"",
        headers={"Authorization": f"Bearer {selection.api_key}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError):
        return False


def _supports_omlx_admin(selection) -> bool:
    return (
        getattr(selection, "backend", "openai-responses") == "openai-responses"
        and bool(getattr(selection, "api_key", None))
    )


def _warm_up_model_via_responses(selection, *, timeout: float = 60.0) -> None:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if selection.api_key and selection.auth_header:
        headers["Authorization"] = f"Bearer {selection.api_key}"
    body = json.dumps(
        {
            "model": selection.model,
            "stream": False,
            "input": "Reply with OK.",
            "max_output_tokens": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(selection.responses_url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(2048)
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"warm-up returned HTTP {response.status} from {selection.responses_url}"
                )
    except HTTPError as exc:
        message = _http_error_message(exc)
        raise RuntimeError(
            message or f"warm-up returned HTTP {exc.code}"
        ) from exc


def _warmup_error_hint(exc: Exception) -> str | None:
    detail = str(exc)
    if "DS4_SESSION_LAZY_GRAPH" in detail or "lazy session graph alloc failed" in detail:
        return (
            "Workstation DeepSeek needs DS4_SESSION_LAZY_GRAPH=0 at server startup, "
            "or a smaller configured context window"
        )
    return None


def _warm_up_model_tool_canary(selection, *, timeout: float = 60.0) -> bool:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if selection.api_key and selection.auth_header:
        headers["Authorization"] = f"Bearer {selection.api_key}"
    body = json.dumps(
        {
            "model": selection.model,
            "stream": False,
            "input": "Immediately call codex_local_warmup with {\"value\":\"ready\"}. Do not explain.",
            "tools": [
                {
                    "type": "function",
                    "name": "codex_local_warmup",
                    "description": "Verifies native tool calling during launcher warm-up.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "required",
            # Reasoning-capable local models may spend more than a few dozen
            # tokens before emitting the native function-call item.
            "max_output_tokens": 64,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(selection.responses_url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024)
        if not 200 <= response.status < 300:
            return False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = [
            json.loads(line[5:].strip())
            for line in raw.splitlines()
            if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"
        ]
    return _contains_function_call(decoded, "codex_local_warmup")


def _tool_canary_cache_path(runtime_dir: Path, selection) -> Path:
    backend = getattr(selection, "backend", "openai-responses")
    identity = f"{backend}\0{selection.model}".encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()[:20]
    return runtime_dir / "tool-canary" / f"{key}.json"


def _run_tool_canary_cached(selection, runtime_dir: Path) -> bool:
    # The canary costs a full inference (and a CLI subprocess spawn for bridge
    # backends) but its result is a property of the backend/model pair, so
    # cache it for a day instead of re-probing on every launch.
    backend = getattr(selection, "backend", "openai-responses")
    path = _tool_canary_cache_path(runtime_dir, selection)
    cached = _read_json_file(path)
    checked_at = cached.get("checked_at")
    if (
        cached.get("backend") == backend
        and cached.get("model") == selection.model
        and isinstance(cached.get("tool_ready"), bool)
        and isinstance(checked_at, (int, float))
        and time.time() - checked_at < TOOL_CANARY_CACHE_TTL_SECONDS
    ):
        return cached["tool_ready"]
    tool_ready = _warm_up_model_tool_canary(selection)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _write_warmup_status(
        path,
        {
            "backend": backend,
            "model": selection.model,
            "tool_ready": tool_ready,
            "checked_at": time.time(),
        },
    )
    return tool_ready


def _contains_function_call(value, name: str) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "function_call" and value.get("name") == name:
            return True
        return any(_contains_function_call(item, name) for item in value.values())
    if isinstance(value, list):
        return any(_contains_function_call(item, name) for item in value)
    return False


def _set_warmup_phase(
    path: Path,
    phase: str,
    status: str,
    *,
    duration_ms: int | None = None,
    error_type: str | None = None,
) -> None:
    payload = _read_json_file(path)
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        phases = {}
    detail: dict[str, object] = {"status": status, "updated_at": time.time()}
    if duration_ms is not None:
        detail["duration_ms"] = duration_ms
    if error_type:
        detail["error_type"] = error_type
    phases[phase] = detail
    payload["phases"] = phases
    payload["active_phase"] = phase
    payload["updated_at"] = time.time()
    _write_warmup_status(path, payload)


def _write_model_capabilities(runtime_dir: Path, selection, capabilities: dict) -> None:
    path = _model_capabilities_path(runtime_dir, selection)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _write_warmup_status(
        path,
        {
            "server": getattr(selection, "server", None),
            "model": selection.model,
            "capabilities": capabilities,
        },
    )


def _model_capabilities_path(runtime_dir: Path, selection) -> Path:
    identity = f"{getattr(selection, 'base_url', '')}\0{selection.model}".encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()[:20]
    return runtime_dir / "capabilities" / f"{key}.json"


def _model_prefix_cache_path(runtime_dir: Path, selection) -> Path:
    identity = f"{getattr(selection, 'base_url', '')}\0{selection.model}".encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()[:20]
    return runtime_dir / "prefix-cache" / f"{key}.json"


def _write_warmup_status(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


def _check_upstream_reachable(base_url: str, *, timeout: float = 3.0) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid upstream URL: {base_url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return
    except OSError as exc:
        raise ConnectionError(
            f"local model server is not reachable at {base_url} "
            f"({exc.strerror or exc}). Start that server, then run the launcher again."
        ) from exc


def _http_error_message(exc: HTTPError) -> str | None:
    try:
        raw = exc.read(2000).decode("utf-8", errors="replace")
    finally:
        exc.close()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return None


def _codex_environment(listen_port: int, cert_path: Path) -> dict[str, str]:
    proxy_url = f"http://127.0.0.1:{listen_port}"
    env = os.environ.copy()
    for name in (
        "CODEX_HOME",
        "CODEX_CLI_PATH",
        "SKY_CUA_SERVICE_PATH",
        "CODEX_LOCAL_SOURCE_CODEX_HOME",
        "CODEX_LOCAL_LOCAL_TOOL_COMPATIBILITY",
        "CODEX_LOCAL_LOCAL_PATCHED_MODEL_COUNT",
    ):
        env.pop(name, None)
    combined_ca = _combined_ca_bundle(cert_path)
    no_proxy = _merged_no_proxy(env)
    env.update(
        {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "CODEX_CA_CERTIFICATE": str(cert_path),
            "SSL_CERT_FILE": str(combined_ca),
            "CURL_CA_BUNDLE": str(combined_ca),
            "GIT_SSL_CAINFO": str(combined_ca),
            "REQUESTS_CA_BUNDLE": str(combined_ca),
            "NODE_EXTRA_CA_CERTS": str(cert_path),
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )
    return env


def _combined_ca_bundle(cert_path: Path) -> Path:
    target = cert_path.parent.parent / "combined-ca.pem"
    chunks: list[bytes] = []
    for candidate in (
        Path("/etc/ssl/cert.pem"),
        Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
    ):
        if candidate.is_file():
            chunks.append(candidate.read_bytes().rstrip() + b"\n")
            break
    chunks.append(cert_path.read_bytes().rstrip() + b"\n")
    target.write_bytes(b"".join(chunks))
    os.chmod(target, 0o600)
    return target


def _merged_no_proxy(env: dict[str, str]) -> str:
    required = [
        "localhost",
        "127.0.0.1",
        "::1",
        "192.168.0.0/16",
        "10.0.0.0/8",
        "172.16.0.0/12",
    ]
    values: list[str] = []
    for source in (env.get("NO_PROXY", ""), env.get("no_proxy", "")):
        values.extend(item.strip() for item in source.split(",") if item.strip())
    values.extend(required)
    return ",".join(dict.fromkeys(values))


def _launch_app(
    *,
    project: str,
    child_env: dict[str, str],
    allow_running: bool,
    runtime_dir: Path,
    collect_attestation: bool,
    lab_mode: bool = False,
) -> int:
    app = _find_app()
    if not app:
        raise FileNotFoundError("ChatGPT.app or Codex.app is not installed")
    running = _running_app_pids()
    if running and not allow_running:
        _update_session_receipt(
            runtime_dir,
            phase="app_launch_refused_running_instance",
        )
        raise RuntimeError(
            "ChatGPT/Codex is already running. Quit it normally, then rerun this command "
            "so its bundled app-server inherits the interceptor environment."
        )
    codex_cli_path = None
    if lab_mode:
        codex_cli_path = _write_codex_lab_wrapper(app=app, runtime_dir=runtime_dir)
        _update_session_receipt(
            runtime_dir,
            codex_lab_mode=True,
            codex_lab_features=list(CODEX_LAB_FEATURES),
        )
    return _launch_app_process(
        app=app,
        project=project,
        child_env=child_env,
        running=running,
        runtime_dir=runtime_dir,
        collect_attestation=collect_attestation,
        codex_cli_path=codex_cli_path,
    )


def _write_codex_lab_wrapper(*, app: str, runtime_dir: Path) -> Path:
    codex_binary = Path(app) / "Contents" / "Resources" / "codex"
    if not codex_binary.is_file():
        raise FileNotFoundError(f"bundled Codex CLI is missing: {codex_binary}")
    directory = runtime_dir / "codex-lab"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    wrapper = directory / "codex"
    command = [str(codex_binary)]
    for override in CODEX_LAB_CONFIG_OVERRIDES:
        command.extend(["-c", override])
    for feature in CODEX_LAB_FEATURES:
        command.extend(["--enable", feature])
    rendered = " ".join(shlex.quote(part) for part in command)
    wrapper.write_text(f'#!/bin/sh\nexec {rendered} "$@"\n', encoding="utf-8")
    os.chmod(wrapper, 0o700)
    return wrapper


def _codex_cli_executable(*, runtime_dir: Path, lab_mode: bool) -> str:
    if not lab_mode:
        return _required_command("codex")
    app = _find_app()
    if not app:
        raise FileNotFoundError("ChatGPT.app or Codex.app is not installed")
    return str(_write_codex_lab_wrapper(app=app, runtime_dir=runtime_dir))


def _should_start_dashboard(*, command: str, live: bool) -> bool:
    # A continuously redrawn dashboard corrupts interactive TUI input because
    # both processes compete for the same terminal cursor. The CLI can still be
    # monitored from another terminal with the `status` command.
    return command == "app" or (live and command != "cli")


def _launch_app_process(
    *,
    app: str,
    project: str,
    child_env: dict[str, str],
    running: list[int],
    runtime_dir: Path,
    collect_attestation: bool,
    codex_cli_path: Path | None = None,
) -> int:
    command = ["/usr/bin/open", "-n", "-W", "-a", app]
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "no_proxy",
    ):
        if name in child_env:
            command.extend(["--env", f"{name}={child_env[name]}"])
    if codex_cli_path is not None:
        command.extend(["--env", f"CODEX_CLI_PATH={codex_cli_path}"])
    command.append(codex_thread_url(project))
    _update_session_receipt(
        runtime_dir,
        phase="app_launching",
        app_path=app,
    )
    try:
        opener = subprocess.Popen(command)
    except Exception as exc:
        _update_session_receipt(
            runtime_dir,
            phase="app_launch_failed",
            error_type=type(exc).__name__,
        )
        raise
    app_pid = _wait_for_new_app_pid(set(running), opener)
    _update_session_receipt(
        runtime_dir,
        phase="app_running" if app_pid else "app_running_pid_unconfirmed",
        app_pid=app_pid,
    )
    if app_pid:
        print(f"Codex desktop active (PID {app_pid}).", file=sys.stderr, flush=True)
    else:
        print("Codex desktop active (PID not confirmed).", file=sys.stderr, flush=True)
    if codex_cli_path is not None:
        print(
            "Codex Lab active · multi-agent v2 · detailed reasoning · capability discovery",
            file=sys.stderr,
            flush=True,
        )
    if collect_attestation:
        print(
            "\nBefore quitting the app, check the things Codex Local promises to "
            "keep working:\n"
            "  1. Your existing Projects and Automations are still visible.\n"
            "  2. A plugin or MCP tool still answers a request.\n"
            "  3. Computer Use can still inspect an app (Finder, read-only).\n"
            "  4. Quit the app normally, then answer the terminal checklist.\n",
            file=sys.stderr,
            flush=True,
        )
    exit_code = opener.wait()
    _record_exit(runtime_dir, phase="app_exited", exit_code=exit_code)
    if collect_attestation and sys.stdin.isatty():
        _collect_desktop_attestation(runtime_dir)
    return exit_code


def _collect_desktop_attestation(runtime_dir: Path) -> int:
    session_path = runtime_dir / "session.json"
    if not session_path.is_file():
        raise FileNotFoundError(f"no interceptor session exists: {session_path}")
    raw = json.loads(session_path.read_text(encoding="utf-8"))
    if raw.get("command") != "app":
        raise ValueError("desktop attestation requires a completed app-mode session")
    print("\nDesktop feature acceptance (answer from what you saw in the fresh app):")
    questions = (
        ("local_model_response_visible", "Did the selected local model return a response"),
        ("projects_sidebar_visible", "Were your existing Projects visible in the sidebar"),
        ("automations_visible", "Were Automations visible"),
        ("plugin_tool_used", "Did a plugin or MCP tool successfully answer a request"),
        ("computer_use_used", "Did Computer Use successfully operate a safe app"),
    )
    attestation = {key: _yes_no(prompt) for key, prompt in questions}
    _update_session_receipt(
        runtime_dir,
        desktop_attestation=attestation,
        desktop_attested_at=time.time(),
    )
    status = interceptor_status(runtime_dir=runtime_dir)
    acceptance = status["desktop_acceptance"]
    print(
        "Desktop acceptance: " + ("COMPLETE" if acceptance["complete"] else "INCOMPLETE")
    )
    if not acceptance["complete"]:
        pending = [
            key
            for key, value in acceptance.items()
            if value is False or (isinstance(value, str) and value.startswith("pending"))
        ]
        print("Pending or failed: " + ", ".join(pending))
    return 0 if acceptance["complete"] else 2


def _yes_no(prompt: str) -> bool:
    while True:
        value = input(f"  {prompt}? [y/N] ").strip().lower()
        if value in {"y", "yes"}:
            return True
        if value in {"", "n", "no"}:
            return False
        print("  Please answer y or n.")


def _wait_for_new_app_pid(
    existing: set[int], opener: subprocess.Popen, timeout_seconds: float = 20
) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if opener.poll() is not None:
            return None
        candidates = set(_running_app_pids()) - existing
        if candidates:
            return max(candidates)
        time.sleep(0.5)
    return None


def _wait_for_proxy(proxy: subprocess.Popen) -> int:
    try:
        return proxy.wait()
    except KeyboardInterrupt:
        return 130


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        if process.stderr:
            process.stderr.close()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    finally:
        if process.stderr:
            process.stderr.close()


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _required_command(name: str) -> str:
    command = _which(name)
    if not command:
        raise FileNotFoundError(f"required command is not installed: {name}")
    return command


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _find_app() -> str | None:
    for path in (
        Path("/Applications/ChatGPT.app"),
        Path("/Applications/Codex.app"),
        Path.home() / "Applications" / "ChatGPT.app",
        Path.home() / "Applications" / "Codex.app",
    ):
        if path.is_dir():
            return str(path)
    return None


def _running_app_pids() -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    pids = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if command.endswith("/ChatGPT.app/Contents/MacOS/ChatGPT") or command.endswith(
            "/Codex.app/Contents/MacOS/Codex"
        ):
            try:
                pids.append(int(pid_text))
            except ValueError:
                pass
    return pids


def _sha256_file(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _record_exit(runtime_dir: Path, *, phase: str, exit_code: int) -> None:
    _update_session_receipt(
        runtime_dir,
        phase=phase,
        exit_code=exit_code,
    )


def _read_session_receipt(runtime_dir: Path) -> dict:
    path = runtime_dir / "session.json"
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid interceptor session receipt: {path}")
    return decoded


def _write_session_receipt(runtime_dir: Path, payload: dict) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    path = runtime_dir / "session.json"
    temporary = runtime_dir / f".session-{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _update_session_receipt(runtime_dir: Path, **fields) -> None:
    receipt = _read_session_receipt(runtime_dir)
    receipt.update(fields)
    receipt["updated_at"] = time.time()
    _write_session_receipt(runtime_dir, receipt)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
