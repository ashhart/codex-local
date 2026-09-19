# Codex Local

Run Codex with a model on your own hardware through an Electron menu-bar app
or a command-line tool.

Codex Local sends one selected model slot to your local server through a proxy.
Other model slots and account services keep their normal routes.
It does not edit `~/.codex/config.toml`.

## Get started

You need Python 3.10+, mitmproxy, Codex, and a model server with an
OpenAI-compatible API on your machine or private network.
The Electron app also needs Node and npm when running from source.

From this checkout:

```bash
./launch.sh doctor
```

The launcher checks prerequisites and offers to install mitmproxy if needed.
Then choose an interface:

| Interface | Platform | Command |
|---|---|---|
| Electron menu-bar app | macOS | `./launch.sh ui` |
| CLI with a terminal model picker | macOS or Linux | `./launch.sh` |

Both use the same Python backend and model configuration.
The CLI does not need Node or Electron.

### Electron app

```bash
./launch.sh ui
```

On first use, the launcher offers to install the Electron dependencies.
Pick a model and project, then click **Launch Codex**.
The menu-bar app shows model warmup, request counts, errors, and session status,
with controls to restart or unload the model and end the session.

Quit any running Codex desktop app before launching a local session so the
new process can inherit the proxy settings.

To build a macOS app bundle:

```bash
cd desktop
npm install
npm run dist
```

The bundle includes the Python backend but still needs `python3` and
`mitmdump` installed on your machine.
See the [Electron guide](desktop/README.md) for packaging details.

### Command-line tool

```bash
./launch.sh
```

The terminal picker discovers your models and launches the available Codex
frontend. To request the Codex terminal explicitly, specify a model:

```bash
./launch.sh cli --source pi --server NAME --model ID --project /path/to/project
```

Use `--source opencode` or `--source omlx` for those sources.
Omit `--source` for a server in Codex Local's own configuration.
Use `app` instead of `cli` to launch the Codex desktop app.

You can also install the Python command:

```bash
pip install -e .
codex-local --help
```

`codex-local` exposes the same Python subcommands as `./launch.sh`.
The Electron shortcut is `./launch.sh ui`; `codex-local ui` is not supported.

## Models and configuration

Codex Local discovers models from Pi, OpenCode, and oMLX configuration.
It offers only private endpoints, including loopback and private LAN addresses.
Public endpoints are rejected.

No configuration file is needed if a supported source already lists your model.
To add a custom server or disable discovery sources:

```bash
./launch.sh config --init
```

This creates a starter configuration in
`~/Library/Application Support/Codex Local/config.json` on macOS, or
`${XDG_CONFIG_HOME:-~/.config}/codex_local/config.json` on Linux.
The file uses owner-only permissions because it can contain endpoint credentials.

## Commands

| Command | Purpose |
|---|---|
| `./launch.sh doctor` | Check prerequisites and model sources |
| `./launch.sh models` | List the model picker data as JSON |
| `./launch.sh config` | Show configuration location and enabled sources |
| `./launch.sh status` | Read the current session receipt |
| `./launch.sh plan --server NAME --model ID` | Inspect a selection without launching |
| `./launch.sh serve --server NAME --model ID` | Run the proxy without launching Codex |

Add `--live` to a session command for request statistics or `--verbose` for
routing events. Run `./launch.sh COMMAND --help` for that command's options.

## Routing and privacy

Codex Local labels its selected model slot `Local` in the Codex model picker.
Select it for local inference, or select another slot for hosted inference.

The proxy and certificate settings apply to the Codex process it launches.
Codex Local does not change system proxy settings or install a system
certificate. It strips OpenAI credentials before forwarding a request to the
local endpoint and uses that endpoint's credentials instead.

Client-executed tools continue through Codex. A turn that invokes an
OpenAI-hosted tool, such as web search, is sent to OpenAI for execution.
On the HTTP fallback transport, advertising a hosted tool routes the turn to
OpenAI before generation starts.

The dashboard and session receipt omit prompts, request bodies, credentials,
and cookies. Runtime files use owner-only permissions.

## Compatibility

Codex Local is unofficial and depends on Codex's request formats.
A Codex update can change them.
Keeping account traffic on its normal route is intended to preserve Projects,
plugins, and automations, but full desktop feature compatibility needs checking
in a fresh session with your installed Codex version.

Automated macOS checks cover the proxy, CLI lifecycle, Electron picker,
launch-error recovery, and packaged-app startup.
These checks do not prove that every local model or Codex desktop feature works.
The Linux CLI has unit coverage but has not been verified on a Linux desktop.

## License

[MIT](LICENSE).
