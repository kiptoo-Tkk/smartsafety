// Shared across all pages - sidebar toggle + live connection indicator helper.
// Kept intentionally tiny: page-specific data/chart logic lives only in the
// pages that actually need it, so inactive pages load none of that JS at all.

function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
}

function setLiveStatus(connected) {
    const dot = document.getElementById('live-dot');
    const text = document.getElementById('live-text');
    if (!dot || !text) return;
    dot.classList.remove('connected', 'disconnected');
    dot.classList.add(connected ? 'connected' : 'disconnected');
    text.innerText = connected ? 'Live' : 'Reconnecting...';
}

function fetchJSON(url) {
    return fetch(url).then(r => r.json());
}

// Called by every page's own existing data update function - reuses data
// each page already fetches, so this adds ZERO extra network requests.
// Lets you see "something needs attention" from anywhere in the app.
function updateNavBadges(state) {
    if (!state) return;
    const aiBadge = document.getElementById('badge-ai');
    if (aiBadge) {
        if (state.ai_pest_detected) { aiBadge.style.display = 'inline-block'; aiBadge.innerText = '!'; }
        else { aiBadge.style.display = 'none'; }
    }
    const alertBadge = document.getElementById('badge-alerts');
    if (alertBadge) {
        const count = (state.sensor_faults || []).length + (state.voc_triggered ? 1 : 0);
        if (count > 0) { alertBadge.style.display = 'inline-block'; alertBadge.innerText = count; }
        else { alertBadge.style.display = 'none'; }
    }
}

// Theme persistence - applied on every page load before anything renders
// visibly, using localStorage so the choice survives across all pages and
// browser restarts, with zero backend involvement (purely cosmetic).
(function applyStoredTheme() {
    const saved = localStorage.getItem('greenhouse_theme') || 'light';
    if (saved === 'dark') document.body.classList.add('theme-dark');
})();

function setTheme(value) {
    localStorage.setItem('greenhouse_theme', value);
    document.body.classList.toggle('theme-dark', value === 'dark');
}
