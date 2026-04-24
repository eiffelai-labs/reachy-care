/* ============================================
   Reachy Care Dashboard — Vanilla JS
   Pas de framework. Requetes fetch pures.
   ============================================ */

// --- Constantes ---

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

// Icônes par nom de mode. Ajoute une entrée si tu crées un nouveau mode/activité.
const MODE_ICONS = {
    normal:   '\u{1F4AC}',  // 💬
    histoire: '\u{1F4D6}',  // 📖
    echecs:   '\u{265F}\uFE0F',  // ♟️
    musique:  '\u{1F3B5}',  // 🎵
    pro:      '\u{1F393}'   // 🎓
};
const DEFAULT_MODE_ICON = '\u{26A1}';  // ⚡ — fallback pour les modes sans icône mappée

// Labels FR des états AttenLabs (source : main.py._attention_state).
// Affichés en MAJUSCULES dans les chips (CSS text-transform: uppercase).
// Sémantique confirmée par Supervisor R19 :
//   SILENT       = aucun visage détecté (ou bbox statique > 60 s)
//   TO_HUMAN     = visage détecté mais décalé/loin de Reachy ("ne regarde pas Reachy")
//   TO_COMPUTER  = visage centré + proche ("regarde Reachy en face")
// On reformule pour que Julie comprenne l'action observée.
const ATTENTION_LABELS = {
    SILENT: 'Personne',
    TO_HUMAN: 'Regarde ailleurs',
    TO_COMPUTER: 'Me regarde',
};

// Entrée virtuelle "Normal" ajoutée en tête de la liste puisqu'elle n'a pas d'activité associée.
const VIRTUAL_NORMAL_MODE = {
    name: 'normal',
    display_name: 'Normal',
    description: 'Conversation libre',
    active: false,
    enabled: true,
    virtual: true
};

// Labels reconstruits à chaque fetchActivities pour que l'indicateur "Mode" affiche
// les mêmes textes que les boutons. Source de vérité = manifests activities/*.
let MODE_LABELS = { normal: 'Normal' };

let currentPerson = '';
let logSource = null;
let pendingUninstall = null;

// --- Initialisation ---

document.addEventListener('DOMContentLoaded', () => {
    setupVideo();
    fetchStatus();
    fetchActivities();
    loadRuntimeState();
    connectLogs();

    // Auto-refresh
    // 1s pour capter au mieux les transitions AttenLabs. Q20 demande à
// Supervisor d'écrire status.json à chaque transition ; en attendant,
// 1 s est le compromis raisonnable entre charge Pi et réactivité.
setInterval(fetchStatus, 1000);
    setInterval(fetchJournalCurrent, 30000);
    setInterval(fetchActivities, 10000);

    // Person selector
    document.getElementById('person-selector').addEventListener('change', (e) => {
        currentPerson = e.target.value;
        fetchJournal(currentPerson);
    });
});

// --- Runtime state (AttenLabs toggle) ---

function attenLabsHintText(enabled) {
    return enabled
        ? 'Classifieur bbox actif : audio ouvert selon SILENT/TO_HUMAN/TO_COMPUTER.'
        : 'Classifieur bypass : audio toujours ouvert (TO_COMPUTER forcé).';
}

async function loadRuntimeState() {
    try {
        const r = await fetch('/api/runtime-state');
        if (!r.ok) return;
        const s = await r.json();
        const cb = document.getElementById('toggle-attenlabs');
        const hint = document.getElementById('attenlabs-hint');
        if (cb) cb.checked = !!s.attenlabs_enabled;
        if (hint) hint.textContent = attenLabsHintText(!!s.attenlabs_enabled);
    } catch (e) { /* silencieux */ }
}

async function toggleAttenLabs(enabled) {
    const cb = document.getElementById('toggle-attenlabs');
    const hint = document.getElementById('attenlabs-hint');
    try {
        const r = await fetch('/api/runtime-state', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({attenlabs_enabled: !!enabled}),
        });
        if (!r.ok) throw new Error('save');
        if (hint) hint.textContent = attenLabsHintText(!!enabled);
    } catch (e) {
        if (cb) cb.checked = !enabled;
        if (hint) hint.textContent = 'Erreur sauvegarde du toggle.';
    }
}

// --- Video ---

// Suivi de l'état Reachy pour relancer le flux vidéo aux transitions.
// Le proxy /api/video renvoie 200 multipart même quand main.py est down
// (connexion suspendue côté browser, jamais de onload ni onerror), donc il
// faut réassigner img.src explicitement au moment où Reachy redevient actif.
let _videoLastAlive = null;

function startVideoStream() {
    const img = document.getElementById('video-feed');
    if (!img) return;
    const base = window.REACHY_API_BASE || '';
    // Cache-bust pour forcer une nouvelle connexion TCP (sinon le browser
    // garde la connexion morte précédente).
    img.src = base + '/api/video?' + Date.now();
}

function stopVideoStream() {
    const img = document.getElementById('video-feed');
    const overlay = document.getElementById('video-offline');
    if (!img) return;
    img.removeAttribute('src');
    if (overlay) overlay.classList.remove('hidden');
}

function setupVideo() {
    const img = document.getElementById('video-feed');
    const overlay = document.getElementById('video-offline');

    img.onload = () => {
        overlay.classList.add('hidden');
    };

    img.onerror = () => {
        overlay.classList.remove('hidden');
        // Retry après 5s seulement si Reachy est censé être vivant —
        // sinon on évite de ré-ouvrir une connexion qui va re-hang.
        setTimeout(() => {
            if (_videoLastAlive === true) startVideoStream();
        }, 5000);
    };

    // Ne lance pas le flux ici : fetchStatus détermine l'état réel
    // et appelle startVideoStream() à la première transition.
}

// --- Status ---

async function fetchStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        // Badge + avatar state : state prend le dessus sur sleeping car quand
        // main.py est mort on veut "Éteint", pas "En ligne" (sleeping:false
        // par défaut dans la réponse vide).
        const badge = document.getElementById('status-badge');
        const avatar = document.getElementById('robot-avatar');
        const sleepWrap = document.getElementById('indicator-sleeping-wrap');

        if (data.state === 'off') {
            badge.textContent = 'Éteint';
            badge.className = 'badge badge-offline';
            avatar.className = 'robot-avatar';
            sleepWrap.style.display = 'none';
        } else if (data.state === 'starting') {
            badge.textContent = 'Démarrage…';
            badge.className = 'badge badge-sleeping';
            avatar.className = 'robot-avatar';
            sleepWrap.style.display = 'none';
        } else if (data.sleeping) {
            badge.textContent = 'Dort';
            badge.className = 'badge badge-sleeping';
            avatar.className = 'robot-avatar sleeping';
            sleepWrap.style.display = '';
        } else {
            badge.textContent = 'En ligne';
            badge.className = 'badge badge-online';
            avatar.className = 'robot-avatar online';
            sleepWrap.style.display = 'none';
        }

        // AttenLabs live : état + speaking (envoyé par /api/status via
        // status.json ; champs alimentés par main.py _write_status_file).
        // Flash visuel à chaque transition pour que les états brefs
        // (TO_COMPUTER pendant un wake word) restent visibles à l'œil.
        const att = (data.attention_state || '').toUpperCase();
        const attLabel = ATTENTION_LABELS[att] || '—';
        const attIndicator = document.getElementById('indicator-attention');
        if (attIndicator) attIndicator.textContent = attLabel;
        const stateChip = document.getElementById('attenlabs-state-chip');
        if (stateChip) {
            const changed = stateChip.dataset.att !== att;
            stateChip.textContent = attLabel;
            stateChip.className = 'state-chip ' + (att
                ? 'state-chip-' + att.toLowerCase().replace('_', '-')
                : 'state-chip-muted');
            stateChip.dataset.att = att;
            if (changed) {
                stateChip.classList.remove('state-chip-flash');
                void stateChip.offsetWidth;  // force reflow pour relancer l'anim
                stateChip.classList.add('state-chip-flash');
            }
        }
        const speakingChip = document.getElementById('attenlabs-speaking-chip');
        if (speakingChip) {
            const wasHidden = speakingChip.hidden;
            speakingChip.hidden = !data.reachy_speaking;
            if (wasHidden && !speakingChip.hidden) {
                speakingChip.classList.remove('state-chip-flash');
                void speakingChip.offsetWidth;
                speakingChip.classList.add('state-chip-flash');
            }
        }

        // Flux vidéo : déclencher aux transitions off↔alive sinon le <img>
        // reste bloqué sur la connexion multipart suspendue du proxy.
        const alive = data.state === 'running' || data.state === 'sleeping';
        if (_videoLastAlive === null) {
            if (alive) startVideoStream(); else stopVideoStream();
        } else if (!_videoLastAlive && alive) {
            startVideoStream();
        } else if (_videoLastAlive && !alive) {
            stopVideoStream();
        }
        _videoLastAlive = alive;

        // Indicateurs
        document.getElementById('indicator-mode').textContent =
            MODE_LABELS[data.mode] || data.mode || '--';
        document.getElementById('indicator-person').textContent =
            capitalize(data.person) || 'Personne inconnue';
        document.getElementById('indicator-uptime').textContent =
            formatUptime(data.uptime);

        // Mettre a jour le selecteur de personnes
        updatePersonSelector(data.persons || []);

        // Si pas de personne selectionnee, prendre la personne courante
        if (!currentPerson && data.person) {
            currentPerson = data.person;
            document.getElementById('person-selector').value = currentPerson;
            fetchJournal(currentPerson);
        }
    } catch (e) {
        const badge = document.getElementById('status-badge');
        badge.textContent = 'Hors ligne';
        badge.className = 'badge badge-offline';
        document.getElementById('robot-avatar').className = 'robot-avatar';
        document.getElementById('indicator-mode').textContent = '--';
        document.getElementById('indicator-person').textContent = '--';
        document.getElementById('indicator-uptime').textContent = '--';
        document.getElementById('indicator-sleeping-wrap').style.display = 'none';
        if (_videoLastAlive !== false) {
            stopVideoStream();
            _videoLastAlive = false;
        }
    }
}

// Capitalise la première lettre d'un prénom pour l'affichage. Les values
// API restent en minuscule (le backend stocke/lookup par nom normalisé).
function capitalize(name) {
    if (!name) return '';
    return name.charAt(0).toUpperCase() + name.slice(1);
}

function formatUptime(seconds) {
    if (!seconds && seconds !== 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'min';
    return m + 'min';
}

function updatePersonSelector(persons) {
    const sel = document.getElementById('person-selector');
    const current = sel.value;

    // Ne reconstruire que si la liste a change
    const existing = Array.from(sel.options).slice(1).map(o => o.value);
    if (JSON.stringify(existing) === JSON.stringify(persons)) return;

    // Garder la premiere option
    while (sel.options.length > 1) sel.remove(1);

    persons.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = capitalize(name);
        sel.appendChild(opt);
    });

    sel.value = current || '';
}

// --- Journal ---

function fetchJournalCurrent() {
    if (currentPerson) {
        fetchJournal(currentPerson);
    }
}

async function fetchJournal(person) {
    const container = document.getElementById('journal-content');

    if (!person) {
        container.innerHTML = '<p class="placeholder">Selectionnez une personne.</p>';
        return;
    }

    try {
        const resp = await fetch('/api/journal/' + encodeURIComponent(person));
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();

        if (!data.entries || data.entries.length === 0) {
            container.innerHTML = '<p class="placeholder">Aucun evenement aujourd\'hui pour ' + escapeHtml(person) + '.</p>';
            return;
        }

        let html = '<table class="journal-table">';
        html += '<thead><tr><th>Heure</th><th></th><th>Evenement</th></tr></thead>';
        html += '<tbody>';

        data.entries.forEach(entry => {
            const emoji = CATEGORY_EMOJI[entry.category] || '\u{1F4DD}';
            const time = entry.time || '';
            const text = entry.description || '';
            html += '<tr>';
            html += '<td class="journal-time">' + escapeHtml(time) + '</td>';
            html += '<td class="journal-category">' + emoji + '</td>';
            html += '<td class="journal-text">' + escapeHtml(text) + '</td>';
            html += '</tr>';
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p class="placeholder">Erreur de chargement du journal.</p>';
    }
}

// --- Activities ---

function _clearGrid(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
}

function _mkPlaceholder(text) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = text;
    return p;
}

function _mkModeButton(mode) {
    const btn = document.createElement('button');
    btn.className = 'cmd-btn' + (mode.active ? ' active-mode' : '');
    btn.addEventListener('click', () => switchMode(mode.name));
    const icon = document.createElement('span');
    icon.className = 'cmd-icon';
    icon.textContent = MODE_ICONS[mode.name] || DEFAULT_MODE_ICON;
    const label = document.createElement('span');
    label.textContent = mode.display_name || mode.name;
    btn.appendChild(icon);
    btn.appendChild(label);
    return btn;
}

function _mkActivityCard(act) {
    const card = document.createElement('div');
    card.className = 'activity-card' + (act.active ? ' active-mode' : '');

    const info = document.createElement('div');
    info.className = 'activity-info';
    const name = document.createElement('div');
    name.className = 'activity-name';
    name.textContent = act.display_name || act.name;
    const desc = document.createElement('div');
    desc.className = 'activity-desc';
    desc.textContent = act.description || '';
    info.appendChild(name);
    info.appendChild(desc);

    const actions = document.createElement('div');
    actions.className = 'activity-actions';

    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'toggle';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = (act.enabled !== false);
    cb.addEventListener('change', (e) => toggleActivity(act.name, e.target.checked));
    const slider = document.createElement('span');
    slider.className = 'toggle-slider';
    toggleLabel.appendChild(cb);
    toggleLabel.appendChild(slider);

    const trash = document.createElement('button');
    trash.className = 'btn-trash';
    trash.title = 'Desinstaller';
    trash.textContent = '\u{1F5D1}';
    trash.addEventListener('click', () => confirmUninstall(act.name, act.display_name || act.name));

    actions.appendChild(toggleLabel);
    actions.appendChild(trash);

    card.appendChild(info);
    card.appendChild(actions);
    return card;
}

async function fetchActivities() {
    const activitiesGrid = document.getElementById('activities-grid');
    const modesGrid = document.getElementById('modes-grid');
    try {
        const resp = await fetch('/api/activities');
        if (!resp.ok) throw new Error(resp.status);
        const raw = await resp.json();

        const activities = (raw || []).slice().sort((a, b) =>
            (a.display_name || a.name).localeCompare(b.display_name || b.name, 'fr')
        );
        const ordered = [VIRTUAL_NORMAL_MODE, ...activities];

        MODE_LABELS = {};
        ordered.forEach(m => { MODE_LABELS[m.name] = m.display_name || m.name; });

        if (modesGrid) {
            _clearGrid(modesGrid);
            ordered.forEach(m => modesGrid.appendChild(_mkModeButton(m)));
        }

        if (activitiesGrid) {
            _clearGrid(activitiesGrid);
            if (activities.length === 0) {
                activitiesGrid.appendChild(_mkPlaceholder('Aucune activite configuree.'));
            } else {
                activities.forEach(act => activitiesGrid.appendChild(_mkActivityCard(act)));
            }
        }
    } catch (e) {
        if (activitiesGrid) {
            _clearGrid(activitiesGrid);
            activitiesGrid.appendChild(_mkPlaceholder('Impossible de charger les activites.'));
        }
        if (modesGrid) {
            _clearGrid(modesGrid);
            modesGrid.appendChild(_mkPlaceholder('Impossible de charger les modes.'));
        }
    }
}

async function toggleActivity(name, enabled) {
    const action = enabled ? 'enable' : 'disable';
    try {
        const resp = await fetch('/api/activities/' + encodeURIComponent(name) + '/' + action, {
            method: 'POST'
        });
        if (!resp.ok) throw new Error(resp.status);
    } catch (e) {
        // Recharger pour refleter l'etat reel
        fetchActivities();
    }
}

// --- Uninstall Confirmation ---

function confirmUninstall(name, displayName) {
    pendingUninstall = name;
    document.getElementById('confirm-message').textContent =
        'Desinstaller \u00ab ' + displayName + ' \u00bb ?';
    document.getElementById('confirm-dialog').classList.remove('hidden');

    const btn = document.getElementById('btn-confirm-action');
    btn.onclick = async () => {
        closeConfirmDialog();
        try {
            await fetch('/api/activities/' + encodeURIComponent(pendingUninstall) + '/uninstall', {
                method: 'POST'
            });
        } catch (e) {
            // silencieux
        }
        pendingUninstall = null;
        fetchActivities();
    };
}

function closeConfirmDialog() {
    document.getElementById('confirm-dialog').classList.add('hidden');
    pendingUninstall = null;
}

// --- Commands ---

async function sendCommand(cmdObj) {
    try {
        await fetch('/api/cmd', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cmdObj)
        });
    } catch (e) {
        // silencieux
    }
}

function switchMode(mode) {
    sendCommand({ cmd: 'switch_mode', mode: mode });
}

function powerOn() {
    // Daemon start + wake_up via REST API du Pi
    fetch('/api/cmd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd: 'wake' })
    });
}

function powerOff() {
    if (confirm('Eteindre Reachy ? (les moteurs seront desactives)')) {
        sendCommand({ cmd: 'sleep_mode' });
    }
}

// --- Logs (SSE) ---

function connectLogs() {
    if (logSource) {
        logSource.close();
    }

    try {
        const base = window.REACHY_API_BASE || '';
        logSource = new EventSource(base + '/api/logs/stream?type=main');

        logSource.onmessage = (event) => {
            appendLog(event.data);
        };

        logSource.onerror = () => {
            logSource.close();
            logSource = null;
            // Reconnexion apres 5s
            setTimeout(connectLogs, 5000);
        };
    } catch (e) {
        // SSE non disponible
    }
}

function appendLog(text) {
    const container = document.getElementById('logs-content');
    const line = document.createElement('span');
    line.className = 'log-line';

    // Coloration basique
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

    // Limiter a 500 lignes
    while (container.children.length > 500) {
        container.removeChild(container.firstChild);
    }

    // Auto-scroll si proche du bas
    const logsContainer = document.getElementById('logs-container');
    const atBottom = logsContainer.scrollHeight - logsContainer.scrollTop - logsContainer.clientHeight < 60;
    if (atBottom) {
        logsContainer.scrollTop = logsContainer.scrollHeight;
    }
}

function toggleLogs() {
    const container = document.getElementById('logs-container');
    const btn = document.querySelector('.btn-toggle-logs');
    container.classList.toggle('collapsed');
    btn.classList.toggle('expanded');
}

// --- Utilitaires ---

function escapeHtml(str) {
    if (!str) return '';
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function escapeAttr(str) {
    if (!str) return '';
    return str
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"');
}

// --- Power lifecycle (reachy_controller.py native, always-on :8090) ---

// Polle /api/status jusqu'à ce que `predicate(data)` soit vrai ou timeout.
// Retourne true si atteint, false sinon. Abandonne aussi si le backend
// signale une erreur sur l'op (op_status.start/stop.state === 'error').
async function waitUntil(predicate, maxSeconds) {
    const deadline = Date.now() + maxSeconds * 1000;
    while (Date.now() < deadline) {
        try {
            const r = await fetch('/api/status');
            if (r.ok) {
                const j = await r.json();
                if (predicate(j)) return true;
                const opStart = j.op_status && j.op_status.start;
                const opStop = j.op_status && j.op_status.stop;
                if (opStart && opStart.state === 'error') return false;
                if (opStop && opStop.state === 'error') return false;
            }
        } catch (e) { /* continue poll */ }
        await new Promise(res => setTimeout(res, 800));
    }
    return false;
}

async function powerOn() {
    const btn = document.querySelector('.power-btn.power-on');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.disabled = true;
    try {
        const r = await fetch('/api/power/start', { method: 'POST' });
        const j = await r.json();
        if (!j.ok) {
            alert('Erreur démarrage : ' + (j.error || r.status));
            return;
        }
        // Le bouton reste vert jusqu'à Reachy vraiment réveillé :
        // state=running ET sleeping=false (wake_motors terminé côté main.py).
        // start_all ~30-60s + wake geste ~5-10s → timeout 120s.
        const ok = await waitUntil(
            j => j.state === 'running' && !j.sleeping,
            120,
        );
        if (!ok) alert('Démarrage n\'a pas abouti dans le délai (120s). Voir logs.');
    } catch (e) {
        alert('Réseau : ' + e.message);
    } finally {
        btn.classList.remove('is-busy');
        btn.disabled = false;
    }
}

// Wake et Sleep : même logique que powerOn/powerOff mais on passe par
// /api/cmd (fire-and-forget vers main.py) et on poll status.json pour
// savoir quand goto_sleep / wake_motors est effectivement terminé
// (main.py bascule `sleeping` dans /tmp/reachy_care_status.json en fin
// d'animation, c'est notre signal de fin).
async function powerSleep() {
    const btn = document.querySelector('.power-btn.power-sleep');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.disabled = true;
    try {
        await sendCommand({ cmd: 'sleep_mode' });
        // _get_state() renvoie "sleeping" (pas "running") dès que main.py
        // a écrit sleeping=true dans status.json, donc on filtre sur le
        // flag sleeping directement.
        const ok = await waitUntil(j => j.sleeping === true, 30);
        if (!ok) alert('Mise en veille n\'a pas abouti dans le délai (30s).');
    } finally {
        btn.classList.remove('is-busy');
        btn.disabled = false;
    }
}

async function powerWake() {
    const btn = document.querySelector('.power-btn.power-wake');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.disabled = true;
    try {
        // /api/power/wake_smart bypass main.py pour éviter le restart daemon
        // qui cassait les WS /ws/sdk (bug Supervisor 23/04).
        const r = await fetch('/api/power/wake_smart', { method: 'POST' });
        const j = await r.json();
        if (!j.ok) {
            alert('Erreur réveil : ' + (j.error || r.status));
            return;
        }
        if (j.path === 'fast_wake') {
            // Animation wake_up daemon ~2 s — petit délai visuel pour que le
            // bouton reste éclairé pendant le geste. Pas de poll sleeping car
            // main.py n'est pas notifié de ce wake (cmd 'mark_awake' ignorée
            // tant que Supervisor ne l'a pas implémentée côté main.py).
            await new Promise(res => setTimeout(res, 2500));
        } else {
            // Cascade complète (cold boot) — attendre le réveil complet.
            const ok = await waitUntil(
                j => j.state === 'running' && !j.sleeping,
                120,
            );
            if (!ok) alert('Réveil n\'a pas abouti dans le délai (120s).');
        }
    } catch (e) {
        alert('Réseau : ' + e.message);
    } finally {
        btn.classList.remove('is-busy');
        btn.disabled = false;
    }
}

async function powerOff() {
    const btn = document.querySelector('.power-btn.power-off');
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.disabled = true;
    try {
        // api_power_stop est sync côté serveur (~5-10s) : l'await bloque
        // jusqu'à retour, puis on poll pour vérifier que state=off.
        const r = await fetch('/api/power/stop', { method: 'POST' });
        const j = await r.json();
        if (!j.ok) {
            alert('Erreur arrêt : ' + (j.error || r.status));
            return;
        }
        // Le bouton reste rouge jusqu'à main.py+conv_app complètement tués
        // (state=off). L'api_power_stop fait sleep_mode → goto_sleep 3s →
        // pkill services, total ~8-15s.
        const ok = await waitUntil(j => j.state === 'off', 30);
        if (!ok) alert('Arrêt n\'a pas abouti dans le délai (30s).');
    } catch (e) {
        alert('Réseau : ' + e.message);
    } finally {
        btn.classList.remove('is-busy');
        btn.disabled = false;
    }
}

// --- Wifi indicator (header) : poll /api/wifi/status every 10 s ---

async function refreshWifiIndicator() {
    try {
        const r = await fetch('/api/wifi/status');
        if (!r.ok) return;
        const j = await r.json();
        if (!j.ok) return;
        const icon = document.getElementById('indicator-wifi-icon');
        const ssid = document.getElementById('indicator-wifi-ssid');
        if (ssid) ssid.textContent = j.ssid || '—';
        if (icon && window.Icons) {
            const isOnline = Boolean(j.ssid);
            const name = isOnline ? 'wifi' : 'wifiOff';
            icon.replaceChildren(Icons.svg(name, 18));
            icon.classList.toggle('wifi-online', isOnline);
            icon.classList.toggle('wifi-offline', !isOnline);
            icon.title = isOnline
                ? `${j.ssid} · ${j.rssi_dbm ?? '?'} dBm`
                : 'Wifi déconnecté';
        }
    } catch (e) { /* silent */ }
}

refreshWifiIndicator();
setInterval(refreshWifiIndicator, 10000);
