"""
Bridge entre Reachy Care et reachy_mini_conversation_app.
Tous les modules Reachy Care utilisent ce singleton pour interagir avec l'IA.

Communication via HTTP IPC : main.py POST les événements au serveur HTTP
que le conv_app démarre sur localhost:8766 au moment de la connexion.
"""
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_CONV_PID_FILE = Path("/tmp/conv_app_v2.pid")
_WAKE_SHM = Path("/dev/shm/reachy_wake.json")

logger = logging.getLogger(__name__)

_RC_PREFIX = "[Reachy Care]"
_IPC_BASE  = "http://127.0.0.1:8766"
# Endpoints qui passent même quand le bridge est muté (gèrent eux-mêmes l'état mute/sleep)
_MUTE_BYPASS_PATHS = frozenset({"/wake", "/unmute", "/set_head_pitch", "/sleep", "/disable_vad", "/enable_vad", "/bt_mode_on", "/bt_mode_off"})


class ConvAppBridge:
    """Singleton qui envoie les événements Reachy Care au conv_app via HTTP IPC."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._muted = False  # True = Reachy en mode silencieux, bridge ne poste rien
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._last_context_person = None  # anti-contamination : dernier interlocuteur

    # ------------------------------------------------------------------
    # Compatibilité ascendante — no-op (l'IPC HTTP ne nécessite plus d'enregistrement)
    # ------------------------------------------------------------------

    def register_handler(self, handler) -> None:
        """No-op conservé pour compatibilité. L'IPC HTTP remplace l'accès direct."""
        logger.debug("ConvAppBridge.register_handler() ignoré — IPC HTTP actif.")

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def set_context(
        self,
        person=None,
        mood=None,
        memory_summary=None,
        profile=None,
    ) -> bool:
        try:
            import config as _cfg
            tz = ZoneInfo(getattr(_cfg, "TIMEZONE", "Europe/Paris"))
        except Exception:
            from datetime import timezone, timedelta
            tz = timezone(timedelta(hours=1))  # fallback CET si tzdata absent
        now_str = datetime.now(tz).strftime("%A %d %B %Y à %Hh%M")

        if person:
            memory_ctx = f" Contexte mémorisé : {memory_summary}." if memory_summary else ""

            profile_ctx = ""
            if profile:
                parts = []
                if profile.get("medications"):
                    parts.append("Médicaments : " + ", ".join(profile["medications"]))
                if profile.get("schedules"):
                    parts.append("Horaires : " + ", ".join(profile["schedules"]))
                if profile.get("emergency_contact"):
                    parts.append("Contact urgence : " + profile["emergency_contact"])
                if profile.get("notes"):
                    parts.append("Notes : " + profile["notes"])
                if parts:
                    profile_ctx = " Profil : " + " | ".join(parts) + "."

            # Anti-contamination : si l'interlocuteur change, prévenir le LLM
            prev = getattr(self, "_last_context_person", None)
            change_notice = ""
            if prev and prev != person:
                change_notice = (
                    f" ATTENTION : l'interlocuteur a changé. Tu parlais à {prev}, maintenant tu parles à {person}. "
                    f"Les souvenirs, goûts et faits de {prev} appartiennent UNIQUEMENT à {prev}. "
                    f"Ne les attribue JAMAIS à {person}."
                )
            self._last_context_person = person

            text = (
                f"{_RC_PREFIX} La personne devant toi s'appelle {person}. "
                f"Nous sommes le {now_str}.{change_notice}{memory_ctx}{profile_ctx} "
                f"Salue-la chaleureusement par son prénom."
            )
            instructions = (
                f"Tu parles maintenant à {person}. "
                "Salue cette personne par son prénom, naturellement. "
                "Une phrase suffit. Reste dans ton personnage."
            )
        else:
            text = (
                f"{_RC_PREFIX} Une personne est devant toi, mais je ne la reconnais pas. "
                f"Nous sommes le {now_str}."
            )
            instructions = (
                "Accueille cette personne simplement, en une phrase. "
                "Ne demande pas son prénom spontanément."
            )
        return self._post("/event", {"text": text, "instructions": instructions})

    def trigger_check_in(self, person: str | None = None) -> None:
        """Demande au LLM de vérifier si la personne va bien (suspicion de chute).

        Le LLM pose une question douce, attend la réponse, puis appelle le tool
        report_wellbeing pour signaler l'issue à main.py.
        """
        who = person.capitalize() if person else "la personne"
        text = (
            f"{_RC_PREFIX} Suspicion de chute : {who} est immobile depuis plusieurs secondes. "
            "Vérifie doucement si elle va bien."
        )
        instructions = (
            "IMPORTANT : interromps immédiatement ce que tu faisais (lecture ou autre). "
            "Pose une question douce et directe pour vérifier que la personne va bien, "
            "comme si tu avais entendu quelque chose d'inhabituel. "
            "Exemple : 'Je m'arrête un instant — tout va bien ?' "
            "Attends sa réponse. "
            "— Si OK : appelle report_wellbeing(status='ok') et reprends ce que tu faisais. "
            "— Si problème : appelle report_wellbeing(status='problem'). "
            "— Si pas de réponse après 20s : appelle report_wellbeing(status='no_response'). "
            "Ne dramatise pas, reste calme."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def trigger_alert(self, alert_type: str, details: str = "") -> None:
        location_part = f" (lieu : {details})" if details else ""
        text = (
            f"{_RC_PREFIX} URGENT — Alerte de type '{alert_type}' détectée{location_part}. "
            "Une personne pourrait avoir besoin d'aide."
        )
        instructions = (
            "Réponds immédiatement de façon très rassurante, en maximum 2 phrases courtes. "
            "Vérifie si la personne va bien. Ne dramatise pas mais montre que tu es là. "
            "Si elle ne répond pas, propose d'appeler un proche."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def set_attention(self, state: str) -> bool:
        """Pousse l'état d'attention vers conv_app (/attention endpoint).

        state : "SILENT" | "TO_HUMAN" | "TO_COMPUTER"
        """
        return self._post("/attention", {"state": state})

    def announce_chess_move(
        self,
        move: str,
        player: str = "",
        score_cp: int | None = None,
        best_reply: str | None = None,
        mate_in: int | None = None,
        move_number: int = 0,
        commentary: str = "",
    ) -> None:
        """Appelé par le module chess pour faire commenter un coup humain observé."""
        parts = []
        if player:
            parts.append(f"Les {player} ont joué {move}")
        else:
            parts.append(f"Le coup '{move}' vient d'être joué")
        if move_number:
            parts[0] += f" (coup n°{move_number})"

        eval_part = ""
        if mate_in is not None:
            plural = "s" if abs(mate_in) > 1 else ""
            eval_part = f" Mat en {abs(mate_in)} coup{plural}."
        elif score_cp is not None:
            if abs(score_cp) < 50:
                eval_part = " Position équilibrée."
            elif score_cp >= 150:
                eval_part = " Avantage clair des Blancs."
            elif score_cp > 0:
                eval_part = f" Légère avance des Blancs ({score_cp} centipions)."
            elif score_cp <= -150:
                eval_part = " Avantage clair des Noirs."
            else:
                eval_part = f" Légère avance des Noirs ({abs(score_cp)} centipions)."

        reply_part = f" Meilleure réponse suggérée : {best_reply}." if best_reply else ""
        if commentary:
            reply_part += f" {commentary}"

        text = f"{_RC_PREFIX} {'. '.join(parts)}.{eval_part}{reply_part}"
        instructions = (
            "Commente ce coup d'échecs en 1 à 2 phrases, avec enthousiasme et bienveillance. "
            "Adopte le ton d'un coach sympa pour personnes âgées. "
            "N'utilise pas le jargon technique — traduis les notations en mots simples. "
            "Encourage le joueur."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def announce_chess_game_start(self, reachy_color: str, skill_label: str) -> None:
        """Annonce le début d'une partie — Reachy explique les règles du jeu."""
        text = (
            f"{_RC_PREFIX} Nouvelle partie d'échecs. "
            f"Je joue les {reachy_color}. Niveau : {skill_label}. "
            "Le joueur humain joue les Blancs et commence."
        )
        instructions = (
            "Annonce joyeusement le début de la partie. "
            "Dis que tu joues les Noirs, que c'est au joueur de commencer, "
            "et que tu vas adapter ton niveau. "
            "Sois enthousiaste, 2 phrases max."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def ask_move_confirmation(self, move_san: str) -> None:
        """Mode jeu strict : demande confirmation du coup interprété par le LLM."""
        text = f"{_RC_PREFIX} Coup interprété : {move_san}. Attente confirmation joueur."
        instructions = (
            f"Dis UNIQUEMENT '{move_san} ?' sur un ton interrogatif. "
            "RIEN d'autre. Pas de commentaire, pas d'encouragement. Juste le coup suivi d'un point d'interrogation."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def announce_human_chess_move(self, move_san: str, move_number: int) -> None:
        """Confirme le coup humain et annonce que Reachy réfléchit."""
        text = (
            f"{_RC_PREFIX} Le joueur humain vient de jouer {move_san} "
            f"(coup n°{move_number}). Je réfléchis à ma réponse."
        )
        instructions = (
            "Confirme le coup du joueur en le traduisant en langage naturel (évite la notation algébrique brute). "
            "Dis que tu réfléchis. 1-2 phrases, ton de joueur concentré."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def announce_reachy_move(
        self,
        move_san: str,
        from_sq: str,
        to_sq: str,
        score_cp: int | None = None,
        mate_in: int | None = None,
        move_number: int = 0,
        fen: str = "",
    ) -> None:
        """Reachy annonce son propre coup et demande au joueur de le placer."""
        eval_hint = ""
        if mate_in is not None and mate_in > 0:
            eval_hint = f" Je vois un mat en {mate_in}."
        elif score_cp is not None and score_cp > 150:
            eval_hint = " J'ai un bon avantage."
        elif score_cp is not None and score_cp < -150:
            eval_hint = " Tu as un bon avantage — je dois me défendre."

        fen_hint = f" | FEN : {fen}" if fen else ""
        text = (
            f"{_RC_PREFIX} Mon coup (Reachy, coup n°{move_number}) : {move_san}. "
            f"Déplacez ma pièce de {from_sq} vers {to_sq}.{eval_hint}{fen_hint}"
        )
        instructions = (
            f"Annonce TON coup '{move_san}' en 4 mots maximum. Format : 'Mon coup : cavalier f6.' "
            f"Puis demande de déplacer ta pièce de {from_sq} vers {to_sq}. "
            "INTERDIT : commentaire, évaluation, encouragement. Juste le coup + la demande de placement. "
            "Exemple : 'Cavalier f6. Déplace-le de g8 en f6.' — C'est TOUT."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def confirm_move_executed(self) -> None:
        """Confirme que le joueur a bien placé la pièce de Reachy."""
        text = f"{_RC_PREFIX} Le joueur a bien placé ma pièce. À ton tour !"
        instructions = (
            "Remercie le joueur d'avoir placé ta pièce et encourage-le pour son prochain coup. "
            "1 phrase courte et enthousiaste."
        )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def announce_chess_game_over(self, winner: str, reason: str, new_skill_label: str) -> None:
        """Annonce la fin de partie et l'ajustement de niveau."""
        level_msg = f" J'ajuste mon niveau : {new_skill_label}." if new_skill_label else ""
        text = (
            f"{_RC_PREFIX} Fin de partie ! Vainqueur : {winner} ({reason}).{level_msg}"
        )
        if winner == "Reachy":
            instructions = (
                "Annonce ta victoire avec joie mais sans te vanter. "
                "Félicite le joueur pour la partie. "
                f"Dis que tu vas jouer un peu plus fort la prochaine fois ({new_skill_label}). "
                "Propose une revanche. 2-3 phrases."
            )
        elif winner == "le joueur":
            instructions = (
                "Félicite chaleureusement le joueur pour sa victoire ! "
                "Sois bon perdant et enthousiaste. "
                f"Dis que tu vas jouer un peu moins fort la prochaine fois ({new_skill_label}). "
                "Propose une revanche. 2-3 phrases."
            )
        else:
            instructions = (
                "Annonce la nulle avec bonne humeur. "
                "C'est une belle partie équilibrée. Propose une revanche. 1-2 phrases."
            )
        self._post("/event", {"text": text, "instructions": instructions}, urgent=True)

    def enroll_complete(self, name: str, success: bool) -> None:
        if success:
            text = f"{_RC_PREFIX} Enrôlement réussi pour '{name}'. J'ai bien mémorisé ce visage."
            instructions = (
                f"Dis à {name} que tu l'as bien mémorisé(e) et que tu le/la reconnaîtras "
                "désormais. Sois chaleureux(se), en une phrase."
            )
        else:
            text = f"{_RC_PREFIX} L'enrôlement de '{name}' a échoué. Je n'ai pas pu mémoriser ce visage."
            instructions = (
                f"Explique gentiment à {name} que tu n'as pas pu mémoriser son visage "
                "et propose de réessayer. Reste encourageant(e), en une ou deux phrases."
            )
        self._post("/event", {"text": text, "instructions": instructions})

    def update_session_instructions(self, instructions: str) -> None:
        self._post("/session_update", {"instructions": instructions})

    def announce_mode_switch(self, announce_text: str) -> None:
        instructions = "Confirme oralement le changement de mode en une phrase enthousiaste et courte."
        self._post("/event", {"text": announce_text, "instructions": instructions})

    def person_departed(self, name: str) -> None:
        """[reachy-care-no-proactive ] Ne parle plus spontanément.

        Le LLM n'est plus notifié du départ — le re-set_context à la prochaine
        reconnaissance faciale suffira à mettre à jour l'interlocuteur.
        """
        logger.debug("person_departed(%s) : event muté (no-proactive)", name)

    def set_visitor_mode(self, active: bool) -> None:
        """[reachy-care-no-proactive ] Ne parle plus spontanément.

        Le mode silence social est désormais le comportement par défaut de Reachy,
        donc plus besoin de l'annoncer oralement à l'entrée/sortie d'un visiteur.
        """
        logger.debug("set_visitor_mode(%s) : event muté (no-proactive)", active)

    def inject_memory(self) -> None:
        """Relit la mémoire de session et la ré-injecte dans le contexte LLM.

        À appeler périodiquement pour que le LLM ne perde pas le fil
        quand la fenêtre de contexte OpenAI Realtime se remplit.
        """
        memory_file = Path("/tmp/reachy_session_memory.json")
        try:
            data = json.loads(memory_file.read_text(encoding="utf-8"))
        except Exception:
            return  # Pas de mémoire — rien à injecter

        # Filtrer les clés internes
        items = {k: v for k, v in data.items() if not k.startswith("_")}
        if not items:
            return

        lines = "\n".join(f"- {k} : {v}" for k, v in items.items())
        text = f"{_RC_PREFIX} Rappel de contexte (mémoire de session) :\n{lines}"
        instructions = (
            "Prends note de ce rappel de contexte et poursuis naturellement "
            "ce que tu faisais, sans le mentionner explicitement."
        )
        self._post("/event", {"text": text, "instructions": instructions})
        logger.debug("ConvAppBridge.inject_memory() : %d clés injectées", len(items))

    def keepalive(self) -> None:
        import random
        styles = [
            "une citation littéraire ou philosophique qui t'a touché, en une phrase, suivie d'un bref commentaire maladroit de ta part",
            "une blague courte — de préférence une que tu trouves trop drôle pour toi-même",
            "une réflexion absurde sur ta condition de cheval-scribe sans corps ni sabots",
            "une anecdote historique surprenante en deux phrases maximum",
            "une observation poétique sur le silence ou l'attente, dans ton style maladroit et attachant",
        ]
        style = random.choice(styles)
        text = f"{_RC_PREFIX} Silence prolongé — reprendre contact de façon légère."
        instructions = (
            f"Tu n'as pas parlé depuis un moment. Brise le silence avec {style}. "
            "Ne demande pas si la personne va bien — ce n'est pas le moment. "
            "Sois naturel, bref, dans ton personnage de Douze. Une à deux phrases max."
        )
        self._post("/event", {"text": text, "instructions": instructions})

    def set_head_pitch(self, pitch_deg: float) -> None:
        """Force la tête à une position pitch permanente dans le système de mouvements conv_app."""
        self._post("/set_head_pitch", {"pitch_deg": pitch_deg})

    def turn_body(self, angle_rad: float, duration: float = 1.0) -> None:
        """Tourne le corps vers un yaw cible (radians). Appelé sur wake word
        pour pointer Reachy vers la source sonore détectée par le DOA XMOS."""
        self._post("/turn_body", {"angle_rad": angle_rad, "duration": duration})

    def set_doa_gate(self, in_cone: bool, angle_deg: float = 0.0, energy: float = 0.0) -> None:
        """Informe conv_app_v2 de l'état de la gate DOA (cône frontal)."""
        self._post("/doa_gate", {"in_cone": in_cone, "angle_deg": angle_deg, "energy": energy})

    def set_motors(self, enabled: bool = True) -> None:
        """Active/désactive les moteurs via IPC → conv_app_v2 → daemon REST."""
        self._post("/set_motors", {"enabled": enabled})

    def get_frame(self) -> "np.ndarray | None":
        """Récupère une frame JPEG depuis la caméra de conv_app_v2 via IPC GET /get_frame."""
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(f"{_IPC_BASE}/get_frame", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            import numpy as np
            import cv2
            arr = np.frombuffer(data, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                # Log throttled : 1 warning toutes les 30s
                now = time.time()
                if now - getattr(self, "_last_getframe_warn", 0) > 30.0:
                    logger.warning("ConvAppBridge.get_frame: imdecode=None (data %d bytes)", len(data))
                    self._last_getframe_warn = now
            return frame
        except Exception as exc:
            now = time.time()
            if now - getattr(self, "_last_getframe_warn", 0) > 30.0:
                logger.warning("ConvAppBridge.get_frame failed: %s", exc)
                self._last_getframe_warn = now
            return None

    def set_user_speaking(self, speaking: bool) -> None:
        """Signal visuel : les lèvres de l'utilisateur bougent (croisé Silero VAD)."""
        self._post("/user_speaking", {"speaking": speaking})

    def send_event(self, text: str, instructions: str = "") -> None:
        """Envoie un événement générique au LLM."""
        self._post("/event", {"text": text, "instructions": instructions})

    def mute(self) -> None:
        """Active le mode silencieux — plus aucun événement envoyé au LLM jusqu'à unmute().

         : /disable_vad au lieu de /sleep. L'ancien /sleep déclenchait
        robot.sleep() côté conv comme side-effect, donc quand le LLM appelait le tool
        stop_speaking (qui envoie mute à main, qui fait bridge.mute()), Reachy se
        couchait physiquement. Le chemin propre est /disable_vad qui fait juste
        engine.mute() sans toucher au robot. sleep_mode explicite passe par
        bridge.sleep() qui garde /sleep.
        """
        self._muted = True
        self._post("/disable_vad", {}, urgent=True)  # mute audio input sans coucher le robot
        logger.info("[Reachy Care] Bridge muté — VAD inhibé (Reachy reste debout).")

    def unmute(self) -> None:
        """Désactive le mode silencieux — reprend l'envoi d'événements au LLM + restaure le VAD.

         : /enable_vad au lieu de /wake. /wake déclenchait toute une
        cascade (cancel + clear + reset_wobbler + wake motors + ack gesture + inject
        event) qui est trop cérémonieuse pour un simple retour de mute. /enable_vad
        fait juste engine.unmute() côté conv.
        """
        self._muted = False
        self._post("/enable_vad", {}, urgent=True)  # restaure VAD sans cérémonie wake
        logger.info("[Reachy Care] Bridge démuté — VAD restauré, reprise normale.")

    def cancel(self) -> bool:
        """Coupe la réponse en cours du LLM (response.cancel)."""
        return self._post("/cancel", {})

    def is_muted(self) -> bool:
        """Retourne True si le bridge est en mode silencieux."""
        return self._muted

    def wake(self) -> None:
        """Wake word détecté — réinitialise l'état idle sans faire parler Reachy."""
        self._post("/wake", {}, urgent=True)

    def wake_interrupt(
        self,
        doa_rad: float | None = None,
        event_text: str | None = None,
        event_instructions: str | None = None,
    ) -> bool:
        """Wake-word prioritaire via SIGUSR1 + payload /dev/shm (Fix).

        Bench Pi 4 : avg 1.8 ms sous loop asyncio saturé (vs IPC HTTP 2-10 s
        timeout quand conv saturé par audio_delta burst). Le signal POSIX
        préempte le epoll_wait du loop, handler exécuté dès le prochain return.

        Returns:
            True si SIGUSR1 envoyé (PID valide, process vivant).
            False si fallback HTTP /wake nécessaire (PID absent, process KO).
        """
        try:
            pid = int(_CONV_PID_FILE.read_text().strip())
            payload: dict = {"ts": time.time_ns()}
            if doa_rad is not None:
                payload["doa_rad"] = doa_rad
            if event_text:
                payload["event_text"] = event_text
            if event_instructions:
                payload["event_instructions"] = event_instructions
            # Write payload BEFORE signal: handler reads whatever is latest.
            _WAKE_SHM.write_text(json.dumps(payload))
            os.kill(pid, signal.SIGUSR1)
            logger.info("[Reachy Care] wake_interrupt SIGUSR1 → conv_app_v2 (pid=%d, doa=%s)",
                        pid, doa_rad)
            return True
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError) as exc:
            logger.warning("wake_interrupt fallback (PID/signal failed: %s)", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("wake_interrupt unexpected error (fallback HTTP): %s", exc)
            return False

    def sleep(self) -> None:
        """Passe la conv_app en mode privacy (VAD 0.99 — micro quasi coupé)."""
        self._post("/sleep", {}, urgent=True)

    def disable_realtime_vad(self) -> None:
        """Coupe l'écoute du LLM (VAD 0.99) pour le pipeline chess vocal dédié."""
        self._post("/disable_vad", {}, urgent=True)

    def enable_realtime_vad(self) -> None:
        """Restaure l'écoute du LLM (VAD normal) après le pipeline chess vocal."""
        self._post("/enable_vad", {}, urgent=True)

    # ------------------------------------------------------------------
    # Méthode interne HTTP
    # ------------------------------------------------------------------

    def _record_failure(self) -> None:
        """Incrémente le compteur d'échecs et ouvre le circuit breaker après 3 échecs consécutifs."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self._circuit_open_until = time.monotonic() + 5.0
            logger.warning(
                "ConvAppBridge : circuit breaker déclenché — pause 5s (%d failures consécutives)",
                self._consecutive_failures,
            )
            self._consecutive_failures = 0

    def _post(self, path: str, data: dict, urgent: bool = False) -> bool:
        # En mode silencieux, bloquer tous les événements SAUF le wake (/wake, /unmute)
        if self._muted and path not in _MUTE_BYPASS_PATHS and not urgent:
            logger.debug("ConvAppBridge IPC %s ignoré — bridge muté.", path)
            return False
        if time.monotonic() < self._circuit_open_until and not urgent:
            logger.debug("ConvAppBridge IPC %s : circuit ouvert, ignoré.", path)
            return False
        try:
            payload = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                f"{_IPC_BASE}{path}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Fix : urgent=True obtient 10 s timeout (wake/unmute/
            # sleep/set_head_pitch), sinon 2 s. Sous charge audio intense (bursts
            # response.audio.delta), conv IPC server asyncio répond en 2-5 s →
            # les wake word étaient perdus silencieusement.
            _timeout = 10 if urgent else 2
            with urllib.request.urlopen(req, timeout=_timeout) as resp:
                resp.read()
            self._consecutive_failures = 0
            logger.debug("ConvAppBridge IPC %s : OK", path)
            return True
        except urllib.error.URLError as exc:
            logger.warning(
                "ConvAppBridge IPC %s : conv_app non disponible (normal si pas encore connecté) : %s",
                path, exc,
            )
            self._record_failure()
            return False
        except Exception as exc:
            logger.error("ConvAppBridge IPC %s : %s", path, exc)
            self._record_failure()
            return False


bridge = ConvAppBridge()
