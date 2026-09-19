'use strict';

// Codex Local menu-bar app. The Python backend stays the engine: this process
// only drives it over its existing seams (JSON subcommands, runtime-directory
// files, the control.jsonl channel the Swift status item already uses) and
// renders the result. No endpoint URLs or credentials ever reach the renderer.

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  ipcMain,
  nativeImage,
  screen,
  shell,
} = require('electron');
const { spawn, execFile, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');

const POLL_MS = 300;
const BACKEND_SPAWN_TIMEOUT_MS = 20000;
const CHILD_OUTPUT_RING_BYTES = 16384;
const SPINNER = ['◐', '◓', '◑', '◒'];

const state = {
  doctor: null,
  models: null,
  child: null,
  childSelection: null,
  childStartedAt: null,
  childExit: null,
  stdoutTail: '',
  stderrTail: '',
  dashboard: null,
  receipt: null,
  warmup: null,
  controlStatus: null,
  spinnerFrame: 0,
  lastPush: '',
};

let tray = null;
let win = null;
let pollTimer = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// Backend plumbing

function backendRoot() {
  // The .app bundles the Python package under Resources/src so it works from
  // /Applications without the repository; in development it is the checkout.
  return app.isPackaged
    ? path.join(process.resourcesPath, 'src')
    : path.resolve(__dirname, '..', 'src');
}

function augmentedPath() {
  const keep = (entry) => entry.startsWith('/') && !entry.includes('\n');
  const parts = (process.env.PATH || '').split(':').filter(keep);
  if (app.isPackaged) {
    // An app launched from Finder starts with a minimal PATH that omits
    // Homebrew, so borrow the login shell's before adding the usual prefixes.
    // Only absolute entries are kept: a login file that prints a greeting
    // would otherwise smuggle arbitrary text into the PATH.
    try {
      const loginPath = execFileSync('/bin/zsh', ['-lc', 'printf %s "$PATH"'], {
        encoding: 'utf8',
        timeout: 3000,
        stdio: ['ignore', 'pipe', 'ignore'],
      });
      for (const part of loginPath.split(':').filter(keep)) {
        if (!parts.includes(part)) parts.push(part);
      }
    } catch {
      // Keep the inherited PATH; doctor will name anything still missing.
    }
  }
  for (const dir of [
    '/opt/homebrew/bin',
    '/usr/local/bin',
    path.join(os.homedir(), '.local/bin'),
    path.join(os.homedir(), '.cargo/bin'),
  ]) {
    if (!parts.includes(dir)) parts.push(dir);
  }
  return parts.join(':');
}

let cachedBackendEnv = null;
function backendEnv() {
  if (!cachedBackendEnv) {
    cachedBackendEnv = {
      ...process.env,
      PATH: augmentedPath(),
      PYTHONPATH: backendRoot(),
    };
  }
  return cachedBackendEnv;
}

function runBackend(args) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('python3', ['-m', 'codex_local', ...args], {
        env: backendEnv(),
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      resolve({ ok: false, error: error.message, stdout: '', stderr: '' });
      return;
    }
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => child.kill('SIGKILL'), BACKEND_SPAWN_TIMEOUT_MS);
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ ok: false, error: error.message, stdout, stderr });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ ok: code === 0, code, stdout, stderr });
    });
  });
}

function safeJson(text) {
  if (typeof text !== 'string' || !text.trim()) return null;
  try {
    const decoded = JSON.parse(text);
    return decoded && typeof decoded === 'object' ? decoded : null;
  } catch {
    return null;
  }
}

async function refreshDoctorAndModels() {
  const [doctorResult, modelsResult] = await Promise.all([
    runBackend(['doctor']),
    runBackend(['models']),
  ]);
  state.doctor = safeJson(doctorResult.stdout);
  // doctor exits non-zero when the machine is not ready but still prints its
  // payload; a missing models payload keeps the previous list on screen.
  state.models = safeJson(modelsResult.stdout) || state.models;
  state.backendError = doctorResult.error || modelsResult.error || null;
  if (!state.backendError) {
    // A backend that exits non-zero without printing JSON (broken install,
    // wrong Python) must not leave the picker on "Loading models…" forever.
    const failed = [doctorResult, modelsResult].find(
      (result) => result.code !== 0 && result.code !== undefined && !safeJson(result.stdout),
    );
    if (failed) {
      const tail = (failed.stderr || '').trim().split('\n').slice(-4).join('\n');
      state.backendError = `codex-local exited with code ${failed.code}${tail ? `:\n${tail}` : ''}`;
    }
  }
  pushState(true);
}

function runtimeDir() {
  return state.models?.runtime_dir || state.doctor?.runtime_dir || null;
}

async function readJsonCached(filePath, previous) {
  try {
    return JSON.parse(await fsp.readFile(filePath, 'utf8'));
  } catch {
    // Files are replaced atomically; a torn read keeps the previous snapshot.
    return previous;
  }
}

// ---------------------------------------------------------------------------
// Session lifecycle

function computePhase() {
  if (state.forcedPhase) return state.forcedPhase;
  if (state.childExit) {
    // Both process exit handlers clear child before publishing the snapshot.
    // Classify the saved exit independently of the live process reference.
    const receiptPhase = state.receipt?.phase;
    if (
      receiptPhase === 'app_launch_refused_running_instance' ||
      receiptPhase === 'proxy_start_failed'
    ) {
      return 'error';
    }
    if (state.childExit.code === 0 || state.childExit.code === 130) {
      return 'ended';
    }
    if (receiptPhase === 'app_running' || receiptPhase === 'app_exited') {
      return 'ended';
    }
    return 'error';
  }
  if (state.child) {
    return state.receipt?.phase === 'app_running' ? 'running' : 'starting';
  }
  return 'idle';
}

function errorInfo() {
  const receiptPhase = state.receipt?.phase;
  const alreadyRunning = receiptPhase === 'app_launch_refused_running_instance';
  const proxyFailed = receiptPhase === 'proxy_start_failed';
  const message = alreadyRunning
    ? 'ChatGPT/Codex is already running. It has to be launched fresh by Codex Local to pick up the proxy.'
    : proxyFailed
      ? 'The local proxy failed to start. Is the port already in use, or mitmdump missing?'
      : `The session exited unexpectedly (code ${state.childExit?.code ?? '?'}).`;
  return { message, alreadyRunning, stderr: state.stderrTail };
}

function ringAppend(key) {
  return (chunk) => {
    state[key] = (state[key] + chunk).slice(-CHILD_OUTPUT_RING_BYTES);
  };
}

function saveLastSelection(selection) {
  // Same file the terminal flow writes, so both frontends share one
  // "last choice" preselection. Best effort: a failed write only costs the
  // preselection next time.
  const dir = runtimeDir();
  if (!dir) return;
  const label = (state.models?.groups || []).find(
    (group) => group.source === selection.source,
  )?.label;
  try {
    const target = path.join(dir, 'last-selection.json');
    fs.writeFileSync(
      target,
      `${JSON.stringify(
        {
          provider: label || selection.server,
          source: selection.source,
          server: selection.server,
          model: selection.model,
          project: String(selection.project || os.homedir()),
          updated_at: Date.now() / 1000,
        },
        null,
        2,
      )}\n`,
      { mode: 0o600 },
    );
  } catch {
    // Preselection is cosmetic; nothing else depends on this write.
  }
}

function launch(selection) {
  if (state.child) return false;
  state.childSelection = selection;
  state.childStartedAt = Date.now();
  state.childExit = null;
  state.dashboard = null;
  state.receipt = null;
  state.warmup = null;
  state.controlStatus = null;
  state.stdoutTail = '';
  state.stderrTail = '';
  saveLastSelection(selection);
  const args = [
    '-m',
    'codex_local',
    'app',
    '--server',
    String(selection.server),
    '--model',
    String(selection.model),
    '--source',
    String(selection.source),
    '--project',
    String(selection.project || os.homedir()),
    '--no-menubar',
    '--no-attestation',
    // The terminal flow enables lab features for app sessions by default;
    // the menu-bar app is the same session, not a lesser one.
    '--lab-mode',
  ];
  const child = spawn('python3', args, {
    env: backendEnv(),
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
  });
  state.child = child;
  child.stdout.on('data', ringAppend('stdoutTail'));
  child.stderr.on('data', ringAppend('stderrTail'));
  child.on('error', (error) => {
    state.childExit = { code: -1, signal: null, message: error.message };
    state.child = null;
    pushState(true);
  });
  child.on('close', async (code, signal) => {
    state.childExit = { code: code ?? -1, signal };
    state.child = null;
    // The final receipt write races the process exit; read it once more so
    // the ended screen shows the teardown fields, not the previous phase.
    const dir = runtimeDir();
    if (dir) {
      state.receipt = await readJsonCached(path.join(dir, 'session.json'), state.receipt);
    }
    refreshDoctorAndModels();
    pushState(true);
  });
  pushState(true);
  return true;
}

function signalProcessGroup(child, signal) {
  try {
    process.kill(-child.pid, signal);
    return true;
  } catch {
    return false; // already gone
  }
}

async function endSession() {
  const child = state.child;
  if (!child) return;
  const exited = new Promise((resolve) => child.once('close', resolve));
  signalProcessGroup(child, 'SIGINT'); // the launcher's graceful teardown path
  let done = await Promise.race([exited.then(() => true), sleep(5000).then(() => false)]);
  if (!done && state.child === child) {
    signalProcessGroup(child, 'SIGTERM');
    done = await Promise.race([exited.then(() => true), sleep(3000).then(() => false)]);
    if (!done && state.child === child) signalProcessGroup(child, 'SIGKILL');
  }
  await quitCodexApp();
}

function quitCodexApp() {
  // A ChatGPT left running without its proxy is broken, so ending the session
  // asks it to quit too. Best effort: the user may have unsaved work.
  const appPath = state.doctor?.codex_app;
  const names =
    typeof appPath === 'string' && appPath
      ? [path.basename(appPath, '.app')]
      : ['ChatGPT', 'Codex'];
  return names.reduce(
    (chain, name) =>
      chain.then(
        () =>
          new Promise((resolve) => {
            execFile(
              'osascript',
              ['-e', `quit app "${name.replace(/"/g, '\\"')}"`],
              { timeout: 5000 },
              () => resolve(),
            );
          }),
      ),
    Promise.resolve(),
  );
}

function appendControl(command) {
  const dir = runtimeDir();
  if (!dir || !state.child) return false;
  try {
    fs.appendFileSync(
      path.join(dir, 'control.jsonl'),
      `${JSON.stringify({ command, time: Date.now() / 1000 })}\n`,
    );
    return true;
  } catch {
    return false;
  }
}

async function teardownForQuit() {
  const child = state.child;
  if (!child) return;
  const exited = new Promise((resolve) => child.once('close', resolve));
  signalProcessGroup(child, 'SIGINT');
  await Promise.race([exited, sleep(2000)]);
  if (state.child === child) signalProcessGroup(child, 'SIGKILL');
  quitCodexApp();
}

async function runDebugCapture() {
  const mode = process.env.CODEX_LOCAL_DESKTOP_E2E;
  await refreshDoctorAndModels();
  if (mode === 'refuse') {
    const last = state.models?.last_selection;
    if (!last) throw new Error('no remembered selection to launch');
    launch({
      source: last.source,
      server: last.server,
      model: last.model,
      project: last.project,
    });
    // Proxy start, warmup kick-off, refusal and teardown take a few seconds;
    // the capture happens once the child has exited on its own.
    for (let i = 0; i < 40 && state.child; i += 1) await sleep(500);
    await sleep(500);
  } else if (mode === 'fixture') {
    const fixture = JSON.parse(
      fs.readFileSync(process.env.CODEX_LOCAL_DESKTOP_FIXTURE, 'utf8'),
    );
    state.forcedPhase = fixture.phase;
    state.childSelection = fixture.selection || null;
    state.childStartedAt = fixture.startedAgoMs
      ? Date.now() - fixture.startedAgoMs
      : Date.now();
    state.dashboard = fixture.dashboard || null;
    state.receipt = fixture.receipt || null;
    state.warmup = fixture.warmup || null;
    state.controlStatus = fixture.controlStatus || null;
  }
  showPopover();
  await sleep(800);
  const image = await win.webContents.capturePage();
  fs.writeFileSync(process.env.CODEX_LOCAL_DESKTOP_SCREENSHOT, image.toPNG());
  app.quit();
}

// ---------------------------------------------------------------------------
// Polling, tray, popover

async function tick() {
  state.spinnerFrame = (state.spinnerFrame + 1) % SPINNER.length;
  if (state.forcedPhase) {
    // Fixture-driven debug rendering: the real runtime files would clobber
    // the injected state.
    updateTray();
    pushState();
    return;
  }
  const dir = runtimeDir();
  if (dir) {
    state.dashboard = await readJsonCached(
      path.join(dir, 'dashboard.json'),
      state.dashboard,
    );
    if (state.child) {
      state.receipt = await readJsonCached(
        path.join(dir, 'session.json'),
        state.receipt,
      );
      state.warmup = await readJsonCached(path.join(dir, 'warmup.json'), state.warmup);
    }
    state.controlStatus = await readJsonCached(
      path.join(dir, 'control-status.json'),
      state.controlStatus,
    );
  }
  updateTray();
  pushState();
}

function trayGlyph() {
  const phase = computePhase();
  if (phase === 'starting') return { glyph: SPINNER[state.spinnerFrame], color: null };
  if (phase === 'running' && state.dashboard) {
    const activity = state.dashboard.activity;
    if (activity === 'local_receiving') {
      return { glyph: SPINNER[state.spinnerFrame], color: '#d97706' };
    }
    if (activity === 'local_generating') {
      return { glyph: SPINNER[state.spinnerFrame], color: '#2f9e44' };
    }
    if (activity === 'local_success') return { glyph: '◆', color: '#2f9e44' };
    if (activity === 'local_error') return { glyph: '◆', color: '#e03131' };
    if (activity === 'remote_error') return { glyph: '◇', color: '#e03131' };
    if (activity === 'remote') return { glyph: '◇', color: null };
    if (state.dashboard.resident) return { glyph: '◇', color: '#2f9e44' };
  }
  return { glyph: '◇', color: null };
}

function tooltipText() {
  if (!state.childSelection) return 'Codex Local · no session';
  const d = state.dashboard || {};
  const local = `${d.local_responses ?? 0}/${d.local_requests ?? 0}`;
  const remote = `${d.remote_responses ?? 0}/${d.remote_requests ?? 0}`;
  return [
    'Codex Local',
    `${state.childSelection.server} · ${state.childSelection.model}`,
    `local ${local} · remote ${remote}`,
  ].join('\n');
}

function updateTray() {
  if (!tray) return;
  const { glyph, color } = trayGlyph();
  try {
    tray.setTitle(glyph, color ? { color } : undefined);
  } catch {
    try {
      tray.setTitle(glyph);
    } catch {
      // Older Electron without title options; the glyph alone still reads.
    }
  }
  tray.setToolTip(tooltipText());
}

function snapshot() {
  const phase = computePhase();
  return {
    phase,
    doctor: state.doctor,
    backendError: state.backendError || null,
    models: state.models,
    selection: state.childSelection,
    startedAt: state.childStartedAt,
    exit: state.childExit,
    dashboard: state.dashboard,
    receipt: state.receipt,
    warmup: state.warmup,
    controlStatus: state.controlStatus,
    error: phase === 'error' ? errorInfo() : null,
    running: phase === 'running' || phase === 'starting',
  };
}

function pushState(force) {
  if (!win || win.isDestroyed()) return;
  if (!force && !win.isVisible()) return;
  const payload = JSON.stringify(snapshot());
  if (!force && payload === state.lastPush) return;
  state.lastPush = payload;
  win.webContents.send('state', snapshot());
}

function createPopover() {
  win = new BrowserWindow({
    width: 384,
    height: 560,
    show: false,
    frame: false,
    resizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    vibrancy: 'menu',
    roundedCorners: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.on('blur', () => {
    if (win.isVisible()) win.hide();
  });
  if (process.env.CODEX_LOCAL_DESKTOP_SCREENSHOT || process.env.CODEX_LOCAL_DESKTOP_E2E) {
    // UI verification without a human: render, capture a PNG, exit.
    //   CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/idle.png npm start
    //   CODEX_LOCAL_DESKTOP_E2E=refuse  CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/err.png npm start
    //   CODEX_LOCAL_DESKTOP_E2E=fixture CODEX_LOCAL_DESKTOP_FIXTURE=f.json \
    //     CODEX_LOCAL_DESKTOP_SCREENSHOT=/tmp/run.png npm start
    // "refuse" launches the remembered selection, which fails fast when
    // ChatGPT is already running (the launcher refuses to reuse it); "fixture"
    // renders the running/ended screens from a JSON payload instead.
    win.webContents.on('did-finish-load', () => {
      setTimeout(() => runDebugCapture().catch((error) => {
        console.error('debug capture:', error);
        app.quit();
      }), 400);
    });
  }
}

function showPopover() {
  if (!win || win.isDestroyed()) return;
  const trayBounds = tray.getBounds();
  const winBounds = win.getBounds();
  const display = screen.getDisplayNearestPoint({
    x: trayBounds.x,
    y: trayBounds.y,
  });
  let x = Math.round(trayBounds.x + trayBounds.width / 2 - winBounds.width / 2);
  const y = Math.round(trayBounds.y + trayBounds.height + 5);
  x = Math.min(
    Math.max(x, display.workArea.x + 4),
    display.workArea.x + display.workArea.width - winBounds.width - 4,
  );
  win.setPosition(x, y, false);
  win.show();
  win.focus();
  pushState(true);
}

function createTray() {
  tray = new Tray(nativeImage.createEmpty());
  tray.setTitle('◇');
  tray.setToolTip('Codex Local');
  tray.on('click', () => {
    if (win && win.isVisible()) win.hide();
    else showPopover();
  });
  tray.on('right-click', () => {
    const phase = computePhase();
    Menu.buildFromTemplate([
      { label: phaseLabel(phase), enabled: false },
      { type: 'separator' },
      {
        label: 'Open diagnostics',
        click: () => shell.openPath(runtimeDir() || os.homedir()),
      },
      { label: 'Reload models', click: () => refreshDoctorAndModels() },
      { type: 'separator' },
      { label: 'End session', enabled: !!state.child, click: () => endSession() },
      { label: 'Quit Codex Local', click: () => app.quit() },
    ]).popup();
  });
}

function phaseLabel(phase) {
  return {
    idle: 'No session',
    starting: 'Starting…',
    running: 'Session running',
    ended: 'Session ended',
    error: 'Session failed',
  }[phase] || phase;
}

// ---------------------------------------------------------------------------
// App lifecycle

const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => showPopover());

  app.whenReady().then(() => {
    if (process.platform !== 'darwin') {
      dialog.showErrorBox(
        'Codex Local',
        'The menu-bar app currently supports macOS only; use ./launch.sh on this platform.',
      );
      app.quit();
      return;
    }
    app.dock?.hide();
    createTray();
    createPopover();
    refreshDoctorAndModels();
    pollTimer = setInterval(tick, POLL_MS);
  });

  app.on('window-all-closed', () => {
    // The tray owns the lifetime; hiding the popover must not quit.
  });

  app.on('before-quit', (event) => {
    if (state.child) {
      event.preventDefault();
      teardownForQuit().finally(() => app.exit(0));
    }
  });
}

// ---------------------------------------------------------------------------
// IPC

ipcMain.handle('ready', () => {
  pushState(true);
  return true;
});
ipcMain.handle('launch', (_event, selection) => launch(selection));
ipcMain.handle('end-session', () => endSession());
ipcMain.handle('dismiss-session', () => {
  state.childExit = null;
  state.childSelection = null;
  state.childStartedAt = null;
  pushState(true);
});
ipcMain.handle('quit-and-retry', () => {
  const selection = state.childSelection;
  quitCodexApp()
    .then(() => sleep(1500))
    .then(() => {
      state.childExit = null;
      if (selection) launch(selection);
      pushState(true);
    });
  return true;
});
ipcMain.handle('restart-model', () => appendControl('restart'));
ipcMain.handle('unload-model', () => appendControl('unload'));
ipcMain.handle('open-diagnostics', () => {
  const dir = runtimeDir();
  if (dir) shell.openPath(dir);
});
ipcMain.handle('reload-models', () => {
  refreshDoctorAndModels();
  return true;
});
ipcMain.handle('choose-project', async () => {
  const result = await dialog.showOpenDialog(win, {
    properties: ['openDirectory', 'createDirectory'],
  });
  return result.canceled ? null : result.filePaths[0] || null;
});
