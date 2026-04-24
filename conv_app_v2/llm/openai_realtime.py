import asyncio
import base64
import json
import logging
import time

# Fix #1  — monkey-patch websockets pour éviter keepalive
# timeout 1011 pendant les bursts OpenAI en mode histoire. Default ping_timeout=20s
# insuffisant si l'event loop est momentanément occupé par un burst audio.
# Voir rapport researcher  : python-websockets doc keepalive.
# Doit s'exécuter AVANT l'import d'openai (qui importe websockets internally).
import websockets.asyncio.client as _ws_client_mod
_original_ws_connect = _ws_client_mod.connect
def _ws_connect_with_keepalive(*args, **kwargs):
    kwargs.setdefault("ping_interval", 10)
    kwargs.setdefault("ping_timeout", 60)
    return _original_ws_connect(*args, **kwargs)
_ws_client_mod.connect = _ws_connect_with_keepalive

import numpy as np
from scipy.signal import resample as scipy_resample
from openai import AsyncOpenAI
from openai.types.realtime import (
    AudioTranscriptionParam,
    RealtimeAudioConfigParam,
    RealtimeAudioConfigInputParam,
    RealtimeAudioConfigOutputParam,
    RealtimeAudioInputTurnDetectionParam,
    RealtimeResponseCreateParamsParam,
    RealtimeSessionCreateRequestParam,
)
from openai.types.realtime.realtime_audio_formats_param import AudioPCM

from .base import LLMAdapter

logger = logging.getLogger(__name__)

# 200ms of silence at 16kHz mono PCM16 = 16000 * 0.2 * 2 bytes = 6400 bytes
_SILENCE_200MS = b"\x00" * 6400
_KEEPALIVE_INTERVAL = 60       # seconds between keepalive pings
_MAX_SESSION_SECONDS = 55 * 60  # proactive reconnect after 55 minutes


class OpenAIRealtimeAdapter(LLMAdapter):
    def __init__(self, api_key: str | None = None, model: str = "gpt-realtime"):
        self._api_key = api_key
        self._model = model
        self._client: AsyncOpenAI | None = None
        self._conn = None
        self._connected = False
        self._session_start: float = 0.0
        self._event_loop_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._response_sender_task: asyncio.Task | None = None
        self._auto_reconnect_task: asyncio.Task | None = None
        # Response serialisation (avoids "conversation_already_has_active_response")
        self._pending_responses: asyncio.Queue[dict] = asyncio.Queue()
        self._response_done_event: asyncio.Event = asyncio.Event()
        # Saved for reconnect
        self._system_prompt: str = ""
        self._tools: list[dict] = []
        self._voice: str = "cedar"
        # Echo detection: compare transcriptions
        self._last_reachy_said: str = ""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    _last_instructions: str | None = None  # Fix : dernier prompt switch_mode

    async def connect(self, system_prompt: str, tools: list[dict], voice: str = "cedar") -> None:
        self._system_prompt = system_prompt
        self._tools = tools
        self._voice = voice
        self._client = AsyncOpenAI(api_key=self._api_key) if self._api_key else AsyncOpenAI()
        # H2: namespace canonique post-GA est client.realtime (sans .beta.)
        # Ref: Pollen vanilla openai_realtime.py:463
        self._conn = await self._client.realtime.connect(model=self._model).__aenter__()

        # H3: format type-safe Pollen avec "type": "realtime" + nesting audio.input/output.*
        # Ref: Pollen vanilla openai_realtime.py:465-481
        # Divergences assumées conservées :
        #   - server_vad + interrupt_response:True (switch  — Pollen pattern, VAD local abandonné)
        #   - language="fr" (démo EHPAD, Pollen utilise "en")
        session_config = RealtimeSessionCreateRequestParam(
            type="realtime",
            instructions=system_prompt,
            audio=RealtimeAudioConfigParam(
                input=RealtimeAudioConfigInputParam(
                    # Fix — OpenAI Realtime impose rate >= 24000 depuis .
                    # Le mic XMOS capture en 16 kHz natif, donc send_audio upsample
                    # 16→24 kHz par interpolation linéaire avant envoi (re-port du
                    # resample Pollen openai_realtime.py:707-708 perdu lors du portage v2).
                    format=AudioPCM(type="audio/pcm", rate=24000),
                    transcription=AudioTranscriptionParam(model="gpt-4o-transcribe", language="fr"),
                    # Pollen pattern (pollen_src/openai_realtime.py:456) : dict direct,
                    # car RealtimeAudioInputTurnDetectionParam est un typing.Union (non
                    # instantiable). Le SDK OpenAI 2.14.0 accepte le dict natif.
                    turn_detection={  # type: ignore[typeddict-item]
                        # semantic_vad eagerness=low : classifieur
                        # sémantique côté OpenAI qui attend jusqu'à 8s sur une
                        # tournure incomplète (pause réflexion, cherche ses mots).
                        # Meilleur pour vieille personne / EHPAD que server_vad
                        # qui coupait à 1s fixe. Pas de threshold ni silence_dur,
                        # OpenAI tranche sur le sens de la phrase. Voir PI_KB §3
                        # et docs OpenAI Realtime VAD guide.
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": False,
                    },
                    noise_reduction={"type": "far_field"},  # type: ignore[typeddict-item]
                ),
                output=RealtimeAudioConfigOutputParam(
                    format=AudioPCM(type="audio/pcm", rate=24000),
                    voice=voice,
                ),
            ),
            tools=tools,  # type: ignore[typeddict-item]
            tool_choice="auto",
        )
        await self._conn.session.update(session=session_config)
        logger.info("Session config: VAD threshold=0.75, silence=1500ms, noise_reduction=far_field")

        self._connected = True
        self._session_start = time.monotonic()

        # Reset response serialisation state for fresh connection
        self._pending_responses = asyncio.Queue()
        self._response_done_event = asyncio.Event()
        self._response_done_event.set()  # No active response yet — ready to send

        self._event_loop_task = asyncio.create_task(self._event_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._response_sender_task = asyncio.create_task(self._response_sender_loop())

        logger.info("OpenAIRealtimeAdapter connected (model=%s, voice=%s)", self._model, voice)

    async def send_audio(self, pcm_chunk: bytes) -> None:
        if not self._connected or self._conn is None:
            return
        # XMOS mic capture = 16 kHz. OpenAI Realtime minimum rate = 24 kHz.
        # Upsample 16 → 24 kHz via scipy.signal.resample (FFT band-limited,
        # zéro aliasing). Port exact du resample Pollen vanilla
        # openai_realtime.py:707-708, perdu lors du portage v2.
        # numpy.interp (linéaire) introduisait des artefacts que server_vad
        # OpenAI prenait pour de la parole parasite → interrupt_response
        # intempestif → session sourde après le premier tour.
        samples_16k = np.frombuffer(pcm_chunk, dtype=np.int16)
        n_out = (len(samples_16k) * 3) // 2
        samples_24k = scipy_resample(samples_16k, n_out).astype(np.int16)
        audio_b64 = base64.b64encode(samples_24k.tobytes()).decode("utf-8")
        await self._conn.input_audio_buffer.append(audio=audio_b64)

    async def send_text_event(self, text: str, instructions: str = "") -> None:
        if not self._connected or self._conn is None:
            return
        await self._conn.conversation.item.create(item={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        })
        await self._safe_response_create()

    async def update_instructions(self, new_instructions: str) -> None:
        if not self._connected or self._conn is None:
            return
        # Fix : stocker le dernier prompt envoyé pour le restaurer
        # après auto-reconnect WS (1011 timeout). Sans ça, la nouvelle session
        # retombe sur _system_prompt (mode normal initial) et Reachy perd son
        # mode actif (histoire, échecs, musique, pro) au milieu d'une lecture.
        self._last_instructions = new_instructions
        # H3 (bis): même format type-safe pour update_instructions
        # Ref: Pollen vanilla openai_realtime.py:465-481 (même pattern)
        session_config = RealtimeSessionCreateRequestParam(
            type="realtime",
            instructions=new_instructions,
            audio=RealtimeAudioConfigParam(
                input=RealtimeAudioConfigInputParam(
                    # Voir connect() — rate 24k obligatoire, send_audio upsample 16→24
                    format=AudioPCM(type="audio/pcm", rate=24000),
                    transcription=AudioTranscriptionParam(model="gpt-4o-transcribe", language="fr"),
                    # Pollen pattern (pollen_src/openai_realtime.py:456) : dict direct,
                    # car RealtimeAudioInputTurnDetectionParam est un typing.Union (non
                    # instantiable). Le SDK OpenAI 2.14.0 accepte le dict natif.
                    turn_detection={  # type: ignore[typeddict-item]
                        # semantic_vad eagerness=low : classifieur
                        # sémantique côté OpenAI qui attend jusqu'à 8s sur une
                        # tournure incomplète (pause réflexion, cherche ses mots).
                        # Meilleur pour vieille personne / EHPAD que server_vad
                        # qui coupait à 1s fixe. Pas de threshold ni silence_dur,
                        # OpenAI tranche sur le sens de la phrase. Voir PI_KB §3
                        # et docs OpenAI Realtime VAD guide.
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": False,
                    },
                    noise_reduction={"type": "far_field"},  # type: ignore[typeddict-item]
                ),
                output=RealtimeAudioConfigOutputParam(
                    format=AudioPCM(type="audio/pcm", rate=24000),
                    voice=self._voice,
                ),
            ),
        )
        await self._conn.session.update(session=session_config)

    async def schedule_session_update(self, new_instructions: str) -> None:
        """Alias used by tools (switch_mode, etc.) to update the LLM prompt."""
        await self.update_instructions(new_instructions)

    async def cancel_response(self) -> None:
        if not self._connected or self._conn is None:
            return
        try:
            await self._conn.response.cancel()
        except Exception as exc:
            logger.debug("cancel_response ignored: %s", exc)

    async def send_tool_result(self, call_id: str, result: dict) -> None:
        if not self._connected or self._conn is None:
            return
        await self._conn.conversation.item.create(item={
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        })
        # Port Pollen: si le tool retourne b64_im, envoyer l'image à OpenAI
        # pour que le LLM puisse la décrire (tool camera)
        if "b64_im" in result:
            b64_im = result["b64_im"]
            await self._conn.conversation.item.create(item={
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{b64_im}",
                }],
            })
            logger.info("Camera image added to conversation (%d KB)",
                         len(b64_im) // 1024)
        # INCO-3: ajouter instructions voice pour eviter silence post-tool call
        # Ref: Pollen vanilla openai_realtime.py:449-454
        await self._safe_response_create(
            response=RealtimeResponseCreateParamsParam(
                instructions="Use the tool result just returned and answer concisely in speech.",
            ),
        )

    async def disconnect(self) -> None:
        self._connected = False
        for task in (
            self._event_loop_task,
            self._keepalive_task,
            self._response_sender_task,
            self._auto_reconnect_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._conn is not None:
            try:
                await self._conn.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("disconnect close error: %s", exc)
            self._conn = None
        logger.info("OpenAIRealtimeAdapter disconnected")

    async def reconnect(self) -> None:
        """Proactive reconnect: close current session and open a new one."""
        logger.info("Reconnecting to OpenAI Realtime...")
        await self.disconnect()
        await asyncio.sleep(1)
        await self.connect(self._system_prompt, self._tools, self._voice)
        logger.info("Reconnected successfully.")

    # ------------------------------------------------------------------
    # Response serialisation (Pollen pattern)
    # ------------------------------------------------------------------

    async def _safe_response_create(self, **kwargs) -> None:
        """Queue a response.create() instead of calling it directly.
        The _response_sender_loop worker ensures only one active response at a time."""
        await self._pending_responses.put(kwargs)

    async def _response_sender_loop(self) -> None:
        """Worker that serialises response.create() calls.
        Waits for _response_done_event between each send to avoid
        'conversation_already_has_active_response' errors.
        Ref: Pollen vanilla openai_realtime.py:306-361 (retry logic)."""
        try:
            while self._connected:
                kwargs = await self._pending_responses.get()
                if not self._connected or self._conn is None:
                    break

                # I1: retry jusqu'a 5x sur conversation_already_has_active_response
                # Ref: Pollen vanilla openai_realtime.py:337-361
                max_retries = 5
                attempts = 0
                sent = False
                while not sent and self._connected:
                    # Wait until the previous response is done
                    try:
                        await asyncio.wait_for(self._response_done_event.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.debug("Timed out waiting for previous response; forcing ahead")
                        self._response_done_event.set()

                    if not self._connected or self._conn is None:
                        break

                    try:
                        await self._conn.response.create(**kwargs)
                        sent = True
                    except Exception as exc:
                        exc_str = str(exc).lower()
                        if "conversation_already_has_active_response" in exc_str and attempts < max_retries:
                            attempts += 1
                            logger.debug("response.create rejected (active response), retry %d/%d", attempts, max_retries)
                            # Attendre que response.done arrive (via response.created handler)
                            self._response_done_event.clear()
                            try:
                                await asyncio.wait_for(self._response_done_event.wait(), timeout=10.0)
                            except asyncio.TimeoutError:
                                logger.debug("Timeout waiting for active response to finish; forcing retry")
                                self._response_done_event.set()
                        else:
                            logger.warning("response.create error: %s", exc)
                            self._response_done_event.set()  # Unblock for next attempt
                            break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    async def _event_loop(self) -> None:
        try:
            async for event in self._conn:
                if not self._connected:
                    break
                await self._dispatch(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Event loop error: %s", exc)
            # Auto-reconnect on unexpected WS disconnect (1011 keepalive timeout, etc.)
            # EHPAD deployment: no human operator to restart — must heal itself.
            if self._connected:
                try:
                    loop = asyncio.get_event_loop()
                    self._auto_reconnect_task = loop.create_task(self._auto_reconnect())
                except Exception as e:
                    logger.error("Failed to schedule auto-reconnect: %s", e)

    async def _auto_reconnect(self) -> None:
        """Heal the WebSocket after a transport-level disconnect (1011 timeout, etc.).

        Called by _event_loop when async for raises. Does NOT go through disconnect()
        because that would cancel the calling event_loop task. Instead we manually
        tear down the dead connection, let the other loops exit on _connected=False,
        then call connect() which re-instantiates _event_loop_task, _keepalive_task,
        _response_sender_task.
        """
        logger.warning("WS disconnect — auto-reconnect sequence engaged")
        self._connected = False
        if self._conn is not None:
            try:
                await self._conn.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("auto-reconnect: old conn close error (ignored): %s", exc)
            self._conn = None
        # Give keepalive + response_sender loops a moment to exit their while loops.
        await asyncio.sleep(0.5)

        max_attempts = 5
        backoff = 2.0
        for attempt in range(1, max_attempts + 1):
            try:
                # Fix : restaurer le dernier prompt switch_mode si
                # présent (sinon prompt initial mode normal). Évite que Reachy
                # perde son mode histoire/échecs/musique/pro après un reconnect
                # 1011 au milieu d'une lecture.
                prompt_to_use = self._last_instructions or self._system_prompt
                await self.connect(prompt_to_use, self._tools, self._voice)
                mode_hint = "restored mode prompt" if self._last_instructions else "initial prompt"
                logger.info("Auto-reconnect SUCCESS on attempt %d/%d (%s)", attempt, max_attempts, mode_hint)
                # Fix : après WS disconnect+reconnect, la response en
                # cours est perdue côté OpenAI (session neuve). Les flags côté
                # engine (_speaking, _speaking_started_at, _cancel_received) sont
                # stuck car response.done n'est jamais arrivée → prochain chunk
                # joue dans un état pipeline incohérent. Callback pour reset.
                on_reconnect = getattr(self, "on_reconnect", None)
                if on_reconnect:
                    try:
                        await on_reconnect()
                    except Exception as exc:
                        logger.warning("on_reconnect callback failed: %s", exc)
                return
            except asyncio.CancelledError:
                return  # disconnect() called — honour shutdown
            except Exception as exc:
                logger.warning(
                    "Auto-reconnect attempt %d/%d FAILED: %s", attempt, max_attempts, exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        logger.error(
            "Auto-reconnect FAILED after %d attempts — conv_app_v2 session dead. "
            "Manual restart required.", max_attempts,
        )

    async def _dispatch(self, event) -> None:
        etype = event.type

        # Diagnostic proactivité  : log TOUS events pour trouver le proactif
        # (log discret — exclut les deltas audio fréquents)
        if etype not in ("response.output_audio.delta", "response.audio.delta",
                         "response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            logger.info("OAI_EVT %s", etype)
        # Log spécial session events (pour voir ce qu'OpenAI applique vraiment)
        if "session" in etype:
            try:
                td = event.session.audio.input.turn_detection
                logger.info("OPENAI_SESSION %s: turn_detection=%r", etype, td)
            except Exception as exc:
                logger.info("OPENAI_SESSION %s (parse fail: %s) full=%r", etype, exc, event)

        if etype == "input_audio_buffer.speech_started":
            logger.info("VAD_EVENT speech_started at %.3f", time.monotonic())
            if self.on_speech_started:
                await self.on_speech_started()

        elif etype == "input_audio_buffer.speech_stopped":
            logger.info("VAD_EVENT speech_stopped at %.3f", time.monotonic())
            if self.on_speech_stopped:
                await self.on_speech_stopped()

        elif etype == "response.output_audio.delta":
            # H1: nom d'event correct dans l'API OpenAI Realtime actuelle
            # Ref: Pollen vanilla openai_realtime.py:601
            # AVANT: "response.audio.delta" → aucun audio TTS reçu (Reachy muet silencieusement)
            if self.on_audio_delta:
                # Pass both decoded PCM (for GStreamer) and raw b64 (for wobbler)
                raw_pcm = base64.b64decode(event.delta)
                await self.on_audio_delta(raw_pcm, event.delta)

        elif etype == "response.function_call_arguments.done":
            if self.on_tool_call:
                try:
                    args = json.loads(event.arguments)
                except json.JSONDecodeError:
                    args = {}
                await self.on_tool_call(event.call_id, event.name, args)

        elif etype == "response.created":
            # I2: handler response.created -> clear event pour eviter race condition
            # Ref: Pollen vanilla openai_realtime.py:535-537
            self._response_done_event.clear()
            logger.debug("Response created (active)")

        elif etype == "response.done":
            self._response_done_event.set()
            if self.on_response_done:
                await self.on_response_done()

        elif etype == "conversation.item.input_audio_transcription.completed":
            user_text = getattr(event, "transcript", "").strip()
            logger.info("User said: %s", user_text)
            # Echo detection: if user transcript matches what Reachy just said, cancel response
            # DÉSACTIVÉ  — test A : vérifier si les coupures de lecture mode histoire
            # (ECHO DETECTED observé 10× dans la journée, 2× en rafale post-SIGUSR1 à 21::37)
            # viennent de cet algo trop permissif (word overlap 40% sur _last_reachy_said ~900 chars
            # fait matcher n'importe quelle phrase user courte). Log conservé pour observer sans agir.
            if self._last_reachy_said and user_text and self._is_echo(user_text, self._last_reachy_said):
                logger.info("ECHO WOULD HAVE CANCELLED (disabled) — user='%s' ~ reachy='%s'",
                            user_text[:50], self._last_reachy_said[:50])
                # await self.cancel_response()   # ← commenté pour test A

        elif etype == "response.output_audio_transcript.done":
            self._last_reachy_said = getattr(event, "transcript", "").strip()
            logger.info("Reachy said: %s", self._last_reachy_said)

        elif etype == "error":
            logger.warning("Realtime API error event: %s", event)

    @staticmethod
    def _is_echo(user_text: str, reachy_text: str) -> bool:
        """Detect if user transcript is an echo of Reachy's last output.
        Uses word overlap ratio — if >40% of user words appear in Reachy text, it's echo."""
        user_words = set(user_text.lower().split())
        reachy_words = set(reachy_text.lower().split())
        if not user_words:
            return False
        overlap = len(user_words & reachy_words)
        ratio = overlap / len(user_words)
        return ratio > 0.4

    async def _keepalive_loop(self) -> None:
        silence_b64 = base64.b64encode(_SILENCE_200MS).decode("utf-8")
        try:
            while self._connected:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                if not self._connected or self._conn is None:
                    break
                # Proactive reconnect if session is nearly 55 minutes old
                elapsed = time.monotonic() - self._session_start
                if elapsed > _MAX_SESSION_SECONDS:
                    logger.info("Session approaching 60min limit — reconnecting")
                    try:
                        await self.reconnect()
                    except Exception as exc:
                        logger.error("Reconnect failed: %s — session will die at 60min", exc)
                    return  # reconnect() starts new keepalive task
                try:
                    await self._conn.input_audio_buffer.append(audio=silence_b64)
                except Exception as exc:
                    logger.debug("Keepalive send error: %s", exc)
        except asyncio.CancelledError:
            pass
