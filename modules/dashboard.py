"""
dashboard.py — Tableau de bord web Flask pour Reachy Care.

Fournit une interface web accessible sur le réseau local pour :
  - Flux vidéo MJPEG en temps réel
  - Journal quotidien par personne
  - Statut courant (mode, personne, uptime, veille)
  - Gestion des activités (liste, activation/désactivation)
  - Envoi de commandes
  - Stream SSE des logs

Le serveur Flask tourne dans un daemon thread pour ne pas bloquer
la boucle principale de main.py.

⚠️  SÉCURITÉ — état actuel : aucun mécanisme d'authentification sur
    ce serveur. Les endpoints /api/cmd, /api/settings/*, /api/power/*
    sont ouverts à quiconque peut joindre le port 8080. C'est
    acceptable en développement local (LAN fermé, Pi sur un wifi
    domestique isolé), inacceptable en production. Avant tout
    déploiement chez un utilisateur, ajouter une authentification
    (voir le pattern de cookie HMAC dans reachy_controller.py
    pour s'en inspirer) ou placer ce dashboard derrière un reverse
    proxy authentifié.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _update_config_local(updates: dict) -> dict:
    """Écrit/patche les valeurs dans config_local.py (overrides gitignored).

    config_local.py est importé par config.py via 'from config_local import *'.
    On ajoute/remplace des assignations ligne par ligne (simple grep-like).
    Ne crée PAS de fichier .bak — Git suit config.py, config_local.py est local.
    Les modifications prennent effet après restart main.py.
    """
    cfg_dir = Path(getattr(config, "BASE_DIR", Path("/home/pollen/reachy_care")))
    local_path = cfg_dir / "config_local.py"
    try:
        # Lire contenu existant (ou créer stub)
        if local_path.exists():
            lines = local_path.read_text().splitlines()
        else:
            lines = ["# config_local.py — overrides locaux (gitignored)", ""]

        # Pour chaque clé à mettre à jour, remplacer ou ajouter
        for key, value in updates.items():
            # Formater la valeur Python
            if value is None:
                val_str = "None"
            elif isinstance(value, bool):
                val_str = "True" if value else "False"
            elif isinstance(value, (int, float)):
                val_str = str(value)
            else:
                # string : échapper quotes simples
                val_str = repr(str(value))
            new_line = f"{key} = {val_str}"

            # Chercher ligne existante
            found = False
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
                    lines[i] = new_line
                    found = True
                    break
            if not found:
                lines.append(new_line)

        local_path.write_text("\n".join(lines) + "\n")
        logger.info("config_local.py mis à jour : %s", list(updates.keys()))
        return {"ok": True, "updated": list(updates.keys()), "path": str(local_path)}
    except OSError as exc:
        logger.exception("Erreur écriture config_local.py")
        return {"error": str(exc)}


class Dashboard:
    """Tableau de bord web Reachy Care — serveur Flask embarqué."""

    def __init__(self, frame_queue, journal, activity_registry):
        """
        Paramètres
        ----------
        frame_queue       : SharedFrameQueue — file de frames pour le flux MJPEG
        journal           : Journal — accès au journal quotidien
        activity_registry : ActivityRegistry — registre des activités/modes
        """
        self._frame_queue = frame_queue
        self._journal = journal
        self._activity_registry = activity_registry

        self._status: dict = {
            "mode": "normal",
            "person": None,
            "sleeping": False,
        }
        self._start_time: float = time.monotonic()
        self._server_thread: threading.Thread | None = None
        self._server = None  # werkzeug Server instance pour shutdown

        self._port = getattr(config, "DASHBOARD_PORT", 8080)
        self._mjpeg_fps = getattr(config, "DASHBOARD_MJPEG_FPS", 5)
        self._mjpeg_quality = getattr(config, "DASHBOARD_MJPEG_QUALITY", 60)

        self._app = self._create_app()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Lance le serveur Flask dans un daemon thread."""
        if self._server_thread is not None and self._server_thread.is_alive():
            logger.warning("Dashboard déjà démarré")
            return

        self._start_time = time.monotonic()
        self._server_thread = threading.Thread(
            target=self._run_server,
            name="dashboard-flask",
            daemon=True,
        )
        self._server_thread.start()
        logger.info("Dashboard démarré sur le port %d", self._port)

    def stop(self) -> None:
        """Arrête le serveur Flask proprement."""
        if self._server is not None:
            self._server.shutdown()
            logger.info("Dashboard arrêté")
        self._server = None
        self._server_thread = None

    def update_status(self, **kwargs) -> None:
        """Met à jour le statut partagé (appelé par main.py).

        Clés acceptées : mode, person, sleeping.
        """
        for key in ("mode", "person", "sleeping"):
            if key in kwargs:
                self._status[key] = kwargs[key]

    # ------------------------------------------------------------------
    # Construction de l'application Flask
    # ------------------------------------------------------------------

    def _create_app(self):
        from flask import Flask, Response, jsonify, request, send_from_directory

        resources_dir = (Path(getattr(config, "BASE_DIR", Path("."))) / "resources" / "dashboard").resolve()

        app = Flask(
            __name__,
            static_folder=str(resources_dir),
            template_folder=str(resources_dir),
        )
        # Pas de tri des clés JSON (préserve l'ordre d'insertion)
        app.config["JSON_SORT_KEYS"] = False

        # ---- Routes statiques ----

        @app.route("/")
        def index():
            return send_from_directory(str(resources_dir), "index.html")

        @app.route("/settings")
        def settings_page():
            return send_from_directory(str(resources_dir), "settings.html")

        @app.route("/static/<path:filename>")
        def static_files(filename):
            return send_from_directory(str(resources_dir), filename)

        # ---- API Settings (lecture + écriture config_local.py, profils) ----

        @app.route("/api/settings")
        def api_settings_get():
            """Renvoie les valeurs actuelles (lues depuis config effectif)."""
            return jsonify({
                "location": getattr(config, "LOCATION", ""),
                "timezone": getattr(config, "TIMEZONE", ""),
                "telegram_enabled": getattr(config, "TELEGRAM_ENABLED", False),
                "telegram_chat_id": getattr(config, "TELEGRAM_CHAT_ID", ""),
                "email_to": getattr(config, "ALERT_EMAIL_TO", ""),
                "email_from": getattr(config, "ALERT_EMAIL_FROM", ""),
                "email_enabled": getattr(config, "ALERT_EMAIL_ENABLED", False),
                "wake_word_threshold": float(getattr(config, "WAKE_WORD_THRESHOLD", 0.35)),
            })

        @app.route("/api/settings/location", methods=["POST"])
        def api_settings_location():
            data = request.get_json(silent=True) or {}
            return jsonify(_update_config_local({
                "LOCATION": data.get("location", ""),
                "TIMEZONE": data.get("timezone", ""),
            }))

        @app.route("/api/settings/telegram", methods=["POST"])
        def api_settings_telegram():
            data = request.get_json(silent=True) or {}
            return jsonify(_update_config_local({
                "TELEGRAM_BOT_TOKEN": data.get("token", ""),
                "TELEGRAM_CHAT_ID": data.get("chat_id", ""),
                "TELEGRAM_ENABLED": bool(data.get("enabled", False)),
            }))

        @app.route("/api/settings/email", methods=["POST"])
        def api_settings_email():
            data = request.get_json(silent=True) or {}
            return jsonify(_update_config_local({
                "ALERT_EMAIL_TO": data.get("to", ""),
                "ALERT_EMAIL_FROM": data.get("from", ""),
                "ALERT_EMAIL_PASSWORD": data.get("password", ""),
                "ALERT_EMAIL_ENABLED": bool(data.get("enabled", False)),
            }))

        @app.route("/api/settings/apikeys", methods=["POST"])
        def api_settings_apikeys():
            data = request.get_json(silent=True) or {}
            updates = {}
            if data.get("audd"): updates["AUDD_API_TOKEN"] = data["audd"]
            if data.get("brave"): updates["BRAVE_API_KEY"] = data["brave"]
            if not updates:
                return jsonify({"ok": True, "note": "aucune clé fournie"})
            return jsonify(_update_config_local(updates))

        @app.route("/api/settings/profile", methods=["POST"])
        def api_settings_profile():
            """Écrit known_faces/<name>_profile.json."""
            data = request.get_json(silent=True) or {}
            name = (data.get("name") or "").strip().lower()
            if not name:
                return jsonify({"error": "name obligatoire"}), 400
            kf = Path(getattr(config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
            profile_path = kf / f"{name}_profile.json"
            profile = {
                "medications": data.get("medications", []),
                "schedules": data.get("schedules", []),
                "emergency_contact": data.get("emergency_contact", ""),
                "notes": data.get("notes", ""),
            }
            try:
                profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2))
                logger.info("Profil enregistré : %s", profile_path)
                return jsonify({"ok": True, "path": str(profile_path)})
            except OSError as exc:
                logger.exception("Erreur écriture profil")
                return jsonify({"error": str(exc)}), 500

        # ---- API Wake words (scan Pi + built-in openWakeWord) ----

        def _wake_word_sounds_dir() -> Path:
            base = Path(getattr(config, "BASE_DIR", "/home/pollen/reachy_care"))
            return base / "resources" / "dashboard" / "sounds"

        def _wake_word_has_sample(name: str) -> bool:
            d = _wake_word_sounds_dir()
            for ext in (".mp3", ".wav", ".m4a", ".ogg"):
                if (d / f"{name}{ext}").exists():
                    return True
            return False

        def _wake_word_sample_url(name: str) -> str | None:
            d = _wake_word_sounds_dir()
            for ext in (".mp3", ".wav", ".m4a", ".ogg"):
                if (d / f"{name}{ext}").exists():
                    return f"/static/sounds/{name}{ext}"
            return None

        # Whitelist des wake words exposés dans l'UI :
        # - Custom : Hey Reachy (hey_Reatchy.onnx, seul custom stabilisé)
        # - Builtin openwakeword : alexa, hey_jarvis, hey_mycroft, hey_rhasspy
        # Tout autre modèle (nabu inexistant en openwakeword, modèles custom
        # expérimentaux) est exclu pour ne pas polluer le choix utilisateur.
        WAKE_WORD_CUSTOM_ALLOWED = {"hey_Reatchy", "hey_reachy"}
        WAKE_WORD_BUILTIN_ALLOWED = {"alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy"}
        WAKE_WORD_DISPLAY = {
            "hey_Reatchy":  "Hey Reachy (custom)",
            "hey_reachy":   "Hey Reachy (custom)",
            "alexa":        "Alexa",
            "hey_jarvis":   "Hey Jarvis",
            "hey_mycroft":  "Hey Mycroft",
            "hey_rhasspy":  "Hey Rhasspy",
        }

        @app.route("/api/wake_words")
        def api_wake_words():
            import importlib.util
            items = []
            models_dir = Path(getattr(config, "MODELS_DIR", "/home/pollen/reachy_care/models"))
            current_path = getattr(config, "WAKE_WORD_MODEL_PATH", None)
            current_fallback = getattr(config, "WAKE_WORD_FALLBACK", "hey_jarvis")
            current_custom_name = None
            if current_path and Path(str(current_path)).exists():
                current_custom_name = Path(str(current_path)).stem

            try:
                for onnx in sorted(models_dir.glob("hey_*.onnx")):
                    name = onnx.stem
                    if name not in WAKE_WORD_CUSTOM_ALLOWED:
                        continue
                    items.append({
                        "name": name,
                        "display_name": WAKE_WORD_DISPLAY.get(name, name),
                        "source": "custom",
                        "has_sample": _wake_word_has_sample(name),
                        "sample_url": _wake_word_sample_url(name),
                        "is_current": (current_custom_name == name),
                    })
            except OSError:
                pass

            try:
                spec = importlib.util.find_spec("openwakeword")
                if spec and spec.origin:
                    builtin_dir = Path(spec.origin).parent / "resources" / "models"
                    for onnx in sorted(builtin_dir.glob("hey_*.onnx")) + sorted(builtin_dir.glob("alexa*.onnx")):
                        raw = onnx.stem
                        name = raw.split("_v")[0] if "_v" in raw else raw
                        if name not in WAKE_WORD_BUILTIN_ALLOWED:
                            continue
                        if any(it["name"] == name for it in items):
                            continue
                        items.append({
                            "name": name,
                            "display_name": WAKE_WORD_DISPLAY.get(name, name),
                            "source": "builtin",
                            "has_sample": _wake_word_has_sample(name),
                            "sample_url": _wake_word_sample_url(name),
                            "is_current": (current_custom_name is None and current_fallback == name),
                        })
            except Exception as exc:
                logger.warning("wake_words: scan built-in échoué : %s", exc)

            return jsonify({"wake_words": items})

        @app.route("/api/settings/wake_word", methods=["POST"])
        def api_settings_wake_word():
            data = request.get_json(silent=True) or {}
            name = (data.get("name") or "").strip()
            source = (data.get("source") or "").strip()
            if not name or "/" in name or ".." in name:
                return jsonify({"error": "name invalide"}), 400
            models_dir = Path(getattr(config, "MODELS_DIR", "/home/pollen/reachy_care/models"))
            if source == "custom":
                candidate = models_dir / f"{name}.onnx"
                if not candidate.exists():
                    return jsonify({"error": f"modèle introuvable : {candidate}"}), 404
                updates = {
                    "WAKE_WORD_MODEL_PATH": str(candidate),
                    "WAKE_WORD_FALLBACK": name,
                }
            elif source == "builtin":
                updates = {
                    "WAKE_WORD_MODEL_PATH": "",
                    "WAKE_WORD_FALLBACK": name,
                }
            else:
                return jsonify({"error": "source doit être 'custom' ou 'builtin'"}), 400

            # Seuil optionnel — borné 0.20-0.80 pour éviter des valeurs absurdes
            if "threshold" in data:
                try:
                    th = float(data["threshold"])
                except (TypeError, ValueError):
                    return jsonify({"error": "threshold doit être un nombre"}), 400
                th = max(0.20, min(0.80, th))
                updates["WAKE_WORD_THRESHOLD"] = th

            result = _update_config_local(updates)
            if "error" not in result:
                result["note"] = "Redémarre main.py pour appliquer."
            return jsonify(result)

        # ---- API Personnes connues (registry.json) ----

        @app.route("/api/persons")
        def api_persons_list():
            kf = Path(getattr(config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
            registry_path = kf / "registry.json"
            try:
                reg = json.loads(registry_path.read_text()) if registry_path.exists() else {}
            except (OSError, json.JSONDecodeError) as exc:
                return jsonify({"error": str(exc)}), 500
            owner = reg.get("owner", "")
            secondary = reg.get("secondary", []) or []
            reserved = {"owner", "secondary", "robot_voice"}
            persons = []
            for name, val in reg.items():
                if name in reserved or not isinstance(val, dict):
                    continue
                persons.append({
                    "name": name,
                    "display_name": val.get("display_name", name.capitalize()),
                    "enrolled_at": val.get("enrolled_at", ""),
                    "n_photos": val.get("n_photos", 0),
                    "is_owner": name == owner,
                    "is_secondary": name in secondary,
                })
            persons.sort(key=lambda p: (not p["is_owner"], p["name"]))
            return jsonify({"persons": persons, "owner": owner})

        @app.route("/api/persons/<name>/profile")
        def api_person_profile_get(name):
            name = (name or "").strip().lower()
            if not name or "/" in name or ".." in name:
                return jsonify({"error": "nom invalide"}), 400
            kf = Path(getattr(config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
            profile_path = kf / f"{name}_profile.json"
            if not profile_path.exists():
                return jsonify({
                    "medications": [], "schedules": [],
                    "emergency_contact": "", "notes": "",
                })
            try:
                return jsonify(json.loads(profile_path.read_text()))
            except (OSError, json.JSONDecodeError) as exc:
                return jsonify({"error": str(exc)}), 500

        @app.route("/api/persons/<name>/primary", methods=["POST"])
        def api_person_set_primary(name):
            name = (name or "").strip().lower()
            if not name or "/" in name or ".." in name:
                return jsonify({"error": "nom invalide"}), 400
            kf = Path(getattr(config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
            registry_path = kf / "registry.json"
            try:
                reg = json.loads(registry_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return jsonify({"error": f"registry introuvable : {exc}"}), 500
            if name not in reg or not isinstance(reg.get(name), dict):
                return jsonify({"error": f"personne inconnue : {name}"}), 404
            old_owner = reg.get("owner", "")
            if old_owner == name:
                return jsonify({"ok": True, "note": "déjà primaire"})
            secondary = list(reg.get("secondary", []) or [])
            if old_owner and old_owner not in secondary:
                secondary.append(old_owner)
            secondary = [s for s in secondary if s != name]
            reg["owner"] = name
            reg["secondary"] = secondary
            try:
                registry_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2))
            except OSError as exc:
                return jsonify({"error": str(exc)}), 500
            logger.info("OWNER primaire changé : %s → %s", old_owner, name)
            return jsonify({
                "ok": True,
                "old_owner": old_owner,
                "new_owner": name,
                "note": "Redémarre main.py pour appliquer.",
            })

        # ---- API Status ----

        @app.route("/api/status")
        def api_status():
            uptime = time.monotonic() - self._start_time
            # Lister les personnes connues (fichiers *_memory.json dans known_faces)
            persons = []
            try:
                kf = Path(getattr(config, "KNOWN_FACES_DIR", "/home/pollen/reachy_care/known_faces"))
                persons = sorted(
                    p.stem.replace("_memory", "")
                    for p in kf.glob("*_memory.json")
                    if not p.stem.startswith(".")
                )
            except OSError:
                pass
            return jsonify({
                "mode": self._status.get("mode", "normal"),
                "person": self._status.get("person"),
                "uptime": round(uptime, 1),
                "sleeping": self._status.get("sleeping", False),
                "persons": persons,
            })

        # ---- API Journal ----

        @app.route("/api/journal/<person>")
        def api_journal_today(person):
            entries = self._journal.get_today(person)
            return jsonify(entries)

        @app.route("/api/journal/<person>/<date>")
        def api_journal_date(person, date):
            entries = self._journal.get_date(person, date)
            return jsonify(entries)

        # ---- API Modes ----

        @app.route("/api/modes")
        def api_modes():
            modes = []
            current_mode = self._status.get("mode", "normal")
            if self._activity_registry is not None:
                for act in self._activity_registry.get_all_manifests():
                    modes.append({
                        "name": act.get("name", ""),
                        "display_name": act.get("display_name", act.get("name", "")),
                        "active": act.get("name") == current_mode,
                        "gates": act.get("gates", {}),
                    })
            return jsonify(modes)

        # ---- API Activities ----

        @app.route("/api/activities")
        def api_activities():
            activities = []
            current_mode = self._status.get("mode", "normal")
            if self._activity_registry is not None:
                for act in self._activity_registry.get_all_manifests():
                    name = act.get("name", "")
                    activities.append({
                        "name": name,
                        "display_name": act.get("display_name", name),
                        "enabled": True,  # pour l'instant toutes les activités chargées sont actives
                        "active": name == current_mode,
                        "tools": act.get("tools", []),
                    })
            return jsonify(activities)

        @app.route("/api/activities/<name>/enable", methods=["POST"])
        def api_activity_enable(name):
            # Placeholder — stockage état dans un fichier JSON à venir
            logger.info("Activation activité : %s", name)
            return jsonify({"ok": True, "name": name, "enabled": True})

        @app.route("/api/activities/<name>/disable", methods=["POST"])
        def api_activity_disable(name):
            logger.info("Désactivation activité : %s", name)
            return jsonify({"ok": True, "name": name, "enabled": False})

        # ---- API Runtime state (toggles lus par main.py en live) ----
        # Fichier partagé avec main.py qui poll ce JSON pour maj ses flags.
        # Path cohérent avec /tmp/reachy_care_status.json déjà en place.

        RUNTIME_STATE_PATH = Path("/tmp/reachy_runtime_state.json")
        RUNTIME_STATE_DEFAULTS = {"attenlabs_enabled": True}

        def _read_runtime_state() -> dict:
            try:
                data = json.loads(RUNTIME_STATE_PATH.read_text())
                if not isinstance(data, dict):
                    return dict(RUNTIME_STATE_DEFAULTS)
                return {**RUNTIME_STATE_DEFAULTS, **data}
            except (FileNotFoundError, json.JSONDecodeError):
                return dict(RUNTIME_STATE_DEFAULTS)

        def _write_runtime_state(updates: dict) -> dict:
            current = _read_runtime_state()
            current.update(updates)
            tmp = RUNTIME_STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(current))
            os.replace(str(tmp), str(RUNTIME_STATE_PATH))
            return current

        @app.route("/api/runtime-state", methods=["GET"])
        def api_runtime_state_get():
            return jsonify(_read_runtime_state())

        @app.route("/api/runtime-state", methods=["POST"])
        def api_runtime_state_set():
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or not data:
                return jsonify({"error": "body JSON non vide requis"}), 400
            allowed = {"attenlabs_enabled"}
            updates: dict = {}
            for key, value in data.items():
                if key not in allowed:
                    return jsonify({"error": f"clé inconnue: {key}"}), 400
                if key == "attenlabs_enabled":
                    if isinstance(value, str):
                        value = value.lower() in ("true", "1", "yes")
                    updates[key] = bool(value)
            state = _write_runtime_state(updates)
            logger.info("Runtime state mis à jour: %s", updates)
            return jsonify({"ok": True, "state": state})

        # ---- API Commandes ----

        @app.route("/api/cmd", methods=["POST"])
        def api_cmd():
            cmd = request.get_json(silent=True)
            if not cmd:
                return jsonify({"error": "corps JSON manquant"}), 400

            # Écriture atomique dans /tmp/reachy_care_cmds/
            cmds_dir = "/tmp/reachy_care_cmds"
            os.makedirs(cmds_dir, exist_ok=True)
            dest = f"{cmds_dir}/{time.time_ns()}.json"
            try:
                with tempfile.NamedTemporaryFile(
                    "w", dir=cmds_dir, delete=False, suffix=".tmp"
                ) as f:
                    json.dump(cmd, f)
                    tmp = f.name
                os.replace(tmp, dest)
            except OSError:
                logger.exception("Erreur écriture commande dashboard")
                return jsonify({"error": "écriture impossible"}), 500

            logger.info("Commande dashboard reçue : %s", cmd.get("cmd", "?"))
            return jsonify({"ok": True})

        # ---- Flux vidéo MJPEG ----

        @app.route("/video_feed")
        def video_feed():
            return Response(
                self._mjpeg_generator(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

        # ---- SSE Logs ----

        @app.route("/api/logs")
        def api_logs():
            return Response(
                self._sse_log_generator(),
                mimetype="text/event-stream",
            )

        return app

    # ------------------------------------------------------------------
    # Générateurs
    # ------------------------------------------------------------------

    def _mjpeg_generator(self):
        """Génère un flux MJPEG depuis la SharedFrameQueue."""
        try:
            import cv2
        except ImportError:
            # cv2 non disponible — placeholder texte
            logger.warning("cv2 non disponible — flux vidéo désactivé")
            yield (
                b"--frame\r\n"
                b"Content-Type: text/plain\r\n\r\n"
                b"video indisponible (cv2 absent)\r\n"
            )
            return

        frame_interval = 1.0 / max(self._mjpeg_fps, 1)
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self._mjpeg_quality]

        while True:
            frame = self._frame_queue.get()
            if frame is None:
                time.sleep(frame_interval)
                continue

            ok, jpeg = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                time.sleep(frame_interval)
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + jpeg.tobytes()
                + b"\r\n"
            )
            time.sleep(frame_interval)

    def _sse_log_generator(self):
        """Stream SSE qui envoie les dernières lignes du log puis suit les nouvelles."""
        log_path = getattr(config, "LOG_FILE", None)
        if log_path is None:
            yield "data: [log file non configuré]\n\n"
            return

        log_path = Path(log_path)
        if not log_path.exists():
            yield "data: [fichier log introuvable]\n\n"
            return

        # Envoyer les 50 dernières lignes existantes
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                tail = lines[-50:] if len(lines) > 50 else lines
                for line in tail:
                    yield f"data: {line.rstrip()}\n\n"
                # Mémoriser la position courante pour le suivi
                pos = f.tell()
        except OSError:
            yield "data: [erreur lecture log]\n\n"
            return

        # Suivre les nouvelles lignes (poll toutes les secondes)
        while True:
            time.sleep(1.0)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    new_lines = f.readlines()
                    if new_lines:
                        for line in new_lines:
                            yield f"data: {line.rstrip()}\n\n"
                        pos = f.tell()
            except OSError:
                yield "data: [erreur lecture log]\n\n"
                break

    # ------------------------------------------------------------------
    # Serveur interne
    # ------------------------------------------------------------------

    def _run_server(self):
        """Exécute le serveur Flask (appelé dans le daemon thread)."""
        from werkzeug.serving import make_server

        try:
            self._server = make_server(
                "0.0.0.0",
                self._port,
                self._app,
                threaded=True,
            )
            self._server.serve_forever()
        except Exception:
            logger.exception("Erreur fatale du serveur dashboard")
