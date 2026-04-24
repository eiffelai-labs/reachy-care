# activities/ — modes pluggables de Reachy Care

Chaque sous-dossier est un mode d'activité déclarable à la volée. Un
`manifest.json` décrit le mode et un `module.py` optionnel peut exposer des
hooks runtime.

## Structure du manifest

```json
{
  "name": "histoire",
  "display_name": "Histoires",
  "instructions_file": "instructions_histoire.txt",
  "announce_message": "...",
  "gates": {
    "suppress_face_recognition": false,
    "silent_face_recognition": true,
    "suppress_cry_detection": true,
    "suppress_fall_detection": false,
    "head_pitch_deg": null,
    "wake_word_interrupts_reading": true
  },
  "tools": ["gutenberg", "set_reading_voice"]
}
```

- `instructions_file` pointe vers un fichier **situé dans `external_profiles/reachy_care/`**, pas dans `activities/`. Ce dossier contient le persona du robot (instructions système du LLM) et les variantes par mode. Il est privé dans notre déploiement (persona narratif spécifique à EIFFEL AI) et ne fait donc pas partie de ce dépôt.
- Pour faire tourner un clone, crée toi-même `external_profiles/reachy_care/` avec au minimum :
  - `instructions.txt` : persona système par défaut
  - `instructions_<mode>.txt` pour chaque mode que tu utilises
  - `voice.txt` (nom de la voix TTS OpenAI par défaut, par exemple `cedar`)
  - `tools.txt` (liste des outils autorisés par défaut, un par ligne)
- `gates` pilote quels capteurs sont actifs dans ce mode (reconnaissance faciale, détection de chute, détection de cri, wake word).
- `tools` liste les outils (fonctions `tools_for_conv_app/`) exposés au LLM dans ce mode.

## Modes fournis dans le dépôt

| Mode | Rôle | Tools |
|---|---|---|
| `echecs` | Partie d'échecs vocale vs Stockfish | `chess_move`, `chess_reset` |
| `histoire` | Lecture à voix haute de textes du domaine public | `gutenberg`, `set_reading_voice` |
| `musique` | Écoute partagée avec reconnaissance AudD | `identify_music`, `groove` |
| `pro` | Exposé sur un sujet donné | (aucun outil spécifique) |

## État réel

Ces modes sont fonctionnels sur notre Reachy Mini de développement. Ils ne sont
pas encore robustes pour un déploiement de production (la lecture en mode
histoire, notamment, est rudimentaire — c'est le prochain gros chantier).
