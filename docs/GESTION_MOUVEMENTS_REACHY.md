# Gestion des mouvements de Reachy Mini — notes critiques

> Document créé le  après une demi-journée perdue à chercher la cause du bug "tête en arrière". À lire AVANT toute intervention sur la vision, la tête, le body_yaw ou les antennes.

---

## Où vit vraiment le code de mouvement (CRITIQUE)

Il existe **6 copies** de `moves.py` et `camera_worker.py` sur le Pi. UN SEUL est exécuté par conv_app_v2.

**Le VRAI chemin exécuté** (via pip editable install) :
- `/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/moves.py`
- `/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/camera_worker.py`
- `/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/audio/head_wobbler.py`

**Les autres copies** (miroirs de lecture, NON exécutés par conv_app_v2) :
- `conv_app_v2/pollen_src/moves.py` (repo Mac)
- `conv_app_v2/pollen_src/_external/...` (clone vanilla)
- `/venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini_conversation_app/` (autre venv)

**Pour vérifier le vrai chemin avant TOUT fix** :
```bash
ssh pollen@reachy-mini.local "cd /home/pollen/reachy_care && \
  PYTHONPATH=/home/pollen/reachy_care:/home/pollen/reachy_care/conv_app_v2 \
  /venvs/apps_venv/bin/python -c \
  'from reachy_mini_conversation_app import moves; print(moves.__file__)'"
```

**head_tracker.py** vit dans un autre package, chemin réel :
- `/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini_toolbox/vision/head_tracker.py`

---

## L'architecture des 2 sources de commande head

### Source 1 — MovementManager (conv_app_v2 via SDK Pollen)

- `MovementManager.working_loop` tourne à 100 Hz (décorateur dans `moves.py`).
- Appelle `self.current_robot.set_target(head, antennas, body_yaw)` à chaque tick.
- La pose `head` est composée via `compose_world_offset(primary_pose, secondary_pose)`.

**Primary pose** = `BreathingMove` (oscillation respiration naturelle) :
- Légère amplitude pitch/roll (simule respiration)
- `BreathingMove: antenna_sway patched to 0.0` dans conv_app_v2 (désactivé antennes pour éviter jitter)
- Contribue un pitch de **+0.10 à +0.16 rad (6-9° vers le bas)** en permanence (pose "vivante" typique)

**Secondary pose** = combinaison additive :
- `face_tracking_offsets` (venus de CameraWorker, voir ci-dessous)
- `speech_offsets` (venus de HeadWobbler, quand Reachy parle)

### Source 2 — play_move via API daemon

- Commandes longues bloquantes : `wake_up`, `goto_sleep`, gestes custom.
- Via `POST /api/move/play/{name}` ou `POST /api/move/play/wake_up`.
- **Pendant qu'un play_move tourne, TOUS les set_target sont ignorés** avec le WARNING :
  ```
  Ignoring set_target command: a move is currently running
  ```
- Idem `set_antennas` et `set_body_yaw`.
- Une cascade de ce warning dans les logs = move long en cours (~2-3 s pour wake_up).

---

## CameraWorker + face_tracking_offsets

### Chaîne de calcul

```
MediaPipe FaceMesh.process(frame RGB contiguous)
  → eye_center normalisé [-1, 1]
  → pixels (u, v) dans l'image
  → look_at_image(u, v) → pose 4x4 (translation + rotation)
  → translation *= 0.6 ; rotation *= 0.6  (scale FOV)
  → face_tracking_offsets = [tx, ty, tz, roll, pitch, yaw]
```

### Pièges majeurs identifiés 

1. **BGR → RGB obligatoire, avec copy()** :
   - Le frame arrive en BGR (OpenCV native).
   - MediaPipe attend du RGB.
   - `frame[:, :, ::-1]` seul crée une VIEW non-contiguë → crash MediaPipe :
     ```
     ValueError: Reference mode is unavailable if 'data' is not c_contiguous.
     ```
   - **Fix validé** : `frame[:, :, ::-1].copy()`.

2. **`min_detection_confidence` Pollen par défaut = 0.05** — ultra permissif, hallucine des visages sur zones lumineuses (plafond, fenêtre contre-jour) et sur mains/peau dans le cadre.
   - 0.5 → encore quelques hallucinations.
   - 0.85 → trop strict, rate les vrais visages à distance EHPAD.
   - **0.6** → compromis validé terrain .

3. **Boucle de rétroaction positive en yaw** (découvert ) :
   - Si user à plus de ±30° du centre caméra, `look_at_image` calcule une rotation → Reachy tourne → user encore plus décentré (caméra a bougé) → rotation se cumule → dérive infinie → Reachy finit par tourner le dos et s'accrocher sur un fantôme du décor.
   - **Fix possible** : clamp yaw ±0.5 rad + baisse scale 0.6 → 0.3. Non appliquécar comportement acceptable en conditions EHPAD réalistes (résident face à Reachy, pas à 90°).

### Conventions de signe (convention scipy "xyz" Euler)

- **pitch positif = tête vers le bas** (menton rentré).
- **pitch négatif = tête en arrière** (menton en l'air, regarde plafond).
- **yaw positif = tête à droite** (perspective user face à Reachy).
- **yaw négatif = tête à gauche**.

Dans `camera_worker.py`, le clamp `max(rotation[1], 0.0)` interdit physiquement toute tête en arrière (pitch < 0).

---

## HeadWobbler (speech_offsets)

- Dans `/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/audio/head_wobbler.py`
- Convertit l'audio PCM de Reachy qui parle en micro-mouvements head.
- Utilise `SwayRollRT` qui génère des oscillations `sin()` symétriques autour de zéro.
- Amplitude MAX : `SWAY_A_PITCH_DEG = 4.5°` (très petit).
- **Ne peut PAS expliquer une tête qui part en arrière** (amplitude trop faible, symétrique).

**Bug subtil** : `reset()` remet à zéro `_generation`, `_base_ts`, `_hops_done`, mais **n'émet pas un offset zéro final**. Donc le dernier offset pitch/roll/yaw reste appliqué dans le MovementManager tant que rien ne le remplace. En pratique ce bug est masqué parce que le MovementManager compose en continu avec la primary pose qui domine.

---

## API daemon utile pour debug

| Route | Usage |
|---|---|
| `GET /api/state/present_head_pose` | Position réelle de la tête live (x, y, z, roll, pitch, yaw) |
| `GET /api/state/full` | État complet : head_pose + body_yaw + antennas_position + doa |
| `GET /api/move/running` | Liste des moves actifs dans le daemon. `[]` = aucun |
| `POST /api/move/stop` | Stoppe le move en cours |
| `GET /api/daemon/status` | État global + control_loop_stats |
| `POST /api/move/play/wake_up` | Déclenche un wake_up (bloquant ~3 s) |
| `POST /api/move/play/goto_sleep` | Endors Reachy |

Ces routes sont essentielles pour différencier "conv_app pilote mal" vs "daemon tient une pose résiduelle".

**Route à NE PAS appeler** : `/api/move/goto_target` (main.py le fait à tort → 404, la vraie route est `/api/move/goto`).

---

## Pièges mémorables

1. **`.pyc` plus récent que `.py`** → Python charge la version compilée sans notre patch. **Toujours `sudo find / -name __pycache__ -type d | xargs rm -rf` avant restart**.

2. **Le service peut rester `active` avec un module cassé** si l'import est lazy ou catché silencieusement (`except` du RobotLayer qui catch `ImportError`). Toujours vérifier les logs pour `WARNING MovementManager not available: ...`.

3. **`reachy-care-conv.service` utilise `/venvs/apps_venv/bin/python`** (pas mini_daemon). Les fixes sur mini_daemon venv n'ont aucun effet sur conv_app_v2. Toujours vérifier le venv ciblé par `ExecStart`.

4. **Un `sed` avec `\n` dans un heredoc SSH devient un vrai newline** qui casse les f-strings Python. Utiliser Python one-liner à la place pour les remplacements multi-lignes, ou `chr(10)` au lieu de `\n`.

5. **`os.linesep` dans une fonction Python nécessite `import os` dans le scope**. Ma ligne injectée utilisait `os.linesep` alors que moves.py importait `os as _os_dbg` — NameError silencieux → toute la fonction `_issue_control_command` crashée → **MovementManager ne pilotait plus la tête** → illusion de "la tête bouge toute seule".

---

## Instrumentation laissée en place ( — À NETTOYER AVANT DÉMO )

Dans `/home/pollen/reachy_mini_conversation_app/src/reachy_mini_conversation_app/moves.py` :
- **Ligne ~35** : `import os as _os_dbg; open("/tmp/moves_imported.log", "a").write(...)` → log au module import.
- **Ligne ~641-644** : log `SET_TARGET pitch=X R21=Y ty=Z tz=W` dans `/tmp/head_debug.log` à 100 Hz.
- **Ligne ~718** : log `FACE_OFFSETS (tx, ty, tz, roll, pitch, yaw)` dans `/tmp/head_debug.log` à 60 Hz.

Pour retirer : remettre les 3 blocs `try/except` et l'assignation `self.state.face_tracking_offsets = offsets` dans leur état original, ou restaurer `moves.py` depuis `pip install --force-reinstall reachy_mini_conversation_app` (attention : écrase aussi camera_worker et autres fichiers).

**Fichiers temporaires à nettoyer** : `/tmp/head_debug.log`, `/tmp/moves_imported.log`, backups `.bak_*` dans `/tmp/`.

---

## ⚠️ RUSTINES TEMPORAIRES À RETIRER DÈS QUE look_at_image EST COMPRIS

**Principe** (feedback Alexandre , cf `feedback_pas_de_brides_rustines.md`) : ne pas empiler de limit/clamp/scale tant qu'on n'a pas compris la mécanique sous-jacente. Une rustine masque la vraie cause sans la résoudre.

Deux brides ont été ajoutées  pour stabiliser le suivi yaw avant démo commanditaire **elles sont explicitement provisoires** :

| Rustine | Fichier:ligne | Pourquoi elle existe | À creuser pour la retirer |
|---|---|---|---|
| Clamp yaw `max(-0.5, min(0.5, rotation[2]))` sur les 2 occurrences | `camera_worker.py:170, 223` (vrai chemin) | Boucle de rétroaction positive : user à plus de ±30° → Reachy tourne → user encore plus décentré → dérive infinie → Reachy tourne le dos | **Étudier `look_at_image`** : est-ce que la pose retournée est absolue (dans repère robot) ou relative (delta depuis position courante) ? Le scale *0.6 est-il appliqué sur une absolue ou une relative ? Le feedback caméra après rotation modifie-t-il la perception du centre image ? |
| Scale translation/rotation `0.6 → 0.3` | `camera_worker.py:~168-169` (vrai chemin) | Amortit le mouvement par 2 pour réduire l'amplitude des dérapages | Probablement lié au même problème de convention absolu/relatif. Si la pose est relative et appliquée cumulativement, le scale contrôle le gain de la boucle. |

**Justifications du clamp pitch `max(x, 0.0)` (gardé, PAS une rustine)** : c'est une contrainte physique explicite (cou Reachy Mini ne doit pas être en rétroflexion prolongée) + défense contre les hallucinations MediaPipe résiduelles qui pourraient envoyer un pitch négatif même à seuil 0.6.

**Investigation à mener dans une session dédiée** :
1. Lire `reachy_mini.utils.look_at_image` (probablement dans le SDK Pollen `/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/utils/`)
2. Comprendre son retour : matrice de pose absolue ou delta ?
3. Comprendre l'interaction avec `compose_world_offset(primary_pose, secondary_pose)` dans `moves.py`
4. Regarder les issues GitHub `pollen-robotics/reachy_mini_conversation_app` sur "head tracking drift" ou "face tracking runaway"
5. Si la boucle positive est confirmée dans look_at_image, fix upstream + PR Pollen plutôt que clamp local

## Chronologie des fixes appliqué s  (dans l'ordre)

1. **AGC XMOS OFF** (`PP_AGCONOFF=0`) dans `start_all.sh §2b` — pour wake word, rien à voir tête.
2. **Canal ASR XMOS** (`AEC_ASROUTONOFF=1`, `AUDIO_MGR_OP_L/R=[7,3]`) dans `start_all.sh §2b` — pour wake word.
3. **HDR IMX708** (`v4l2-ctl wide_dynamic_range=1`) dans `start_all.sh §4e` — caméra contre-jour.
4. **BGR → RGB avec `.copy()`** dans `camera_worker.py` ligne 132 du VRAI chemin.
5. **min_detection_confidence 0.05 → 0.6** dans `head_tracker.py:18` des 2 venv + persisté dans `start_all.sh §4g`.
6. **Clamp pitch `max(rotation[1], 0.0)`** sur les 2 occurrences du VRAI `camera_worker.py` (lignes 170 et 223) + persisté dans `start_all.sh §4g`.

Tous les `sed` de `start_all.sh §4g` sont idempotents pour survivre aux pip reinstalls du SDK Pollen.

---

## Done vérifiable (Karpathy §4)

Validé terrain par Alexandre~18h :
- ✅ Tête ne part plus au plafond en contre-jour fenêtre.
- ✅ Tête ne part plus en arrière quand utilisateur s'approche.
- ✅ Tête ne part plus en arrière quand mains devant les yeux.
- ✅ Reachy suit Alexandre (face + body_yaw actifs).
- ⚠ Reachy peut encore déraper à ±90° du centre si user se place très latéralement (boucle de rétroaction yaw non corrigée).
- ⚠ Conv perdue quand Reachy tourne le dos suite à cette dérive.

**À surveiller pour la démo** : rester en position frontale vis-à-vis du robot. Si usage EHPAD implique latéralisation, appliquer clamp yaw ±0.5 rad + scale 0.3.
