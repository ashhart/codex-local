'use strict';

// Renderer for the Codex Local popover. It receives one snapshot object per
// push and turns it into one of four screens; every action goes back through
// the preload bridge, never directly to a file or process.

const PHASE_TEXT = {
  proxy_starting: 'Starting proxy…',
  proxy_ready: 'Proxy ready',
  proxy_serving: 'Proxy serving',
  app_launching: 'Launching ChatGPT…',
  app_running: 'Codex is running',
};

const ACTIVITY_TEXT = {
  local_receiving: 'Receiving from local model…',
  local_generating: 'Generating (local)…',
  local_success: 'Local turn complete',
  local_error: 'Local model error',
  remote: 'Hosted turn (OpenAI tool)',
  remote_error: 'Hosted turn failed',
  idle: 'Idle',
};

const WARMUP_LABELS = {
  model_load: 'Load model',
  first_token: 'First token',
  tool_canary: 'Tools',
};

const SPINNER = ['◐', '◓', '◑', '◒'];
let spinnerFrame = 0;
setInterval(() => {
  spinnerFrame = (spinnerFrame + 1) % SPINNER.length;
}, 300);

const picker = { selection: null, project: '', filter: '', initialized: false };
let lastSnapshot = null;

window.codexLocal.onState(render);
window.codexLocal.ready();

const content = document.getElementById('content');
content.addEventListener('click', onClick);
content.addEventListener('input', onInput);

function render(snapshot) {
  lastSnapshot = snapshot;
  if (!picker.initialized && snapshot.models) {
    picker.initialized = true;
    const last = snapshot.models.last_selection;
    if (last) {
      picker.project = last.project || '';
      if (last.source && last.server && last.model) {
        picker.selection = {
          source: last.source,
          server: last.server,
          model: last.model,
        };
      }
    }
  }
  if (snapshot.phase === 'idle' && picker.selection && snapshot.models) {
    // A remembered selection whose server no longer exists would launch into
    // a confusing failure; only preselect what the tree still offers.
    if (!selectionExists(snapshot.models, picker.selection)) {
      picker.selection = null;
    }
  }
  const headerSub = document.getElementById('header-sub');
  headerSub.textContent = headerText(snapshot);
  let html;
  switch (snapshot.phase) {
    case 'starting':
    case 'running':
      html = sessionScreen(snapshot);
      break;
    case 'ended':
      html = endedScreen(snapshot);
      break;
    case 'error':
      html = errorScreen(snapshot);
      break;
    default:
      html = idleScreen(snapshot);
  }
  renderHtml(html);
}

// A full re-render on each push is simplest, but it must not eat the caret in
// the filter/project fields or jump the model list back to the top.
function renderHtml(html) {
  const active = document.activeElement;
  const focus = active && active.id ? active.id : null;
  const selectionStart = active && active.selectionStart;
  const selectionEnd = active && active.selectionEnd;
  const scroller = document.getElementById('model-list');
  const scrollTop = scroller ? scroller.scrollTop : 0;
  content.innerHTML = html;
  const newScroller = document.getElementById('model-list');
  if (newScroller) newScroller.scrollTop = scrollTop;
  if (focus) {
    const restored = document.getElementById(focus);
    if (restored) {
      restored.focus();
      try {
        restored.setSelectionRange(selectionStart, selectionEnd);
      } catch {
        // Not a text field; focus alone is enough.
      }
    }
  }
}

function headerText(s) {
  if (s.phase === 'starting' || s.phase === 'running') {
    return `${s.selection?.server || ''} · ${s.selection?.model || ''}`;
  }
  if (s.phase === 'ended') return 'session ended';
  if (s.phase === 'error') return 'session failed';
  const count = modelCount(s.models);
  const readiness = !s.doctor ? '' : s.doctor.ready ? 'ready' : 'not ready';
  return [count ? `${count} models` : 'no models', readiness]
    .filter(Boolean)
    .join(' · ');
}

function modelCount(models) {
  if (!models || !Array.isArray(models.groups)) return 0;
  return models.groups.reduce(
    (total, group) =>
      total +
      (group.devices || []).reduce(
        (sum, device) => sum + ((device.models || []).length),
        0,
      ),
    0,
  );
}

function selectionExists(models, selection) {
  return (models.groups || []).some(
    (group) =>
      group.source === selection.source &&
      (group.devices || []).some(
        (device) =>
          device.provider === selection.server &&
          (device.models || []).some((model) => model.id === selection.model),
      ),
  );
}

// ---------------------------------------------------------------------------
// Screens

function idleScreen(s) {
  const banners = [];
  if (s.backendError) {
    banners.push(
      `<div class="banner danger"><strong>python3 could not start.</strong><br/>${esc(
        s.backendError,
      )}</div>`,
    );
  }
  if (s.doctor && s.doctor.ready === false) {
    const steps = Array.isArray(s.doctor.next_steps)
      ? `<ul>${s.doctor.next_steps.map((step) => `<li>${esc(step)}</li>`).join('')}</ul>`
      : '';
    banners.push(`<div class="banner"><strong>Not ready.</strong>${steps}</div>`);
  }
  const rows = modelRows(s.models);
  return `
    ${banners.join('')}
    <input id="filter-input" type="text" placeholder="Filter models…"
           value="${esc(picker.filter)}" autocomplete="off" spellcheck="false" />
    <div id="model-list">${rows}</div>
    <div class="project-row">
      <input id="project-input" type="text" placeholder="Project directory"
             value="${esc(picker.project)}" spellcheck="false" />
      <button class="btn" data-action="choose-project">Choose…</button>
    </div>
    <div class="actions">
      <button class="btn primary wide" data-action="launch"
              ${picker.selection ? '' : 'disabled'}>
        Launch Codex
      </button>
    </div>
  `;
}

function modelRows(models) {
  if (!models || !Array.isArray(models.groups)) {
    return '<div class="empty">Loading models…</div>';
  }
  const needle = picker.filter.trim().toLowerCase();
  const html = [];
  for (const group of models.groups) {
    const devices = [];
    for (const device of group.devices || []) {
      const modelHtml = [];
      for (const model of device.models || []) {
        const haystack = `${model.name || ''} ${model.id || ''} ${device.provider || ''} ${group.label || ''}`.toLowerCase();
        if (needle && !haystack.includes(needle)) continue;
        const selected =
          picker.selection &&
          picker.selection.source === group.source &&
          picker.selection.server === device.provider &&
          picker.selection.model === model.id;
        modelHtml.push(`
          <div class="model-row${selected ? ' selected' : ''}" data-action="pick-model"
               data-source="${esc(group.source)}" data-server="${esc(device.provider)}"
               data-model="${esc(model.id)}">
            <span class="radio">${selected ? '●' : '○'}</span>
            <span class="name">${esc(model.name || model.id)}</span>
            ${model.name && model.name !== model.id ? `<span class="id">${esc(model.id)}</span>` : ''}
          </div>`);
      }
      if (modelHtml.length) {
        devices.push(`<div class="device-label">${esc(device.provider)}</div>${modelHtml.join('')}`);
      }
    }
    if (devices.length) {
      html.push(`<div class="group-label">${esc(group.label)}</div>${devices.join('')}`);
    }
  }
  return html.length ? html.join('') : '<div class="empty">No models found.<br/>Run codex-local doctor.</div>';
}

function sessionScreen(s) {
  const selection = s.selection || {};
  const dashboard = s.dashboard || {};
  const receiptPhase = s.receipt?.phase;
  const activity = dashboard.activity;
  const busy =
    activity === 'local_receiving' || activity === 'local_generating';
  const phaseText = s.phase === 'running'
    ? ACTIVITY_TEXT[activity] || 'Running'
    : PHASE_TEXT[receiptPhase] || 'Starting…';
  return `
    <div class="session-head">
      <div class="session-model">${esc(selection.model || '')}</div>
      <div class="session-server">${esc(selection.server || '')} · ${esc(selection.source || '')}</div>
    </div>
    <div class="phase-line${activity === 'local_error' || activity === 'remote_error' ? ' error' : ''}">
      <span class="spinner">${s.phase === 'running' && !busy ? '' : SPINNER[spinnerFrame]}</span>
      <span>${esc(phaseText)}</span>
    </div>
    ${s.phase === 'starting' ? warmupHtml(s.warmup) : ''}
    ${s.dashboard ? statsHtml(dashboard) : ''}
    ${controlLine(s)}
    ${dashboard.performance_warning ? `<div class="warn-line">${esc(dashboard.performance_warning)}</div>` : ''}
    ${dashboard.last_error ? `<div class="error-line">${esc(dashboard.last_error)}</div>` : ''}
    <div class="actions">
      <button class="btn" data-action="restart-model" ${s.phase === 'running' ? '' : 'disabled'}>Restart model</button>
      <button class="btn" data-action="unload-model" ${s.phase === 'running' ? '' : 'disabled'}>Unload model</button>
      <button class="btn" data-action="open-diagnostics">Diagnostics</button>
      <button class="btn danger" data-action="end-session">End session</button>
    </div>
  `;
}

function warmupHtml(warmup) {
  if (!warmup || !warmup.phases) return '';
  const pills = Object.entries(WARMUP_LABELS).map(([key, label]) => {
    const phase = warmup.phases[key] || {};
    const status = phase.status || 'pending';
    let cls = '';
    let icon = '·';
    if (status === 'ok') {
      cls = 'ok';
      icon = '✓';
    } else if (status === 'failed') {
      cls = 'failed';
      icon = '✗';
    } else if (key === warmup.active_phase || status === 'starting') {
      cls = 'active';
      icon = SPINNER[spinnerFrame];
    }
    const duration = phase.duration_ms
      ? ` ${(phase.duration_ms / 1000).toFixed(1)}s`
      : '';
    return `<span class="pill ${cls}">${icon} ${label}${duration}</span>`;
  });
  const hint = warmup.error_hint
    ? `<div class="error-line">${esc(warmup.error_hint)}</div>`
    : '';
  return `<div class="pills">${pills.join('')}</div>${hint}`;
}

function statsHtml(dashboard) {
  const firstByte =
    typeof dashboard.first_byte_ms === 'number'
      ? `${Math.round(dashboard.first_byte_ms)} ms`
      : '—';
  const total =
    typeof dashboard.total_ms === 'number'
      ? `${(dashboard.total_ms / 1000).toFixed(1)} s`
      : '—';
  const cache =
    typeof dashboard.cache_hit_percent === 'number'
      ? `${Math.round(dashboard.cache_hit_percent)}%`
      : '—';
  const replayed = dashboard.replay_saved_tokens
    ? formatTokens(dashboard.replay_saved_tokens)
    : '—';
  return `
    <div class="stats">
      ${stat(`${formatCount(dashboard.local_responses ?? 0)}/${formatCount(dashboard.local_requests ?? 0)}`, 'local turns')}
      ${stat(`${formatCount(dashboard.remote_responses ?? 0)}/${formatCount(dashboard.remote_requests ?? 0)}`, 'hosted turns')}
      ${stat(dashboard.resident ? 'yes' : 'no', 'model resident')}
      ${stat(firstByte, 'first byte')}
      ${stat(total, 'last turn')}
      ${stat(cache, 'prefix cache')}
      ${stat(replayed, 'replayed tok')}
      ${stat(dashboard.prefix_cached ? 'yes' : 'no', 'prefix kept')}
    </div>
  `;
}

function stat(value, label) {
  return `<div class="stat"><div class="value">${esc(value)}</div><div class="label">${esc(label)}</div></div>`;
}

function controlLine(s) {
  // Feedback for Restart/Unload: the control loop writes its result to
  // control-status.json; show it for a short window after each command.
  const result = s.controlStatus;
  if (!result || typeof result.updated_at !== 'number') return '';
  if (Date.now() / 1000 - result.updated_at > 12) return '';
  const mark =
    result.status === 'ok' ? '✓' : result.status === 'unsupported' ? '·' : '✗';
  const cls = result.status === 'ok' ? 'ok' : result.status === 'unsupported' ? '' : 'failed';
  return `
    <div class="phase-line">
      <span class="${cls}">${mark}</span>
      <span>${esc(result.command)}: ${esc(result.status)}${
        result.error_type ? ` (${esc(result.error_type)})` : ''
      }</span>
    </div>
  `;
}

function endedScreen(s) {
  const receipt = s.receipt || {};
  const unchanged = receipt.codex_config_unchanged;
  const duration = formatDuration(s.startedAt, receipt.proxy_stopped_at);
  const local = s.dashboard?.local_responses ?? 0;
  const remote = s.dashboard?.remote_responses ?? 0;
  const configRow =
    unchanged === true
      ? `<div class="receipt-row good"><span class="mark">✓</span><span class="detail">~/.codex/config.toml unchanged</span></div>`
      : unchanged === false
        ? `<div class="receipt-row bad"><span class="mark">✗</span><span class="detail">config.toml changed during this session</span></div>`
        : '';
  return `
    ${configRow}
    <div class="receipt-row"><span class="mark">·</span><span class="detail">exit code ${esc(
      s.exit?.code ?? '?',
    )}${s.exit?.signal ? ` (${esc(s.exit.signal)})` : ''}</span></div>
    <div class="receipt-row"><span class="mark">·</span><span class="detail">${esc(
      duration,
    )}</span></div>
    <div class="receipt-row"><span class="mark">·</span><span class="detail">${local} local · ${remote} hosted turn${
    local + remote === 1 ? '' : 's'
  }</span></div>
    <div class="actions">
      <button class="btn primary" data-action="dismiss-session">Launch again</button>
      <button class="btn" data-action="open-diagnostics">Diagnostics</button>
    </div>
  `;
}

function errorScreen(s) {
  const error = s.error || {};
  return `
    <div class="banner danger">${esc(error.message)}</div>
    ${error.stderr ? `<pre class="stderr">${esc(error.stderr)}</pre>` : ''}
    <div class="actions">
      ${
        error.alreadyRunning
          ? '<button class="btn primary wide" data-action="quit-and-retry">Quit ChatGPT and retry</button>'
          : ''
      }
      <button class="btn ${error.alreadyRunning ? '' : 'primary'}" data-action="dismiss-session">Back to picker</button>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Events

function onClick(event) {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  if (action === 'pick-model') {
    picker.selection = {
      source: target.dataset.source,
      server: target.dataset.server,
      model: target.dataset.model,
    };
    render(lastSnapshot);
  } else if (action === 'launch') {
    if (!picker.selection) return;
    const projectInput = document.getElementById('project-input');
    window.codexLocal.launch({
      ...picker.selection,
      project: (projectInput ? projectInput.value : picker.project) || picker.project,
    });
  } else if (action === 'choose-project') {
    window.codexLocal.chooseProject().then((chosen) => {
      if (chosen) {
        picker.project = chosen;
        render(lastSnapshot);
      }
    });
  } else if (action === 'end-session') {
    window.codexLocal.endSession();
  } else if (action === 'restart-model') {
    window.codexLocal.restartModel();
  } else if (action === 'unload-model') {
    window.codexLocal.unloadModel();
  } else if (action === 'open-diagnostics') {
    window.codexLocal.openDiagnostics();
  } else if (action === 'reload-models') {
    window.codexLocal.reloadModels();
  } else if (action === 'dismiss-session') {
    window.codexLocal.dismissSession();
  } else if (action === 'quit-and-retry') {
    window.codexLocal.quitAndRetry();
  }
}

function onInput(event) {
  if (event.target.id === 'filter-input') {
    picker.filter = event.target.value;
    const list = document.getElementById('model-list');
    const scroll = list ? list.scrollTop : 0;
    const focus = document.activeElement;
    const caret = event.target.selectionStart;
    if (list) list.innerHTML = modelRows(lastSnapshot?.models);
    if (list) list.scrollTop = scroll;
    if (focus) {
      focus.focus();
      try {
        focus.setSelectionRange(caret, caret);
      } catch {
        // Not a text field.
      }
    }
  } else if (event.target.id === 'project-input') {
    picker.project = event.target.value;
  }
}

// ---------------------------------------------------------------------------
// Formatting

function esc(value) {
  return String(value ?? '').replace(
    /[&<>"']/g,
    (char) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[
        char
      ],
  );
}

function formatTokens(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

function formatCount(value) {
  if (value >= 10000) return `${Math.round(value / 1000)}k`;
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

function formatDuration(startedAt, endedAtSeconds) {
  if (!startedAt) return 'duration unknown';
  const endMs =
    typeof endedAtSeconds === 'number' ? endedAtSeconds * 1000 : Date.now();
  const seconds = Math.max(0, Math.round((endMs - startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}
