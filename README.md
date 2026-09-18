# Codex Local

**Run Codex on your own local model, without giving up everything else Codex does.**

Codex Local starts Codex behind a small proxy that recognises exactly one model
slot. Requests for that slot are answered by a model on your own hardware.
Every other request, including the ones carrying Projects, plugins,
automations, your account and your history, goes to its normal destination
untouched.

Codex configuration is never edited. Not one line.

```bash
codex-local
```

That is the whole thing. It finds the models you already have, lets you pick
one, and opens Codex.

---

## Why not just edit config.toml?

This is the part worth understanding, because it is the reason Codex Local exists.

The usual way to point Codex at a local model is to add a custom provider to
`~/.codex/config.toml`. It works, and the model responds, but you are changing
*who Codex is talking to*. Codex is no longer a client of your ChatGPT account;
it is a client of `http://localhost:9000`. And the features that exist because
of that account relationship go with it: Projects, automations, plugins,
Computer Use, mobile and remote access, your synced history.

You end up with a local model in a hollowed-out Codex.

Codex Local takes the other road. Codex still authenticates to OpenAI, still
believes it is talking to OpenAI, still has every feature it woke up with.
Codex Local sits in the middle of one specific request path and answers it locally.
The provider identity, the account, and the config file are all exactly as they
were:

| | Edit `config.toml` | Codex Local |
|---|---|---|
| Local model answers your turns | ✅ | ✅ |
| Projects | ✗ | ✅ |
| Automations | ✗ | ✅ |
| Plugins / MCP servers | ✗ | ✅ |
| Computer Use | ✗ | ✅ |
| Mobile + remote access to your threads | ✗ | ✅ |
| `~/.codex/config.toml` modified | yes | no |
| Codex's reported provider | your endpoint | `openai` |

The left column is what follows from Codex no longer being a client of your
ChatGPT account: those features exist because of that relationship, and they go
when it does. The right column is not a promise: it is what Codex Local's own
end-of-session checklist asks you to confirm, every session, before it will
call a run complete.

Pair it with a capable local model (DeepSeek, GLM, Qwen, whatever your machine
runs well) and you get local inference without the amputation.

> **Verify it yourself, don't take my word for it.** Take the SHA-256 of
> `~/.codex/config.toml` before and after a session; it is unchanged. Codex's
> own banner still reports `provider: openai`. Codex Local's session receipt
> records both.

---

## What you need

- **macOS or Linux**, with Python 3.10+
- **mitmproxy**: `./launch.sh` offers to install it if it is missing
- **Codex**: the ChatGPT desktop app, or the `codex` CLI
- **A local model** served over an OpenAI-compatible API

Codex Local itself has no Python dependencies. mitmproxy runs as its own binary,
installed via Homebrew, pipx or uv depending on what you already have.

It **offers** rather than installs: this tool generates a private CA and proxies
your traffic, so it has no business putting software on your machine without
being asked. Decline and it prints the one command to run. In a script, set
`CODEX_LOCAL_ASSUME_YES=1` to skip the prompt, or install mitmproxy yourself
beforehand.

## Run it

Nothing to install. Clone this repository, then:

```bash
cd codex-local
./launch.sh
```

`launch.sh` checks that Python 3.10+ and mitmproxy are present, puts this
repository's `src/` on the path, and hands off to the launcher. No venv, no
`pip install`, no dependencies to resolve.

```bash
./launch.sh                 # pick a model and launch Codex
./launch.sh ui              # the same thing from the menu bar
./launch.sh doctor          # check this machine is ready
./launch.sh config --init   # write a starting config file
./launch.sh status          # the session receipt
./launch.sh app --server NAME --model ID --project /path
./launch.sh cli --server NAME --model ID --project /path
```

Start with `./launch.sh doctor`. It reports what it found and, if something is
missing, exactly what to do about it.

### If you would rather install it

```bash
pip install -e .
```

That puts a `codex-local` command on your PATH. Every subcommand and flag is
identical, and `./launch.sh` passes straight through to the same entry point,
so the two are interchangeable.

## The menu-bar app

If you would rather click than type, `desktop/` holds a small Electron app
that lives in the macOS menu bar. Pop it open, pick a model, launch Codex;
the tray glyph spins while your local model is answering and the popover
shows warmup progress, live request stats, and the end-of-session receipt,
including the config-unchanged check. Restart and unload sit next to it.

```bash
cd desktop
npm install        # once; Node 18+ needed here only
npm start          # or from the repository root: ./launch.sh ui
npm run dist       # build Codex Local.app into desktop/dist/
```

The app is a frontend to the same engine: it runs the same launcher in
`app` mode, reads the same runtime-directory files, and never sees an
endpoint URL or credential. The `.app` bundles the Python backend, so it
works from /Applications without the repository (it still needs `python3`
and `mitmdump` on your machine). See `desktop/README.md` for the details,
including how each screen can be rendered to a PNG for checking without
clicking.

## Finding your models

**There is no configuration file to write.** Codex Local reads the tools that
already hold your models (**Pi**, **OpenCode**, and a local **oMLX** install)
straight from their own config, and shows you what they have:

```
Codex Local: run Codex on your own model

Choose a model source
› Pi          · 4 devices
  OpenCode    · 4 devices
  oMLX        · 1 device

Choose a Pi device
› Workstation · 6 models
  Laptop      · 5 models

Choose a model on Workstation
› DeepSeek-V4-Flash    · deepseek-v4-flash
  GLM-4.7-4bit         · glm-4.7-4bit
  Qwen3.6 27B          · qwen3.6-27b
```

Arrow keys or `j`/`k`, Enter to choose, and every menu has a Back item. Your
last choice is remembered and pre-selected next time.

### If you want to configure something

You only need a config file for three things: turning a source off, pinning a
slot, or adding an endpoint none of those tools know about.

```bash
codex-local config          # what's enabled, and where the file would live
codex-local config --init   # write a starting file
```

```json
{
  "sources": { "pi": true, "opencode": true, "omlx": false },
  "routing": { "local_slot": "auto", "display_name": "Local" },
  "servers": [
    {
      "name": "Workstation",
      "base_url": "http://192.168.1.50:8000/v1",
      "api_key": "…",
      "models": [{ "id": "your-model-id", "name": "Your Model" }]
    }
  ]
}
```

Anything under `servers` shows up in the selector as **Custom**, grouped by
name exactly like a Pi or OpenCode device, so you can use Codex Local with no
Pi and no OpenCode at all, just your own endpoint and credentials. Omitted
`sources` stay on, and `codex-local config --init` writes a fully annotated
starting file if you would rather edit than type.

The file lives at `~/Library/Application Support/Codex Local/config.json` on macOS,
or `${XDG_CONFIG_HOME:-~/.config}/codex_local/config.json` on Linux, and is created
mode `0600` because it holds endpoint credentials.

**Only private endpoints are offered.** Loopback, private LAN ranges,
link-local, and `.local` hosts. A model configured behind a public URL is
deliberately skipped, because sending your conversation there would not be
local inference, and Codex Local will not do it quietly.

## Which slot does it claim?

Codex ships several model slots. Codex Local claims the lowest-ranked one visible
in your build (the one you are least likely to want for hosted work) and
relabels it, so you see something like `Local · Workstation · qwen3.6-27b` in
the model picker.

Select that slot and ordinary inference is served locally. If the turn actually
invokes an OpenAI-hosted built-in such as web search, that turn is handed to
OpenAI so the tool can execute. Select any other model and Codex talks to OpenAI
exactly as before. You can switch between them mid-thread.

The slot name comes from Codex's own catalogue, so a Codex update does not
strand you. Pin a specific one with `--local-slot`, or `routing.local_slot` in
the config.

## What it does with a turn

A local model is not a hosted model with a different URL, and most of Codex Local
is the difference between those two things:

- **Compaction follows each task's model.** Local-task compaction stays local;
  cloud-task compaction stays hosted. Response IDs and Codex's per-task cache key
  keep that ownership intact across WebSocket reconnects, so concurrent local
  and cloud tasks cannot borrow one another's compaction state.
- **Parallel tasks stay isolated.** Each WebSocket has its own bridge state, and
  queued hosted responses retain separate IDs and timers. Opening twenty local,
  cloud, or mixed tasks does not collapse them into one last-used route.
- **The tool array is kept on compaction turns**, with `tool_choice: none`
  rather than an emptied list. Emptying it rewrites the front of the prompt and
  costs the server its cache on the one turn that carries the whole
  conversation: measured at 22528 of 23975 prompt tokens served from cache with
  the tools kept, and 0 without.
- **The advertised context window is the local model's**, so Codex compacts
  when your model is actually full rather than when the hosted slot would have
  been. Codex reads the compaction limit from its cache under `CODEX_HOME`, so
  Codex Local writes the figure to both the response and that cache. Patching
  only the response left a million-token model compacting at 113500 tokens six
  times in one evening. Codex Local changes no other cache fields.
  `CODEX_LOCAL_MODELS_CACHE_PATH=0` disables the cache write.
- **Reasoning controls follow the local model's profile.** A Pi-discovered model
  exposes only the efforts its `thinkingLevelMap` supports. Stale unsupported
  settings are clamped without increasing compute, so a Codex task cannot send
  `xhigh` to a model that explicitly disables it. Choose `none` or `low` for the
  fastest response, and `high` when the task benefits from deeper reasoning.
- **Real turns do not race a duplicate prefix request.** Codex normally follows
  its prewarm frame with the actual turn within a few hundred milliseconds, so
  background full-prefix prefill is off by default. The model's normal prefix
  cache remains active, and prefill can still be explicitly enabled for servers
  where measurement shows a benefit.
- **Loop guards.** A model repeating one identical tool call, or churning
  through near-identical variations of it, gets a note and then loses tool
  access for that turn. Thresholds were calibrated against 60 recorded sessions
  rather than picked: they fire on the 4 genuinely stuck ones and none of the
  other 56. Deferred catalog searches use a tighter limit because repeating the
  same query cannot reveal a different result; Codex is redirected to an
  already-loaded tool or a clear limitation instead of searching forever.
- **Requests are validated before they cost you inference.** The malformed
  shapes local servers reject are caught by name up front, rather than found
  out after a 60–200 second round trip.
- **Answers already paid for are replayed.** A cancelled stream makes Codex
  re-send a turn your model already answered; that one is served from a store
  instead of run again.
- **Tool-call repair.** A mangled tool name is fixed only when it maps
  unambiguously to a tool actually registered in that same request. Invented or
  ambiguous names are left alone rather than guessed at.
- **Every advertised Codex tool keeps its real execution path.** Client tools
  such as functions, custom tools, namespaces and deferred tool search continue
  through Codex normally. OpenAI-hosted built-ins such as web search are shown
  to the local model as callable handoff signals. If the model invokes one, the
  bridge suppresses the compatibility call and replays the complete turn over
  Codex's already-authenticated hosted WebSocket, preserving the original
  built-in definition. An unsupported compatibility function is never sent to
  the Codex client. Contract gates run before and after transformation: every
  catalog entry must be structurally valid, and every definition sent to a
  local server must be a unique callable function.

## Privacy

- The session receipt and dashboard never record prompts, bodies, headers,
  cookies, API keys, or query strings. The runtime directory is owner-only.
- Your OpenAI credentials are stripped from a request before it goes to your
  local endpoint. Your endpoint's key is injected in memory and never printed.
- A turn that actually invokes an OpenAI-hosted built-in, such as web search,
  is necessarily sent to OpenAI so that tool can execute. The handoff is
  recorded as `hosted_tool_handoff` and the final response is marked as remotely
  served; ordinary local turns and client-executed tool turns remain local. On
  the legacy HTTP fallback transport, a turn advertising a hosted tool is routed
  remotely up front because an HTTP stream cannot be withdrawn after the local
  model starts a compatibility call.
- Codex Local sets the proxy and CA **only in the environment of the Codex process
  it launches**. No system proxy, no system keychain, no app bundle changes.
  When Codex exits, so does Codex Local.
- The child's trust store combines Codex Local's CA with your existing public
  roots, so everything else keeps working normally.

## Commands

```bash
codex-local                  # pick a model and launch Codex
codex-local doctor           # what's installed, what's missing, what to do
codex-local config           # sources, and where the optional config file lives
codex-local config --init    # write a starting config file
codex-local status           # the current session receipt
codex-local models           # the picker tree as JSON, for GUI frontends
codex-local plan  --server NAME --model ID --project /path    # show, don't launch
codex-local app   --server NAME --model ID --project /path    # desktop app
codex-local cli   --server NAME --model ID --project /path    # codex CLI
codex-local serve --server NAME --model ID --project /path    # proxy only
```

Useful flags: `--live` for the request dashboard, `--verbose` for
privacy-safe routing events, `--idle-unload-seconds` to free VRAM after an idle
period, `--warm-model` to warm a second model on the same server.

## Limits, stated plainly

- **macOS is the verified platform.** The desktop app path, the menu-bar
  status item, and process handling are all developed and tested there. The
  Linux CLI path is implemented and unit-tested, but has not been exercised on
  a real Linux desktop. Reports welcome.
- **Desktop activation needs a fresh app launch.** The bundled Codex process
  inherits the proxy only from the environment it starts with, so Codex Local will
  ask you to quit a running ChatGPT first.
- **This is unofficial.** Codex Local is not affiliated with OpenAI. It works
  by recognising Codex's current request paths, and a Codex update could change
  them. Nothing it does is hidden from you: the routing rules are the top of
  `src/codex_local/routing.py`: the hosts, the exact paths, and the single
  condition under which a request is touched at all.
- **`workspace-write` style trust applies.** Your local model is answering as
  Codex's model, and Codex will act on what it says. Point it at models you
  trust.

## License

MIT. See [LICENSE](LICENSE).
