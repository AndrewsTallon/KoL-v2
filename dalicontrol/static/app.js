// KoL Lighting Control Dashboard - Frontend Logic

const API = '';  // Same origin
let ws = null;
let reconnectTimer = null;

// Chart data buffers
const MAX_POINTS = 2000;
const chartData = {
  timestamps: [],
  brightness: [],
  lux: [],
  cct: [],
  occupied: [],
};

// ---- Charts ----

const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  scales: {
    x: {
      type: 'category',
      ticks: { color: '#999', maxTicksLimit: 10, maxRotation: 0 },
      grid: { color: '#2a2a4a' },
    },
  },
  plugins: {
    legend: { labels: { color: '#eee', boxWidth: 12 } },
  },
};

const brightnessLuxChart = new Chart(
  document.getElementById('brightnessLuxChart'),
  {
    type: 'line',
    data: {
      labels: chartData.timestamps,
      datasets: [
        {
          label: 'Brightness %',
          data: chartData.brightness,
          borderColor: '#e94560',
          backgroundColor: 'rgba(233,69,96,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'y',
        },
        {
          label: 'Lux',
          data: chartData.lux,
          borderColor: '#ffa726',
          backgroundColor: 'rgba(255,167,38,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'y1',
        },
      ],
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: {
          position: 'left',
          min: 0, max: 100,
          title: { display: true, text: 'Brightness %', color: '#e94560' },
          ticks: { color: '#e94560' },
          grid: { color: '#2a2a4a' },
        },
        y1: {
          position: 'right',
          min: 0,
          title: { display: true, text: 'Lux', color: '#ffa726' },
          ticks: { color: '#ffa726' },
          grid: { drawOnChartArea: false },
        },
      },
    },
  }
);

const cctChart = new Chart(
  document.getElementById('cctChart'),
  {
    type: 'line',
    data: {
      labels: chartData.timestamps,
      datasets: [
        {
          label: 'CCT (K)',
          data: chartData.cct,
          borderColor: '#42a5f5',
          backgroundColor: 'rgba(66,165,245,0.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        },
      ],
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: {
          min: 2700, max: 6500,
          title: { display: true, text: 'CCT (K)', color: '#42a5f5' },
          ticks: { color: '#42a5f5' },
          grid: { color: '#2a2a4a' },
        },
      },
    },
  }
);

const occupancyChart = new Chart(
  document.getElementById('occupancyChart'),
  {
    type: 'bar',
    data: {
      labels: chartData.timestamps,
      datasets: [
        {
          label: 'Occupied',
          data: chartData.occupied,
          backgroundColor: chartData.occupied.map(v =>
            v ? 'rgba(102,187,106,0.7)' : 'rgba(42,42,74,0.5)'
          ),
          borderWidth: 0,
          barPercentage: 1.0,
          categoryPercentage: 1.0,
        },
      ],
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: {
          min: 0, max: 1,
          ticks: {
            color: '#999',
            callback: v => v === 1 ? 'Yes' : 'No',
          },
          title: { display: true, text: 'Occupied', color: '#999' },
          grid: { color: '#2a2a4a' },
        },
      },
    },
  }
);

function addChartPoint(data) {
  const time = new Date(data.ts * 1000).toLocaleTimeString();

  chartData.timestamps.push(time);
  chartData.brightness.push(data.lamp.is_off ? 0 : data.lamp.brightness_pct);
  chartData.lux.push(data.sensor.lux);
  chartData.cct.push(data.lamp.cct_kelvin);
  chartData.occupied.push(data.sensor.occupied ? 1 : 0);

  // Trim to max points
  if (chartData.timestamps.length > MAX_POINTS) {
    chartData.timestamps.shift();
    chartData.brightness.shift();
    chartData.lux.shift();
    chartData.cct.shift();
    chartData.occupied.shift();
  }

  // Update occupancy bar colors
  occupancyChart.data.datasets[0].backgroundColor = chartData.occupied.map(v =>
    v ? 'rgba(102,187,106,0.7)' : 'rgba(42,42,74,0.5)'
  );

  brightnessLuxChart.update('none');
  cctChart.update('none');
  occupancyChart.update('none');
}

// ---- Toast Notifications ----

function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  // Trigger animation
  requestAnimationFrame(() => { toast.classList.add('show'); });

  setTimeout(() => {
    toast.classList.remove('show');
    toast.classList.add('hide');
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

// ---- WebSocket ----

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/live`);

  ws.onopen = () => {
    document.getElementById('connStatus').textContent = 'Connected';
    document.getElementById('connStatus').className = 'conn-status connected';
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateDashboard(data);
    addChartPoint(data);
  };

  ws.onclose = () => {
    document.getElementById('connStatus').textContent = 'Disconnected';
    document.getElementById('connStatus').className = 'conn-status disconnected';
    reconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => { ws.close(); };
}

// ---- Dashboard Updates ----

function updateDashboard(data) {
  // Lamp
  const lampBadge = document.getElementById('lampBadge');
  if (data.lamp.is_off) {
    lampBadge.textContent = 'OFF';
    lampBadge.className = 'badge off';
  } else {
    lampBadge.textContent = 'ON';
    lampBadge.className = 'badge on';
  }
  document.getElementById('lampBrightness').textContent =
    data.lamp.is_off ? '0' : Math.round(data.lamp.brightness_pct);
  document.getElementById('lampCCT').textContent = data.lamp.cct_kelvin;

  // Sensor
  const occBadge = document.getElementById('occupancyBadge');
  if (data.sensor.occupied) {
    occBadge.textContent = 'OCCUPIED';
    occBadge.className = 'badge occupied';
  } else {
    occBadge.textContent = 'VACANT';
    occBadge.className = 'badge vacant';
  }
  document.getElementById('sensorLux').textContent =
    data.sensor.lux !== null ? Math.round(data.sensor.lux) : '--';
  document.getElementById('sensorMotion').textContent =
    data.sensor.moving === true ? 'Yes' : data.sensor.moving === false ? 'No' : '--';
  document.getElementById('sensorAge').textContent =
    data.sensor.age_s !== null ? data.sensor.age_s.toFixed(1) : '--';

  // Stats
  document.getElementById('statRuntime').textContent = formatDuration(data.runtime_s);
  document.getElementById('statEnergy').textContent =
    data.energy_est_wh !== undefined ? data.energy_est_wh.toFixed(1) : '--';
  document.getElementById('statMode').textContent = data.mode.toUpperCase();

  // Mode toggle sync
  syncModeButtons(data.mode);

  // Auto toggle sync — only show auto card in AI mode
  const autoCard = document.getElementById('autoCard');
  if (data.mode === 'ai') {
    autoCard.style.display = '';
    document.getElementById('autoToggle').checked = data.auto;
    document.getElementById('autoDesc').textContent = data.auto
      ? 'Active - lights respond to presence'
      : 'Disabled - manual control only';
  } else {
    autoCard.style.display = 'none';
  }

  // AI panel visibility
  document.getElementById('aiPanel').style.display =
    data.mode === 'ai' ? 'flex' : 'none';

  // Latest decision
  if (data.last_decision && data.last_decision.rationale) {
    const ld = data.last_decision;
    const latestEl = document.getElementById('latestDecision');
    const timeStr = ld.ts_iso ? ld.ts_iso.substring(11) : new Date(ld.ts * 1000).toLocaleTimeString();
    latestEl.querySelector('.decision-time').textContent = timeStr + ' · ' + (ld.mode || '').toUpperCase();
    latestEl.querySelector('.decision-rationale').textContent = ld.rationale;

    // Context badges
    const badgesEl = document.getElementById('latestBadges');
    badgesEl.innerHTML = '';
    if (ld.circadian_phase) {
      badgesEl.appendChild(makeBadge(ld.circadian_phase, 'circadian'));
    }
    if (ld.weather) {
      badgesEl.appendChild(makeBadge(ld.weather, 'weather'));
    }
    if (ld.model_type) {
      badgesEl.appendChild(makeBadge(ld.model_type, 'model'));
    }
  }
}

function makeBadge(text, type) {
  const span = document.createElement('span');
  span.className = `context-badge context-${type}`;
  span.textContent = text;
  return span;
}

function syncModeButtons(mode) {
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
    btn.classList.remove('loading');
  });
}

function formatDuration(seconds) {
  if (!seconds || seconds < 0) return '--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ---- API Calls ----

async function apiPost(endpoint, body) {
  try {
    const resp = await fetch(API + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await resp.json();
  } catch (err) {
    console.error('API error:', err);
    return null;
  }
}

async function apiGet(endpoint) {
  try {
    const resp = await fetch(API + endpoint);
    return await resp.json();
  } catch (err) {
    console.error('API error:', err);
    return null;
  }
}

// ---- Event Handlers ----

// Brightness slider
const brightnessSlider = document.getElementById('brightnessSlider');
const brightnessVal = document.getElementById('brightnessVal');
brightnessSlider.oninput = () => { brightnessVal.textContent = brightnessSlider.value; };
document.getElementById('brightnessBtn').onclick = () => {
  apiPost('/api/lamp/brightness', { pct: parseFloat(brightnessSlider.value) });
};

// CCT slider
const cctSlider = document.getElementById('cctSlider');
const cctVal = document.getElementById('cctVal');
cctSlider.oninput = () => { cctVal.textContent = cctSlider.value; };
document.getElementById('cctBtn').onclick = () => {
  apiPost('/api/lamp/cct', { kelvin: parseInt(cctSlider.value) });
};

// Power buttons
document.getElementById('onBtn').onclick = () => { apiPost('/api/lamp/on', {}); };
document.getElementById('offBtn').onclick = () => { apiPost('/api/lamp/off', {}); };

// Mode toggle buttons
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.onclick = async () => {
    const mode = btn.dataset.mode;
    btn.classList.add('loading');
    try {
      const result = await apiPost('/api/mode', { mode });
      if (result) {
        syncModeButtons(mode);
        showToast(`Switched to ${mode === 'ai' ? 'AI Adaptive' : 'Manual'} mode`, 'success');
        // On first AI activation, check if preferences are completed
        if (mode === 'ai') {
          checkPreferencesOnAiActivation();
        }
      } else {
        btn.classList.remove('loading');
        showToast('Failed to switch mode', 'error');
      }
    } catch (err) {
      console.error('Mode switch error:', err);
      btn.classList.remove('loading');
      showToast('Failed to switch mode', 'error');
    }
  };
});

// Auto toggle
document.getElementById('autoToggle').onchange = async (e) => {
  const enabled = e.target.checked;
  const result = await apiPost('/api/mode', { auto: enabled });
  if (result) {
    document.getElementById('autoDesc').textContent = enabled
      ? 'Active - lights respond to presence'
      : 'Disabled - manual control only';
    showToast(enabled ? 'Auto occupancy enabled' : 'Auto occupancy disabled', 'info');
  }
};

// Train AI models
document.getElementById('trainBtn').onclick = async () => {
  const status = document.getElementById('trainStatus');
  status.textContent = 'Training...';
  const result = await apiPost('/api/ai/train', {});
  status.textContent = result && result.ok ? 'Models trained successfully!' : 'Training failed.';
};

// Download CSV
document.getElementById('downloadBtn').onclick = async () => {
  const runSelect = document.getElementById('runSelect');
  const selected = runSelect.value;
  if (selected === 'live') {
    const runs = await apiGet('/api/telemetry/runs');
    if (runs && runs.length > 0) {
      window.open(`/api/telemetry/download/${runs[0].name}`, '_blank');
    }
  } else {
    window.open(`/api/telemetry/download/${selected}`, '_blank');
  }
};

// Load run list
async function loadRuns() {
  const runs = await apiGet('/api/telemetry/runs');
  if (!runs) return;
  const select = document.getElementById('runSelect');
  while (select.options.length > 1) select.remove(1);
  for (const run of runs) {
    const opt = document.createElement('option');
    opt.value = run.name;
    opt.textContent = `${run.name} (${run.size_kb} KB)`;
    select.appendChild(opt);
  }
}

// Load historical data when a run is selected
document.getElementById('runSelect').onchange = async (e) => {
  if (e.target.value === 'live') return;

  const window_min = parseInt(document.getElementById('chartWindow').value);
  const data = await apiGet(`/api/telemetry/data?run=${e.target.value}&last=${window_min}`);
  if (!data || !Array.isArray(data)) return;

  chartData.timestamps.length = 0;
  chartData.brightness.length = 0;
  chartData.lux.length = 0;
  chartData.cct.length = 0;
  chartData.occupied.length = 0;

  for (const row of data) {
    const ts = row.ts_iso || '';
    const time = ts.length > 11 ? ts.substring(11) : ts;
    chartData.timestamps.push(time);

    const isOff = row.lamp_is_off === 'True' || row.lamp_is_off === 'true';
    const level = parseInt(row.lamp_level) || 0;
    const pct = isOff ? 0 : Math.round((level / 254) * 100);
    chartData.brightness.push(pct);
    chartData.lux.push(parseFloat(row.lux) || 0);

    const dtr = parseInt(row.lamp_temp_dtr) || 16;
    const t = (dtr - 16) / (50 - 16);
    const cctK = Math.round(2700 + t * (6500 - 2700));
    chartData.cct.push(cctK);

    chartData.occupied.push(
      row.filt_occupied === 'True' || row.filt_occupied === 'true' ? 1 : 0
    );
  }

  occupancyChart.data.datasets[0].backgroundColor = chartData.occupied.map(v =>
    v ? 'rgba(102,187,106,0.7)' : 'rgba(42,42,74,0.5)'
  );

  brightnessLuxChart.update();
  cctChart.update();
  occupancyChart.update();
};

// ---- Decision Log ----

async function loadDecisions() {
  const decisions = await apiGet('/api/decisions');
  if (!decisions || !Array.isArray(decisions)) return;

  const logEl = document.getElementById('decisionLog');
  logEl.innerHTML = '';

  const recent = decisions.slice(-50).reverse();

  for (const d of recent) {
    const entry = document.createElement('div');
    entry.className = 'decision-entry';

    const timeStr = d.ts_iso ? d.ts_iso.substring(11) : new Date(d.ts * 1000).toLocaleTimeString();

    let badgesHtml = '';
    if (d.circadian_phase) {
      badgesHtml += `<span class="context-badge context-circadian">${escapeHtml(d.circadian_phase)}</span>`;
    }
    if (d.weather) {
      badgesHtml += `<span class="context-badge context-weather">${escapeHtml(d.weather)}</span>`;
    }
    if (d.model_type) {
      badgesHtml += `<span class="context-badge context-model">${escapeHtml(d.model_type)}</span>`;
    }

    entry.innerHTML =
      `<span class="de-time">${timeStr}</span>` +
      `<span class="de-action">${escapeHtml(d.reason || '')}</span>` +
      `<span class="de-rationale">${escapeHtml(d.rationale || d.action || '')}` +
      (badgesHtml ? `<div class="de-badges">${badgesHtml}</div>` : '') +
      `</span>` +
      `<span class="de-mode">${(d.mode || '').toUpperCase()}</span>`;

    logEl.appendChild(entry);
  }

  if (recent.length > 0) {
    const ld = recent[0];
    const latestEl = document.getElementById('latestDecision');
    const timeStr = ld.ts_iso ? ld.ts_iso.substring(11) : '';
    latestEl.querySelector('.decision-time').textContent = timeStr + ' · ' + (ld.mode || '').toUpperCase();
    latestEl.querySelector('.decision-rationale').textContent = ld.rationale || ld.action || '';

    const badgesEl = document.getElementById('latestBadges');
    badgesEl.innerHTML = '';
    if (ld.circadian_phase) {
      badgesEl.appendChild(makeBadge(ld.circadian_phase, 'circadian'));
    }
    if (ld.weather) {
      badgesEl.appendChild(makeBadge(ld.weather, 'weather'));
    }
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ---- Settings Panel ----

const settingsToggle = document.getElementById('settingsToggle');
const settingsBody = document.getElementById('settingsBody');
const collapseIcon = document.getElementById('collapseIcon');

settingsToggle.onclick = () => {
  const collapsed = settingsBody.classList.toggle('collapsed');
  collapseIcon.textContent = collapsed ? '\u25B6' : '\u25BC';
};

// Settings field mappings
const settingsFields = {
  dim_delay: 'sDimDelay',
  dim_level: 'sDimLevel',
  absence_timeout: 'sAbsenceTimeout',
  eval_interval: 'sEvalInterval',
  brightness_threshold: 'sBrightnessThreshold',
  cct_threshold: 'sCctThreshold',
  nominal_power_watts: 'sNominalPower',
  weather_api_key: 'sWeatherApiKey',
  weather_location: 'sWeatherLocation',
};

async function loadSettings() {
  const settings = await apiGet('/api/settings');
  if (!settings) return;

  for (const [key, elId] of Object.entries(settingsFields)) {
    const el = document.getElementById(elId);
    if (el && settings[key] !== undefined) {
      el.value = settings[key];
    }
  }
}

document.getElementById('settingsSaveBtn').onclick = async () => {
  const payload = {};
  for (const [key, elId] of Object.entries(settingsFields)) {
    const el = document.getElementById(elId);
    if (!el) continue;
    const val = el.value;
    if (val === '' || val === undefined) continue;

    if (['weather_api_key', 'weather_location'].includes(key)) {
      payload[key] = val;
    } else {
      payload[key] = parseFloat(val);
    }
  }

  const result = await apiPost('/api/settings', payload);
  if (result && result.ok) {
    showToast('Settings saved successfully', 'success');
  } else if (result && result.error) {
    showToast('Error: ' + result.error, 'error');
  } else {
    showToast('Failed to save settings', 'error');
  }
};

// ---- Preferences Questionnaire Modal ----

const prefsModal = document.getElementById('preferencesModal');
let wizardStep = 1;
const totalSteps = 4;

function openPrefsModal() {
  // Load current preferences first
  apiGet('/api/preferences').then(prefs => {
    if (prefs) {
      populatePrefsForm(prefs);
    }
    prefsModal.style.display = 'flex';
    setWizardStep(1);
  });
}

function closePrefsModal() {
  prefsModal.style.display = 'none';
}

function populatePrefsForm(p) {
  document.getElementById('prefWakeTime').value = p.wake_time || '07:00';
  document.getElementById('prefSleepTime').value = p.sleep_time || '23:00';
  document.getElementById('prefWorkStart').value = p.work_start || '09:00';
  document.getElementById('prefWorkEnd').value = p.work_end || '17:00';

  setSliderVal('prefMorningBrightness', p.morning_brightness || 70);
  setSliderVal('prefMiddayBrightness', p.midday_brightness || 60);
  setSliderVal('prefEveningBrightness', p.evening_brightness || 50);
  setSliderVal('prefNightBrightness', p.night_brightness || 30);

  setSliderVal('prefMorningCCT', p.morning_cct || 4000);
  setSliderVal('prefMiddayCCT', p.midday_cct || 5500);
  setSliderVal('prefEveningCCT', p.evening_cct || 3000);
  setSliderVal('prefNightCCT', p.night_cct || 2700);

  const warmCoolRadios = document.querySelectorAll('input[name="warmCoolPref"]');
  warmCoolRadios.forEach(r => { r.checked = r.value === (p.warm_cool_preference || 'neutral'); });

  const sensitivityRadios = document.querySelectorAll('input[name="changeSensitivity"]');
  sensitivityRadios.forEach(r => { r.checked = r.value === (p.change_sensitivity || 'medium'); });
}

function setSliderVal(id, val) {
  const slider = document.getElementById(id);
  if (slider) {
    slider.value = val;
    const valEl = document.getElementById(id + 'Val');
    if (valEl) valEl.textContent = val;
  }
}

function setWizardStep(step) {
  wizardStep = step;
  document.querySelectorAll('.wizard-page').forEach(p => p.classList.remove('active'));
  document.getElementById('wizardStep' + step).classList.add('active');

  document.querySelectorAll('.wizard-step').forEach(s => {
    const sStep = parseInt(s.dataset.step);
    s.classList.toggle('active', sStep === step);
    s.classList.toggle('completed', sStep < step);
  });

  document.getElementById('wizardPrevBtn').style.visibility = step === 1 ? 'hidden' : 'visible';
  document.getElementById('wizardNextBtn').textContent = step === totalSteps ? 'Save' : 'Next';
}

function collectPrefsData() {
  const warmCool = document.querySelector('input[name="warmCoolPref"]:checked');
  const sensitivity = document.querySelector('input[name="changeSensitivity"]:checked');

  return {
    wake_time: document.getElementById('prefWakeTime').value,
    sleep_time: document.getElementById('prefSleepTime').value,
    work_start: document.getElementById('prefWorkStart').value,
    work_end: document.getElementById('prefWorkEnd').value,
    morning_brightness: parseInt(document.getElementById('prefMorningBrightness').value),
    midday_brightness: parseInt(document.getElementById('prefMiddayBrightness').value),
    evening_brightness: parseInt(document.getElementById('prefEveningBrightness').value),
    night_brightness: parseInt(document.getElementById('prefNightBrightness').value),
    warm_cool_preference: warmCool ? warmCool.value : 'neutral',
    morning_cct: parseInt(document.getElementById('prefMorningCCT').value),
    midday_cct: parseInt(document.getElementById('prefMiddayCCT').value),
    evening_cct: parseInt(document.getElementById('prefEveningCCT').value),
    night_cct: parseInt(document.getElementById('prefNightCCT').value),
    change_sensitivity: sensitivity ? sensitivity.value : 'medium',
    completed: true,
  };
}

async function savePreferences() {
  const data = collectPrefsData();
  const result = await apiPost('/api/preferences', data);
  if (result && result.ok) {
    showToast('Lighting preferences saved', 'success');
    closePrefsModal();
    updatePrefsStatus(true);
  } else {
    showToast('Failed to save preferences', 'error');
  }
}

function updatePrefsStatus(completed) {
  const statusEl = document.getElementById('prefsStatus');
  if (statusEl) {
    statusEl.textContent = completed ? 'Preferences configured' : 'Not configured';
    statusEl.className = 'prefs-status ' + (completed ? 'configured' : 'not-configured');
  }
}

async function checkPreferencesOnAiActivation() {
  const prefs = await apiGet('/api/preferences');
  if (prefs && !prefs.completed) {
    openPrefsModal();
  }
  updatePrefsStatus(prefs && prefs.completed);
}

// Wizard navigation
document.getElementById('wizardNextBtn').onclick = () => {
  if (wizardStep < totalSteps) {
    setWizardStep(wizardStep + 1);
  } else {
    savePreferences();
  }
};

document.getElementById('wizardPrevBtn').onclick = () => {
  if (wizardStep > 1) {
    setWizardStep(wizardStep - 1);
  }
};

document.getElementById('prefsCloseBtn').onclick = closePrefsModal;
prefsModal.onclick = (e) => {
  if (e.target === prefsModal) closePrefsModal();
};

// Open preferences from AI panel and settings panel
document.getElementById('openPrefsBtn').onclick = openPrefsModal;
document.getElementById('settingsPrefsBtn').onclick = openPrefsModal;

// Wizard step indicators are clickable
document.querySelectorAll('.wizard-step').forEach(s => {
  s.onclick = () => setWizardStep(parseInt(s.dataset.step));
});

// Bind slider value displays
['prefMorningBrightness', 'prefMiddayBrightness', 'prefEveningBrightness', 'prefNightBrightness',
 'prefMorningCCT', 'prefMiddayCCT', 'prefEveningCCT', 'prefNightCCT'].forEach(id => {
  const slider = document.getElementById(id);
  if (slider) {
    slider.oninput = () => {
      const valEl = document.getElementById(id + 'Val');
      if (valEl) valEl.textContent = slider.value;
    };
  }
});

// ---- Init ----
connectWS();
loadRuns();
loadDecisions();
loadSettings();
setInterval(loadDecisions, 30000);

// Load preferences status on init
apiGet('/api/preferences').then(prefs => {
  if (prefs) updatePrefsStatus(prefs.completed);
});

// Fetch initial status
apiGet('/api/status').then(data => {
  if (data) {
    if (!data.lamp.is_off) {
      brightnessSlider.value = Math.round(data.lamp.brightness_pct);
      brightnessVal.textContent = Math.round(data.lamp.brightness_pct);
    }
    cctSlider.value = data.lamp.cct_kelvin;
    cctVal.textContent = data.lamp.cct_kelvin;
    syncModeButtons(data.mode);
    document.getElementById('autoToggle').checked = data.auto;
  }
});
