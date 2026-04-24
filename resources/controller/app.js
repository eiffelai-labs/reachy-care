/* ============================================
   Reachy Care Controller — Vanilla JS SPA
   Hash-based routing, status polling, power
   control, modules, modes, persons, settings,
   logs (SSE).
   ============================================ */

// --- Constants ---

const CATEGORY_EMOJI = {
    medication: '\u{1F48A}',
    repas:      '\u{1F37D}\uFE0F',
    visite:     '\u{1F465}',
    lecture:    '\u{1F4D6}',
    activite:   '\u{1F3AF}',
    humeur:     '\u{1F60A}',
    sante:      '\u{2764}\uFE0F',
    sommeil:    '\u{1F634}',
    note:       '\u{1F4DD}'
};

const MODULE_NAMES = {
    face:         'Vision',
    chess:        'Echecs',
    wake_word:    'Wake Word',
    sound:        'Detection son',
    fall:         'Detection chute',
    conversation: 'Conversation'
};

const PAGES = ['control', 'persons', 'settings', 'logs'];

let logSource = null;
let startLogSource = null;
let opPollTimer = null;
let confirmCallback = null;
let promptCallback = null;

// --- Router ---

function navigate(page) {
    if (!PAGES.includes(page)) page = 'control';

    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const target = document.getElementById('page-' + page);
    if (target) target.classList.add('active');

    document.querySelectorAll('.nav-item').forEach(a => {
        a.classList.toggle('active', a.dataset.page === page);
    });

    if (page === 'persons') loadPersons();
    if (page === 'settings') { loadSettings(); btLoadStatus(); }
}

function onHashChange() {
    const hash = location.hash.replace('#', '') || 'control';
    navigate(hash);
}

// --- Status polling ---

async function fetchStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        const state = data.state || 'off';

        // Header status
        const dot = document.getElementById('status-dot');
        dot.className = 'status-dot ' + state;
        document.getElementById('status-label').textContent =
            state === 'running' ? 'En ligne' :
            state === 'sleeping' ? 'En veille' :
            state === 'off' ? 'Eteint' : 'Erreur';
        document.getElementById('status-uptime').textContent =
            state !== 'off' ? formatUptime(data.uptime) : '';

        // Robot avatar
        const avatar = document.querySelector('.robot-avatar');
        if (avatar) {
            avatar.classList.remove('online', 'sleeping', 'off');
            if (state === 'running') avatar.classList.add('online');
            else if (state === 'sleeping') avatar.classList.add('sleeping');
            else avatar.classList.add('off');
        }

        // Power buttons
        renderPowerButtons(state);

        // Modules
        if (state === 'off') {
            const grid = document.getElementById('modules-grid');
            grid.innerHTML = '<p class="placeholder-text">Robot eteint</p>';
        } else {
            renderModules(data.modules || {});
        }

        // Active mode
        document.querySelectorAll('.mode-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === data.mode);
        });

        // Operation banner — op_status est un dict { action: {state, message} }
        const ops = data.op_status || {};
        const activeOp = Object.values(ops).find(o => o && o.state === 'running');
        if (activeOp) {
            showOpBanner(activeOp.message || 'Operation en cours...');
        } else if (state !== 'starting') {
            hideOpBanner();
        }
    } catch (e) {
        document.getElementById('status-dot').className = 'status-dot off';
        document.getElementById('status-label').textContent = 'Hors ligne';
        document.getElementById('status-uptime').textContent = '';
        const avatar = document.querySelector('.robot-avatar');
        if (avatar) {
            avatar.classList.remove('online', 'sleeping');
            avatar.classList.add('off');
        }
        renderPowerButtons('off');
    }
}

function formatUptime(seconds) {
    if (!seconds && seconds !== 0) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'min';
    return m + 'min';
}

// --- Power buttons ---

function renderPowerButtons(state) {
    const container = document.getElementById('power-controls');
    let html = '';

    // SVG icons for power buttons
    const iconPower = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 2v6"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>';
    const iconMoon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z"/></svg>';
    const iconSun = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
    const iconStop = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>';
    const iconMute = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';
    const iconUnmute = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';

    switch (state) {
        case 'off':
            html = '<button class="power-btn power-on" onclick="powerStart()">' + iconPower + '<span>On</span></button>';
            break;
        case 'starting':
            html = '<button class="power-btn power-starting" disabled>' + iconPower + '<span>Demarrage...</span></button>';
            break;
        case 'running':
            html = '<button class="power-btn power-on-active" disabled>' + iconPower + '<span>On</span></button>' +
                   '<button class="power-btn power-mute" id="btn-mute" onclick="toggleMute()">' + iconMute + '<span>Silence</span></button>' +
                   '<button class="power-btn power-sleep" onclick="powerSleep()">' + iconMoon + '<span>Dodo</span></button>' +
                   '<button class="power-btn power-off" onclick="powerStop()">' + iconStop + '<span>Off</span></button>';
            break;
        case 'sleeping':
            html = '<button class="power-btn power-sleep-active" disabled>' + iconMoon + '<span>Dodo</span></button>' +
                   '<button class="power-btn power-wake" onclick="powerWake()">' + iconSun + '<span>Reveil</span></button>' +
                   '<button class="power-btn power-off" onclick="powerStop()">' + iconStop + '<span>Off</span></button>';
            break;
        case 'error':
            html = '<button class="power-btn power-on" onclick="powerRestart()">' + iconPower + '<span>Relancer</span></button>' +
                   '<button class="power-btn power-off" onclick="powerStop()">' + iconStop + '<span>Off</span></button>';
            break;
    }

    container.innerHTML = html;
}

async function powerStart() {
    renderPowerButtons('starting');
    showOpBanner('Demarrage en cours... (30-40s)');
    startStartupLogStream();
    await fetch('/api/power/start', { method: 'POST' });
    fetchStatus();
}

function powerStop() {
    showDialog('Eteindre Reachy ? Les moteurs seront desactives.', async () => {
        showOpBanner('Arret en cours...');
        await fetch('/api/power/stop', { method: 'POST' });
        hideOpBanner();
        fetchStatus();
    });
}

async function powerSleep() {
    showOpBanner('Mise en veille...');
    await fetch('/api/power/sleep', { method: 'POST' });
    setTimeout(hideOpBanner, 3000);
    fetchStatus();
}

async function powerWake() {
    showOpBanner('Reveil en cours...');
    await fetch('/api/power/wake', { method: 'POST' });
    setTimeout(hideOpBanner, 5000);
    fetchStatus();
}

async function powerRestart() {
    showOpBanner('Redemarrage en cours... (30-40s)');
    renderPowerButtons('starting');
    startStartupLogStream();
    await fetch('/api/power/restart', { method: 'POST' });
    fetchStatus();
}

// --- LLM Mute/Unmute ---

let llmMuted = false;

async function toggleMute() {
    const btn = document.getElementById('btn-mute');
    if (!btn) return;
    if (llmMuted) {
        await fetch('/api/llm/unmute', { method: 'POST' });
        llmMuted = false;
        btn.classList.remove('power-mute-active');
        btn.querySelector('span').textContent = 'Silence';
    } else {
        await fetch('/api/llm/mute', { method: 'POST' });
        llmMuted = true;
        btn.classList.add('power-mute-active');
        btn.querySelector('span').textContent = 'Parler';
    }
}

// --- Module cards ---

function renderModules(modules) {
    const grid = document.getElementById('modules-grid');
    const names = Object.keys(modules);

    if (names.length === 0) {
        grid.innerHTML = '<p class="placeholder-text">Aucun module actif</p>';
        return;
    }

    grid.innerHTML = names.map(name => {
        const active = modules[name];
        const displayName = MODULE_NAMES[name] || name;
        const dotClass = active ? 'running' : 'stopped';
        const detail = active ? 'Actif' : 'Inactif';
        const checked = active ? 'checked' : '';

        return '<div class="module-card">' +
            '<span class="module-dot ' + dotClass + '"></span>' +
            '<div class="module-info">' +
                '<div class="module-name">' + escapeHtml(displayName) + '</div>' +
                '<div class="module-detail">' + detail + '</div>' +
            '</div>' +
            '<label class="toggle">' +
                '<input type="checkbox" ' + checked +
                ' onchange="toggleModule(\'' + escapeAttr(name) + '\', this.checked)">' +
                '<span class="toggle-slider"></span>' +
            '</label>' +
        '</div>';
    }).join('');
}

async function toggleModule(name, enabled) {
    try {
        await fetch('/api/module/' + encodeURIComponent(name) + '/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled })
        });
    } catch (e) {
        // silent
    }
    fetchStatus();
}

// --- Modes ---

async function switchMode(name) {
    try {
        await fetch('/api/mode/' + encodeURIComponent(name), { method: 'POST' });
    } catch (e) {
        // silent
    }
    fetchStatus();
}

function promptProMode() {
    showPrompt('Sujet de l\'expose :', async (topic) => {
        if (!topic) return;
        try {
            await fetch('/api/mode/pro', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic: topic })
            });
        } catch (e) {
            // silent
        }
        fetchStatus();
    });
}

// --- Video ---

let videoTimer = null;

function setupVideo() {
    refreshSnapshot();
    // Rafraichir toutes les 2 secondes (leger, un seul JPEG a la fois)
    videoTimer = setInterval(refreshSnapshot, 2000);
}

function refreshSnapshot() {
    const img = document.getElementById('video-feed');
    const overlay = document.getElementById('video-offline');
    if (!img) return;

    const snap = new Image();
    snap.onload = () => {
        img.src = snap.src;
        if (overlay) overlay.classList.add('hidden');
    };
    snap.onerror = () => {
        if (overlay) overlay.classList.remove('hidden');
    };
    snap.src = '/api/snapshot?' + Date.now();
}

// --- Deploy ---

function deploy(type) {
    if (type === 'full') {
        showDialog('Deploiement complet (git pull + pip install). Continuer ?', () => {
            doDeploy('/api/deploy/full');
        });
    } else {
        doDeploy('/api/deploy');
    }
}

async function doDeploy(url) {
    showOpBanner('Deploiement en cours...');
    try {
        await fetch(url, { method: 'POST' });
    } catch (e) {
        // silent
    }
    pollOperation();
}

function pollOperation() {
    if (opPollTimer) clearInterval(opPollTimer);
    opPollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/op/status');
            if (!resp.ok) throw new Error(resp.status);
            const data = await resp.json();

            if (data.in_progress) {
                showOpBanner(data.message || 'Operation en cours...');
            } else {
                hideOpBanner();
                clearInterval(opPollTimer);
                opPollTimer = null;
                fetchStatus();
            }
        } catch (e) {
            hideOpBanner();
            clearInterval(opPollTimer);
            opPollTimer = null;
        }
    }, 2000);
}

function showOpBanner(text) {
    const banner = document.getElementById('op-banner');
    const label = document.getElementById('op-banner-text');
    if (banner) banner.classList.remove('hidden');
    if (label) label.textContent = text;
}

function hideOpBanner() {
    const banner = document.getElementById('op-banner');
    if (banner) banner.classList.add('hidden');
    stopStartupLogStream();
    const logEl = document.getElementById('op-banner-log');
    if (logEl) logEl.textContent = '';
}

// --- Startup log streaming (SSE) ---

function startStartupLogStream() {
    stopStartupLogStream();
    const logEl = document.getElementById('op-banner-log');
    if (logEl) logEl.textContent = '';
    try {
        startLogSource = new EventSource('/api/power/start/log');
        startLogSource.onmessage = (event) => {
            appendStartupLine(event.data);
        };
        startLogSource.onerror = () => {
            stopStartupLogStream();
        };
    } catch (e) { /* SSE indisponible */ }
}

function stopStartupLogStream() {
    if (startLogSource) {
        try { startLogSource.close(); } catch (e) {}
        startLogSource = null;
    }
}

function appendStartupLine(text) {
    const logEl = document.getElementById('op-banner-log');
    if (!logEl) return;
    const current = logEl.textContent ? logEl.textContent.split('\n') : [];
    current.push(text);
    // Garde les 20 dernières lignes
    logEl.textContent = current.slice(-20).join('\n');
    logEl.scrollTop = logEl.scrollHeight;
}

// --- Persons page ---

async function loadPersons() {
    try {
        const resp = await fetch('/api/persons');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        const sel = document.getElementById('person-selector');
        const current = sel.value;
        while (sel.options.length > 1) sel.remove(1);

        const persons = Array.isArray(data) ? data : (data.persons || []);
        persons.forEach(p => {
            const name = typeof p === 'string' ? p : p.name;
            const display = typeof p === 'string' ? p : (p.display_name || p.name);
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = display;
            sel.appendChild(opt);
        });

        if (current) sel.value = current;
    } catch (e) {
        // silent
    }
}

async function loadPerson(name) {
    const container = document.getElementById('person-detail');
    if (!name) {
        container.innerHTML = '<p class="placeholder-text">Selectionnez une personne pour voir ses details.</p>';
        return;
    }

    try {
        const resp = await fetch('/api/persons/' + encodeURIComponent(name));
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        let html = '';

        // Profile section
        html += '<div class="person-section"><h3>Profil</h3>';
        html += '<div class="person-field"><span class="field-label">Derniere visite</span><span>' + escapeHtml(data.last_seen || '--') + '</span></div>';
        html += '<div class="person-field"><span class="field-label">Sessions</span><span>' + (data.sessions_count || 0) + '</span></div>';

        if (data.medications && data.medications.length) {
            html += '<div class="person-field"><span class="field-label">Medicaments</span><ul>';
            data.medications.forEach(m => { html += '<li>' + escapeHtml(m) + '</li>'; });
            html += '</ul></div>';
        }

        if (data.emergency_contact) {
            html += '<div class="person-field"><span class="field-label">Contact urgence</span><span>' + escapeHtml(data.emergency_contact) + '</span></div>';
        }
        if (data.schedules) {
            html += '<div class="person-field"><span class="field-label">Emploi du temps</span><span>' + escapeHtml(data.schedules) + '</span></div>';
        }
        if (data.notes) {
            html += '<div class="person-field"><span class="field-label">Notes</span><span>' + escapeHtml(data.notes) + '</span></div>';
        }
        if (data.reading_progress) {
            html += '<div class="person-field"><span class="field-label">Lecture</span><span>' + escapeHtml(data.reading_progress) + '</span></div>';
        }
        html += '</div>';

        // Journal section
        if (data.journal && data.journal.length) {
            html += '<div class="person-section"><h3>Journal</h3>';
            html += '<table class="journal-table"><thead><tr><th>Heure</th><th></th><th>Evenement</th></tr></thead><tbody>';
            data.journal.forEach(entry => {
                const emoji = CATEGORY_EMOJI[entry.category] || '\u{1F4DD}';
                html += '<tr>';
                html += '<td class="journal-time">' + escapeHtml(entry.time || '') + '</td>';
                html += '<td class="journal-category">' + emoji + '</td>';
                html += '<td class="journal-text">' + escapeHtml(entry.description || '') + '</td>';
                html += '</tr>';
            });
            html += '</tbody></table></div>';
        }

        // Facts section
        if (data.facts && data.facts.length) {
            html += '<div class="person-section"><h3>Faits connus</h3><ul>';
            data.facts.forEach(f => { html += '<li>' + escapeHtml(f) + '</li>'; });
            html += '</ul></div>';
        }

        // Sessions section
        if (data.sessions && data.sessions.length) {
            html += '<div class="person-section"><h3>Dernieres sessions</h3>';
            data.sessions.slice(0, 10).forEach(s => {
                html += '<div class="session-entry">';
                html += '<span class="session-date">' + escapeHtml(s.date || '') + '</span>';
                html += '<span class="session-summary">' + escapeHtml(s.summary || '') + '</span>';
                html += '</div>';
            });
            html += '</div>';
        }

        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="placeholder-text">Erreur de chargement.</p>';
    }
}

function enrollPerson() {
    showPrompt('Prenom a enroler :', async (name) => {
        if (!name) return;
        try {
            await fetch('/api/persons/enroll', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            });
        } catch (e) {
            // silent
        }
        loadPersons();
    });
}

function forgetPerson() {
    const sel = document.getElementById('person-selector');
    const name = sel.value;
    if (!name) return;

    showDialog('Oublier ' + name + ' ?', async () => {
        try {
            await fetch('/api/persons/forget', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name })
            });
        } catch (e) {
            // silent
        }
        loadPersons();
        document.getElementById('person-detail').innerHTML =
            '<p class="placeholder-text">Selectionnez une personne pour voir ses details.</p>';
    });
}

// --- Settings page ---

async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        Object.keys(data).forEach(key => {
            const el = document.getElementById('cfg-' + key);
            if (!el) return;

            if (el.type === 'checkbox') {
                el.checked = !!data[key];
            } else {
                el.value = data[key] != null ? data[key] : '';
            }
        });
    } catch (e) {
        // silent
    }
}

async function saveSetting(key) {
    const el = document.getElementById('cfg-' + key);
    if (!el) return;

    const value = el.type === 'checkbox' ? el.checked : el.value;
    const btn = el.closest('.setting-row')?.querySelector('.btn-ok');

    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: key, value: value })
        });

        if (btn) {
            btn.classList.add(resp.ok ? 'flash-ok' : 'flash-err');
            setTimeout(() => btn.classList.remove('flash-ok', 'flash-err'), 1000);
        }
    } catch (e) {
        if (btn) {
            btn.classList.add('flash-err');
            setTimeout(() => btn.classList.remove('flash-err'), 1000);
        }
    }
}

// --- Logs page ---

function connectLogs(type) {
    if (logSource) {
        logSource.close();
        logSource = null;
    }

    try {
        logSource = new EventSource('/api/logs/stream?type=' + encodeURIComponent(type || 'main'));

        logSource.onmessage = (event) => {
            appendLog(event.data);
        };

        logSource.onerror = () => {
            logSource.close();
            logSource = null;
            setTimeout(() => connectLogs(type), 5000);
        };
    } catch (e) {
        // SSE not available
    }
}

function appendLog(text) {
    const container = document.getElementById('logs-content');
    const line = document.createElement('span');
    line.className = 'log-line';

    const lower = text.toLowerCase();
    if (lower.includes('error') || lower.includes('exception') || lower.includes('traceback')) {
        line.classList.add('log-error');
    } else if (lower.includes('warning') || lower.includes('warn')) {
        line.classList.add('log-warning');
    } else if (lower.includes('info') || lower.includes('started') || lower.includes('ok')) {
        line.classList.add('log-info');
    }

    line.textContent = text + '\n';
    container.appendChild(line);

    // Limit to 500 lines
    while (container.children.length > 500) {
        container.removeChild(container.firstChild);
    }

    // Auto-scroll if near bottom
    const wrap = document.getElementById('logs-container');
    const atBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 60;
    if (atBottom) {
        wrap.scrollTop = wrap.scrollHeight;
    }
}

function switchLogTab(type) {
    document.querySelectorAll('.log-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === type);
    });
    document.getElementById('logs-content').innerHTML = '';
    connectLogs(type);
}

// --- Dialogs ---

function showDialog(msg, onConfirm) {
    confirmCallback = onConfirm;
    document.getElementById('confirm-message').textContent = msg;
    document.getElementById('confirm-dialog').classList.remove('hidden');
}

function closeConfirm(accepted) {
    document.getElementById('confirm-dialog').classList.add('hidden');
    if (accepted && confirmCallback) {
        confirmCallback();
    }
    confirmCallback = null;
}

function showPrompt(msg, onSubmit) {
    promptCallback = onSubmit;
    document.getElementById('prompt-message').textContent = msg;
    const input = document.getElementById('prompt-input');
    input.value = '';
    document.getElementById('prompt-dialog').classList.remove('hidden');
    input.focus();
}

function closePrompt(value) {
    document.getElementById('prompt-dialog').classList.add('hidden');
    if (promptCallback) {
        promptCallback(value);
    }
    promptCallback = null;
}

// --- Bluetooth ---

let btPollTimer = null;

async function btLoadStatus() {
    try {
        const resp = await fetch('/api/bt/status');
        if (!resp.ok) return;
        const data = await resp.json();

        // Connected device
        const connDiv = document.getElementById('bt-connected');
        const noConnDiv = document.getElementById('bt-not-connected');
        if (data.connected) {
            connDiv.classList.remove('hidden');
            noConnDiv.classList.add('hidden');
            document.getElementById('bt-connected-name').textContent =
                data.connected.name || data.connected.address;
        } else {
            connDiv.classList.add('hidden');
            noConnDiv.classList.remove('hidden');
        }

        // Paired devices
        const pairedSection = document.getElementById('bt-paired-section');
        const pairedList = document.getElementById('bt-paired-list');
        if (data.paired && data.paired.length > 0) {
            pairedSection.classList.remove('hidden');
            pairedList.innerHTML = data.paired.map(d => {
                const isConn = data.connected && data.connected.address === d.address;
                return '<div class="bt-device-row">' +
                    '<span class="bt-device-name">' + escapeHtml(d.name) + '</span>' +
                    '<span class="bt-device-addr">' + escapeHtml(d.address) + '</span>' +
                    (isConn
                        ? '<span class="bt-badge bt-badge-connected">Connecte</span>'
                        : '<button class="btn btn-sm" onclick="btConnect(\'' + escapeAttr(d.address) + '\')">Connecter</button>') +
                    '<button class="btn btn-sm btn-danger" onclick="btRemove(\'' + escapeAttr(d.address) + '\')">Suppr</button>' +
                '</div>';
            }).join('');
        } else {
            pairedSection.classList.add('hidden');
        }

        // Audio output state
        const audioResp = await fetch('/api/bt/audio-output');
        if (audioResp.ok) {
            const audioData = await audioResp.json();
            document.getElementById('btn-audio-usb').classList.toggle('active', !audioData.bt_active);
            document.getElementById('btn-audio-bt').classList.toggle('active', audioData.bt_active);
        }
    } catch (e) {
        // silent
    }
}

async function btScan() {
    const btn = document.getElementById('btn-bt-scan');
    const status = document.getElementById('bt-scan-status');
    const results = document.getElementById('bt-scan-results');

    btn.disabled = true;
    btn.textContent = 'Scan...';
    status.textContent = 'Recherche en cours (10s)...';
    results.classList.add('hidden');

    try {
        await fetch('/api/bt/scan', { method: 'POST' });
    } catch (e) {
        status.textContent = 'Erreur de scan';
        btn.disabled = false;
        btn.textContent = 'Scanner';
        return;
    }

    // Poll for results
    if (btPollTimer) clearInterval(btPollTimer);
    btPollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/bt/devices');
            if (!resp.ok) return;
            const data = await resp.json();

            if (!data.scanning) {
                clearInterval(btPollTimer);
                btPollTimer = null;
                btn.disabled = false;
                btn.textContent = 'Scanner';
                status.textContent = data.devices.length + ' appareil(s) trouve(s)';

                if (data.devices.length > 0) {
                    results.classList.remove('hidden');
                    results.innerHTML = data.devices.map(d =>
                        '<div class="bt-device-row">' +
                            '<span class="bt-device-name">' + escapeHtml(d.name) + '</span>' +
                            '<span class="bt-device-addr">' + escapeHtml(d.address) + '</span>' +
                            '<button class="btn btn-sm btn-primary" onclick="btPair(\'' + escapeAttr(d.address) + '\')">Appairer</button>' +
                        '</div>'
                    ).join('');
                }
            }
        } catch (e) {
            // silent
        }
    }, 2000);
}

async function btPair(address) {
    try {
        const resp = await fetch('/api/bt/pair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ address: address })
        });
        const data = await resp.json();
        if (data.ok) {
            // Auto-connect after pairing
            await btConnect(address);
        }
    } catch (e) {
        // silent
    }
    btLoadStatus();
}

async function btConnect(address) {
    try {
        await fetch('/api/bt/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ address: address })
        });
    } catch (e) {
        // silent
    }
    btLoadStatus();
}

async function btDisconnect() {
    try {
        await fetch('/api/bt/disconnect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
    } catch (e) {
        // silent
    }
    btLoadStatus();
}

async function btRemove(address) {
    showDialog('Supprimer cet appareil ?', async () => {
        try {
            await fetch('/api/bt/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ address: address })
            });
        } catch (e) {
            // silent
        }
        btLoadStatus();
    });
}

async function btSwitchAudio(target) {
    try {
        const resp = await fetch('/api/bt/audio-output', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target: target })
        });
        const data = await resp.json();
        if (!data.ok) {
            document.getElementById('bt-scan-status').textContent = data.error || 'Erreur';
        }
    } catch (e) {
        // silent
    }
    btLoadStatus();
}

// --- Utilities ---

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function escapeAttr(str) {
    if (!str) return '';
    return String(str)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"');
}

// --- Initialization ---

document.addEventListener('DOMContentLoaded', () => {
    // Router
    window.addEventListener('hashchange', onHashChange);
    onHashChange();

    // Status polling (every 3s)
    fetchStatus();
    setInterval(fetchStatus, 3000);

    // Person selector change handler
    document.getElementById('person-selector').addEventListener('change', (e) => {
        loadPerson(e.target.value);
    });

    // Initial data loads
    loadPersons();
    loadSettings();
    setupVideo();
    connectLogs('main');
});
