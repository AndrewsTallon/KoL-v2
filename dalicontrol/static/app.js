// KoL Lighting Control Dashboard - Frontend Logic

const API = '';  // Same origin
let ws = null;
let reconnectTimer = null;

const LOADING_MIN_MS = 1400;
const loadingStartedAt = performance.now();

function finishDashboardLoading() {
  const overlay = document.getElementById('dashboardLoadingOverlay');
  if (!overlay) return;

  const elapsed = performance.now() - loadingStartedAt;
  const wait = Math.max(0, LOADING_MIN_MS - elapsed);
  window.setTimeout(() => {
    overlay.classList.add('is-hiding');
    overlay.addEventListener('transitionend', () => {
      overlay.hidden = true;
    }, { once: true });
  }, wait);
}

window.addEventListener('load', finishDashboardLoading);

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
    if (Number(ld.weather_brightness_adjust_pct || 0) !== 0) {
      badgesEl.appendChild(makeBadge(`weather ${formatSignedPct(ld.weather_brightness_adjust_pct)}`, 'weather'));
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

// ---- Weather Status ----

function setWeatherBadge(text, className) {
  const badge = document.getElementById('weatherStatusBadge');
  badge.textContent = text;
  badge.className = `weather-status-badge ${className}`;
}

function renderWeatherForecast(forecast) {
  const list = document.getElementById('weatherForecastList');
  list.innerHTML = '';
  (forecast || []).slice(0, 8).forEach(item => {
    const card = document.createElement('div');
    card.className = 'weather-forecast-item';
    const when = item.time
      ? new Date(item.time.replace(' ', 'T') + 'Z').toLocaleString([], {
          weekday: 'short',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '--';
    const temp = item.temp_c !== null && item.temp_c !== undefined
      ? `${Math.round(item.temp_c)} C`
      : '--';
    const pop = item.pop !== null && item.pop !== undefined
      ? `Rain ${Math.round(item.pop * 100)}%`
      : '';
    card.innerHTML =
      `<strong>${escapeHtml(when)}</strong>` +
      `<span>${escapeHtml(item.condition || 'Unknown')}, ${escapeHtml(temp)}</span>` +
      (pop ? `<span>${escapeHtml(pop)}</span>` : '');
    list.appendChild(card);
  });
}

function renderWeatherStatus(data) {
  const locationEl = document.getElementById('weatherLocationLabel');
  const summaryEl = document.getElementById('weatherCurrentSummary');
  const updatedEl = document.getElementById('weatherUpdatedAt');

  if (!data || !data.configured) {
    setWeatherBadge('Not configured', 'weather-muted');
    locationEl.textContent = '--';
    summaryEl.textContent = 'Add an OpenWeather API key and verified location.';
    updatedEl.textContent = '';
    renderWeatherForecast([]);
    return;
  }

  if (!data.ok) {
    const notVerified = data.error && data.error.toLowerCase().includes('verified');
    setWeatherBadge(notVerified ? 'Location not verified' : 'Error', 'weather-error');
    locationEl.textContent = data.location && data.location.label ? data.location.label : '--';
    summaryEl.textContent = data.error || 'Weather check failed.';
    updatedEl.textContent = '';
    renderWeatherForecast([]);
    return;
  }

  setWeatherBadge('Live', 'weather-live');
  locationEl.textContent = data.location && data.location.label ? data.location.label : '--';
  const current = data.current || {};
  const temp = current.temp_c !== null && current.temp_c !== undefined
    ? `${Math.round(current.temp_c)} C`
    : '--';
  const humidity = current.humidity !== null && current.humidity !== undefined
    ? `Humidity ${current.humidity}%`
    : '';
  summaryEl.textContent = [current.condition, temp, humidity].filter(Boolean).join(' - ');
  updatedEl.textContent = data.fetched_at
    ? `Checked ${new Date(data.fetched_at).toLocaleTimeString()}`
    : '';
  renderWeatherForecast(data.forecast || []);
}

async function loadWeatherStatus(showToastOnResult = false) {
  setWeatherBadge('Checking', 'weather-muted');
  const data = await apiGet('/api/weather/status');
  renderWeatherStatus(data);
  if (showToastOnResult && data && data.configured) {
    if (data.ok) {
      showToast('Weather API check passed', 'success');
    } else if (data.error) {
      showToast('Weather check: ' + data.error, 'error');
    }
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
  status.textContent = result && result.ok
    ? 'Models trained successfully!'
    : (result && result.error ? result.error : 'Training failed.');
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
    if (Number(d.weather_brightness_adjust_pct || 0) !== 0) {
      badgesHtml += `<span class="context-badge context-weather">weather ${escapeHtml(formatSignedPct(d.weather_brightness_adjust_pct))}</span>`;
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
    if (Number(ld.weather_brightness_adjust_pct || 0) !== 0) {
      badgesEl.appendChild(makeBadge(`weather ${formatSignedPct(ld.weather_brightness_adjust_pct)}`, 'weather'));
    }
  }
}

function formatSignedPct(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? '+' : '';
  return `${sign}${Math.round(n)}%`;
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
const apiSettingsKeys = new Set([
  'openai_api_key',
  'openai_model',
  'weather_api_key',
  'weather_location',
  'weather_location_label',
]);

const coordinateSettingsKeys = new Set(['weather_lat', 'weather_lon']);

const settingsFields = {
  dim_delay: 'sDimDelay',
  dim_level: 'sDimLevel',
  absence_timeout: 'sAbsenceTimeout',
  eval_interval: 'sEvalInterval',
  brightness_threshold: 'sBrightnessThreshold',
  cct_threshold: 'sCctThreshold',
  dali_command_gap_s: 'sDaliCommandGap',
  brightness_feedback_window_s: 'sBrightnessFeedbackWindow',
  brightness_feedback_min_lux_delta: 'sBrightnessFeedbackMinLux',
  nominal_power_watts: 'sNominalPower',
  openai_api_key: 'sOpenAiApiKey',
  openai_model: 'sOpenAiModel',
  weather_api_key: 'sWeatherApiKey',
  weather_location: 'sWeatherLocation',
  weather_lat: 'sWeatherLat',
  weather_lon: 'sWeatherLon',
  weather_location_label: 'sWeatherLocationLabel',
};

const onboardingApiFields = {
  openai_api_key: 'onboardingOpenAiApiKey',
  openai_model: 'onboardingOpenAiModel',
  weather_api_key: 'onboardingWeatherApiKey',
  weather_location: 'onboardingWeatherLocation',
  weather_lat: 'onboardingWeatherLat',
  weather_lon: 'onboardingWeatherLon',
  weather_location_label: 'onboardingWeatherLocationLabel',
};

function populateFieldMap(fieldMap, values) {
  for (const [key, elId] of Object.entries(fieldMap)) {
    const el = document.getElementById(elId);
    if (el && values[key] !== undefined) {
      el.value = values[key];
    }
  }
}

function collectSettingsPayload(fieldMap, includeEmptyApiValues = false) {
  const payload = {};
  for (const [key, elId] of Object.entries(fieldMap)) {
    const el = document.getElementById(elId);
    if (!el) continue;
    const val = el.value;

    if (coordinateSettingsKeys.has(key)) {
      if (val === '' || val === undefined) {
        if (includeEmptyApiValues) payload[key] = null;
        continue;
      }
      payload[key] = parseFloat(val);
      continue;
    }

    if (apiSettingsKeys.has(key)) {
      if (val === '' && !includeEmptyApiValues) continue;
      payload[key] = val;
      continue;
    }

    if (val === '' || val === undefined) continue;
    payload[key] = parseFloat(val);
  }
  return payload;
}

function getWeatherUi(prefix) {
  return {
    apiKey: document.getElementById(`${prefix}WeatherApiKey`),
    location: document.getElementById(`${prefix}WeatherLocation`),
    lat: document.getElementById(`${prefix}WeatherLat`),
    lon: document.getElementById(`${prefix}WeatherLon`),
    label: document.getElementById(`${prefix}WeatherLocationLabel`),
    findBtn: document.getElementById(`${prefix}WeatherFindBtn`),
    status: document.getElementById(`${prefix}WeatherVerifyStatus`),
    candidates: document.getElementById(`${prefix}WeatherCandidates`),
  };
}

function setLocationStatus(ui, message, className = '') {
  if (!ui.status) return;
  ui.status.textContent = message;
  ui.status.className = `weather-verify-status ${className}`.trim();
}

function clearLocationVerification(ui) {
  if (ui.lat) ui.lat.value = '';
  if (ui.lon) ui.lon.value = '';
  if (ui.label) ui.label.value = '';
  if (ui.candidates) ui.candidates.innerHTML = '';
  setLocationStatus(ui, 'Choose a verified location before saving.');
}

function applyWeatherSettingsToUi(prefix, settings) {
  const ui = getWeatherUi(prefix);
  if (!ui.location) return;
  if (settings.weather_lat !== null && settings.weather_lat !== undefined) {
    ui.lat.value = settings.weather_lat;
  }
  if (settings.weather_lon !== null && settings.weather_lon !== undefined) {
    ui.lon.value = settings.weather_lon;
  }
  if (settings.weather_location_label) {
    ui.label.value = settings.weather_location_label;
    ui.location.value = settings.weather_location_label;
  }

  if (ui.lat.value && ui.lon.value) {
    setLocationStatus(ui, `Verified: ${ui.label.value || ui.location.value}`, 'verified');
  } else {
    clearLocationVerification(ui);
    if (settings.weather_location) {
      ui.location.value = settings.weather_location;
      setLocationStatus(ui, 'Location is not verified yet.', 'error');
    }
  }
}

function locationLabel(candidate) {
  return candidate.label || [candidate.name, candidate.state, candidate.country].filter(Boolean).join(', ');
}

function renderLocationCandidates(ui, locations) {
  ui.candidates.innerHTML = '';
  if (!locations || locations.length === 0) {
    setLocationStatus(ui, 'No matching locations found.', 'error');
    return;
  }

  locations.forEach(candidate => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'location-candidate';
    btn.textContent = locationLabel(candidate);
    btn.onclick = () => {
      const label = locationLabel(candidate);
      ui.location.value = label;
      ui.lat.value = candidate.lat;
      ui.lon.value = candidate.lon;
      ui.label.value = label;
      ui.candidates.innerHTML = '';
      setLocationStatus(ui, `Verified: ${label}`, 'verified');
    };
    ui.candidates.appendChild(btn);
  });
  setLocationStatus(ui, 'Select the matching location.');
}

async function findWeatherLocation(prefix) {
  const ui = getWeatherUi(prefix);
  const query = (ui.location.value || '').trim();
  const apiKey = (ui.apiKey.value || '').trim();
  if (!apiKey) {
    setLocationStatus(ui, 'Add the OpenWeather API key first.', 'error');
    return;
  }
  if (!query) {
    setLocationStatus(ui, 'Type a location to search.', 'error');
    return;
  }

  setLocationStatus(ui, 'Searching...');
  const result = await apiPost('/api/weather/locations', { query, api_key: apiKey });
  if (result && result.ok) {
    renderLocationCandidates(ui, result.locations || []);
  } else {
    setLocationStatus(ui, result && result.error ? result.error : 'Location search failed.', 'error');
  }
}

function bindWeatherLocationUi(prefix) {
  const ui = getWeatherUi(prefix);
  if (!ui.location || !ui.findBtn) return;
  ui.findBtn.onclick = () => findWeatherLocation(prefix);
  ui.location.addEventListener('input', () => clearLocationVerification(ui));
}

async function loadSettings() {
  const settings = await apiGet('/api/settings');
  if (!settings) return;
  populateFieldMap(settingsFields, settings);
  populateFieldMap(onboardingApiFields, settings);
  applyWeatherSettingsToUi('s', settings);
  applyWeatherSettingsToUi('onboarding', settings);
}

document.getElementById('settingsSaveBtn').onclick = async () => {
  const payload = collectSettingsPayload(settingsFields, true);
  if (payload.weather_lat === null || payload.weather_lon === null) {
    payload.weather_location = '';
    payload.weather_location_label = '';
  }

  const result = await apiPost('/api/settings', payload);
  if (result && result.ok) {
    populateFieldMap(settingsFields, result.settings || {});
    populateFieldMap(onboardingApiFields, result.settings || {});
    applyWeatherSettingsToUi('s', result.settings || {});
    applyWeatherSettingsToUi('onboarding', result.settings || {});
    showToast('Settings saved successfully', 'success');
    await loadWeatherStatus(true);
  } else if (result && result.error) {
    showToast('Error: ' + result.error, 'error');
  } else {
    showToast('Failed to save settings', 'error');
  }
};

bindWeatherLocationUi('s');
bindWeatherLocationUi('onboarding');

// ---- Participant Profiles and Questionnaires ----

let activeProfile = null;

const AGE_OPTIONS = ['18-29', '30-39', '40-49', '50-59', '60+'];
const FINAL_QUESTIONS = [
  ['q1', 'The brightness level of the lighting was appropriate for my work tasks.'],
  ['q2', 'The lighting provided sufficient illumination at my workstation.'],
  ['q3', 'The lighting conditions were visually comfortable during my work.'],
  ['q4', 'The lighting did not cause glare or visual discomfort.'],
  ['q5', 'The lighting conditions remained stable and comfortable during my work.'],
  ['q6', 'The color tone of the light (warm or cool) felt appropriate for my work activities.'],
  ['q7', 'Overall, I am satisfied with the lighting conditions at my workstation.'],
  ['q8', 'I noticed changes in the lighting conditions during my work.'],
  ['q9', 'The lighting changes were disturbing.'],
  ['q10', 'AI Phase Only: Compared to the previous lighting control, I prefer the adaptive lighting system.'],
];

function participantInfoFields(prefix) {
  const ageOptions = AGE_OPTIONS.map(age =>
    `<option value="${age}">${age}</option>`
  ).join('');
  return `
    <div class="pref-field">
      <label for="${prefix}AgeGroup">Age group</label>
      <select id="${prefix}AgeGroup" required>
        <option value="">Select age group</option>
        ${ageOptions}
      </select>
    </div>
    <div class="pref-field">
      <label>Do you normally use glasses or contact lenses while working?</label>
      <div class="pref-radio-group">
        <label class="pref-radio"><input type="radio" name="${prefix}Glasses" value="yes" required> Yes</label>
        <label class="pref-radio"><input type="radio" name="${prefix}Glasses" value="no" required> No</label>
      </div>
    </div>
    <div class="pref-field">
      <label>I generally prefer brighter lighting while working.</label>
      ${likertRadios(`${prefix}BrighterPreference`, true)}
    </div>
  `;
}

function likertRadios(name, required = false) {
  return `<div class="likert-row">${[1, 2, 3, 4, 5].map(v => `
    <label class="likert-option">
      <input type="radio" name="${name}" value="${v}" ${required ? 'required' : ''}>
      <span>${v}</span>
    </label>
  `).join('')}</div>`;
}

function collectParticipantInfo(prefix) {
  const glasses = document.querySelector(`input[name="${prefix}Glasses"]:checked`);
  const brighter = document.querySelector(`input[name="${prefix}BrighterPreference"]:checked`);
  return {
    age_group: document.getElementById(`${prefix}AgeGroup`).value,
    glasses_or_contacts: glasses ? glasses.value : '',
    brighter_lighting_preference: brighter ? parseInt(brighter.value) : null,
  };
}

function populateParticipantInfo(prefix, info) {
  if (!info) return;
  document.getElementById(`${prefix}AgeGroup`).value = info.age_group || '';
  const glasses = document.querySelector(`input[name="${prefix}Glasses"][value="${info.glasses_or_contacts}"]`);
  if (glasses) glasses.checked = true;
  const brighter = document.querySelector(
    `input[name="${prefix}BrighterPreference"][value="${info.brighter_lighting_preference}"]`
  );
  if (brighter) brighter.checked = true;
}

function renderProfileList(profiles) {
  const list = document.getElementById('profileList');
  list.innerHTML = '';
  if (!profiles || profiles.length === 0) {
    list.innerHTML = '<p class="empty-state">No profiles yet.</p>';
    return;
  }
  profiles.forEach(profile => {
    const btn = document.createElement('button');
    btn.className = 'profile-list-item';
    btn.type = 'button';
    btn.textContent = profile.display_name;
    btn.onclick = () => selectProfile(profile.profile_id);
    list.appendChild(btn);
  });
}

function updateProfileUi(profile) {
  activeProfile = profile || null;
  document.getElementById('activeProfileName').textContent =
    activeProfile ? activeProfile.display_name : 'None';
  const status = document.getElementById('profileModelStatus');
  if (status) {
    status.textContent = activeProfile
      ? `Active profile: ${activeProfile.display_name}`
      : 'Select a participant profile before training.';
  }
}

async function loadProfiles(showGateIfMissing = true) {
  const data = await apiGet('/api/profiles');
  if (!data) return;
  renderProfileList(data.profiles || []);
  const active = (data.profiles || []).find(p => p.profile_id === data.active_profile_id);
  if (active) {
    updateProfileUi(active);
    if (showGateIfMissing) {
      document.getElementById('profileGateOverlay').style.display = 'none';
    }
    await loadFinalEvaluationStatus();
  } else if (showGateIfMissing) {
    updateProfileUi(null);
    document.getElementById('profileGateOverlay').style.display = 'flex';
  }
}

async function selectProfile(profileId) {
  const result = await apiPost('/api/profiles/select', { profile_id: profileId });
  if (result && result.ok) {
    if (!await saveOnboardingApiSettings()) return;
    updateProfileUi(result.profile);
    document.getElementById('profileGateOverlay').style.display = 'none';
    await loadProfiles(false);
    await loadFinalEvaluationStatus();
    showToast('Profile selected', 'success');
  } else {
    showToast(result && result.error ? result.error : 'Failed to select profile', 'error');
  }
}

document.getElementById('createProfileForm').onsubmit = async (e) => {
  e.preventDefault();
  const payload = {
    display_name: document.getElementById('newProfileName').value.trim(),
    participant_info: collectParticipantInfo('create'),
  };
  const result = await apiPost('/api/profiles', payload);
  if (result && result.ok) {
    if (!await saveOnboardingApiSettings()) return;
    document.getElementById('createProfileForm').reset();
    updateProfileUi(result.profile);
    document.getElementById('profileGateOverlay').style.display = 'none';
    await loadProfiles(false);
    await loadFinalEvaluationStatus();
    showToast('Profile created', 'success');
  } else {
    showToast(result && result.error ? result.error : 'Failed to create profile', 'error');
  }
};

async function saveOnboardingApiSettings() {
  const payload = collectSettingsPayload(onboardingApiFields, false);
  if (payload.weather_location && (
    payload.weather_lat === undefined || payload.weather_lon === undefined
  )) {
    payload.weather_location = '';
    payload.weather_location_label = '';
    payload.weather_lat = null;
    payload.weather_lon = null;
  }
  if (Object.keys(payload).length === 0) {
    return true;
  }
  const result = await apiPost('/api/settings', payload);
  if (result && result.ok) {
    populateFieldMap(settingsFields, result.settings || {});
    populateFieldMap(onboardingApiFields, result.settings || {});
    applyWeatherSettingsToUi('s', result.settings || {});
    applyWeatherSettingsToUi('onboarding', result.settings || {});
    await loadWeatherStatus(true);
    return true;
  }
  showToast(result && result.error ? result.error : 'Failed to save API keys', 'error');
  return false;
}

document.getElementById('switchProfileBtn').onclick = () => {
  loadProfiles(false);
  document.getElementById('profileGateOverlay').style.display = 'flex';
};

async function openParticipantInfoModal() {
  if (!activeProfile) {
    document.getElementById('profileGateOverlay').style.display = 'flex';
    return;
  }
  const info = await apiGet('/api/profile/participant-info');
  document.getElementById('participantInfoForm').reset();
  populateParticipantInfo('edit', info);
  document.getElementById('participantInfoModal').style.display = 'flex';
}

function closeParticipantInfoModal() {
  document.getElementById('participantInfoModal').style.display = 'none';
}

document.getElementById('participantInfoForm').onsubmit = async (e) => {
  e.preventDefault();
  const result = await apiPost('/api/profile/participant-info', collectParticipantInfo('edit'));
  if (result && result.ok) {
    closeParticipantInfoModal();
    showToast('Participant info saved', 'success');
  } else {
    showToast(result && result.error ? result.error : 'Failed to save participant info', 'error');
  }
};

document.getElementById('participantInfoCloseBtn').onclick = closeParticipantInfoModal;
document.getElementById('participantInfoCancelBtn').onclick = closeParticipantInfoModal;
document.getElementById('settingsParticipantInfoBtn').onclick = openParticipantInfoModal;

function renderFinalQuestions() {
  const container = document.getElementById('finalQuestionFields');
  container.innerHTML = FINAL_QUESTIONS.map(([key, text]) => `
    <div class="final-question">
      <label>${key.toUpperCase()}. ${text}</label>
      ${likertRadios(key, true)}
    </div>
  `).join('');
}

function collectFinalEvaluation() {
  const payload = {};
  FINAL_QUESTIONS.forEach(([key]) => {
    const selected = document.querySelector(`input[name="${key}"]:checked`);
    payload[key] = selected ? parseInt(selected.value) : null;
  });
  payload.comments = document.getElementById('finalComments').value.trim();
  return payload;
}

function closeFinalEvaluationModal() {
  document.getElementById('finalEvaluationModal').style.display = 'none';
}

async function loadFinalEvaluationStatus() {
  const status = document.getElementById('finalEvalStatus');
  if (!activeProfile) {
    status.textContent = 'No active profile';
    return;
  }
  const data = await apiGet('/api/profile/final-evaluations');
  const evaluations = data && data.evaluations ? data.evaluations : [];
  if (evaluations.length === 0) {
    status.textContent = 'No final evaluation submitted yet';
  } else {
    const last = evaluations[0];
    status.textContent = `Last submitted: ${last.submitted_at || last.filename}`;
  }
}

document.getElementById('finalEvalBtn').onclick = () => {
  if (!activeProfile) {
    document.getElementById('profileGateOverlay').style.display = 'flex';
    return;
  }
  document.getElementById('finalEvaluationForm').reset();
  document.getElementById('finalEvaluationModal').style.display = 'flex';
};

document.getElementById('finalEvaluationForm').onsubmit = async (e) => {
  e.preventDefault();
  const result = await apiPost('/api/profile/final-evaluations', collectFinalEvaluation());
  if (result && result.ok) {
    closeFinalEvaluationModal();
    await loadFinalEvaluationStatus();
    showToast('Final evaluation saved', 'success');
  } else {
    showToast(result && result.error ? result.error : 'Failed to save final evaluation', 'error');
  }
};

document.getElementById('finalEvalCloseBtn').onclick = closeFinalEvaluationModal;
document.getElementById('finalEvalCancelBtn').onclick = closeFinalEvaluationModal;

document.getElementById('participantInfoModal').onclick = (e) => {
  if (e.target.id === 'participantInfoModal') closeParticipantInfoModal();
};
document.getElementById('finalEvaluationModal').onclick = (e) => {
  if (e.target.id === 'finalEvaluationModal') closeFinalEvaluationModal();
};

document.getElementById('createParticipantInfoFields').innerHTML = participantInfoFields('create');
document.getElementById('editParticipantInfoFields').innerHTML = participantInfoFields('edit');
renderFinalQuestions();

// ---- Init ----
connectWS();
loadRuns();
loadDecisions();
loadSettings();
loadWeatherStatus();
loadProfiles();
setInterval(loadDecisions, 30000);

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
