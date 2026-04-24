import os
from pathlib import Path

# Chemins — racine du projet (par défaut, le dossier qui contient ce config.py).
# REACHY_CARE_DIR peut surcharger pour pointer ailleurs en test.
BASE_DIR          = Path(os.environ.get("REACHY_CARE_DIR", Path(__file__).resolve().parent))
MODELS_DIR        = BASE_DIR / "models"
KNOWN_FACES_DIR   = BASE_DIR / "known_faces"
LOGS_DIR          = BASE_DIR / "logs"
LOG_FILE          = LOGS_DIR / "reachy_care.log"
PID_FILE          = Path("/tmp/reachy_care.pid")

# Activités (modules pluggables)
ACTIVITIES_DIR        = BASE_DIR / "activities"

# Module 1A — Face Recognition
FACE_MODEL_NAME         = "buffalo_s"
FACE_DET_SIZE           = (320, 320)
FACE_COSINE_THRESHOLD   = 0.50    # seuil ID directe (architecture N-best avec écart d'ambiguïté ci-dessous)
FACE_CANDIDATE_MIN      = 0.35    # seuil minimum N-best candidats (en dessous = ignoré)
FACE_AMBIGUITY_ECART    = 0.15    # écart min entre 1er et 2e candidat pour ID sans ambiguïté
FACE_AMBIGUITY_ASK_COOLDOWN = 30.0  # secondes entre deux questions d'ambiguïté
FACE_DET_SCORE_MIN      = 0.65
FACE_INTERVAL_SEC       = 5.0
FACE_FRAME_SKIP         = 10      # InsightFace : 1 frame traitée toutes les N (réduit CPU)
FACE_MAX_PERSONS        = 5       # max personnes enrôlées
FACE_ENROLL_PHOTOS      = 10      # photos par enrôlement
FACE_MISS_RESET_COUNT   = 150     # misses consécutives avant de réinitialiser _last_greeted (150×2s=5min)
VOICE_ONLY_THRESHOLD    = 0.55    # score min pour reconnaissance vocale seule
# AttenLabs — seuils de classification attention (bbox heuristique)
# heading = écart horizontal du centre de la bbox vs centre du frame (0=face au robot, 0.5=bord)
# size_ratio = largeur bbox / largeur frame (0=loin, 1=très proche)
# TO_COMPUTER si heading < HEADING_MAX ET size > SIZE_MIN
ATTENTION_HEADING_MAX   = 0.40    # écart horizontal max pour état TO_COMPUTER (hystérésis gérée en aval)
ATTENTION_SIZE_MIN      = 0.12    # 12% — visage assez proche pour être en conversation
SPEAKER_CACHE_TTL        = 8.0    # secondes — durée cache locuteur (skip WeSpeaker si identifié récemment)
VISITOR_ABSENT_COUNT    = 60      # misses sans visage avant retour mode normal (60×2s=2min)

# Module 1B — Chess (vocal uniquement — détection visuelle YOLO supprimée)
CHESS_STOCKFISH_PATHS   = ["/usr/games/stockfish", "/usr/local/bin/stockfish", "/usr/bin/stockfish"]
CHESS_THINK_TIME        = 2.0

# Module 1D — Fall Detection
FALL_MODEL_COMPLEXITY   = 0
FALL_DETECTION_CONF     = 0.50
FALL_RATIO_THRESHOLD    = 0.15
FALL_SUSTAINED_SEC      = 3.0
FALL_INTERVAL_SEC       = 2.0
FALL_GHOST_TRIGGER_SEC  = 20.0  # Algo B : secondes sans squelette avant alerte (20s = tolérant pour déplacements normaux dans la maison)
FALL_GHOST_RESET_SEC    = 45.0  # Algo B : secondes sans squelette → personne sortie (reset)

# Module 1E — Sound Detection (YAMNet TFLite)
SOUND_DETECTION_ENABLED  = True
SOUND_MODEL_PATH         = MODELS_DIR / "yamnet.tflite"
SOUND_IMPACT_THRESHOLD   = 0.65    # score min pour déclencher une suspicion d'impact

# Silero VAD (filtrage parole pour speaker ID)
VAD_MODEL_PATH           = MODELS_DIR / "silero_vad.onnx"
VAD_SPEECH_THRESHOLD     = 0.3    # seuil probabilité parole Silero

# Boucle principale
FRAME_INTERVAL_SEC      = 0.2     # 5 Hz

# TTS
TTS_VOICE               = "fr"
TTS_SPEED               = 140
TTS_AMPLITUDE           = 200     # Volume espeak-ng : 0-200 (défaut système : 100)
TTS_BACKEND             = "none"    # "espeak" désactivé — seul le LLM (cedar) parle via GStreamer

# Reachy SDK
REACHY_DAEMON_URL       = "http://localhost:8000"
REACHY_DAEMON_TIMEOUT   = 5

# Localisation
LOCATION                = ""
TIMEZONE                = "Europe/Paris"    # fuseau horaire (IANA, ex: "America/New_York", "Asia/Tokyo")

# Personne principale + connaissances : lus depuis known_faces/registry.json via
# _load_registry_roles() plus bas (OWNER_NAME, SECONDARY_PERSONS). Édités par le
# dashboard via /api/persons/<name>/primary et endpoints d'enrôlement.
# Les interpolations {PRIMARY_PERSON} et {KNOWN_PEOPLE} dans les prompts LLM
# utilisent ces valeurs au runtime (voir conversation_engine.py + mode_manager.py).

# Alertes Telegram (recommandé — plus simple que l'email)
TELEGRAM_BOT_TOKEN      = ""   # configurer dans config_local.py sur le Pi
TELEGRAM_CHAT_ID        = ""   # configurer dans config_local.py sur le Pi
TELEGRAM_ENABLED        = False

# Alertes email (alternatif)
ALERT_EMAIL_TO          = ""             # adresse de destination des alertes email
ALERT_EMAIL_FROM        = ""         # ex: "reachy.alerts@gmail.com"
ALERT_EMAIL_PASSWORD    = ""         # mot de passe d'application Gmail
ALERT_EMAIL_ENABLED     = False      # passer à True une fois les identifiants configurés

# Comportements
HEAD_IDLE_PITCH_DEG     = 10   # positif = tête vers le bas (pour regarder l'utilisateur assis)
HEAD_CHESS_PITCH_DEG    = 15   # tête baissé e vers la table pour voir l'échiquier
SLEEP_HEAD_PITCH_DEG    = 30    # tête baissé e en mode veille (dans l'habitacle, sans couper le daemon)
ANTENNA_HAPPY           = [0.5, 0.5]
ANTENNA_ALERT           = [-1.0, -1.0]

# Modes
CHESS_SKILL_LEVEL_INIT         = 3     # niveau Stockfish initial (0=débutant, 20=expert)

# Wake Word
WAKE_WORD_ENABLED       = True
WAKE_WORD_MODEL_PATH    = MODELS_DIR / "hey_Reatchy.onnx"  # modèle openwakeword custom
WAKE_WORD_TFLITE_PATH   = None
WAKE_WORD_FALLBACK      = "hey_jarvis"  # modèle openwakeword intégré utilisé si MODEL_PATH absent
WAKE_WORD_THRESHOLD     = 0.35     # seuil de détection wake word (balance vrais / faux positifs)
WAKE_WORD_DEVICE_INDEX  = None   # None = device par défaut (PulseAudio)

# Reconnaissance musicale (audd.io — 500 identifications/mois gratuites)
# S'inscrire sur https://audd.io pour obtenir une clé gratuite
AUDD_API_TOKEN          = ""

# Dashboard web
DASHBOARD_ENABLED       = True      # dashboard web accessible sur http://<ip-pi>:8080
DASHBOARD_PORT          = 8080
DASHBOARD_MJPEG_FPS     = 5
DASHBOARD_MJPEG_QUALITY = 60

# Controller web (service systemd independant)
CONTROLLER_PORT         = 8090

# Journal quotidien
JOURNAL_EMAIL_HOUR      = 20        # heure d'envoi du récapitulatif (0-23, heure locale)
JOURNAL_EMAIL_TO        = ""        # si vide → utilise ALERT_EMAIL_TO
JOURNAL_TELEGRAM_ENABLED = True     # envoyer le récap aussi par Telegram

# Groq API (Whisper STT)
GROQ_API_KEY            = ""       # configurer dans config_local.py sur le Pi

HF_TOKEN                = ""

# Rôles persons — lus depuis known_faces/registry.json au démarrage
# OWNER_NAME : seul monitoré par AttenLabs (gate attention active)
# SECONDARY_PERSONS : reconnus et salués, mais pas monitorés (gate forcée TO_COMPUTER)
# ROBOT_VOICE_NAME : voix propre du robot (exclue de la reconnaissance speaker)
def _load_registry_roles() -> tuple[str, list[str], str]:
    import json as _j
    try:
        reg = _j.loads((KNOWN_FACES_DIR / "registry.json").read_text())
        return reg.get("owner", ""), list(reg.get("secondary", [])), reg.get("robot_voice", "cedar")
    except Exception:
        return "", [], "cedar"

OWNER_NAME, SECONDARY_PERSONS, ROBOT_VOICE_NAME = _load_registry_roles()

# Overrides locaux (gitignored) — pour les credentials
# Créer config_local.py à la racine du projet avec les valeurs à surcharger
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
