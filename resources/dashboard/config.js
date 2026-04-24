/* config.js — routing des appels API selon l'origine de la page.
 *
 * Deux contextes de déploiement possibles :
 *   1. Servi par le Pi (reachy_controller.py :8090 en LAN ou via Tailscale)
 *      → fetch('/api/…') relatif, même origine
 *   2. Servi par Infomaniak (https://reachy-care.eiffelai.io, statique)
 *      → fetch('/api/…') doit être préfixé par l'URL Tailscale du Pi
 *
 * Détection automatique via location.hostname.
 */
(function () {
    const hostname = window.location.hostname;
    const EIFFELAI_HOSTS = ['reachy-care.eiffelai.io', 'www.reachy-care.eiffelai.io'];
    // Tailscale Serve expose le Pi en HTTPS (cert Let's Encrypt auto).
    // Supervisor 22/04 R14 : `sudo tailscale serve --bg 8090`.
    // Plus de mixed content HTTPS→HTTP, le fetch depuis eiffelai.io passe.
    const PI_TAILSCALE_BASE = '';  // À renseigner : URL HTTPS du Pi (Tailscale Serve ou équivalent)

    let apiBase = '';  // relative par défaut
    if (EIFFELAI_HOSTS.includes(hostname)) {
        apiBase = PI_TAILSCALE_BASE;
    }

    window.REACHY_API_BASE = apiBase;

    if (!apiBase) return;

    // Monkey-patch fetch : tout appel à `/api/…` (relatif) est préfixé par la
    // base Tailscale. credentials:'include' pour que le cookie d'auth soit
    // envoyé en cross-origin (le middleware auth du Pi accepte les origins
    // eiffelai.io via CORS_ALLOWED_ORIGINS).
    const origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        let url = typeof input === 'string' ? input : input.url;
        if (url.startsWith('/api/')) {
            url = apiBase + url;
            if (typeof input === 'string') {
                input = url;
            } else {
                input = new Request(url, input);
            }
            init = init || {};
            if (init.credentials === undefined) init.credentials = 'include';
        }
        return origFetch(input, init);
    };

    // Idem pour le MJPEG stream <img src="/api/video"> et EventSource pour SSE.
    // Les URLs absolues passées directement restent inchangées.
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('[src^="/api/"], [href^="/api/"]').forEach(function (el) {
            const attr = el.hasAttribute('src') ? 'src' : 'href';
            el.setAttribute(attr, apiBase + el.getAttribute(attr));
        });
    });
})();
