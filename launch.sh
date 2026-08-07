#!/usr/bin/env bash
# Codex Local: one command to run Codex on your own local model.
#
# This is the checkout-first start script. It needs nothing installed:
# it runs Codex Local straight from this repository, checks that the two
# hard prerequisites exist, and hands the rest to the launcher (which picks
# a model, claims the slot, starts the proxy and opens Codex for you).
#
#   ./launch.sh                     interactive picker -> launch Codex
#   ./launch.sh doctor              check this machine is ready
#   ./launch.sh config --init       write a starting config file
#   ./launch.sh status              the session receipt
#   ./launch.sh cli / app / serve   passthrough to any subcommand + flags
#
# Environment overrides:
#   PYTHON=/path/to/python   use a specific interpreter (needs 3.10+)
#   DEBUG=1                  keep the launcher's own traceback instead of ours
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

# --- helpers -----------------------------------------------------------------
die() {
    printf '\033[31mCodex Local:\033[0m %s\n' "$*" >&2
    exit 1
}

# --- 1. Python present and new enough ---------------------------------------
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    die "could not find '$PYTHON'. Install Python 3.10+ (brew install python) or set PYTHON=..."
fi

version="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
major="${version%%.*}"
minor="${version#*.}"
minor="${minor%%.*}"
if (( major < 3 )) || (( major == 3 && minor < 10 )); then
    die "Python 3.10+ required, but '$PYTHON' is $version."
fi

# --- 2. mitmproxy present ----------------------------------------------------
# The launcher also checks this with a friendly message, but we do it up front
# so doctor-style commands give you one clear line instead of a menu prompt.
#
# We offer to install it, but never do so without being asked: Codex Local
# generates a private CA and proxies your traffic, and a tool that does that
# has no business installing system packages behind your back. Set
# CODEX_LOCAL_ASSUME_YES=1 to skip the prompt in a script.
if ! command -v mitmdump >/dev/null 2>&1; then
    installer=()
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        installer=(brew install mitmproxy)
    elif command -v pipx >/dev/null 2>&1; then
        installer=(pipx install mitmproxy)
    elif command -v uv >/dev/null 2>&1; then
        installer=(uv tool install mitmproxy)
    fi

    if (( ${#installer[@]} == 0 )); then
        if [[ "$(uname -s)" == "Darwin" ]]; then
            die "mitmdump was not found, and neither was Homebrew.
       Install Homebrew from https://brew.sh, then: brew install mitmproxy"
        else
            die "mitmdump was not found, and neither was pipx or uv.
       Install one of them, then: pipx install mitmproxy"
        fi
    fi

    reply=n
    if [[ "${CODEX_LOCAL_ASSUME_YES:-0}" == "1" ]]; then
        reply=y
    elif [[ -t 0 ]]; then
        printf 'Codex Local needs mitmproxy, which is not installed.\n'
        printf '  It will run: %s\n' "${installer[*]}"
        printf 'Install it now? [y/N] '
        read -r reply || reply=n
    else
        # Non-interactive: never install unasked, just say what to run.
        die "mitmdump was not found on PATH.
       Install it once: ${installer[*]}"
    fi

    case "$reply" in
        y | Y | yes | YES)
            printf 'Installing mitmproxy with: %s\n' "${installer[*]}"
            if ! "${installer[@]}"; then
                die "'${installer[*]}' failed. Install mitmproxy manually and re-run."
            fi
            ;;
        *)
            die "mitmproxy is required. Install it with: ${installer[*]}"
            ;;
    esac

    if ! command -v mitmdump >/dev/null 2>&1; then
        die "mitmproxy installed, but 'mitmdump' is still not on PATH.
       Open a new shell and re-run, or check your PATH."
    fi
fi

# --- 3. Make the checkout importable ----------------------------------------
# Codex Local is regularly run straight from a checkout and the package is not
# installed, so put this repository's src/ on PYTHONPATH for us and children.
SRC_DIR="$REPO_ROOT/src"
if [[ -z "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$SRC_DIR"
else
    export PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}"
fi

# --- 4. Delegation -----------------------------------------------------------
command="${1-}"

case "$command" in
    "" | interactive | doctor | config | status | attest-desktop | plan | serve | exec | app | cli)
        ;;
    -h | --help)
        exec "$PYTHON" -m codex_local --help
        ;;
    *)
        # Unknown first word, so show the real error from the launcher.
        exec "$PYTHON" -m codex_local "$@"
        ;;
esac

if [[ "$command" == "doctor" ]]; then
    if [[ "${DEBUG:-0}" == "1" ]]; then
        exec "$PYTHON" -m codex_local doctor
    fi
    if output="$("$PYTHON" -m codex_local doctor 2>&1)"; then
        printf '\033[32mCodex Local is ready.\033[0m\n%s\n' "$output"
    else
        printf '\033[33mCodex Local is not quite ready yet.\033[0m\n%s\n' "$output" >&2
        exit 1
    fi
    exit 0
fi

exec "$PYTHON" -m codex_local "$@"
