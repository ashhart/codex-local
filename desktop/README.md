# Codex Local Electron app

A macOS menu-bar app for choosing a local model and launching Codex.
It uses the same Python backend as the command-line tool.

## Run from source

Install Python 3.10+, mitmproxy, Node, and npm, then run these commands from
this directory:

```bash
npm install
npm start
```

From the repository root, `./launch.sh ui` starts the same app and offers to
install missing dependencies.

Choose a model and project, then click **Launch Codex**.
Quit any existing Codex desktop process first.
The app shows warmup progress, request statistics, and session errors.
Ending a session stops the proxy and asks the Codex desktop app to quit.

## Build a macOS app

```bash
npm run dist
```

On Apple Silicon, the output is `dist/mac-arm64/Codex Local.app`.
Copy it to `/Applications` to run it without the checkout.
The bundle includes the Python source but still needs `python3` and
`mitmdump` installed. It checks these prerequisites at startup.

The build uses an available signing identity unless signing is disabled.
It is not notarized by this build command.
For an unsigned local build:

```bash
CSC_IDENTITY_AUTO_DISCOVERY=false npm run dist
```

## Capture a screen

These commands render a screen to a PNG and exit:

```bash
CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/idle.png npm start

CODEX_LOCAL_DESKTOP_E2E=fixture CODEX_LOCAL_DESKTOP_FIXTURE=fixture.json \
  CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/session.png npm start
```

The fixture file supplies a `phase`, such as `running` or `ended`, plus optional
`selection`, `dashboard`, `receipt`, and `warmup` objects.
Screenshots can include model names and project paths; review them before sharing.
