# FONCTIONNEMENT_REACHY_CARE.md — Rapport total mars-avril 2026

> **Document vivant, pérenne.** Rédigé le  après 2 mois de travail intense sur le robot.
> Objectif : toute personne (ou agent IA) reprenant le projet doit pouvoir comprendre le robot, son architecture, ses chaînes de données, les solutions validées et abandonnées, sans devoir lire 65+ documents d'archive.
> À tenir à jour à chaque inflexion architecturale majeure. Les mises à jour incrémentales session-par-session restent dans STATE.md et DECISIONS.md.

---

## TABLE DES MATIÈRES

1. [Le robot et sa mission](#1-le-robot-et-sa-mission)
2. [Architecture matérielle](#2-architecture-matérielle)
3. [Architecture logicielle — les 4 processus](#3-architecture-logicielle--les-4-processus)
4. [Chaîne audio entrante (micro → LLM)](#4-chaîne-audio-entrante-micro--llm)
5. [Chaîne audio sortante (LLM → haut-parleur)](#5-chaîne-audio-sortante-llm--haut-parleur)
6. [Chaîne vision (caméra → AttenLabs → gate)](#6-chaîne-vision-caméra--attenlabs--gate)
7. [Chaîne mode (histoire, sommeil, conv)](#7-chaîne-mode-histoire-sommeil-conv)
8. [LLM tools embarqués](#8-llm-tools-embarqués)
9. [Découvertes majeures des 2 mois](#9-découvertes-majeures-des-2-mois)
10. [Solutions validées (table par sous-système)](#10-solutions-validé es-table-par-sous-système)
11. [Solutions abandonnées et pourquoi](#11-solutions-abandonnées-et-pourquoi)
12. [P0 ouverts et dette technique connue](#12-p0-ouverts-et-dette-technique-connue)
13. [Le plan speaker USB-C en cours](#13-le-plan-speaker-usb-c-en-cours)
14. [Comment l'équipe travaille (workflow humain + agents)](#14-comment-léquipe-travaille-workflow-humain--agents)
15. [Roadmap stratégique — 3 horizons](#15-roadmap-stratégique--3-horizons)
16. [Glossaire](#16-glossaire)

---

## 1. Le robot et sa mission

**Reachy Care** est un assistant robotique conversationnel conçu pour des résidents d'EHPAD âgés de 65 à 95 ans. Il repose sur la plateforme hardware **Reachy Mini (Wireless)** de Pollen Robotics, augmentée par une stack logicielle custom développée par Eiffel AI (*ce projet*).

**Contraintes de déploiement absolues** :
- **24/7 sans opérateur humain** : tout plantage nécessitant une intervention est un bug critique.
- **Utilisateurs malentendants** : la sortie audio doit être forte et claire. Le speaker interne 5 W du Reachy Mini est inaudible en EHPAD. Une enceinte externe (BT ou USB) est obligatoire.
- **Pas de barge-in agressif** : les personnes âgées parlent lentement, avec des pauses. Le VAD doit tolérer des silences longs sans couper le tour.
- **Pas de proactivité intempestive** : le robot ne doit jamais parler seul, inventer des sujets, ou "relancer" quand l'utilisateur se tait.
- **Stabilité de plusieurs heures** : une session doit tenir une après-midi complète sans drift sémantique ni boucle.

**Le test étalon T14** (validé par Alexandre le ) est la référence de stabilité : robot silencieux quand personne ne parle, répond une seule fois quand on lui parle, reprend la conversation à la demande, pas de boucle. Citation : *"Je n'ai jamais eu cette stabilité."*

---

## 2. Architecture matérielle

### 2.1 Plateforme Reachy Mini (Wireless)

Source : `docs/source/platforms/reachy_mini/hardware.md` (SDK Pollen).

| Composant | Détail |
|---|---|
| Contrôleur | **Raspberry Pi 4 Compute Module CM4104016** (pas le Pi 4 classique) — Wifi, 4 GB RAM, 16 GB flash |
| Alimentation | Batterie LiFePO4 2000 mAh, 6,4 V, 12,8 Wh, protections intégrées |
| Moteurs | 1 Dynamixel XC330 (base) + 2 XL330 (antennes) + 6 XL330 (tête Stewart Platform) |
| Micro | Array 4 PDM MEMS digital, 16 kHz, basé **Seeed reSpeaker XMOS XVF3800** |
| Caméra | Raspberry Pi Camera v3 wide (Sony IMX708, 12 MP, 120°, autofocus) en CSI |
| Speaker interne | **5 W @ 4 Ω — inaudible EHPAD** |
| Port data exposé | **USB-C arrière, data** (*"one can plug a device such as a usb key"*) — pas de charge |
| Wifi | 2,4 / 5 GHz dual-band patch antenna, 2,79 dBi omnidirectionnel |

**Absence critique confirmée** : pas de jack 3.5 mm accessible. Le CM4 n'a pas le TRRS du Pi 4 model B, et Pollen n'a pas exposé de jack analogique sur le carrier board.

### 2.2 Chip audio XMOS XVF3800

Le cœur du traitement audio est le **XMOS XVF3800** (découvert ) qui fait :
- **AEC hardware** : annule l'echo du signal joué sur `hw:0,0` (son propre speaker). Dans le firmware, toujours actif, pas de contrôle ALSA.
- **Beamforming** : focalise le micro sur la source sonore.
- **DOA** (Direction of Arrival) : direction du son.
- **AGC** : contrôle automatique de gain.

**Limite fondamentale** : l'AEC XMOS n'annule que l'echo du signal envoyé sur `hw:0,0`. Si la sortie audio passe par Bluetooth (bluealsa), le XMOS n'a pas de signal de référence et **ne peut pas annuler l'echo BT**. C'est la cause racine du P0-13 (écho BT pollue la transcription).

Le SDK Pollen expose les paramètres XMOS via USB HID (`audio_control_utils.py`) : `AEC_AECPATHCHANGE`, `PP_ECHOONOFF`, `AEC_FIXEDBEAMSONOFF`, `DOA_VALUE_RADIANS`.

### 2.3 Enceintes externes utilisées / envisagées

| Enceinte | Statut | Note |
|---|---|---|
| Claire Beats (Beats Pill BT) MAC `2C:81:BF:11:FA:C7` | Actuelle | BT A2DP SBC 44 100 Hz. Pas d'AEC possible (XMOS hors fenêtre). USB-C du Pill = HID only propriétaire Apple, pas UAC. |
| Creative Pebble V3 USB-C | Envisagée | UAC2 standard Linux, chip AEC XMOS redevient utilisable car latence quasi nulle. |
| Jabra Speak 510 (USB-A + adaptateur) | Plan B pro | Speakerphone omnidirectionnel pour qualité voix EHPAD. |
| Dongle USB-C DAC + enceinte jack | **Plan retenu** | UGREEN/Apple/Pixel (~10-15 €) + enceinte mini-jack existante. Moins cher, même bénéfice architectural. |
| Speaker interne Reachy 5 W | **BANNI** | Inaudible EHPAD, jamais une option. |

### 2.4 Réseau et accès

- IP : `192.168.x.x` — hostname `reachy-mini.local` (mDNS avahi)
- SSH : `ssh pollen@reachy-mini.local`, mot de passe `root`
- Dossier projet : `/home/pollen/reachy_care/`
- Profils externes : `/home/pollen/reachy_care/external_profiles/reachy_care/`
- Venv Python : `/venvs/apps_venv/bin/python3` (apps_venv) et `/venvs/mini_daemon/bin/python3` (daemon systemd)
- Dashboard controller : `http://reachy-mini.local:8090` (UI web + API power + API settings)
- IPC conv_app_v2 ↔ main.py : HTTP `localhost:8766`

---

## 3. Architecture logicielle — les 4 processus

Quatre processus tournent en parallèle sur le Pi, avec des responsabilités nettes et une communication IPC HTTP.

```
┌──────────────────────────────────────────────────────────┐
│  reachy-mini-daemon.service  (systemd, apps_venv)        │
│  ├─ SDK robot low-level (moteurs + pipeline GStreamer    │
│  │   caméra via reachy_mini.media)                       │
│  ├─ Tient par défaut le pipeline caméra → conv_app_v2    │
│  │   doit négocier avec lui via /api/media/release       │
│  └─ Tient les moteurs, acquis/release via wake_up        │
└──────────────────────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────┐
│  reachy-controller.service (systemd, apps_venv)          │
│  ├─ reachy_controller.py, Flask :8090                    │
│  ├─ UI web + API                                          │
│  ├─ POST /api/power/start → lance start_all.sh           │
│  ├─ POST /api/power/stop  → kill propre                  │
│  ├─ POST /api/deploy → rsync depuis Mac                  │
│  └─ GET /api/settings, POST /api/settings (LOCATION…)    │
└──────────────────────────────────────────────────────────┘
                        │
                        ▼
        ┌───────────────┴──────────────────┐
        ▼                                    ▼
┌──────────────────┐             ┌──────────────────────┐
│ main.py          │   IPC :8766 │ conv_app_v2/main.py  │
│ (VISION)         │◄───────────►│ (LLM VOCAL)          │
│ ─ FaceRecognizer │             │ ─ AudioIO (XMOS→USB) │
│   buffalo_s      │             │ ─ ConversationEngine │
│ ─ AttenLabs      │             │ ─ OpenAIRealtime     │
│ ─ wake_word      │             │   Adapter            │
│ ─ sound_detector │             │ ─ RobotLayer         │
│ ─ fall detector  │             │   (ReachyMini +      │
│ ─ chess det      │             │    CameraWorker +    │
│ ─ mode_manager   │             │    MovementManager)  │
└──────────────────┘             └──────────────────────┘
```

### 3.1 `reachy-mini-daemon.service` (systemd, venv `mini_daemon`)

Lancé au boot. Possède le pipeline GStreamer caméra et les moteurs Dynamixel. Service critique, jamais tué.

**Conséquence importante** : le daemon tient `/dev/video0` et la socket `/tmp/reachymini_camera_socket` par défaut. Pour que conv_app_v2 puisse lire les frames, il faut soit **release** le daemon soit **consommer via l'IPC socket** qu'il expose. La deuxième approche est celle qui marche  (CameraWorker de conv_app_v2 ouvre un pipeline `unixfdsrc → queue → v4l2convert → appsink` sur la socket).

### 3.2 `reachy-controller.service` (systemd, venv `apps_venv`)

- Flask sur `:8090`, UI web de contrôle.
- `POST /api/power/start` → `subprocess.Popen(start_all.sh)` → lance main.py + conv_app_v2.
- `POST /api/power/stop` → `pkill` atomique de conv_app + main.py (jamais l'un sans l'autre — règle absolue, sinon socket caméra corrompue).
- `POST /api/deploy` → `rsync` depuis Mac via SSH.
- `GET /api/settings` → retourne `LOCATION`, `TZ`, etc. (config runtime utilisateur).

### 3.3 `main.py` (VISION, apps_venv)

**Responsabilités** :
- Capture frames via `bridge.get_frame()` → IPC HTTP GET `conv_app_v2:8766/get_frame` (**il n'accède PAS directement à la caméra**).
- Face reco via InsightFace buffalo_s, seuil cosinus **0.50** + **N-best** (règle absolue, jamais 0.45 ni seuil unique).
- **AttenLabs** : calcule `_compute_attention(frame)` → renvoie `SILENT` / `TO_HUMAN` / `TO_COMPUTER` → POST vers conv_app_v2 `:8766/attention`. Seuils actuels : `HEADING_MAX=0.15`, `SIZE_MIN=0.12`.
- Wake word (openWakeWord "Hey Reachy") — **actuellement muet** (P0-15).
- Sound detector (cassage verre, etc.).
- Fall detector (stub).
- Chess detector (détection échiquier via camera).
- `mode_manager` : écoute les commandes `/tmp/reachy_care_cmds/*.json` (wake_motors, sleep_mode, reveille, etc.) → applique les changements de mode.

Log : `reachy_care.log`.

### 3.4 `conv_app_v2/main.py` (LLM VOCAL, apps_venv)

**Responsabilités** (c'est le cœur vocal du robot) :
- `AudioIO` : capture mic XMOS via GStreamer `alsasrc reachymini_audio_src`, playback via subprocess `aplay -D reachymini_audio_sink`. Depuis commit `de2e3bd`, le sink est une **carte USB externe** (`hw:CARD=Device,device=0` via plug), plus du BT.
- `ConversationEngine` : chef d'orchestre, reçoit callbacks audio, gère session OpenAI, applique les gates (AttenLabs hysteresis, speaking gate, mute).
- `OpenAIRealtimeAdapter` : wrapper WebSocket OpenAI Realtime API avec auto-reconnect 1011.
- `RobotLayer` : instance SDK Pollen `ReachyMini` + `MovementManager` + `CameraWorker` (thread 30 Hz) + `HeadWobbler`.
- `IPCServer` : FastAPI `:8766` — expose `/get_frame`, `/attention`, `/user_speaking`, `/api/settings`.

Log : `conv_app.log`.

---

## 4. Chaîne audio entrante (micro → LLM)

```
Micro XMOS array (4 PDM)
      │  16 kHz, mono, AEC hw + beamforming + AGC (firmware XMOS)
      ▼
GStreamer alsasrc "reachymini_audio_src" (dsnoop de hw:0,0)
      │  audio/x-raw,format=S16LE,rate=16000,channels=1
      ▼
audio_io.py GStreamer pipeline → appsink "_on_mic_sample"
      │  pcm_chunk bytes
      ▼
conversation_engine.on_audio_captured(pcm_chunk)
      │
      ├── guard 1 : _audio_muted
      ├── guard 2 : _llm is None or _loop is None
      ├── guard 3 : AttenLabs gate (hysteresis 10 s)
      │           → si attention ≠ TO_COMPUTER depuis >10 s → DROP
      ├── guard 4 : CONV_DISABLE_LLM_SEND (killswitch diag)
      └── guard 5 : speaking gate — si self._speaking → DROP
      │
      ▼
OpenAIRealtimeAdapter.send_audio(pcm_chunk)
      │
      ├── upsample 16 → 24 kHz via scipy.signal.resample (FFT band-limited)
      │   (OpenAI a durci rate >= 24000 le sans upsample, Whisper hallucine)
      │
      ▼
WebSocket OpenAI Realtime (input_audio_buffer.append, PCM 24 kHz)
      │
      ▼
server_vad (OpenAI) → speech_started / speech_stopped → commit
      │
      ▼
Transcription + génération réponse
```

**Points critiques** :
- Le **resampler 16 → 24** doit être **band-limited** (`scipy.signal.resample`). `numpy.interp` produit des artefacts d'aliasing qui déclenchent des faux VAD (abandonné ).
- La **gate AttenLabs hysteresis 10 s** permet au robot de ne pas perdre le fil si l'utilisateur détourne le regard brièvement pendant qu'il réfléchit.
- La **speaking gate** (temporaire, depuiscommit `84c1b7d`) bloque complètement l'envoi audio pendant que Reachy parle → évite que l'écho BT revienne dans la session comme "user turn". Sera affinée en gate mode-aware quand l'AEC XMOS redeviendra utilisable (= speaker USB).

---

## 5. Chaîne audio sortante (LLM → haut-parleur)

```
OpenAI Realtime response.output_audio.delta
      │  PCM 24 kHz mono S16_LE (format natif OpenAI)
      ▼
conversation_engine.on_audio_delta(pcm_bytes)
      │  self._speaking = True (lève la speaking gate en entrée)
      │
      ▼
audio_io.push_playback(pcm_bytes)
      │
      ▼
Queue Python (thread-safe)
      │
      ▼
_bt_playback_worker (thread Python dédié)
      │
      ▼
subprocess aplay -D reachymini_audio_sink -f S16_LE -r 24000 -c 1 -t raw
      │  (alias ALSA vers bluealsa, conversion 24k mono → 44100 Hz stéréo)
      ▼
bluealsa-aplay daemon
      │  A2DP SBC 44 100 Hz
      ▼
Beats Pill BT (Claire Beats)
```

**Points critiques** :
- Le `_bt_playback_worker` thread + subprocess aplay souffre de **contention GIL** quand face reco + AttenLabs + caméra tournent en parallèle → **underruns BT** en mode histoire (lectures longues) → mots hachés puis silence. La solution identifiée (non implémentée) est de porter le pipeline GStreamer `appsrc → alsasink` de Pollen vanilla, qui est en C natif hors GIL.
- **Interdictions hard-apprises** : GStreamer `alsasink` direct sur `bluealsa` échoue au preroll (`set_hwparams`). `bluealsa-aplay` tient déjà le transport A2DP en exclusif, `alsasink` est refusé "Device busy". Le subprocess `aplay` est le seul chemin fonctionnel pour l'instant.
- **mini.wake_up() squat le sink BT** : découvertpar bisection runtime. Le SDK Pollen `ReachyMini()` ouvre lui-même une connexion bluealsa A2DP sur le sink BT au wake_up et ne la libère jamais. Solution mise en place : démarrer `AudioIO` **avant** `RobotLayer.start()` dans `ConversationEngine.start()` → notre aplay prend le sink en premier (first-come-first-served A2DP), le robot se lève quand même (les moteurs sont déjà enabled par RobotLayer avant le wake_up).

---

## 6. Chaîne vision (caméra → AttenLabs → gate)

```
IMX708 wide /dev/video0 (CSI)
      │  30 Hz, 1280x720
      ▼
reachy-mini-daemon.service : pipeline GStreamer caméra
      │  → socket /tmp/reachymini_camera_socket
      ▼
conv_app_v2 CameraWorker (thread 30 Hz)
      │  pipeline unixfdsrc → queue → v4l2convert → appsink
      │  ← emit("try-pull-sample", 20_000_000)    [fix P0-12,]
      ▼
latest_frame (BGR numpy array)
      │
      ▼
IPC GET conv_app_v2:8766/get_frame → JPEG
      │
      ▼
main.py bridge.get_frame()  → numpy BGR
      │
      ├── FaceRecognizer.identify_nbest(frame) → "Claire" / "Ada" / None
      │   (seuil cosinus 0.50, N-best oscillation-free, enroll via enroll_cedar.py)
      │
      └── AttenLabs._compute_attention(frame)
          │
          ├── heading estimation (kps 5pts SCRFD)
          ├── face size / distance
          ├── seuils HEADING_MAX=0.15, SIZE_MIN=0.12
          └── hysteresis : dernière transition TO_COMPUTER
          │
          ▼
          attention_state ∈ {SILENT, TO_HUMAN, TO_COMPUTER}
          │
          ▼
main.py POST conv_app_v2:8766/attention {state: …}
          │
          ▼
conversation_engine._attention_state = "…"
          │  utilisé par la gate AttenLabs dans on_audio_captured
```

**Points critiques** :
- Le fix **`emit("try-pull-sample")`** (commit du , pérennisé dans `start_all.sh` section 4f via sed idempotent) a débloqué P0-12. Le bug était que `appsink.try_pull_sample()` lève `AttributeError` dans le contexte multi-pipeline de conv_app_v2 (cause racine exacte inconnue, probablement ordre d'init GStreamer). Même fichier utilise `emit()` dans `open()` sans problème.
- L'**hysteresis 10 s** sur la gate AttenLabs évite de couper le micro quand l'utilisateur regarde brièvement ailleurs pendant qu'il réfléchit.
- La **face reco** est **silencieuse pendant une conversation active** (si moins de 15 s depuis la dernière réponse de Reachy) → elle envoie juste un `session.update` discret au LLM au lieu d'un `inject_event` qui déclencherait une response.create intempestive.

---

## 7. Chaîne mode (histoire, sommeil, conv)

Les modes spéciaux du robot (histoire, sommeil, détective, médecin, etc.) ne sont pas des états de `conv_app_v2`. Ils sont gérés par un pattern distribué :

1. Le LLM décide via un **tool call** (`switch_mode`, `endors_reachy`, `reveille_reachy`, etc.).
2. Le tool écrit une commande JSON dans `/tmp/reachy_care_cmds/*.json` (ex: `{"cmd": "wake_motors"}`).
3. Le LLM tool `switch_mode.schedule_session_update(instructions)` fait également un **update du prompt système** OpenAI pour adopter la persona du mode.
4. `main.py mode_manager` poll les fichiers `/tmp/reachy_care_cmds/*.json` et applique le changement côté vision/moteurs (ex: `goto_sleep` pour dormir, `wake_up` pour réveil).

**Règle absolue** : le mode histoire **ne vit PAS dans `conv_app_v2/conversation_engine.py`**. Toute tentative de gérer le mode histoire dans conv_app_v2 est une erreur de cible (voir `feedback_mode_histoire_vit_hors_conv_app.md`). Le mode histoire = `instructions_histoire.txt` (prompt) + `main.py mode_manager` (gates) + `galileo_library.py` (contenu lecture).

---

## 8. LLM tools embarqués

Les tools exposés au LLM OpenAI Realtime sont définis dans `external_profiles/reachy_care/tools.txt` (un par ligne, 13 tools après le cleanup ). Chaque tool est un fichier Python dans `tools_for_conv_app/` avec une fonction handler.

| Tool | Rôle | État |
|---|---|---|
| `camera` | Prend une image caméra et l'envoie en `input_image` à OpenAI (vision) | ✅ Porté(b64_im → input_image OpenAI) |
| `search` | Recherche Brave API (actualité, facts) | ✅ Porté|
| `galileo_library` | Lecture du roman *Galileo Le Lion Blanc* (7 chapitres embarqués, action `resume` + progression JSON) | ✅ Porté|
| `switch_mode` | Change de persona (histoire, détective, médecin, normale) | ✅ |
| `endors_reachy` | Mode sommeil : moteurs goto_sleep + mute | ✅ |
| `reveille_reachy` | Réveil : daemon stop(goto_sleep=false) → start(wake_up=true) | ✅ (fix `wake` → `wake_motors`) |
| `enroll_face` | Enregistre un visage dans `known_faces/` | ✅ |
| `confirm_identity` | Voit N-best, demande confirmation user | ✅ |
| `chess_move`, `chess_reset` | Pièces d'échecs | ✅ (laissé pour Aristote) |
| `identify_music` | Shazam-like via mic (non utilisé EHPAD) | ✅ |
| `groove` | Mini-danse sur rythme | ✅ |
| `log_event` | Trace un événement dans `reachy_care.log` | ✅ |
| `gutenberg` | Lecture aléatoire Project Gutenberg | ✅ (legacy, superseded par galileo pour EHPAD) |

**Règle absolue** : les échecs / erreurs tools passent par la **confirmation LLM** (ex: "Je n'ai pas trouvé, tu peux confirmer ?") **jamais** par le pipeline vocal direct, qui est trop fragile (STT hallucine). Décision .

---

## 9. Découvertes majeures des 2 mois

Classées par ordre chronologique. Chacune a coûté de plusieurs heures à plusieurs jours d'investigation.

### Mars 2026

- **** — Seuil face reco unique 0.68 → oscillations entre visiteurs. **Fix** : 0.50 + N-best voting. Jamais remettre en cause.
- **** — `killall python3` sur le Pi tue aussi le daemon et le controller → reboot sauvage, socket caméra corrompue, face reco morte. **Règle absolue** : ne JAMAIS kill main.py seul, toujours en paire avec conv_app.
- **** — PulseAudio conflit avec PortAudio → micro main.py mort. **Interdit à vie** : PulseAudio sur le Pi. ALSA pur uniquement.
- **** — `pip install --force-reinstall` sur le Pi écrase les versions ARM aarch64 épinglées → SIGILL wespeaker, torchaudio cassé. **Interdit à vie**. Pour wespeaker, pattern `pip install wespeakerruntime --no-deps` puis ajout manuel des deps.
- **** — `espeak` bloque le pipeline GStreamer → **TTS_BACKEND="none"** en prod. Cedar TTS OpenAI uniquement.

### Avril 2026

- **** — Désempilement Option B (main.py sans `ReachyMini()` direct) validé terrain. Conv_app_v2 remplace la conv_app Pollen (patch_source.py trop fragile).
- **** — **Découverte chip XMOS XVF3800** dans le Reachy Mini. AEC hardware dans le firmware, mais limité au signal sur hw:0,0.
- **** — Séquence **daemon AVANT conv_app_v2 toujours** : sinon désync moteurs.
- **** — T60 RAM leak fix : C-native allocations dans GStreamer, killswitch RAM diag ajouté.
- **** — Nuit d'audits parallèles (8 agents). HP 300 Hz coupait les fondamentaux vocaux → Whisper hallucine espagnol. Fix : suppression complète du HP filter custom (désempilé avec les 10 couches anti-écho artisanales).
- **** — **Découverte `mini.wake_up()` squat le sink BT bluealsa A2DP**. Cause cachée des "Device busy" historiques. Fix : AudioIO avant RobotLayer dans ConversationEngine.start() (commit T67c).
- **** — Code review parallèle (4 agents Sonnet). 11 fixes P0/P1/P2 appliqués. `_turn_response_sent` reset dans try/finally, fusion des locks TOCTOU echo_canceller, BT worker termine subprocess sur BrokenPipe.
- **** — server_vad Pollen-pattern adopté. Upsample 16→24 via scipy.signal.resample (OpenAI a durci rate >=24000).
- **** — Session  difficile. Étape 1 d'ingestion wiki sautée → 3 fausses pistes (restore EchoCanceller, numpy.interp, Beats Pill USB-C). Revert partiel, 5 commits gardés. **Leçon structurelle** : CLAUDE.md réécrit avec PRÉ-REQUIS ABSOLUS bloquants, 7 feedbacks mémoire auto-injectés, audit complet `AUDIT_SESSION_20260415.md`. Ne plus JAMAIS démarrer une session sans les 5 min de checklist.
- **** — P0-12 résolu (emit try-pull-sample pérennisé start_all.sh §4f). P0-14 résolu (interpolation LOCATION/DATETIME). T14 validé étalon stabilité. Tools camera+search+galileo_library portés. Wake handler aligné sur wake word. AttenLabs seuils + hysteresis 10 s. Face reco silencieuse en conversation active. `semantic_vad eagerness=low` adopté.
- **** — Audit total + rapport + plan speaker USB-C pour résoudre P0-13.

### 9 faits qu'on croyait vrai et qui étaient faux

1. "Restaurer le backup .bak_1776100010 répare le mode histoire" → **FAUX** : mode histoire ne vit pas dans conv_app_v2.
2. "numpy.interp suffit pour resampler 16→24 kHz" → **FAUX** : aliasing déclenche server_vad.
3. "Beats Pill en USB-C peut servir d'USB Audio" → **FAUX** : HID only propriétaire Apple.
4. "La speaker gate doit être conditionnelle au mode" (conv = non, histoire = oui) → **FAUX à court terme** : sans gate globale, l'écho BT pollue toute session. Gate globale temporaire jusqu'à speaker USB.
5. "`CameraWorker` est démarré automatiquement dans le portage v2" → **FAUX** : bug latent, `.start()` jamais appelé.
6. "`appsink.try_pull_sample()` fonctionne partout" → **FAUX** : `AttributeError` en contexte multi-pipeline conv_app_v2, doit passer par `emit()`.
7. "GStreamer `alsasink → bluealsa` est un chemin viable" → **FAUX** : crash preroll systématique, `aplay` subprocess seul chemin.
8. "Le daemon peut être tué, seuls main.py et conv_app tournent" → **FAUX** : le daemon possède les moteurs et le pipeline caméra, toujours laisser actif.
9. "Le speaker interne 5 W peut servir en fallback" → **FAUX** : inaudible EHPAD, jamais une option (répété mille fois).

---

## 10. Solutions validé es (table par sous-système)

### Audio

| Solution | Validée quand | Pourquoi |
|---|---|---|
| AEC via aplay plughw:0,0 séparé (pas GStreamer tee, pas ALSA multi) || tee conflit dsnoop, multi = décalage |
| Séquence daemon avant conv_app_v2 toujours || désync moteurs sinon |
| Subprocess aplay sur reachymini_audio_sink pour BT || GStreamer alsasink crashe preroll |
| AudioIO.start() **avant** RobotLayer.start() || wake_up squat A2DP, 1er client gagne |
| Upsample 16→24 via scipy.signal.resample || OpenAI rate >= 24000, band-limited |
| Auto-reconnect WS OpenAI sur 1011 || EHPAD zéro opérateur, reconnect 1,2-1,4 s |
| Speaking gate globale dans on_audio_captured || écho BT → user turn fantôme sinon |
| interrupt_response = False || écho BT déclenche auto-cancel sinon |
| silence-fill BT worker → continue (pas de silence bytes) || 241 s d'underrun cumulé sinon |
| semantic_vad eagerness=low || 3-8 s au lieu de 0,5 s, mieux pour personnes âgées |

### Vision

| Solution | Validée quand | Pourquoi |
|---|---|---|
| FACE_COSINE_THRESHOLD = 0.50 + N-best || oscillation a coûté des heures sinon |
| CameraWorker.start() dans RobotLayer.start() || bug latent portage v2 |
| emit("try-pull-sample") au lieu de .try_pull_sample() || AttributeError context conv_app_v2 |
| AttenLabs HEADING_MAX=0.15, SIZE_MIN=0.12 || profil de côté ne déclenche plus TO_COMPUTER |
| Hysteresis gate attention 10 s || glances away ne coupent plus audio |
| Face reco silencieuse en conv active || inject_event → session_update si <15 s |

### Conv app

| Solution | Validée quand | Pourquoi |
|---|---|---|
| conv_app_v2 remplace conv_app Pollen || patch_source.py fragile |
| Coexistence Option B (main.py sans ReachyMini direct) || conflit gRPC, un seul ReachyMini possible |
| TTS_BACKEND = "none" || espeak bloque pipeline, Cedar inline markers |
| Échecs = LLM tools + confirmation || STT hallucine |
| `{LOCATION}` / `{DATETIME}` interpolés au boot || LLM hallucinait "Antibes 14h32" sinon |
| reveille_reachy wake → wake_motors || handler main.py n'écoutait pas "wake" |

### Packages / environnement

| Solution | Validée quand | Pourquoi |
|---|---|---|
| wespeakerruntime `--no-deps` + deps manuelles || torchaudio SIGILL ARM |
| onnx 1.21.1, album <=1.3.1 pin | ≈ | compat buffalo_s aarch64 |
| pip force-reinstall INTERDIT || écrase ARM, casse tout |
| PulseAudio INTERDIT || conflit PortAudio |

---

## 11. Solutions abandonnées et pourquoi

Ne plus jamais re-proposer ces pistes sans raison nouvelle explicite.

| Approche | Abandon | Raison |
|---|---|---|
| Beats Pill en USB-C wired || HID only propriétaire Apple, pas UAC (testé `lsusb -v` deux configs HID) |
| Restore EchoCanceller Python || mode histoire ne vit pas dans conv_app_v2, mauvaise cible |
| numpy.interp upsample audio || artefacts aliasing → server_vad false positives |
| GStreamer alsasink → bluealsa playback || crash preroll `set_hwparams` systématique, 3 fix tentés échoués |
| GStreamer tee pour dupliquer audio || conflit dsnoop |
| ALSA multi device (playback dupliqué) || décalage temporel AEC |
| PulseAudio sur le Pi || conflit PortAudio, micro main.py mort |
| pip install --force-reinstall || écrase versions ARM épinglées |
| espeak prod || bloque GStreamer |
| Seuil face reco unique 0.68 || oscillations, remplacé par N-best 0.50 |
| set_reading_voice (voix dédiée mode histoire) | ≈ | pas supporté Realtime API, géré via prompt |
| Vosk STT || pipeline vocal trop fragile, abandonné avec tout STT local |
| Pipeline vocal direct pour échecs || STT hallucine les coups |
| keepalive / inject_memory / person_departed / set_visitor_mode || triggers spontanés, source de mode proactif |
| send_idle_signal Pollen || boucle dialogue interne |
| HP filter 300 Hz dans patch_source.py || détruit fondamentaux vocaux, Whisper hallucine |
| interrupt_response = True avec BT || boucle écho BT, confirmé terrain |
| Lip detection sur Pi 4 || CPU 171% → throttle thermique (à réactiver sur ordi externe) |

---

## 12. P0 ouverts et dette technique connue

### P0 actifs (au )

| # | Sujet | État | Piste |
|---|---|---|---|
| **P0-13** | Écho BT pollue la transcription | Mitigé par speaking gate globale (84c1b7d) | **Résoudre avec speaker USB** (dongle USB-C DAC + enceinte mini-jack, test ). AEC XMOS redevient viable car latence USB << 500 ms. |
| **P0-15** | Wake word muet (max_score=0.001) | Non attaqué | PyAudio fallback sur hw:0,0 exclusif verrouillé par GStreamer alsasrc → lit silence. Piste : test isolé modèle ONNX + mesure gain mic dsnoop. |

### Nouveaux problèmes identifiés 

| # | Sujet | Piste |
|---|---|---|
| VAD coupe trop tôt | `silence_duration_ms` ignoré par SDK Python (bug #2199 typed params). `semantic_vad eagerness=low` aide mais insuffisant pour parole lente. | VAD manuelle `turn_detection: null` + `input_audio_buffer.commit` client-side. |
| Underruns BT mode histoire | Subprocess aplay + GIL contention sur lectures longues → mots hachés. | Porter le pipeline GStreamer `appsrc → alsasink` de Pollen vanilla (C natif hors GIL). Si le speaker USB marche, passer en GStreamer playback direct sur la carte USB. |
| CPU main.py 170%+ | Même sans lip detection actif. | Investiguer InsightFace `_app.get()` appelé à chaque frame de `_compute_attention`. Piste : throttle à 5 Hz au lieu de 30 Hz. |
| Fichiers en double | `ipc_server.py` racine vs `conv_app_v2/` → handler `/user_speaking` invisible. | Dédupliquer, source unique. |
| Code mort GStreamer | `audio_io.py::_build_playback_pipeline` derrière `CONV_USE_GST_PLAYBACK=1` env var, jamais activé. | Supprimer si GStreamer playback direct est adopté avec USB. |

### Dette technique identifiée

- **Tous les backups `.bak_1776xxxxx`** sur le Pi (15 fichiers, du ) : pièges connus (session ). À supprimer.
- **Git Pi désynchronisé** : `git log` Pi montre `a17bb67` mais les fichiers datent du(rsync). C'est un pattern, pas un bug, mais à documenter.
- **14 fichiers + 3 tools uncommitted Mac au** : tout le travail(P0-12, P0-14, tools, AttenLabs seuils) pas encore dans un commit. **Risque de perte si crash.**
- **`patch_source.py`** : pour mode Pollen (legacy). On est en mode v2 . À archiver ou supprimer après validation stable.
- **`reachy_mini_conversation_app`** sur le Pi (17 Mo) : copie legacy Pollen, plus utilisée côté runtime. Peut être supprimée du Pi.

---

## 13. Plan speaker USB-C — bascule hardware RÉALISÉE, validation AEC EN COURS

**Contexte** : le problème P0-13 (écho BT pollue la transcription) n'avait pas de solution propre tant que la sortie audio passait par Bluetooth (XMOS AEC hors fenêtre). La speaking gate globale (commit `84c1b7d`, ) était un pansement temporaire qui coûtait le barge-in.

**Confirmation** : le port USB-C arrière du Reachy Mini est un port **data** (datasheet officiel Pollen : *"one can plug a device such as a usb key"*). Options envisagées :

1. Enceinte USB-C UAC native (ex Creative Pebble V3, ~40 €). Envisagée le , finalement écartée lepour format stéréo desktop inadapté socle fermé (voir `DECISIONS.md §00h`).
2. **Dongle USB-C → jack 3.5 mm avec DAC intégré** (UGREEN, Apple, Pixel, ~10-15 €) + enceinte mini-jack existante d'Alexandre. **Plan retenu et implémenté  (commit `de2e3bd`)**.

**Bénéfices architecturaux attendus** (à valider au fur et à mesure) :
- **P0-13 résolu** : le XMOS AEC voit à nouveau le signal de référence sur `hw:0,0` (duplication prévue du flux USB). L'écho BT n'existe plus (déjà effectif par suppression du BT).
- **Underruns mode histoire résolus** : on peut passer au pipeline GStreamer `appsrc → alsasink hw:USB` (C natif hors GIL).
- **Speaking gate affinée** : retour à la règle cible (gate uniquement en mode histoire, barge-in libre en conv).
- **CPU main.py potentiellement amélioré** : sans subprocess aplay + queue Python, moins de context switching.

### Avancement au  00h

| Étape | Statut | Détail |
|---|---|---|
| 1. Brancher dongle + enceinte, vérifier `aplay -l` liste la carte USB | ✅ FAIT| Carte détectée comme `Device` (GeneralPlus). `lsusb` confirme. |
| 2. Ajuster `asound.conf` → `reachymini_audio_sink` vers la carte USB au lieu de bluealsa | ✅ FAIT(commit `de2e3bd`) | `pcm.reachymini_audio_sink` → `type plug` → `hw:CARD=Device,device=0`. Installé dans `/etc/asound.conf` sur le Pi. |
| 3. Tester playback direct `speaker-test` | ✅ FAIT~16h | Son entendu sur enceinte USB. Conv_app_v2 envoie bien les deltas OpenAI vers USB. |
| 4. Tester le XMOS AEC sur boucle hw:0,0 → RMS captured avec/sans référence | ⏳ **PROGRAMMÉ ** | Procédure complète `docs/TEST_AEC_XMOS_USB_20260421.md`. **Gate décision achat enceinte Julie.** |
| 5. Porter `_bt_playback_worker` → pipeline GStreamer `appsrc → alsasink` en C | ⏳ Conditionnel étape 4 | Si AEC PASS, refacto dans la foulée. Si FAIL, bascule speakerphone Jabra qui change la cible. |
| 6. Reverter la speaking gate globale (commit `84c1b7d`) en gate mode-aware | ⏳ Conditionnel étape 4 | Dépend validation AEC : sans AEC fonctionnel, gate globale maintenue. |
| 7. Reconsidérer `interrupt_response=True` avec `server_vad threshold=0.8` | ⏳ Conditionnel étape 6 | Seulement si étape 6 validée en présentiel T14bis. |

### Enceinte cible remise Julie 

Shortlist arrêtée00h (voir `DECISIONS.md §00h`) :
- **Anker PowerConf S3** (~110 €, USB-C UAC, omni 360°, filetage trépied, batterie 40 h) → **candidat favori** si test AEC PASS.
- **Jabra Speak 510 MS** (~130 €, speakerphone USB complet avec AEC matériel autonome) → **fallback** si test AEC FAIL.
- Creative Pebble V3 éliminée (stéréo desktop, pas omni).

Socle cible chez Julie = tronc évidé ou caisson bois fermé, enceinte logée dedans, câble USB-C court, évents 10-15 % surface latérale pour éviter effet caisse close.

---

## 14. Comment l'équipe travaille (workflow humain + agents)

### Humain (Alexandre)

- **Présentiel** : valide les comportements terrain (T14 étalon = présentiel -13:31).
- **Git** : branche `julie-demo-clean` active. Merge vers `master` seulement quand P0 sont résolus.
- **Règle NE JAMAIS commit sans demande explicite** : Claude ne commit rien sans accord d'Alexandre.

### Claude Code

- Un seul Claude Code orchestrateur (Opus [1m]).
- **Ingestion obligatoire** en début de session (CLAUDE.md § PRÉ-REQUIS ABSOLUS) — 5 min, checklist 7 points. **Le sauter = journée perdue (leçon ).**
- Délègue aux subagents :
  - **Challenger** : avant tout retry/polling/threading
  - **Tester** : avant chaque déploiement (checklist non-régression SSH)
  - **Researcher** : obligatoire pour tout nouveau package/SDK/API
  - **Dev A** : main.py, modules/, config.py, start_all.sh
  - **Dev B** : conv_app_bridge.py, tools_for_conv_app/, external_profiles/
  - **Deployer** : rsync + restart (après Verify + Tester PASS)
  - **Log Reader** : 45 s après chaque déploiement (logs Pi → PASS/FAIL)

### MemPalace (knowledge graph partagé)

- Tous les agents lisent/écrivent dans MemPalace.
- Protocole : `kg_query` AVANT de répondre sur un fait durable, `kg_add` quand un fait s'établit, `diary_write` en fin de session.
- Ontologie stricte (`_Cowork/references/mempalace-ontologie.md`) : pas d'invention de prédicat.

### Wiki vivant (ce repo)

5 fichiers wiki maintenus **à chaud** :
- `INDEX.md` — catalogue (lu en premier, économise 97 % des tokens)
- `STATE.md` — snapshot vivant (P0, blocages, dernier déploiement)
- `DECISIONS.md` — VALIDÉ / ABANDONNÉ par sous-système
- `PI_KNOWLEDGE_BASE.md` — packages, ALSA, SDK, comportements connus, fichiers sensibles
- `CLAUDE.md` — supervisor instructions (≤ 500 lignes, extractions vers `docs/WIKI_MAINTENANCE.md` et `docs/MEMPALACE_USAGE.md`)

**Interdit** : fichiers datés type `RAPPORT_SESSION_*`, `CORRECTIONS_DEV_*`. Tout va dans les 5 fichiers vivants.

### Processus de changement

```
SESSION → Ingestion wiki (5 min) → Pi-Only check → P0 d'abord
PLAN    → Researcher si nouveau → Challenger si retry/polling
EXE     → Dev A/B (rappeler KB) → Verify → PASS/FAIL (max 3 tentatives)
DEPLOY  → Tester PASS → Deployer (rsync + restart) → Log Reader 45s après
FIN     → STATE.md + DECISIONS.md + PI_KB update + diary_write + commit
```

---

## 15. Roadmap stratégique — 3 horizons

Réflexion ouverte le  sur la trajectoire long terme : **se libérer d'OpenAI** (voix + LLM) et **externaliser le calcul** vers un ordinateur dédié dans la version filaire EHPAD finale.

### Horizon 1 — semaine du  — Speaker USB-C (EN COURS DE VALIDATION)

**Objectif** : sortir du Bluetooth audio, résoudre P0-13 écho BT, restaurer le barge-in.

**Actions (statut au  00h)** :
1. ✅ **FAIT** : dongle USB-C → jack 3.5 mm avec DAC actif (Cabletime 24k gold 8 cm) + enceinte mini-jack existante. Carte ALSA détectée comme `Device` (GeneralPlus).
2. ✅ **FAIT(commit `de2e3bd`)** : bascule `asound.conf` → `reachymini_audio_sink` pointe `hw:CARD=Device,device=0` via plug. `start_all.sh` refactoré sans bluealsa.
3. ⏳ **EN COURS ** : validation XMOS AEC sur sortie USB externe via duplication `hw:0,0` volume 0. Procédure `docs/TEST_AEC_XMOS_USB_20260421.md`. **Gate décision enceinte Julie.**
4. ⏳ Conditionnel étape 3 : porter `_bt_playback_worker` → pipeline GStreamer `appsrc → alsasink` hors GIL.
5. ⏳ Conditionnel étape 3 : reverter la speaking gate globale (`84c1b7d`) en gate mode-aware AttenLabs.
6. ⏳ Conditionnel étape 5 : reconsidérer `interrupt_response=True` avec `server_vad threshold=0.8`.

**Volet enceinte dédiée Julie** (ouvert ) : commande mardi matin après test AEC. Anker PowerConf S3 candidat (UAC natif, 360°, filetage trépied) ou Jabra Speak 510 fallback. Voir `DECISIONS.md §00h`.

**Résultats attendus (si étape 3 PASS)** : P0-13 résolu, underruns mode histoire résolus, barge-in retrouvé, CPU main.py amélioré.

### Horizon 2 — mai 2026 — Mode histoire local (voix hors OpenAI)

**Problème** : OpenAI Realtime Cedar est une voix unique "chaleureuse" sans modulation fine. En mode histoire (lecture de *Galileo Le Lion Blanc*), on aimerait :
- **Modulation pitch** (dialogue du lion vs narration).
- **Émotion contrôlée** (tension, tendresse, surprise).
- **Voix dédiée** pour chaque personnage / changement de timbre.
- **Coût zéro** sur les lectures longues.

**Candidats TTS open source** (à évaluer Researcher) :

| Modèle | Qualité | Pitch / émotion | CPU/GPU | Licence |
|---|---|---|---|---|
| **StyleTTS 2** | Excellente, quasi ElevenLabs | Oui (prosody control) | GPU recommandé (~2 GB VRAM) | MIT |
| **XTTS v2 (Coqui)** | Très bonne, voice cloning 6s | Partiel (emotion via reference audio) | GPU (~4 GB VRAM) | Coqui PL (perso/recherche OK, commercial payant) |
| **F5-TTS** | Nouvelle génération, très rapide | Oui (instruct text) | GPU (~3 GB VRAM) | CC BY-NC (non-commercial) |
| **Mars5** | Voice cloning + prosody | Oui | GPU | AGPL |
| **Piper TTS** | Correcte, voix FR dispos | Non (voix fixe par modèle) | CPU ARM Pi OK | MIT |
| **ElevenLabs API** | Excellente, contrôle fin | Oui (voice design API) | Cloud | Propriétaire (plus cher qu'OpenAI) |

**Plan pragmatique** :
1. Spawner un **Researcher** pour évaluer StyleTTS 2 + F5-TTS en condition EHPAD : qualité FR, prosody control, latence streaming, licence commerciale.
2. **POC** : générer une phrase Galileo avec modulation pitch (dialogue lion grave/menaçant vs narration). Tester sur Mac M1 Max d'abord (Metal), puis évaluer GPU externe nécessaire.
3. **Bridge Pi ↔ TTS local** : tool `galileo_library` (ou nouveau `tts_histoire`) appelle un service HTTP local au lieu de passer par OpenAI. Le service renvoie du PCM 24 kHz streaming vers `audio_io.push_playback`.
4. **OpenAI Realtime reste pour la conv courante** (on garde ce qu'on maîtrise).

**Coût approximatif** : un Mac mini M4 32 GB ou un mini-PC avec RTX 4060 Ti 16 GB (~700-1200 €) tourne StyleTTS 2 en streaming sans souci.

### Horizon 3 — été 2026 — Architecture filaire, libération totale

**Vision** : la version EHPAD finale n'est plus sur batterie. Reachy est branché à un ordinateur dédié (mini-PC ou Mac mini) via USB 3 ou Ethernet. Le Pi CM4 devient un **thin client** qui capture audio/vidéo et exécute les commandes moteurs, le reste tourne sur l'ordi.

**Bénéfices** :
- **Tout local, tout sous contrôle** : RGPD EHPAD OK (aucune donnée patient sur cloud sauf choix explicite).
- **Coût récurrent éliminé** (OpenAI gpt-4o-realtime = plusieurs centaines d'€/mois en EHPAD ).
- **Choix du modèle** : on peut swaper LLM selon qualité/coût/vitesse.
- **Face reco lourde** : buffalo_l (qualité pro) au lieu de buffalo_s (CPU Pi). Moins de faux positifs.
- **Fall detection** : YOLOv8-pose ou modèle custom en temps réel (impossible sur Pi 4 170 % CPU).
- **Latence réduite** : pas de round-trip US pour le LLM.
- **Résilience** : pas de panne cloud, pas de dérives API.

**Architecture cible** :

```
┌─────────────────────────────────────────────────────────────┐
│  Reachy Mini (CM4, thin client)                              │
│  ├─ XMOS capture mic (stream 16 kHz)                        │
│  ├─ IMX708 camera (stream 30 fps)                           │
│  ├─ Moteurs Dynamixel (commandes)                           │
│  └─ Sortie audio USB-C (playback depuis ordi)               │
└──────────────────────┬──────────────────────────────────────┘
                       │  USB 3.0 ou Ethernet Gb
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Ordinateur dédié (mini-PC RTX 4060 Ti / Mac mini M4)        │
│  ├─ Orchestrateur voice agent (Pipecat / Livekit Agents)    │
│  ├─ STT streaming : Whisper local ou Ultravox direct         │
│  ├─ LLM local : Llama 3.3 70B / Qwen 2.5 72B / Mistral Large │
│  ├─ TTS : StyleTTS 2 avec voice cloning (Cedar-like + pitch) │
│  ├─ Vision : InsightFace buffalo_l, YOLO fall det, lip det   │
│  └─ Bridge vers Pi via HTTP / gRPC / WebSocket               │
└─────────────────────────────────────────────────────────────┘
```

**Candidats stack** (à évaluer Researcher) :

| Brique | Candidats | Notes |
|---|---|---|
| Orchestrateur voice agent | **Pipecat** (Daily.co), Livekit Agents | Pipecat open source, pluggable STT/LLM/TTS |
| STT streaming | **Whisper large-v3 turbo**, Ultravox, Distil-Whisper | Ultravox = speech-to-speech direct |
| LLM local | **Llama 3.3 70B FP8**, Qwen 2.5 72B, Mistral Large 2411 | 48 GB VRAM nécessaire pour 70B FP8 |
| TTS open | **StyleTTS 2**, XTTS v2, F5-TTS | Contrôle pitch/émotion |
| Face reco | InsightFace **buffalo_l** (pro) | vs buffalo_s actuel (lite) |
| Fall detection | YOLOv8-pose, modèle custom CNN | Temps réel 30 fps possible |
| Lip detection | Silero + kps 106pts | Aujourd'hui désactivé CPU Pi |

**Hardware cible minimum** :
- **Mini-PC avec RTX 4060 Ti 16 GB** (~700-900 €) : tourne Whisper + Llama 3.1 8B (FP16) + StyleTTS 2.
- **Pour Llama 70B FP8** : RTX 4090 24 GB ou 2× 3090 NVLink (~1500-2000 €), ou Mac Studio M2 Ultra 192 GB (~4500 €).
- **Budget recommandé ~1500 €** : mini-PC RTX 4060 Ti + alim solide + SSD 2 TB. Tient un EHPAD 24/7 sans difficulté.

**Plan de migration** :
1. **Phase A (mai)** : POC TTS local mode histoire (horizon 2 ci-dessus). Teste le bridge Pi ↔ ordi.
2. **Phase B (juin)** : POC LLM local conv courante. Llama 3.1 8B FP16 suffit pour test (~8 GB VRAM). Qualité < GPT-4o mais acceptable EHPAD.
3. **Phase C (juillet)** : Migration fall detection + face reco lourde sur ordi. Pi n'héberge plus QUE la capture + commandes moteurs.
4. **Phase D (août)** : Orchestrateur Pipecat complet remplace `conv_app_v2` côté logique. Conv_app_v2 ne reste que pour la gestion audio I/O Pi.
5. **Phase E** : OpenAI retiré de la chaîne critique. Gardé en fallback via feature flag pour A/B testing qualité.

**Risques / questions ouvertes** :
- **Latence voice-to-voice** : OpenAI Realtime ~500 ms. Pipecat local viser < 800 ms pour acceptabilité EHPAD.
- **Qualité FR** : Llama 3.3 70B est bon FR mais Mistral Large est natif. À benchmarker.
- **Interruption / barge-in** : Pipecat gère, mais à valider avec XMOS AEC.
- **Packaging EHPAD** : mini-PC + Pi + enceinte + alim = ≥ 4 boîtes. Intégrer dans un socle Reachy élargi ?
- **Maintenance** : mises à jour sécurité Linux, modèles LLM à mettre à jour périodiquement. Contrat mainteneur ?

---

## 16. Glossaire

- **Reachy Mini** : plateforme hardware Pollen Robotics (CM4, XMOS, IMX708, batterie, moteurs Dynamixel).
- **Reachy Care** : notre stack logicielle custom dessus.
- **CM4** : Raspberry Pi 4 Compute Module (différent du Pi 4 model B, pas de jack 3.5 mm natif).
- **XMOS XVF3800** : chip audio avec AEC hardware + beamforming + DOA, dans le firmware (invisible ALSA).
- **XMOS** (par raccourci) : le chip audio XMOS XVF3800 ci-dessus.
- **bluealsa** : daemon Linux qui expose une enceinte BT comme une carte ALSA. Single-client par profil A2DP.
- **dsnoop / dmix** : plugins ALSA qui permettent de partager un device entre plusieurs processus en lecture (dsnoop) ou écriture (dmix).
- **AEC** : Acoustic Echo Cancellation, annule l'écho du signal qu'on joue dans la capture micro.
- **AttenLabs** : notre module custom dans main.py qui décide SILENT/TO_HUMAN/TO_COMPUTER à partir des frames caméra.
- **server_vad** : Voice Activity Detection côté OpenAI Realtime, détecte les frontières de tour automatiquement.
- **semantic_vad** : variante VAD qui utilise la sémantique du discours pour mieux tenir les pauses.
- **Speaking gate** : guard dans `on_audio_captured` qui bloque l'envoi audio à OpenAI pendant que Reachy parle.
- **Barge-in** : possibilité pour l'utilisateur d'interrompre Reachy pendant qu'il parle.
- **Cedar** : voix OpenAI Realtime utilisée par défaut (empreinte "chaleureuse").
- **T14** : test de silence 60s = étalon de stabilité validé .
- **P0-12, P0-13, P0-14, P0-15** : priorités 0 numérotées, traçabilité inter-session.
- **julie-demo-clean** : branche git active, nommée initialement pour un personnage EHPAD fictif, devenue le support de la **vraie remise Julie le ** (résidente réelle).

---

## 17. Scénarios de démo — Commanditaire  et remise Julie 

> Ajouté . Livrable attendu côté Alexandre et côté  pour cadrer les 10 derniers jours avant livraison.

### 17.1 Remise commanditaire Art 51 — Mar 

**Contexte** : présentation finale au commanditaire du projet Article 51 (dispositif expérimentation santé, département des Hautes-Pyrénées). Validation technique et fonctionnelle avant déploiement terrain.

**Critères PASS attendus** (à affiner avec Alexandre, version ) :
- **Stabilité T14 étalon préservée** : silence de 60 s sans parole spontanée du robot, réponse propre quand on parle, retour au silence après. Référence commit état -13:31.
- **Face reco Alexandre fiable** : reconnaît le principal à <2 s d'exposition, seuil cosinus 0.50 + N-best.
- **Mode histoire propre** : lecture d'un chapitre *Galileo Le Lion Blanc* sans sauts de paragraphes ni micro-coupures > 2 s.
- **Wake word fonctionnel** : "Hey Reachy" déclenche feedback visible côté user (P0-15 à résoudre d'ici là).
- **Dashboard opérationnel** : `:8090` accessible, settings LOCATION/TZ ajustables, power start/stop fiable.
- **Endurance 1 h sans crash** : pas de drop audio, pas de WebSocket déconnexion non recouverte, pas de fuite RAM visible.
- **Reset one-click** : si état instable, un bouton/commande remet le robot en état nominal sans SSH.
- **Audit visuel** : câblage propre, pas de dongle qui pendouille, robot stable sur son pied.

**Scénario type (~20 min de démo)** :
1. Robot éteint. Alexandre met sous tension via dashboard `:8090`.
2. Boot visible, daemon + controller + main.py + conv_app_v2 démarrent en séquence.
3. Robot réveillé par wake word ou bouton. Salue Alexandre par son nom.
4. Conversation libre 5 min (actualité, météo, question pratique). Démontre barge-in (si AEC validé) ou stabilité anti-écho (gate globale sinon).
5. Switch mode histoire. Robot lit 1 chapitre *Galileo*. Alexandre interrompt, robot sort du mode histoire, reprend conv.
6. Switch mode dodo. Robot se couche, moteurs coupés.
7. Reset one-click, retour état nominal.

**Documents à produire avant** :
- Doc 1 page commanditaire (non-technique, parle du service délivré aux résidents).
- Scénario détaillé pas-à-pas comme ci-dessus, répété en rehearsal .
- Journal de bord des décisions techniques (le dossier `docs/journal/` répond à ce besoin).

### 17.2 Remise Julie — Jeu 

**Contexte** : remise physique du robot à la résidente Julie, en EHPAD. Robot reste chez elle. Alexandre récupère son enceinte perso, doit laisser une enceinte dédiée avec le robot.

**Différences avec la démo commanditaire** :
- **Enceinte dédiée sur place** (voir `DECISIONS.md §00h` pour le candidat).
- **Socle fermé final** (tronc évidé ou caisson bois), évents acoustiques 10-15 %, câbles invisibles, résidente ne peut rien débrancher ni casser.
- **Face reco apprise** sur Julie avant livraison (photos prises lors d'une visite préalable ou sur place).
- **LOCATION** configurée au nom de l'EHPAD (évite hallucinations type "Antibes, 14h32" observées avant interpolation P0-14).
- **Mode histoire prioritaire** : Julie est la cible principale pour la lecture soir, validation mode histoire sur contenu long (20-30 min) indispensable.
- **Autonomie sur site** : pas d'opérateur présent , donc toute instabilité qui demande un humain = bug critique (règle `CLAUDE.md §Contexte projet` renforcée pour EHPAD).

**Checklist J-0 (, avant départ)** :
- Stabilité T14 re-vérifiée la veille .
- Batterie pleine.
- `.env` configuré (OPENAI_API_KEY, LOCATION, TZ).
- Face reco Julie validée (embedding dans `known_faces/`).
- Wake word confirmé sur un tiers (pas Alexandre).
- Scénario de crash récupérable testé (coupure courant → redémarrage auto).
- Doc 1 page laissée à Julie avec numéro d'assistance.

### 17.3 Rehearsal Dim  — conditions réelles

**But** : dernière répétition complète, hors tout contexte développeur. Alexandre joue un résident, le robot joue en état nominal, un tiers observe la qualité perçue.

Critère PASS rehearsal = les 7 critères de la démopassent **sans intervention SSH pendant 1 h**. Si un critère tombe, on fixe ou on retire de la démo.

---

*Fin du rapport. Ce document doit évoluer mais reste la référence "comment marche Reachy Care" pour toute personne ou agent qui reprend le projet.*
