"""
tts.py — Wrapper TTS léger pour espeak-ng avec fallback pyttsx3.

Priorité des backends :
  1. espeak-ng  (subprocess, non-bloquant par défaut)
  2. pyttsx3    (si espeak-ng absent)
  3. print()    (mode dégradé silencieux, aucune exception levée)

Usage :
    tts = TTSEngine(voice="fr", speed=140)
    tts.say("Bonjour !")
    tts.say("Attention !", blocking=True)
    tts.stop()
"""

import logging
import os
import subprocess
import shutil
import struct
import tempfile
import wave

logger = logging.getLogger(__name__)


class TTSEngine:
    """Moteur TTS abstrait avec trois niveaux de dégradation."""

    MAX_TEXT_LENGTH = 200

    def __init__(self, voice: str = "fr", speed: int = 140, amplitude: int = 200, backend: str = "espeak"):
        """
        Paramètres
        ----------
        voice   : code langue/voix espeak-ng (ex. "fr", "en", "fr+f3")
        speed   : vitesse en mots/min (espeak-ng -s)
        backend : ignoré — le backend est résolu automatiquement selon disponibilité
        """
        self.voice = voice
        self.speed = speed
        self.amplitude = amplitude
        self._process: subprocess.Popen | None = None
        self._backend = backend if backend != "espeak" else self._resolve_backend()
        self._has_paplay = shutil.which("paplay") is not None
        self._has_aplay = shutil.which("aplay") is not None
        self._dmix_device = self._detect_dmix()

    @staticmethod
    def _detect_dmix() -> str | None:
        """Détecte le device dmix 'reachymini_audio_sink' dans ~/.asoundrc."""
        for path in [os.path.expanduser("~/.asoundrc"), "/etc/asound.conf"]:
            try:
                with open(path) as f:
                    if "reachymini_audio_sink" in f.read():
                        return "reachymini_audio_sink"
            except FileNotFoundError:
                continue
        return None

    @staticmethod
    def _resample_wav(src: str, dst: str, target_rate: int = 16000, target_channels: int = 2) -> None:
        """Resample un WAV espeak (mono 22050Hz) vers stereo 16kHz pour dmix."""
        with wave.open(src, "rb") as r:
            data = r.readframes(r.getnframes())
            rate = r.getframerate()
            ch = r.getnchannels()
            sw = r.getsampwidth()
        # Mono → stereo
        if ch < target_channels:
            samples = struct.unpack(f"<{len(data) // sw}h", data)
            data = struct.pack(f"<{len(samples) * 2}h", *[s for s in samples for _ in range(2)])
        # Resample naïf (nearest neighbor) — suffisant pour TTS
        if rate != target_rate:
            with wave.open(src, "rb") as r:
                n = r.getnframes()
            ratio = target_rate / rate
            samples_stereo = struct.unpack(f"<{len(data) // sw}h", data)
            new_len = int(n * ratio)
            resampled = []
            for i in range(new_len):
                src_idx = min(int(i / ratio) * target_channels, len(samples_stereo) - target_channels)
                for c in range(target_channels):
                    resampled.append(samples_stereo[src_idx + c])
            data = struct.pack(f"<{len(resampled)}h", *resampled)
        with wave.open(dst, "wb") as w:
            w.setnchannels(target_channels)
            w.setsampwidth(sw)
            w.setframerate(target_rate)
            w.writeframes(data)

    def _resolve_backend(self) -> str:
        if shutil.which("espeak-ng"):
            return "espeak"
        try:
            import pyttsx3  # noqa: F401
            return "pyttsx3"
        except ImportError:
            pass
        return "print"

    def say(self, text: str, blocking: bool = False) -> None:
        """
        Synthétise `text` (tronqué à MAX_TEXT_LENGTH caractères).

        Paramètres
        ----------
        text     : texte à prononcer
        blocking : si True, attend la fin de la synthèse avant de rendre la main
        """
        if not text:
            return

        text = text[:self.MAX_TEXT_LENGTH]

        if self._backend == "espeak":
            self._say_espeak(text, blocking)
        elif self._backend == "pyttsx3":
            self._say_pyttsx3(text)
        else:
            print(f"[TTS] {text}")

    def stop(self) -> None:
        """Interrompt la synthèse espeak-ng en cours (sans effet si bloquant)."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def is_speaking(self) -> bool:
        """Retourne True si une synthèse non-bloquante est en cours."""
        return self._process is not None and self._process.poll() is None

    def _say_espeak(self, text: str, blocking: bool) -> None:
        self.stop()
        if self._dmix_device and self._has_aplay:
            # dmix : espeak écrit WAV → resample → aplay -D dmix (partage device avec conv_app)
            self._say_espeak_dmix(text, blocking)
        elif self._has_paplay:
            # PulseAudio : espeak écrit WAV → paplay le joue
            self._say_espeak_pulse(text, blocking)
        else:
            # Fallback ALSA direct (peut échouer si device occupé)
            cmd = ["espeak-ng", "-v", self.voice, "-s", str(self.speed), "-a", str(self.amplitude), text]
            if blocking:
                subprocess.run(cmd, check=False)
            else:
                self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _say_espeak_dmix(self, text: str, blocking: bool) -> None:
        """espeak-ng → WAV temp → resample 16kHz stereo → aplay -D dmix."""
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
        os.close(wav_fd)
        wav_16k = wav_path.replace(".wav", "_16k.wav")
        try:
            espeak_cmd = ["espeak-ng", "-w", wav_path, "-v", self.voice,
                          "-s", str(self.speed), "-a", str(self.amplitude), text]
            subprocess.run(espeak_cmd, check=False, timeout=10)
            self._resample_wav(wav_path, wav_16k)
            aplay_cmd = ["aplay", "-D", self._dmix_device, wav_16k]
            if blocking:
                subprocess.run(aplay_cmd, check=False, timeout=30)
                os.unlink(wav_path)
                os.unlink(wav_16k)
                return
            else:
                self._process = subprocess.Popen(
                    aplay_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                import threading
                def _cleanup(proc, *paths):
                    proc.wait()
                    for p in paths:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                threading.Thread(target=_cleanup, args=(self._process, wav_path, wav_16k), daemon=True).start()
                return
        except Exception as exc:
            logger.warning("TTS dmix failed: %s — fallback ALSA direct", exc)
        for p in (wav_path, wav_16k):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _say_espeak_pulse(self, text: str, blocking: bool) -> None:
        """espeak-ng → WAV temp → paplay (PulseAudio)."""
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
        os.close(wav_fd)
        try:
            # Générer WAV sans toucher au device audio
            espeak_cmd = ["espeak-ng", "-w", wav_path, "-v", self.voice,
                          "-s", str(self.speed), "-a", str(self.amplitude), text]
            subprocess.run(espeak_cmd, check=False, timeout=10)
            if blocking:
                subprocess.run(["paplay", wav_path], check=False, timeout=30)
                os.unlink(wav_path)
            else:
                self._process = subprocess.Popen(
                    ["paplay", wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Nettoyage asynchrone : un thread supprime le fichier quand paplay termine
                import threading
                def _cleanup(proc, path):
                    proc.wait()
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                threading.Thread(target=_cleanup, args=(self._process, wav_path), daemon=True).start()
        except Exception as exc:
            logger.warning("TTS PulseAudio failed: %s — fallback ALSA", exc)
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            cmd = ["espeak-ng", "-v", self.voice, "-s", str(self.speed), "-a", str(self.amplitude), text]
            if blocking:
                subprocess.run(cmd, check=False)
            else:
                self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _say_pyttsx3(self, text: str) -> None:
        """pyttsx3 est toujours bloquant dans ce wrapper."""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", self.speed)
            for voice in engine.getProperty("voices"):
                voice_id = (voice.id or "").lower()
                voice_name = (voice.name or "").lower()
                if self.voice in voice_id or self.voice in voice_name:
                    engine.setProperty("voice", voice.id)
                    break
            engine.say(text)
            engine.runAndWait()
        except Exception:
            print(f"[TTS] {text}")
