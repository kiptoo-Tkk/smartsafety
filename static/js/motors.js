// Shared helpers for the motor portal. Relies on common.js for fetchJSON/setTheme/toggleSidebar/setLiveStatus.
const POLL_MS = (parseInt(localStorage.getItem('portal_poll_seconds'), 10) || 3) * 1000;
const CHART_MS = (parseInt(localStorage.getItem('portal_chart_poll_seconds'), 10) || 10) * 1000;

function fmtEta(m) { return m < 90 ? Math.round(m) + ' min' : (m / 60).toFixed(1) + ' h'; }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function chip(status) { const s = status || 'OFFLINE'; return '<span class="chip ' + esc(s.toLowerCase()) + '">' + esc(s) + '</span>'; }
function $(id) { return document.getElementById(id); }

function postJSON(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})})
        .then(r => r.json().catch(() => ({})).then(d => Object.assign({ok: r.ok}, d)))
        .catch(() => ({ok: false, error: 'Could not reach the Pi'}));
}

function updateBadges(f) {
    const b = $('badge-alerts'); if (!b || !f || !f.summary) return;
    b.textContent = f.summary.alerts || ''; b.style.display = f.summary.alerts ? '' : 'none';
}

function pollFleet(render) {
    const run = () => fetchJSON('/api/fleet').then(f => { updateBadges(f); render(f); }).catch(() => {});
    run(); setInterval(run, POLL_MS);
}

// Keeps a <select> of motors in sync; remembers the choice across pages (?motor=id overrides).
function fillMotorSelect(sel, motors) {
    const cur = sel.value || new URLSearchParams(location.search).get('motor') || localStorage.getItem('portal_motor') || '';
    const key = motors.map(m => m.id + '|' + m.name).join(',');
    if (sel.dataset.key !== key) {
        sel.innerHTML = motors.map(m => '<option value="' + esc(m.id) + '">' + esc(m.name) + '</option>').join('');
        sel.dataset.key = key;
    }
    sel.value = motors.some(m => m.id === cur) ? cur : (motors[0] ? motors[0].id : '');
    localStorage.setItem('portal_motor', sel.value);
}

// 0-100 fill and colour class for a reading against its limit (warning margins mirror the firmware)
function level(value, max, warnDelta) {
    const state = value >= max ? 'bad' : (value >= max - warnDelta ? 'warn' : 'ok');
    return {pct: Math.max(0, Math.min(100, max ? value / max * 100 : 0)), state: state};
}

// ---- overall condition chart: every sensor on one 0-100 % scale, plus a thick TOTAL line (= worst sensor) ----
const COND_COLORS = {Temperature: '#FF3377', Humidity: '#00B4D8', Smoke: '#FFC300', Sound: '#0077B6', Vibration: '#8E44AD'};
function makeCondChart(canvas) {
    const bands = {id: 'bands', beforeDatasetsDraw(c) {
        const a = c.chartArea, y = c.scales.y, ctx = c.ctx;
        [[0, 50, 'rgba(46,204,113,.10)'], [50, 80, 'rgba(243,156,18,.13)'], [80, 100, 'rgba(231,76,60,.15)']].forEach(b => {
            const y1 = y.getPixelForValue(b[1]), y0 = y.getPixelForValue(b[0]);
            ctx.fillStyle = b[2]; ctx.fillRect(a.left, y1, a.right - a.left, y0 - y1);
        });
    }};
    const ds = Object.keys(COND_COLORS).map(n => ({label: n, data: [], borderColor: COND_COLORS[n], borderWidth: 1.3, pointRadius: 0, tension: 0.3, spanGaps: true, fill: false}));
    ds.push({label: 'TOTAL CONDITION', data: [], borderColor: getComputedStyle(document.body).color || '#000', borderWidth: 3.5, pointRadius: 0, tension: 0.3, spanGaps: true, fill: false});
    const ch = new Chart(canvas, {type: 'line', data: {labels: [], datasets: ds}, plugins: [bands], options: {
        responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: 'index', intersect: false},
        plugins: {legend: {position: 'bottom'}, tooltip: {callbacks: {footer: it => (ch.drivers && ch.drivers[it[0].dataIndex]) ? 'Driven by: ' + ch.drivers[it[0].dataIndex] : ''}}},
        scales: {x: {ticks: {maxTicksLimit: 8, autoSkip: true}}, y: {min: 0, max: 100, title: {display: true, text: '% toward limit'}}}}});
    return ch;
}
function drawCond(ch, d, chipEl) {
    if (!d || !d.labels) return;
    ch.data.labels = d.labels;
    ch.data.datasets.forEach(s => { s.data = s.label === 'TOTAL CONDITION' ? d.total : (d.sensors[s.label] || []); });
    ch.drivers = d.driver; ch.update('none');
    if (chipEl) {
        const n = d.now;
        chipEl.textContent = n ? n.index + '% ' + n.band + ' - ' + n.driver : 'Collecting data...';
        chipEl.className = 'chip ' + (!n ? 'offline' : n.band === 'Critical' ? 'danger' : n.band === 'Watch' ? 'warning' : 'safe');
    }
}
