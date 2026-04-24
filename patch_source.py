#!/usr/bin/env python3
"""
patch_source.py — Patche le clone git de reachy_mini_conversation_app.

Cible : /home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/
- openai_realtime.py : injecte _external_events, _asyncio_loop, asyncio task, méthodes bridge
- main.py            : enregistre le bridge après instanciation du handler
"""

import os
import subprocess
import sys

CONV_APP_DIR = "/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app"
REALTIME_PATH = f"{CONV_APP_DIR}/openai_realtime.py"
MAIN_PATH = f"{CONV_APP_DIR}/main.py"
MARKER_ALREADY_PATCHED = "reachy-care-events-clean"

# ---------------------------------------------------------------------------
# Patch openai_realtime.py
# ---------------------------------------------------------------------------

REALTIME_INIT_MARKER = "        self.deps = deps"
REALTIME_INIT_INJECTION = """        self.deps = deps
        # [Reachy Care] bridge
        self._external_events: asyncio.Queue = asyncio.Queue()
        self._asyncio_loop = None
        self._reachy_response_free = None
        self._reachy_sleeping = False
        self._reading_mode = False
        self._reachy_tool_idle = True
        self._stop_speaking_pending = False
        self._chess_vad_disabled = False
        self._reachy_tts_locked = False
        self._tts_response_done_at = 0.0
        self._current_interrupt_response = False
        self._echo_stt_cache: dict = {}
        self._attention_state: str = "SILENT" """

REALTIME_CONN_MARKER = "            self.connection = conn"
REALTIME_CONN_INJECTION = """            self.connection = conn
            # [Reachy Care] bridge — capture loop + IPC HTTP server
            self._asyncio_loop = asyncio.get_event_loop()
            self._reachy_response_free = asyncio.Event()
            self._reachy_response_free.set()
            asyncio.create_task(self._process_external_events(), name='reachy-care-events-clean')
            self._start_reachy_care_server()"""

REALTIME_METHODS = '''
    # ------------------------------------------------------------------
    # [Reachy Care] bridge — external event injection
    # ------------------------------------------------------------------

    async def _process_external_events(self) -> None:
        """Consomme la queue d\'événements externes et les injecte dans la session.

        Attend que la réponse OpenAI Realtime en cours soit terminée (asyncio.Event)
        avant d\'injecter — zéro retry, zéro CPU gaspillé.
        """
        import asyncio as _asyncio_ev
        import logging as _log_ev
        _log = _log_ev.getLogger(__name__)
        while True:
            text, instructions = await self._external_events.get()
            try:
                if self._reachy_response_free is not None:
                    try:
                        await _asyncio_ev.wait_for(
                            self._reachy_response_free.wait(), timeout=15.0
                        )
                    except _asyncio_ev.TimeoutError:
                        _log.warning("[Reachy Care] Timeout attente réponse (15s) — Event réinitialisé")
                        self._reachy_response_free.set()
                # Attendre que les background tools soient aussi terminés (max 2s)
                for _ in range(20):
                    if getattr(self, '_reachy_tool_idle', True):
                        break
                    await _asyncio_ev.sleep(0.1)
                if getattr(self.connection, "conversation", None) is None:
                    _log.warning("[Reachy Care] conversation=None — injection ignorée (session expirée)")
                    continue
                await self.connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                )
                if getattr(self, '_stop_speaking_pending', False):
                    self._stop_speaking_pending = False
                    _log.info("[Reachy Care] stop_speaking_pending — réponse LLM supprimée.")
                else:
                    # Fix : s'assurer qu'aucune response n'est en cours (race condition)
                    if self._reachy_response_free is not None and not self._reachy_response_free.is_set():
                        _log.debug("[Reachy Care] response encore active — attente 3s supplémentaires")
                        try:
                            await _asyncio_ev.wait_for(self._reachy_response_free.wait(), timeout=3.0)
                        except _asyncio_ev.TimeoutError:
                            _log.warning("[Reachy Care] response toujours active — cancel avant re-create")
                            await self.cancel_current_response()
                            await _asyncio_ev.sleep(0.3)
                    await self._safe_response_create(
                        response={"instructions": instructions}
                    )
            except Exception as exc:
                _log.warning("[Reachy Care] Injection événement échouée : %s", exc)
            finally:
                self._external_events.task_done()

    async def schedule_external_event(self, text: str, response_instructions: str) -> None:
        """Planifie un événement externe depuis un thread synchrone."""
        await self._external_events.put((text, response_instructions))

    async def schedule_session_update(self, new_instructions: str) -> None:
        """Met à jour les instructions de session OpenAI Realtime en runtime."""
        import logging as _logging
        import os as _os
        import json as _json_m
        _log = _logging.getLogger(__name__)
        if not getattr(self, "connection", None):
            _log.warning("[Reachy Care] schedule_session_update: pas de connexion, ignoré.")
            return
        # Item 55 — ne pas écraser le VAD 0.99 posé par stop_speaking si bridge muet
        # Vérifie la queue répertoire pour un cmd "mute" en attente de traitement par main.py
        _is_muted = False
        try:
            _cmd_dir = "/tmp/reachy_care_cmds"
            for _p in _os.scandir(_cmd_dir):
                if _p.name.endswith(".json"):
                    try:
                        with open(_p.path) as _f:
                            if _json_m.load(_f).get("cmd") == "mute":
                                _is_muted = True
                                break
                    except Exception:
                        pass
        except Exception:
            pass
        is_lecture = getattr(self, '_reading_mode', False)
        is_echecs = "MODE ÉCHECS" in new_instructions or "chess_move" in new_instructions
        # _current_interrupt_response contrôle le vidage local de la queue audio (patch speech_started).
        # False en mode lecture : ne pas couper la queue audio si l'utilisateur parle pendant la lecture.
        # True en mode échecs : le LLM doit pouvoir s'interrompre pour écouter le joueur.
        self._current_interrupt_response = not is_lecture
        _chess_vad_off = getattr(self, '_chess_vad_disabled', False)
        if _is_muted or _chess_vad_off:
            # Bridge muté ou pipeline chess vocal actif : instructions uniquement, VAD 0.99 préservé
            session_payload = {"type": "realtime", "instructions": new_instructions}
            _log.info("[Reachy Care] Session update (muted=%s, chess_vad=%s) : %d chars — VAD préservé.", _is_muted, _chess_vad_off, len(new_instructions))
        else:
            session_payload = {
                "type": "realtime",
                "instructions": new_instructions,
                "audio": {"output": {"speed": 0.88 if is_lecture else 1.0}},
            }
        try:
            await self.connection.session.update(session=session_payload)
            if not _is_muted:
                _log.info(
                    "[Reachy Care] Session update : %d chars, speed=%.2f, echecs=%s.",
                    len(new_instructions),
                    0.88 if is_lecture else 1.0,
                    is_echecs,
                )
            # Mode échecs : le LLM écoute le joueur pour interpréter les coups
            # (pas de VAD spécial — le LLM a besoin d'entendre)
        except Exception as exc:
            _log.error("[Reachy Care] Erreur session.update : %s", exc)

    async def cancel_current_response(self) -> None:
        """Annule la réponse vocale en cours côté serveur OpenAI Realtime."""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            await self.connection.response.cancel()
        except Exception as exc:
            _log.debug(
                "[Reachy Care] response.cancel ignoré (pas de réponse active) : %s", exc
            )

    def _start_reachy_care_server(self) -> None:
        """Démarre le serveur HTTP IPC Reachy Care sur localhost:8766."""
        import threading as _threading
        import json as _json
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import asyncio as _asyncio
        import logging as _logging
        import time as _time
        import sys as _sys
        _log = _logging.getLogger(__name__)
        _PORT = 8766
        _self = self
        _loop = self._asyncio_loop
        # Stocke le handler au niveau module pour accès direct depuis switch_mode.py
        _sys.modules[__name__]._reachy_care_handler = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self_h):
                """Gère les requêtes GET — uniquement /get_frame pour l\'instant."""
                try:
                    if self_h.path == "/get_frame":
                        import cv2 as _cv2_gf
                        cw_gf = getattr(getattr(_self, "deps", None), "camera_worker", None)
                        _jpg_bytes = None
                        if cw_gf is not None:
                            _raw_frame = cw_gf.get_latest_frame()
                            if _raw_frame is not None:
                                _ok, _buf = _cv2_gf.imencode(".jpg", _raw_frame, [_cv2_gf.IMWRITE_JPEG_QUALITY, 70])
                                if _ok:
                                    _jpg_bytes = _buf.tobytes()
                        if _jpg_bytes:
                            self_h.send_response(200)
                            self_h.send_header("Content-Type", "image/jpeg")
                            self_h.send_header("Content-Length", str(len(_jpg_bytes)))
                            self_h.end_headers()
                            self_h.wfile.write(_jpg_bytes)
                        else:
                            self_h.send_response(204)
                            self_h.end_headers()
                    else:
                        self_h.send_response(404)
                        self_h.end_headers()
                except Exception as exc:
                    _log.error("[Reachy Care] GET handler error: %s", exc)
                    try:
                        self_h.send_response(500)
                        self_h.end_headers()
                    except Exception:
                        pass

            def do_POST(self_h):
                try:
                    length = int(self_h.headers.get("Content-Length", 0))
                    body = _json.loads(self_h.rfile.read(length)) if length else {}
                    path = self_h.path

                    if path == "/event":
                        # urgent=True (chute, alerte) → cancel immédiat avant injection
                        if body.get("urgent"):
                            _asyncio.run_coroutine_threadsafe(
                                _self.cancel_current_response(),
                                _loop,
                            )
                        _asyncio.run_coroutine_threadsafe(
                            _self.schedule_external_event(body["text"], body["instructions"]),
                            _loop,
                        )
                    elif path == "/session_update":
                        _asyncio.run_coroutine_threadsafe(
                            _self.schedule_session_update(body["instructions"]),
                            _loop,
                        )
                    elif path == "/cancel":
                        _asyncio.run_coroutine_threadsafe(
                            _self.cancel_current_response(),
                            _loop,
                        )
                    elif path == "/attention":
                        _att_state = body.get("state", "SILENT")
                        if _att_state in ("SILENT", "TO_HUMAN", "TO_COMPUTER"):
                            _self._attention_state = _att_state
                            _log.info("[Reachy Care] AttenLabs état → %s", _att_state)
                        else:
                            _log.warning("[Reachy Care] AttenLabs état inconnu ignoré : %s", _att_state)
                    elif path == "/wake":
                        # Réinitialise l\'état idle + mode veille sans injecter de message
                        if hasattr(_self, "last_activity_time"):
                            _self.last_activity_time = _time.time()
                        if hasattr(_self, "is_idle_tool_call"):
                            _self.is_idle_tool_call = False
                        _was_sleeping = _self._reachy_sleeping
                        _self._reachy_sleeping = False
                        _self._reachy_tts_locked = False  # libère le lock stop_speaking
                        # Couper la lecture/TTS en cours (fix mode histoire)
                        _self._reading_mode = False
                        _self._stop_speaking_pending = False  # wake doit CLEAR le pending
                        _asyncio.run_coroutine_threadsafe(
                            _self.cancel_current_response(),
                            _loop,
                        )
                        # Restaurer le VAD normal si on sort du sleep mode (privacy → écoute)
                        if _was_sleeping:
                            async def _do_wake_vad(conn=_self.connection):
                                try:
                                    await conn.session.update(session={
                                        "type": "realtime",
                                        "audio": {"input": {"turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800, "prefix_padding_ms": 500}}},
                                    })
                                except Exception:
                                    pass
                            _asyncio.run_coroutine_threadsafe(_do_wake_vad(), _loop)
                            _log.info("[Reachy Care] /wake : VAD threshold → 0.5 (restauré depuis sleep)")
                        _log.info("[Reachy Care] /wake : response.cancel + reading_mode=False")

                    elif path == "/sleep":
                        # Inhibe l\'idle timer + coupe l\'écoute micro (privacy)
                        _self._reachy_sleeping = True
                        async def _do_sleep_vad(conn=_self.connection):
                            try:
                                await conn.session.update(session={
                                    "type": "realtime",
                                    "audio": {"input": {"turn_detection": None}},
                                })
                            except Exception:
                                pass
                        _asyncio.run_coroutine_threadsafe(_do_sleep_vad(), _loop)
                        _log.info("[Reachy Care] /sleep : turn_detection=null (privacy mute)")

                    elif path == "/disable_vad":
                        # [Reachy Care] Chess voice pipeline : couper l\'écoute LLM
                        _self._chess_vad_disabled = True
                        async def _do_disable_vad(conn=_self.connection):
                            try:
                                await conn.session.update(session={
                                    "type": "realtime",
                                    "audio": {"input": {"turn_detection": None}},
                                })
                            except Exception:
                                pass
                        _asyncio.run_coroutine_threadsafe(_do_disable_vad(), _loop)
                        _log.info("[Reachy Care] /disable_vad : turn_detection=null (chess pipeline — mute total)")

                    elif path == "/enable_vad":
                        # [Reachy Care] Chess voice pipeline : restaurer l\'écoute LLM
                        _self._chess_vad_disabled = False
                        async def _do_enable_vad(conn=_self.connection):
                            try:
                                await conn.session.update(session={
                                    "type": "realtime",
                                    "audio": {"input": {"turn_detection": {"type": "server_vad", "threshold": 0.5}}},
                                })
                            except Exception:
                                pass
                        _asyncio.run_coroutine_threadsafe(_do_enable_vad(), _loop)
                        _log.info("[Reachy Care] /enable_vad : VAD threshold → 0.5 (normal)")

                    elif path == "/bt_mode_on":
                        # Active le mode BT : VAD haut + interrupt_response=False
                        _self._current_interrupt_response = False
                        async def _do_bt_vad(conn=_self.connection):
                            try:
                                await conn.session.update(session={
                                    "type": "realtime",
                                    "audio": {"input": {"turn_detection": {"type": "server_vad", "threshold": 0.85, "silence_duration_ms": 1200, "prefix_padding_ms": 500}}},
                                })
                            except Exception:
                                pass
                        _asyncio.run_coroutine_threadsafe(_do_bt_vad(), _loop)
                        _log.info("[Reachy Care] /bt_mode_on : BT audio actif")

                    elif path == "/bt_mode_off":
                        # Désactive le mode BT : restaure VAD normal + interrupt_response=True
                        _self._current_interrupt_response = True
                        async def _do_usb_vad(conn=_self.connection):
                            try:
                                await conn.session.update(session={
                                    "type": "realtime",
                                    "audio": {"input": {"turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800, "prefix_padding_ms": 500}}},
                                })
                            except Exception:
                                pass
                        _asyncio.run_coroutine_threadsafe(_do_usb_vad(), _loop)
                        _log.info("[Reachy Care] /bt_mode_off : USB audio restauré")

                    elif path == "/set_head_pitch":
                        import math as _math
                        pitch_deg = float(body.get("pitch_deg", 0.0))
                        pitch_rad = _math.radians(pitch_deg)
                        cw = getattr(getattr(_self, "deps", None), "camera_worker", None)
                        if cw is not None:
                            # Désactive le face tracking en mode echecs (sinon il ramène la tête à 0°)
                            if hasattr(cw, "set_head_tracking_enabled"):
                                cw.set_head_tracking_enabled(pitch_deg == 0.0)
                            # Utilise set_head_pitch_override si dispo (résistant à l\'interpolation face tracking)
                            if hasattr(cw, "set_head_pitch_override"):
                                cw.set_head_pitch_override(pitch_rad)
                            else:
                                ft = getattr(cw, "face_tracking_offsets", None)
                                if ft is not None and len(ft) > 4:
                                    ft[4] = pitch_rad
                        _log.info("[Reachy Care] Head pitch override → %.1f°", pitch_deg)

                    elif path == "/get_frame":
                        # [reachy-care-camera] Expose la dernière frame caméra de la conv_app en JPEG
                        # Utilisé par bridge.get_frame() → main.py → dashboard MJPEG
                        import cv2 as _cv2_gf
                        cw_gf = getattr(getattr(_self, "deps", None), "camera_worker", None)
                        _jpg_bytes = None
                        if cw_gf is not None:
                            _raw_frame = cw_gf.get_latest_frame()
                            if _raw_frame is not None:
                                _ok, _buf = _cv2_gf.imencode(".jpg", _raw_frame, [_cv2_gf.IMWRITE_JPEG_QUALITY, 70])
                                if _ok:
                                    _jpg_bytes = _buf.tobytes()
                        if _jpg_bytes is not None:
                            self_h.send_response(200)
                            self_h.send_header("Content-Type", "image/jpeg")
                            self_h.send_header("Content-Length", str(len(_jpg_bytes)))
                            self_h.end_headers()
                            self_h.wfile.write(_jpg_bytes)
                        else:
                            self_h.send_response(204)
                            self_h.end_headers()
                        return  # headers déjà envoyés — skip le send_response générique

                    self_h.send_response(200)
                except Exception as exc:
                    _log.error("[Reachy Care] IPC handler error: %s", exc)
                    self_h.send_response(500)
                finally:
                    self_h.end_headers()

            def log_message(self_h, *args):
                pass  # Silence HTTP logs

        def _serve():
            try:
                class _ReuseHTTPServer(HTTPServer):
                    allow_reuse_address = True
                srv = _ReuseHTTPServer(("127.0.0.1", _PORT), _Handler)
                _log.info("[Reachy Care] Serveur IPC HTTP démarré sur localhost:%d ✅", _PORT)
                srv.serve_forever()
            except Exception as exc:
                _log.error("[Reachy Care] Serveur IPC échec : %s", exc)

        t = _threading.Thread(target=_serve, name="reachy-care-ipc", daemon=True)
        t.start()
'''


# ---------------------------------------------------------------------------
# Patch openai_realtime.py — P0 : ne pas couper la lecture si interrupt_response=False
# Fix : speech_started ne vide la queue QUE si interrupt_response est actif
# ---------------------------------------------------------------------------

REALTIME_SPEECH_STARTED_MARKER = 'if event.type == "input_audio_buffer.speech_started":\n                        if hasattr(self, "_clear_queue") and callable(self._clear_queue):\n                            self._clear_queue()'
REALTIME_SPEECH_STARTED_INJECTION = (
    'if event.type == "input_audio_buffer.speech_started":\n'
    '                        if getattr(self, "_current_interrupt_response", False) and hasattr(self, "_clear_queue") and callable(self._clear_queue):\n'
    '                            self._clear_queue()\n'
    '                    elif event.type == "conversation.item.input_audio_transcription.completed":\n'
    '                        # [reachy-care-attenlabs] AttenLabs gate : bloquer STT si pas orienté vers le robot\n'
    '                        _stt_text = getattr(event, "transcript", "") or ""\n'
    '                        _is_echo = False\n'
    '                        if not _is_echo and _stt_text:\n'
    '                            _att_state = getattr(self, "_attention_state", "SILENT")\n'
    '                            if _att_state != "TO_COMPUTER":\n'
    '                                # Ne gate que si aucune réponse en cours (ne jamais couper une lecture)\n'
    '                                _resp_free = getattr(self, "_reachy_response_free", None)\n'
    '                                _is_speaking = (_resp_free is not None and not _resp_free.is_set())\n'
    '                                if not _is_speaking:\n'
    '                                    _is_echo = True\n'
    '                                    import logging as _log_att\n'
    '                                    _log_att.getLogger(__name__).info("[Reachy Care] AttenLabs: état=%s → STT ignoré (\'%s\')", _att_state, _stt_text[:60])\n'
    '                        if _is_echo:\n'
    '                            # P4 — log structuré : contexte complet du blocage\n'
    '                            import logging as _log_blk\n'
    '                            _rf_blk = getattr(self, "_reachy_response_free", None)\n'
    '                            _log_blk.getLogger(__name__).info(\n'
    '                                "[Reachy Care] STT_BLOCKED | att=%s speaking=%s | \'%s\'",\n'
    '                                getattr(self, "_attention_state", "?"),\n'
    '                                _rf_blk is not None and not _rf_blk.is_set(),\n'
    '                                _stt_text[:50] if _stt_text else "",\n'
    '                            )\n'
    '                            try:\n'
    '                                await self.connection.response.cancel()\n'
    '                            except Exception:\n'
    '                                pass\n'
    '                            try:\n'
    '                                await self.connection.input_audio_buffer.clear()\n'
    '                            except Exception:\n'
    '                                pass\n'
    '                            if self._reachy_response_free is not None:\n'
    '                                self._reachy_response_free.set()\n'
    '                    elif event.type in ("response.created", "response.output_item.added"):\n'
    '                        if self._reachy_response_free is not None:\n'
    '                            self._reachy_response_free.clear()\n'
    '                        # Cancel si stop_speaking, sleeping ou tts_lock persistant (BUG A)\n'
    '                        if getattr(self, "_stop_speaking_pending", False) or getattr(self, "_reachy_sleeping", False) or getattr(self, "_reachy_tts_locked", False):\n'
    '                            try:\n'
    '                                await self.connection.response.cancel()\n'
    '                                import logging as _log_mute\n'
    '                                _log_mute.getLogger(__name__).info("[Reachy Care] Cancel: sleeping=%s stop_pending=%s locked=%s", self._reachy_sleeping, self._stop_speaking_pending, self._reachy_tts_locked)\n'
    '                            except Exception:\n'
    '                                pass\n'
    '                    elif event.type == "response.done":\n'
    '                        if self._reachy_response_free is not None:\n'
    '                            self._reachy_response_free.set()\n'
    '                        # Clear chess_move anti-spam flag 10s après (laisser le joueur parler)\n'
    '                        if getattr(self, "_chess_move_just_returned", False):\n'
    '                            import asyncio as _asyncio_cm\n'
    '                            async def _clear_chess_flag(handler):\n'
    '                                await _asyncio_cm.sleep(10)\n'
    '                                handler._chess_move_just_returned = False\n'
    '                            _asyncio_cm.ensure_future(_clear_chess_flag(self))\n'
    '                    elif event.type == "response.output_item.done":\n'
    '                        import json as _json_roid\n'
    '                        _item_roid = getattr(event, "item", None)\n'
    '                        if _item_roid is not None and getattr(_item_roid, "type", None) == "function_call":\n'
    '                            _fname = getattr(_item_roid, "name", None)\n'
    '                            if _fname == "gutenberg":\n'
    '                                try:\n'
    '                                    _out = getattr(_item_roid, "output", None)\n'
    '                                    _parsed = _json_roid.loads(_out) if isinstance(_out, str) else (_out or {})\n'
    '                                    self._reading_mode = not _parsed.get("end_of_book", False)\n'
    '                                except Exception:\n'
    '                                    self._reading_mode = False\n'
    '                            elif _fname == "stop_speaking":\n'
    '                                self._stop_speaking_pending = True\n'
    '                                self._reachy_tts_locked = True\n'
    '                                self._tts_response_done_at = 0.0  # reset — user peut parler immédiatement\n'
    '                                async def _auto_unlock_stop(h=self):\n'
    '                                    import asyncio as _aw_ul\n'
    '                                    import logging as _log_ul\n'
    '                                    await _aw_ul.sleep(1.0)\n'
    '                                    h._reachy_tts_locked = False\n'
    '                                    h._stop_speaking_pending = False\n'
    '                                    try:\n'
    '                                        await h.connection.input_audio_buffer.clear()\n'
    '                                    except Exception:\n'
    '                                        pass\n'
    '                                    _log_ul.getLogger(__name__).info("[Reachy Care] stop_speaking : verrou levé après 1s — Reachy écoute")\n'
    '                                asyncio.create_task(_auto_unlock_stop())\n'
    '                            elif _fname == "chess_move":\n'
    '                                self._chess_move_just_returned = True\n'
    '                            self._reachy_tool_idle = True\n'
    '                    elif event.type == "response.function_call_arguments.done":\n'
    '                        self._reachy_tool_idle = False\n'
    '                        # Anti-spam chess_move : si le LLM re-appelle chess_move juste après un retour, annuler\n'
    '                        import time as _time_fc\n'
    '                        _fc_name = getattr(event, "name", None) or getattr(getattr(event, "item", None), "name", None)\n'
    '                        if _fc_name == "chess_move" and getattr(self, "_chess_move_just_returned", False):\n'
    '                            import logging as _log_fc\n'
    '                            _log_fc.getLogger(__name__).warning("[Reachy Care] chess_move anti-spam — cancel response")\n'
    '                            try:\n'
    '                                await self.connection.response.cancel()\n'
    '                            except Exception:\n'
    '                                pass\n'
    '                    elif event.type == "session.expired":\n'
    '                        import sys as _sys_se\n'
    '                        import logging as _log_se\n'
    '                        _log_se.getLogger(__name__).warning("[Reachy Care] session.expired — exit propre (watchdog 55min relancera)")\n'
    '                        _sys_se.exit(0)'
)

# ---------------------------------------------------------------------------
# Patch moves.py — backoff sur connexion gRPC perdue (évite saturation CPU)
# ---------------------------------------------------------------------------

MOVES_PATH = f"{CONV_APP_DIR}/moves.py"
MOVES_MARKER = 'logger.error(f"Failed to set robot target: {e}"'
MOVES_INJECTION = '''import time as _time
        _now = _time.monotonic()
        if not hasattr(self, "_last_grpc_error_t") or _now - self._last_grpc_error_t > 1.0:
            logger.error(f"Failed to set robot target: {e}")
            self._last_grpc_error_t = _now'''

MOVES_ANTENNA_MARKER = "        self.antenna_sway_amplitude = np.deg2rad(15)  # 15 degrees"
MOVES_ANTENNA_INJECTION = "        self.antenna_sway_amplitude = 0.0  # antennes immobiles au repos"

# PI ONLY #2 : breathing phase — antennes = None (skip gRPC commande servo)
MOVES_BREATHING_ANTENNA_MARKER = (
    "            # Antenna sway (opposite directions)\n"
    "            antenna_sway = self.antenna_sway_amplitude * np.sin(2 * np.pi * self.antenna_frequency * breathing_time)\n"
    "            antennas = np.array([antenna_sway, -antenna_sway], dtype=np.float64)"
)
MOVES_BREATHING_ANTENNA_INJECTION = (
    "            # Antennes immobiles en phase breathing — aucune commande gRPC envoyée\n"
    "            antennas = None"
)

# ---------------------------------------------------------------------------
# Patch openai_realtime.py — interrupt_response=False (mode lecture)
# Garde la voix de lecture intacte pendant les petits bruits ambiants.
# ---------------------------------------------------------------------------

VAD_FIX_MARKER = '"interrupt_response": True,'
VAD_FIX_INJECTION = '"interrupt_response": False,  # [reachy-care-vad-fix]'

def patch_file(path: str, patches: list[tuple[str, str]], methods_append: str = "") -> int:
    """Applique les patches. Retourne le nombre de marqueurs manqués."""
    bak_path = path + ".bak"
    with open(path, encoding="utf-8") as f:
        txt = f.read()

    if MARKER_ALREADY_PATCHED in txt:
        # Re-patch depuis .bak pour intégrer les nouveaux patches
        if os.path.exists(bak_path):
            print(f"  Re-patch depuis .bak : {path}")
            with open(bak_path, encoding="utf-8") as f:
                txt = f.read()
        else:
            print(f"  Déjà patché (pas de .bak) : {path}")
            return 0
    else:
        # Premier patch — créer le backup
        with open(bak_path, "w", encoding="utf-8") as f:
            f.write(txt)

    missing = 0
    for marker, injection in patches:
        if marker not in txt:
            print(f"  ⚠ Marqueur introuvable : {repr(marker[:60])}")
            missing += 1
            continue
        txt = txt.replace(marker, injection, 1)
        print(f"  Patch appliqué : {repr(marker[:60])}")

    if methods_append:
        txt = txt.rstrip() + "\n" + methods_append + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"  Fichier mis à jour : {path}")
    return missing


print("=" * 60)
print("  patch_source.py — Reachy Care bridge injection")
print("=" * 60)

print("\n[1/6] Patch openai_realtime.py …")
_missing = patch_file(
    REALTIME_PATH,
    [
        (REALTIME_INIT_MARKER, REALTIME_INIT_INJECTION),
        (REALTIME_CONN_MARKER, REALTIME_CONN_INJECTION),
        (REALTIME_SPEECH_STARTED_MARKER, REALTIME_SPEECH_STARTED_INJECTION),
    ],
    methods_append=REALTIME_METHODS,
)
# Validation critique : sans ces deux injections, le bridge n'existe pas
# et Reachy ignorerait silencieusement toutes les chutes détectées.
with open(REALTIME_PATH, encoding="utf-8") as _f:
    _patched = _f.read()
_critical_ok = "_start_reachy_care_server" in _patched and "_reachy_response_free" in _patched
if not _critical_ok:
    print("\n  ERREUR CRITIQUE : bridge IPC ou Event asyncio absent de openai_realtime.py")
    print("  Reachy démarrerait sans détection de chute — déploiement annulé.")
    sys.exit(1)

print("\n[1c/6] Patch openai_realtime.py — interrupt_response=False (mode lecture) …")
with open(REALTIME_PATH, "r") as _f:
    _vad_content = _f.read()
if "reachy-care-vad-fix" not in _vad_content:
    if VAD_FIX_MARKER not in _vad_content:
        print(f"  WARN: marqueur VAD '{VAD_FIX_MARKER}' introuvable dans {REALTIME_PATH}")
        print("  Le patch VAD est peut-être déjà appliqué ou le format a changé.")
    else:
        _vad_content = _vad_content.replace(VAD_FIX_MARKER, VAD_FIX_INJECTION, 1)
        with open(REALTIME_PATH, "w") as _f:
            _f.write(_vad_content)
        print("  VAD fix appliqué (interrupt_response=False)")
else:
    print("  VAD fix déjà appliqué — skip")

# ---------------------------------------------------------------------------
# Patch openai_realtime.py — désactiver send_idle_signal (meubler le silence)
# Le code vanilla Pollen appelle send_idle_signal toutes les 15s dans emit(),
# qui injecte un faux message user "[Idle time update: ...] You've been idle
# for a while. Feel free to get creative - dance, show an emotion, look around,
# do nothing, or just be yourself!" et force tool_choice="required".
# Résultat : le LLM appelle un tool toutes les ~15s (dance/play_emotion/
# do_nothing/groove/camera selon la liste disponible) → le robot bouge seul.
#
# Ce comportement est en contradiction directe avec la persona Douze qui dit
# "Si la conversation est silencieuse, reste silencieux. Tu n'inities JAMAIS
# de parole spontanée". On désactive donc l'idle signal entièrement en
# remplaçant le `if idle_duration > 15.0 and ...is_idle()` par `if False`.
# ---------------------------------------------------------------------------

NO_IDLE_MARKER = "        if idle_duration > 15.0 and self.deps.movement_manager.is_idle():"
NO_IDLE_REPLACEMENT = "        if False:  # [reachy-care-no-idle-signal] idle signal désactivé — Douze reste silencieux"

print("\n[1d/6] Patch openai_realtime.py — désactiver send_idle_signal …")
with open(REALTIME_PATH, "r") as _f:
    _idle_content = _f.read()
if "reachy-care-no-idle-signal" in _idle_content:
    print("  idle signal déjà désactivé — skip")
elif NO_IDLE_MARKER in _idle_content:
    _idle_content = _idle_content.replace(NO_IDLE_MARKER, NO_IDLE_REPLACEMENT, 1)
    with open(REALTIME_PATH, "w") as _f:
        _f.write(_idle_content)
    print("  idle signal désactivé (plus de tool_choice='required' toutes les 15s)")
else:
    print(f"  WARN: marqueur idle '{NO_IDLE_MARKER[:60]}...' introuvable dans {REALTIME_PATH}")
    print("  Vérifier si le format emit() a changé dans la version Pollen courante.")

print("\n[2/6] Patch moves.py — backoff gRPC …")
patch_file(
    MOVES_PATH,
    [
        (MOVES_MARKER, MOVES_INJECTION),
        (MOVES_ANTENNA_MARKER, MOVES_ANTENNA_INJECTION),
        (MOVES_BREATHING_ANTENNA_MARKER, MOVES_BREATHING_ANTENNA_INJECTION),
    ],
)
print("  (marqueur introuvable = version moves.py différente — non bloquant)")

# ---------------------------------------------------------------------------
# Patch wespeakerruntime/speaker.py — torchaudio → soundfile + scipy + kaldi_native_fbank
# ---------------------------------------------------------------------------

import glob as _glob

_ws_matches = _glob.glob("/venvs/apps_venv/lib/python*/site-packages/wespeakerruntime/speaker.py")
WESPEAKER_PATH = _ws_matches[0] if _ws_matches else None

WESPEAKER_IMPORT_MARKER = "import torchaudio\nimport onnxruntime as ort\nimport torchaudio.compliance.kaldi as kaldi"
WESPEAKER_IMPORT_INJECTION = """import soundfile as sf
import scipy.signal
import kaldi_native_fbank as knf
import numpy as _np_kf
import onnxruntime as ort"""

WESPEAKER_FBANK_MARKER = """\
        waveform, sample_rate = torchaudio.load(wav_path)
        if sample_rate != resample_rate:
            waveform = torchaudio.transforms.Resample(
                orig_freq=sample_rate, new_freq=resample_rate)(waveform)
        waveform = waveform * (1 << 15)
        mat = kaldi.fbank(waveform,
                          num_mel_bins=num_mel_bins,
                          frame_length=frame_length,
                          frame_shift=frame_shift,
                          dither=dither,
                          sample_frequency=sample_rate,
                          window_type='hamming',
                          use_energy=False)
        mat = mat.numpy()
        if cmn:
            # CMN, without CVN
            mat = mat - np.mean(mat, axis=0)
        return mat"""

WESPEAKER_FBANK_INJECTION = """\
        waveform, sample_rate = sf.read(wav_path, dtype='float32', always_2d=False)
        if sample_rate != resample_rate:
            waveform = scipy.signal.resample_poly(
                waveform, resample_rate, sample_rate
            ).astype(_np_kf.float32)
        waveform = waveform * (1 << 15)
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = float(resample_rate)
        opts.frame_opts.frame_length_ms = float(frame_length)
        opts.frame_opts.frame_shift_ms = float(frame_shift)
        opts.frame_opts.dither = float(dither)
        opts.frame_opts.window_type = 'hamming'
        opts.mel_opts.num_bins = num_mel_bins
        opts.use_energy = False
        fbank_computer = knf.OnlineFbank(opts)
        fbank_computer.accept_waveform(float(resample_rate), waveform.tolist())
        fbank_computer.input_finished()
        frames = [fbank_computer.get_frame(i) for i in range(fbank_computer.num_frames_ready)]
        mat = _np_kf.array(frames, dtype=_np_kf.float32)
        if cmn:
            mat = mat - mat.mean(axis=0)
        return mat"""


def _patch_wespeaker(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        txt = f.read()

    if "soundfile" in txt:
        print("  Déjà patché : wespeakerruntime/speaker.py")
        return

    # Backup
    with open(path + ".bak", "w", encoding="utf-8") as f:
        f.write(txt)

    original = txt
    txt = txt.replace(WESPEAKER_IMPORT_MARKER, WESPEAKER_IMPORT_INJECTION, 1)
    if WESPEAKER_IMPORT_MARKER in original:
        print("  Patch appliqué : imports torchaudio → soundfile+kaldi")
    else:
        print("  ⚠ Marqueur imports introuvable (déjà absent ?)")

    txt = txt.replace(WESPEAKER_FBANK_MARKER, WESPEAKER_FBANK_INJECTION, 1)
    if WESPEAKER_FBANK_MARKER in original:
        print("  Patch appliqué : _compute_fbank → soundfile+kaldi_native_fbank")
    else:
        print("  ⚠ Marqueur _compute_fbank introuvable (déjà absent ?)")

    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"  Fichier mis à jour : {path}")


print("\n[3/6] Patch wespeakerruntime/speaker.py — torchaudio → soundfile+kaldi …")
if WESPEAKER_PATH is None:
    print("  ERREUR : wespeakerruntime introuvable — speaker_id ne fonctionnera pas.")
    sys.exit(1)
else:
    _patch_wespeaker(WESPEAKER_PATH)

# ---------------------------------------------------------------------------
# Patch camera_worker.py — head pitch override (index 4 dans face_tracking_offsets)
# ---------------------------------------------------------------------------

_CW_GLOB_PATTERN = "/home/pollen/reachy_mini_conversation_app/**/camera_worker.py"

# Marqueur __init__ : la ligne créant le lock de face tracking
_CW_INIT_MARKER = "self.face_tracking_lock"
# Ligne de return dans get_face_tracking_offsets : on cherche un return avec offsets[4]
# On ne peut pas lire le fichier directement depuis Mac, donc on utilise une approche
# "append method" pour set_head_pitch_override et un patch regex pour le return.
# L'injection __init__ se fait sur la première occurrence de face_tracking_lock.
_CW_INIT_INJECTION_SUFFIX = "\n        self._head_pitch_override: float = 0.0\n        self._head_tracking_enabled: bool = True"

# Marqueur du return dans get_face_tracking_offsets : la méthode retourne un tuple
# contenant offsets[4]. On vise la ligne la plus probable d'après l'architecture.
_CW_RETURN_MARKER = "return (offsets[0], offsets[1], offsets[2], offsets[3], offsets[4], offsets[5])"
_CW_RETURN_INJECTION = None  # Construit dynamiquement dans _patch_camera_worker (indentation-aware)

# Méthode à ajouter à la classe (avant la dernière ligne du fichier)
_CW_METHOD = '''
    def set_head_pitch_override(self, pitch_rad: float) -> None:
        """[Reachy Care] Maintient un offset de pitch persistant malgré l\'interpolation."""
        self._head_pitch_override = pitch_rad
        logger.info(
            '[Reachy Care] Head pitch override set to %.3f rad (%.1f°)',
            pitch_rad, pitch_rad * 57.3,
        )

    def set_head_tracking_enabled(self, enabled: bool) -> None:
        """[Reachy Care] Active/désactive le face tracking (ex: mode échecs)."""
        self._head_tracking_enabled = enabled
        logger.info('[Reachy Care] Head tracking %s', 'enabled' if enabled else 'disabled')
'''


def _patch_camera_worker(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        txt = f.read()

    if "_head_pitch_override" in txt:
        print("  Déjà patché : camera_worker.py")
        return

    # Backup
    with open(path + ".bak", "w", encoding="utf-8") as f:
        f.write(txt)

    original = txt

    # 1) Injection _head_pitch_override dans __init__
    if _CW_INIT_MARKER in txt:
        # Trouve la première occurrence et insère après la fin de cette ligne
        idx = txt.find(_CW_INIT_MARKER)
        end_of_line = txt.find("\n", idx)
        txt = txt[:end_of_line] + _CW_INIT_INJECTION_SUFFIX + txt[end_of_line:]
        print("  Patch appliqué : _head_pitch_override dans __init__")
    else:
        print("  ⚠ Marqueur '__init__ face_tracking_lock' introuvable — __init__ non patché")

    # 2) Patch du return dans get_face_tracking_offsets (indentation-aware)
    if _CW_RETURN_MARKER in txt:
        # Détecte l'indentation de la ligne contenant le return
        _ret_idx = txt.find(_CW_RETURN_MARKER)
        _line_start = txt.rfind("\n", 0, _ret_idx) + 1
        _indent = txt[_line_start:_ret_idx]  # espaces avant "return"
        _i4 = _indent + "    "  # indentation +4
        _replacement = (
            f"if self._head_tracking_enabled:\n"
            f"{_i4}return (offsets[0], offsets[1], offsets[2], offsets[3], offsets[4] + self._head_pitch_override, offsets[5])\n"
            f"{_indent}else:\n"
            f"{_i4}return (0, 0, 0, 0, self._head_pitch_override, 0)"
        )
        txt = txt.replace(_CW_RETURN_MARKER, _replacement, 1)
        print("  Patch appliqué : return → if/else _head_tracking_enabled + _head_pitch_override")
    else:
        print("  ⚠ Marqueur return offsets[4] introuvable — version différente de camera_worker.py")
        print("     (le return exact peut différer — à vérifier sur le Pi)")

    # 3) Ajout de la méthode set_head_pitch_override en fin de classe
    txt = txt.rstrip() + "\n" + _CW_METHOD + "\n"
    print("  Méthode set_head_pitch_override ajoutée")

    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"  Fichier mis à jour : {path}")


# ---------------------------------------------------------------------------
# Patch openai_realtime.py — /set_head_pitch utilise set_head_pitch_override
# Ce patch cible le fichier APRÈS injection de REALTIME_METHODS (patch_file ci-dessus)
# La ligne ft[4] = pitch_rad est dans le code injecté par ce script lui-même.
# ---------------------------------------------------------------------------


def _patch_openai_realtime_pitch(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        txt = f.read()

    if "set_head_pitch_override" in txt:
        print("  Déjà patché : openai_realtime.py /set_head_pitch → set_head_pitch_override")
        return

    # Backup (suffixe distinct pour ne pas écraser le .bak du patch principal)
    with open(path + ".bak2", "w", encoding="utf-8") as f:
        f.write(txt)

    _PITCH_MARKER = "                                ft[4] = pitch_rad"
    _PITCH_INJECTION = """\
                                if hasattr(cw, 'set_head_pitch_override'):
                                    cw.set_head_pitch_override(pitch_rad)
                                else:
                                    ft[4] = pitch_rad"""

    if _PITCH_MARKER in txt:
        txt = txt.replace(_PITCH_MARKER, _PITCH_INJECTION, 1)
        print("  Patch appliqué : ft[4] = pitch_rad → set_head_pitch_override")
    else:
        print("  ⚠ Marqueur 'ft[4] = pitch_rad' introuvable dans openai_realtime.py")
        print("     (le patch bridge principal doit être appliqué en premier)")
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"  Fichier mis à jour : {path}")


print("\n[4/6] Patch camera_worker.py — head pitch override …")
_cw_matches = _glob.glob(_CW_GLOB_PATTERN, recursive=True)
if not _cw_matches:
    print("  camera_worker.py introuvable (conv_app non installée ?) — patch ignoré.")
else:
    _patch_camera_worker(_cw_matches[0])

print("\n[5/6] Patch openai_realtime.py — set_head_pitch_override …")
_patch_openai_realtime_pitch(REALTIME_PATH)

print("\n[5b/6] Patch conv_app main.py — media_backend=gstreamer (audio BT sans WebRTC) …")
import re as _re
try:
    _main_content = open(MAIN_PATH).read()
    if "reachy-care-gstreamer-full" in _main_content:
        print("  Déjà patché : conv_app main.py media_backend=gstreamer")
    elif "reachy-care-gstreamer-novideo" in _main_content:
        # Migration no_video → gstreamer complet (caméra réactivée pour dashboard)
        _patched = _main_content.replace(
            '"media_backend": "gstreamer"}  # [reachy-care-gstreamer-novideo]',
            '"media_backend": "gstreamer"}  # [reachy-care-gstreamer-full]'
        )
        open(MAIN_PATH, "w").write(_patched)
        print("  ✅ Patch mis à jour : gstreamer-novideo → gstreamer-full (caméra activée)")
    elif "reachy-care-no-webrtc" in _main_content:
        # Ancien patch no_media → remplacer par gstreamer
        _patched = _main_content.replace(
            '"media_backend": "no_media"}  # [reachy-care-no-webrtc]',
            '"media_backend": "gstreamer"}  # [reachy-care-gstreamer-novideo]'
        )
        open(MAIN_PATH, "w").write(_patched)
        print("  ✅ Patch mis à jour : no_media → gstreamer")
    else:
        # Premier patch
        _patched, _n = _re.subn(
            r'([ \t]*)robot_kwargs\s*=\s*\{\}',
            r'\1robot_kwargs = {"media_backend": "gstreamer"}  # [reachy-care-gstreamer-novideo]',
            _main_content
        )
        if _n > 0:
            open(MAIN_PATH, "w").write(_patched)
            print(f"  ✅ Patch appliqué : media_backend=gstreamer ({_n} occurrence(s))")
        else:
            print("  ⚠ 'robot_kwargs = {}' introuvable dans conv_app main.py — vérifier le fichier")
except FileNotFoundError:
    print(f"  ⚠ Fichier introuvable : {MAIN_PATH}")

print("\n[5c/6] Patch media_manager.py — suppression spam 'Audio system not initialized' …")
_MEDIA_MGR_PATH = "/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/media/media_manager.py"
try:
    _mm_content = open(_MEDIA_MGR_PATH).read()
    if "reachy-care-no-audio-spam" in _mm_content:
        print("  Déjà patché : media_manager.py")
    else:
        import re as _re2
        _mm_patched, _mm_n = _re2.subn(
            r'([ \t]*if self\.audio is None:\n)[ \t]*self\.logger\.warning\("Audio system is not initialized\."\)\n([ \t]*return)',
            r'\1# [reachy-care-no-audio-spam]\n\2',
            _mm_content
        )
        if _mm_n > 0:
            open(_MEDIA_MGR_PATH, "w").write(_mm_patched)
            print(f"  ✅ Patch appliqué : {_mm_n} warning(s) de spam supprimés")
        else:
            print("  ⚠ Patterns introuvables dans media_manager.py")
except FileNotFoundError:
    print(f"  ⚠ Fichier introuvable : {_MEDIA_MGR_PATH}")

print("\n[6/6] Patch audio_utils.py — .asoundrc minimal (fix PortAudio crash) …")
_AUDIO_UTILS_PATH = "/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/media/audio_utils.py"
_AUDIO_UTILS_OLD = '''    asoundrc_content = f"""
pcm.!default {{
    type hw
    card {card_id}
}}

ctl.!default {{
    type hw
    card {card_id}
}}

pcm.reachymini_audio_sink {{'''
_AUDIO_UTILS_NEW = '''    asoundrc_content = f"""pcm.!default {{
    type hw
    card {card_id}
}}

ctl.!default {{
    type hw
    card {card_id}
}}
"""
    # [reachy-care-asoundrc-fix] placeholder — do not remove
    if False: asoundrc_content += f"""pcm.reachymini_audio_sink {{'''

try:
    _au_content = open(_AUDIO_UTILS_PATH).read()
    if "reachy-care-asoundrc-fix" in _au_content:
        print("  Déjà patché : audio_utils.py")
    elif _AUDIO_UTILS_OLD in _au_content:
        open(_AUDIO_UTILS_PATH, "w").write(_au_content.replace(_AUDIO_UTILS_OLD, _AUDIO_UTILS_NEW))
        print("  ✅ Patch appliqué : audio_utils.py — .asoundrc sans dmix/dsnoop")
    else:
        print("  ⚠ Marqueur introuvable dans audio_utils.py — patch manuel requis")
except FileNotFoundError:
    print(f"  ⚠ Fichier introuvable : {_AUDIO_UTILS_PATH}")

print("\n[7/6] Réinstallation en mode editable (pip install -e) …")
result = subprocess.run(
    [sys.executable, "-m", "pip", "install", "-e", "/home/pollen/reachy_mini_conversation_app/"],
    capture_output=True, text=True,
)
if result.returncode == 0:
    print("  ✅ Paquet réinstallé en mode editable — le patch est actif.")
else:
    print("  ⚠ Échec réinstallation editable :")
    print(result.stderr[-500:])

print("\n✅ Patch terminé. (7 patches)")
