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

  // Mode/Auto sync
  document.getElementById('modeSelect').value = data.mode;
  document.getElementById('autoToggle').checked = data.auto;

  // AI panel visibility
  document.getElementById('aiPanel').style.display =
    data.mode === 'ai' ? 'flex' : 'none';
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

// Mode select
document.getElementById('modeSelect').onchange = (e) => {
  apiPost('/api/mode', { mode: e.target.value });
};

// Auto toggle
document.getElementById('autoToggle').onchange = (e) => {
  apiPost('/api/mode', { auto: e.target.checked });
};

// Nominal power
document.getElementById('powerBtn').onclick = () => {
  const watts = parseFloat(document.getElementById('powerInput').value);
  if (watts > 0) apiPost('/api/config/power', { nominal_power_watts: watts });
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
    // Download current run — get the latest run name
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
  // Keep the "Live Data" option
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

  // Clear and repopulate charts
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

    // Approximate CCT from DTR
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

// ---- Init ----
connectWS();
loadRuns();

// Fetch initial status
apiGet('/api/status').then(data => {
  if (data) {
    // Set sliders to current values
    if (!data.lamp.is_off) {
      brightnessSlider.value = Math.round(data.lamp.brightness_pct);
      brightnessVal.textContent = Math.round(data.lamp.brightness_pct);
    }
    cctSlider.value = data.lamp.cct_kelvin;
    cctVal.textContent = data.lamp.cct_kelvin;
    document.getElementById('powerInput').value = data.nominal_power_watts || 40;
  }
});
