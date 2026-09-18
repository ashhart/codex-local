# Codex Local, as a menu-bar app

The same engine as the terminal flow, driven from a small popover that lives
in the macOS menu bar: click the status item, pick a model, launch Codex.
The tray glyph tracks each turn (spinner while the local model is working,
filled diamond on success, red on failure), and the popover shows warmup
progress, live request stats, and the end-of-session receipt.

The Electron app is a frontend, nothing more. It spawns the Python launcher
(`app` mode, `--no-menubar --no-attestation`), reads the same
`dashboard.json` / `session.json` / `warmup.json` files the terminal
dashboard writes, and sends restart/unload through the same `control.jsonl`
channel the Swift status item uses. It holds no endpoint URLs and no
credentials; the model list comes from `codex-local models`, which
whitelists provider names and model ids.

## Run it from a checkout

```bash
npm install          # once; downloads Electron, about 100 MB
npm start            # or from the repository root: ./launch.sh ui
```

Prerequisites beyond the usual Codex Local ones: Node 18+ (for development
only; the packaged .app needs nothing but what it bundles).

## Package a .app

```bash
npm run dist
```

Produces `dist/mac-arm64/Codex Local.app`. Drag it to /Applications. The
bundle carries the Python backend under `Contents/Resources/src`, so the app
runs without the repository; it still needs `python3` and `mitmdump` on your
PATH, which the popover's readiness banner checks via `codex-local doctor`.
An app launched from Finder starts with a minimal PATH, so it borrows your
login shell's PATH first and falls back to the usual Homebrew prefixes.

The app is signed with whatever identity electron-builder finds locally and
is not notarized; it is built for your own machine, not for distribution.
Quitting the app ends the session: the proxy is stopped and ChatGPT is asked
to quit, because a ChatGPT left running without its proxy is broken.

## Verifying the UI without clicking anything

Three environment hooks render each screen to a PNG and exit, for
regression-checking the interface:

```bash
CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/idle.png npm start

# The real refusal path: launches the remembered selection, which fails
# fast when ChatGPT is already running.
CODEX_LOCAL_DESKTOP_E2E=refuse CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/err.png npm start

# Renders the running/ended screens from a JSON fixture instead.
CODEX_LOCAL_DESKTOP_E2E=fixture CODEX_LOCAL_DESKTOP_FIXTURE=f.json \
  CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/run.png npm start
```

The app icon is generated from geometry (no image files in the repository):

```bash
python3 tools/make_app_icon.py desktop/icons/icon.icns
```
