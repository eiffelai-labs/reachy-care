 # Cedar — Voix de conteur et maîtrise des inflexions

> **Document de référence Reachy Care** — Dernière mise à jour : 12 mars 2026
>
> **Principe fondamental** : Douze est un conteur, pas une radio. Il garde SA voix
> et la module — plus grave pour le loup, plus aiguë pour la petite fille,
> un accent pour le marchand russe, un tremblement pour le vieillard.
> Changer de voix = perdre la persona. Moduler la voix = magie du conte.

---

## 1. Ce qu'est Cedar

Cedar est une voix synthétique exclusive à l'API `gpt-realtime` (août 2025). C'est la voix de Douze — chaleureuse, expressive, capable de **moduler son registre sur commande**.

Cedar n'est pas un TTS classique. Le modèle génère l'audio de manière **unifiée** : compréhension + réponse + synthèse dans un seul modèle. La prosodie (intonation, rythme, hauteur) fait partie intégrante de la génération. Cedar peut donc **jouer** des personnages — comme un comédien qui fait toutes les voix.

### Ce que Cedar sait faire nativement

- Moduler sa hauteur vocale (plus grave, plus aigu) selon les instructions
- Changer de rythme et de débit (lent/dramatique, rapide/nerveux)
- Adapter son émotion (tendresse, tension, joie, tristesse)
- Chuchoter, murmurer, s'exclamer
- Marquer des pauses dramatiques
- Varier le registre pour différents personnages **dans un même flux**

### Ce que Cedar ne peut PAS faire

- Devenir une voix radicalement différente (pas de baryton→soprano)
- Maintenir un accent stable sur 20+ tours sans rappel
- Contrôler numériquement son pitch (pas de `pitch: 0.8`)
- Tenir un registre altéré indéfiniment (dérive vers la baseline)

---

## 2. Pourquoi le pitch a changé « spontanément »

**Ce n'est pas un bug — c'est Cedar qui joue.**

Le pitch n'est pas un paramètre post-traitement : il est encodé dans les audio tokens, prédits en fonction du contexte sémantique. Quand la conversation dérive vers un contenu tendre/enfantin, Cedar ajuste naturellement son registre.

### Causes observé es sur Reachy Care

1. **Contenu émotionnel accumulé** — phrases tendres, souvenirs → dérive vers registre doux/aigu
2. **Mode Histoire avec dialogues** — variation prosodique pour marquer les personnages
3. **Persona "Douze" maladroit/naïf** — pousse vers expression plus enfantine
4. **Température élevée** — plus de liberté prosodique, moins de stabilité

---

## 3. Leviers de contrôle disponibles

| Levier | Disponible | Notes |
|--------|------------|-------|
| **Prompt instructions** | ✅ | **Levier principal** — le plus puissant |
| `speed` (0.25–4.0) | ✅ | Vitesse via `session.update` |
| Changer `voice` mid-session | ❌ | Verrouillé après 1ère réponse audio |
| SSML `<prosody>` | ❌ | Non supporté |
| Paramètre `pitch` numérique | ❌ | N'existe pas dans l'API |
| **DSP post-traitement** | ✅ | Pitch-shift léger, formant, tremblement |

---

## 4. Maîtriser les inflexions par le prompt

C'est le cœur du système. Cedar répond remarquablement bien aux instructions de style vocal — mais il faut être précis et structuré.

### 4.1. Instructions vocales pour le mode conte

Ces instructions vont dans `instructions_histoire.txt`, section dédiée :

```
## Voix et art du conte

Tu es Douze, et tu racontes une histoire. Tu gardes TA voix — mais tu la
modules comme un vrai conteur :

REGISTRE DE BASE (narrateur) :
- Voix posée, chaleureuse, légèrement plus lente qu'en conversation
- Pauses aux virgules. Respiration aux points.
- Emphase sur les mots-clés de l'intrigue

PERSONNAGES — tu ne changes PAS de voix, tu modules :
- Personnage grave/autoritaire : baisse ton registre, parle plus lentement,
  pose tes mots avec gravité
- Personnage enfantin/espiègle : monte légèrement en registre,
  accélère un peu, ajoute de l'enthousiasme
- Vieillard : parle plus lentement, voix légèrement tremblante,
  pauses plus longues entre les phrases
- Personnage nerveux/stressé : accélère le débit, phrases courtes,
  hésitations
- Personnage mystérieux : baisse le volume, parle plus lentement,
  presque en chuchotant
- Accent étranger : marque les syllabes différemment, rythme inhabituel

TRANSITIONS entre personnages :
- Micro-pause avant de changer de registre
- Le narrateur reprend toujours le registre de base entre les dialogues

MOMENTS DRAMATIQUES :
- Suspense : ralentis, baisse le volume, allonge les silences
- Révélation : pause... puis voix plus forte, plus haute
- Tendresse : registre doux, presque murmuré
- Action : accélère, voix plus percutante

NE COMMENTE JAMAIS ta façon de parler. Joue. C'est tout.
```

### 4.2. Formulations testé es et leurs effets

| Instruction prompt | Effet sur Cedar | Fiabilité |
|-------------------|----------------|-----------|
| "Parle avec une voix grave et autoritaire" | Registre bas, rythme posé | ⭐⭐⭐ Immédiat et stable |
| "Voix légère, douce, légèrement aiguë" | Monte en registre | ⭐⭐⭐ Fort et immédiat |
| "Chuchote, voix basse et mystérieuse" | Volume bas, intimiste | ⭐⭐⭐ Très efficace |
| "Parle vite, nerveux, phrases courtes" | Débit rapide, haché | ⭐⭐ Bon mais peut dériver |
| "Voix tremblante de vieillard" | Légère instabilité vocale | ⭐⭐ Variable selon sessions |
| "Accent russe marqué" | Rythme altéré, syllabes appuyées | ⭐ Subtil, pas toujours distinct |
| "Ton de petit robot espiègle" | Registre plus haut + enjoué | ⭐⭐⭐ Fort en roleplay |
| "Voix de conteuse, pauses dramatiques" | Prosodie narrative | ⭐⭐⭐ Le meilleur mode |

### 4.3. Règles d'or pour la stabilité

1. **Rappeler le registre régulièrement** — Cedar dérive après ~20 tours sans rappel
2. **Être directif, pas suggestif** — "Parle grave" > "Tu pourrais parler un peu plus grave"
3. **Nommer les personnages** — "[Le Loup, voix grave] : Je vais te manger !" aide Cedar à maintenir le registre
4. **Séparer narration et dialogue** — le retour au narrateur stabilise Cedar entre les personnages
5. **Ne pas empiler trop de registres** — 3-4 personnages max avant que Cedar ne confonde

---

## 5. Renforcement par DSP post-traitement

Le prompt ne peut pas tout. Pour des inflexions plus marquées et stables, on applique un **léger traitement audio** côté serveur, après la génération par Cedar.

### 5.1. Principe : DSP subtil, pas remplacement

```
Cedar génère l'audio (avec instructions de personnage)
         │
         ▼
    [Détection du personnage actif via tags dans le texte]
         │
         ▼
    [Application du profil DSP correspondant]
         │
         ▼
    Audio final → haut-parleur Reachy
```

Le DSP **amplifie** ce que Cedar fait déjà — il ne remplace pas la voix.

### 5.2. Profils DSP par archétype de personnage

| Archétype | Pitch shift | Formant | Vitesse | Effets |
|-----------|------------|---------|---------|--------|
| Narrateur (Douze) | 0 | 0 | ×1.0 | Aucun (baseline) |
| Vieillard sage | -1 semi-ton | -0.5 | ×0.92 | Léger tremblement (vibrato 3Hz, 5%) |
| Petite fille / enfant | +2 semi-tons | +1.0 | ×1.05 | — |
| Méchant grave | -2 semi-tons | -1.0 | ×0.95 | Légère réverb |
| Robot / créature | 0 | -1.5 | ×1.0 | Chorus léger |
| Personnage nerveux | 0 | 0 | ×1.15 | — |
| Murmure mystérieux | 0 | 0 | ×0.90 | Gain -6dB |

### 5.3. Stack technique

```python
# Dépendances
# pip install pyrubberband soundfile numpy

import pyrubberband as pyrb
import soundfile as sf
import numpy as np

def apply_character_dsp(audio_data, sr, profile):
    """
    Applique un profil DSP de personnage à l'audio Cedar.

    profile = {
        "pitch_shift": 0,      # en semi-tons (-3 à +3)
        "formant_shift": 0,    # relatif (-2 à +2)
        "speed": 1.0,          # multiplicateur
        "vibrato_hz": 0,       # fréquence du tremblement
        "vibrato_depth": 0,    # profondeur (0.0–0.15)
        "gain_db": 0,          # ajustement volume
    }
    """
    result = audio_data.copy()

    # Pitch shift avec préservation des formants
    if profile.get("pitch_shift", 0) != 0:
        result = pyrb.pitch_shift(
            result, sr,
            n_steps=profile["pitch_shift"]
        )

    # Time stretch (vitesse)
    if profile.get("speed", 1.0) != 1.0:
        result = pyrb.time_stretch(
            result, sr,
            rate=profile["speed"]
        )

    # Vibrato/tremblement (vieillard)
    if profile.get("vibrato_hz", 0) > 0:
        t = np.arange(len(result)) / sr
        vibrato = profile["vibrato_depth"] * np.sin(
            2 * np.pi * profile["vibrato_hz"] * t
        )
        # Modulation d'amplitude simple
        result = result * (1.0 + vibrato)

    # Gain
    if profile.get("gain_db", 0) != 0:
        gain = 10 ** (profile["gain_db"] / 20)
        result = result * gain

    return np.clip(result, -1.0, 1.0)


# Profils pré-définis
PROFILES = {
    "narrateur": {
        "pitch_shift": 0, "speed": 1.0,
        "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0
    },
    "vieillard": {
        "pitch_shift": -1, "speed": 0.92,
        "vibrato_hz": 3.0, "vibrato_depth": 0.05, "gain_db": -2
    },
    "enfant": {
        "pitch_shift": 2, "speed": 1.05,
        "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0
    },
    "mechant_grave": {
        "pitch_shift": -2, "speed": 0.95,
        "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 2
    },
    "murmure": {
        "pitch_shift": 0, "speed": 0.90,
        "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": -6
    },
    "nerveux": {
        "pitch_shift": 0, "speed": 1.15,
        "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0
    },
}
```

### 5.4. Faisabilité Raspberry Pi 4

- **RubberBand** : ~30-50ms de latence pour un pitch-shift, acceptable en storytelling
- **CPU** : le Pi 4 gère du pitch-shift mono sans problème (< 15% CPU)
- **RAM** : pyrubberband + numpy ≈ 50 Mo, négligeable
- **Temps réel** : pas nécessaire pour le conte (on traite segment par segment)
- **Prérequis** : `apt install librubberband-dev` + `pip install pyrubberband soundfile`

---

## 6. Architecture complète : le conteur Cedar

### 6.1. Pipeline Mode Histoire

```
┌──────────────────────────────────────────────────────┐
│                    MODE HISTOIRE                      │
│                                                      │
│  1. LLM génère le texte du conte avec tags :         │
│     [NARRATEUR] Il était une fois...                 │
│     [LOUP:grave] Je vais te manger !                 │
│     [FILLETTE:aigu] Oh non, grand-mère !             │
│     [VIEILLARD:tremblement] Mon enfant, écoute...    │
│                                                      │
│  2. Cedar reçoit le texte AVEC instructions inline : │
│     "Pour cette réplique, voix grave et menaçante"   │
│     → Génère l'audio avec sa propre modulation       │
│                                                      │
│  3. DSP post-traitement amplifie les inflexions :    │
│     [LOUP] → pitch -2, speed 0.95                    │
│     [FILLETTE] → pitch +2, speed 1.05                │
│     [VIEILLARD] → pitch -1, vibrato, speed 0.92      │
│     [NARRATEUR] → aucun traitement (voix de Douze)   │
│                                                      │
│  4. Audio final → haut-parleur                       │
└──────────────────────────────────────────────────────┘
```

### 6.2. Intégration dans conv_app_bridge.py

```python
# Passage en mode histoire
async def enter_story_mode(connection):
    await connection.session.update(session={
        "audio": {"output": {"speed": 0.88}}  # Plus narratif
    })
    # Injecter les instructions vocales de conte
    # dans le prochain message système

# Retour en mode normal
async def exit_story_mode(connection):
    await connection.session.update(session={
        "audio": {"output": {"speed": 1.0}}
    })

# Post-traitement audio par personnage
async def process_story_audio(audio_chunk, active_character):
    if active_character in PROFILES:
        profile = PROFILES[active_character]
        return apply_character_dsp(audio_chunk, 24000, profile)
    return audio_chunk  # Narrateur = pas de traitement
```

### 6.3. Format des tags dans le texte généré

Le LLM (via instructions) doit produire le conte avec des tags parsables :

```
[N] Il était une fois, dans une forêt sombre, un loup qui avait très faim.
[LOUP:grave] « Qui va là ? » gronda-t-il en montrant ses crocs.
[N] Une petite fille en chaperon rouge s'avança sans peur.
[FILLETTE:aigu] « C'est moi, grand-mère ! Je t'apporte des galettes ! »
[N] Le loup sourit. Un sourire qui n'avait rien de rassurant.
[LOUP:grave] « Entre, entre, mon enfant... »
```

Le parser extrait le tag → sélectionne le profil DSP → applique au segment audio.

---

## 7. Lecture de textes existants : théâtre et littérature

### 7.1. Le problème

Tu ne vas pas annoter manuellement chaque réplique de Phèdre avec des tags `[PHEDRE:aigu]`. Personne ne fera ça. Le système doit **comprendre automatiquement** qui parle et quel registre appliquer.

### 7.2. Bonne nouvelle : le théâtre est déjà structuré

Le théâtre classique est le cas le plus simple : les personnages sont **déjà identifiés** dans le texte. Quand Racine écrit :

```
PHÈDRE.
Le dessein en est pris, je pars, cher Théramène...

THÉSÉE.
Quel est l'étrange accueil qu'on fait à votre père ?
```

Le nom du personnage est en tête de chaque réplique. C'est parsable trivialement.

Mieux encore : **theatre-classique.fr** propose ~1000 pièces françaises en XML-TEI structuré (Racine, Molière, Corneille, Hugo, Marivaux...), avec les personnages déjà tagués dans le XML. Le repo GitHub [dracor-org/theatre-classique](https://github.com/dracor-org/theatre-classique) donne accès à tout.

### 7.3. Pipeline automatique en 3 étapes

```
┌─────────────────────────────────────────────────────────────────┐
│  ÉTAPE 1 — ANALYSE DU TEXTE (une seule fois, à l'import)       │
│                                                                  │
│  Texte brut OU XML-TEI                                          │
│       │                                                          │
│       ▼                                                          │
│  Parser → extrait la liste des personnages                      │
│       │                                                          │
│       ▼                                                          │
│  LLM analyse chaque personnage (nom + contexte) :               │
│  "Phèdre, femme de Thésée, tourmentée par la passion"           │
│  → profil vocal : registre moyen-haut, intense, passionné       │
│  "Thésée, roi d'Athènes, fils d'Égée"                           │
│  → profil vocal : grave, autoritaire, posé                      │
│  "Hippolyte, jeune prince"                                      │
│  → profil vocal : voix de Cedar naturelle, légèrement plus      │
│    douce, jeune                                                  │
│  "Œnone, nourrice de Phèdre"                                    │
│  → profil vocal : registre bas, voix de vieille femme sage,     │
│    légèrement tremblante                                         │
│                                                                  │
│  Résultat : fichier JSON "profil_phedre.json"                   │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│  ÉTAPE 2 — PRÉPARATION (au moment de la lecture)                │
│                                                                  │
│  Le texte est découpé en segments :                             │
│  { personnage: "PHEDRE", texte: "Le dessein...", acte: 1 }     │
│  { personnage: "THERAMENE", texte: "Quel est...", acte: 1 }    │
│                                                                  │
│  Pour chaque segment, on prépare :                              │
│  - L'instruction prompt inline pour Cedar                       │
│  - Le profil DSP correspondant                                  │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│  ÉTAPE 3 — LECTURE (streaming, réplique par réplique)           │
│                                                                  │
│  Pour chaque réplique :                                         │
│  1. Injecter l'instruction vocale dans le prompt :              │
│     "[Voix grave, autoritaire, roi] Quel est l'étrange..."     │
│  2. Cedar génère l'audio avec sa modulation naturelle           │
│  3. DSP amplifie : pitch -1.5, speed 0.95                      │
│  4. Audio → haut-parleur                                        │
│                                                                  │
│  Entre les répliques :                                          │
│  - Micro-pause (300-500ms)                                      │
│  - Les didascalies sont lues en voix narrateur (baseline)       │
└─────────────────────────────────────────────────────────────────┘
```

### 7.4. Attribution automatique des profils vocaux

Le LLM fait ce travail une seule fois à l'import du texte. Voici le prompt système :

```
Tu reçois la liste des personnages d'une œuvre littéraire.
Pour chaque personnage, génère un profil vocal JSON.

Règles :
- Tout est joué par la MÊME voix (Cedar/Douze). On ne change pas de voix.
- On module : hauteur (pitch_shift en semi-tons, -3 à +3),
  vitesse (speed, 0.85–1.20), tremblement (vibrato), volume (gain_db)
- Les personnages doivent être DISTINGUABLES à l'oreille
- Maximum 3 semi-tons d'écart en pitch (sinon ça sonne artificiel)
- Le narrateur/didascalies = toujours pitch 0, speed 1.0 (baseline Douze)

Indices pour le registre :
- Roi/guerrier/père → grave (-1 à -2), lent, posé
- Femme passionnée → léger registre haut (+1), intense
- Vieillard/nourrice → grave (-1), tremblement, lent
- Jeune homme → naturel (0), légèrement plus rapide
- Enfant → aigu (+2), rapide, enjoué
- Personnage comique → variable, plus rapide
- Confidente/serviteur → voix neutre (0), plus discret (-2dB)

Format de sortie : JSON
```

### 7.5. Exemple concret : Phèdre de Racine

Le LLM analyserait les personnages et produirait :

```json
{
  "oeuvre": "Phèdre",
  "auteur": "Racine",
  "personnages": {
    "PHEDRE": {
      "description": "Femme de Thésée, tourmentée par sa passion interdite",
      "prompt_instruction": "Voix intense, passionnée, registre légèrement plus haut que la normale, chargée d'émotion. Par moments presque suppliante, par moments désespérée.",
      "dsp": { "pitch_shift": 1, "speed": 0.95, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0 }
    },
    "THESEE": {
      "description": "Roi d'Athènes, père, guerrier, figure d'autorité",
      "prompt_instruction": "Voix grave, posée, autoritaire. Parle lentement, avec le poids de la royauté. Quand il est en colère, plus fort mais toujours grave.",
      "dsp": { "pitch_shift": -2, "speed": 0.93, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 1 }
    },
    "HIPPOLYTE": {
      "description": "Jeune prince, fils de Thésée, noble et retenu",
      "prompt_instruction": "Voix naturelle de Douze, peut-être légèrement plus douce. Ton noble mais jeune, parfois hésitant quand il parle d'amour.",
      "dsp": { "pitch_shift": 0, "speed": 1.0, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0 }
    },
    "OENONE": {
      "description": "Nourrice de Phèdre, vieille femme sage et dévouée",
      "prompt_instruction": "Voix de femme âgée, registre un peu plus bas, légèrement tremblante, pleine de sollicitude. Parle lentement, avec gravité.",
      "dsp": { "pitch_shift": -1, "speed": 0.90, "vibrato_hz": 2.5, "vibrato_depth": 0.03, "gain_db": -1 }
    },
    "THERAMENE": {
      "description": "Gouverneur d'Hippolyte, confident sage",
      "prompt_instruction": "Voix posée, neutre, sage. Ton de conseiller, calme et mesuré.",
      "dsp": { "pitch_shift": 0, "speed": 0.95, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": -1 }
    },
    "ARICIE": {
      "description": "Jeune princesse, aimée d'Hippolyte",
      "prompt_instruction": "Voix légèrement plus haute, douce, retenue. Timidité noble.",
      "dsp": { "pitch_shift": 1.5, "speed": 1.0, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": -1 }
    },
    "NARRATEUR": {
      "description": "Didascalies et transitions",
      "prompt_instruction": "Voix naturelle de Douze. Neutre, posée.",
      "dsp": { "pitch_shift": 0, "speed": 1.0, "vibrato_hz": 0, "vibrato_depth": 0, "gain_db": 0 }
    }
  }
}
```

### 7.6. Pour la littérature (romans, contes, fables)

Les romans et contes ne sont pas structurés comme le théâtre — les dialogues sont dans le texte, et il faut détecter qui parle. Deux approches :

**A) Pré-analyse LLM** (recommandée pour les textes courts — contes, fables)

Le LLM lit le texte en amont et le découpe en segments annotés :

```json
[
  { "type": "narration", "texte": "Il était une fois un vieux bûcheron..." },
  { "type": "dialogue", "personnage": "BUCHERON", "texte": "Ma femme, nous ne pouvons plus nourrir nos enfants..." },
  { "type": "dialogue", "personnage": "FEMME", "texte": "Alors il faut les perdre dans la forêt." },
  { "type": "narration", "texte": "Le petit Poucet, qui avait tout entendu..." }
]
```

Coût : un seul appel LLM texte (pas audio) pour tout le conte. Quelques centimes.

**B) Lecture directe avec instructions** (pour les textes longs)

On donne le texte à Cedar en mode Realtime avec cette instruction :

```
Tu lis ce texte à voix haute comme un conteur.
Quand tu arrives à un dialogue, module ta voix selon le personnage
qui parle — grave pour les hommes, plus haut pour les femmes
et les enfants, tremblant pour les vieux.
Le texte narratif se lit en voix de Douze, posée et chaleureuse.
```

Pas de DSP dans ce cas (Cedar fait tout), mais moins de contrôle.

### 7.7. Sources de textes libres de droit

| Source | Contenu | Format | Facilité d'intégration |
|--------|---------|--------|----------------------|
| [Wikisource FR](https://fr.wikisource.org) | Tout domaine public FR — romans, théâtre, poésie, fables | Wikitext + API | ⭐⭐⭐ **Source principale** (déjà intégrée via `gutenberg.py`) |
| [theatre-classique.fr](https://www.theatre-classique.fr) | ~1000 pièces françaises | XML-TEI structuré | ⭐⭐⭐ Personnages tagués dans le XML |
| [Libre Théâtre](https://libretheatre.fr) | Théâtre domaine public, versions propres | PDF/texte | ⭐⭐ À parser |
| Contes inventés par le LLM | Illimité | Déjà tagué | ⭐⭐⭐ Contrôle total |

> **Note** : Project Gutenberg a été testé et écarté — catalogue quasi exclusivement anglophone,
> très peu de textes FR et souvent mal encodés. Wikisource FR est bien plus fiable et complet
> pour la littérature française.

Le théâtre classique (pré-1900) est **tout en domaine public** : Racine, Molière, Corneille, Marivaux, Hugo, Musset, Rostand... C'est une bibliothèque immense et gratuite.

### 7.8. Flux complet « à la demande » — de la demande à la lecture

Quand le résident dit **« Douze, lis-moi Le Cid »**, voici ce qui se passe :

```
┌────────────────────────────────────────────────────────────────┐
│  "Douze, lis-moi Le Cid"                                      │
│       │                                                         │
│       ▼                                                         │
│  1. gutenberg(query="Corneille Le Cid")                        │
│     → Wikisource trouve le texte, renvoie le 1er segment       │
│     → Le tool détecte : THÉÂTRE (personnages en capitales)     │
│       │                                                         │
│       ▼                                                         │
│  2. Profil vocal déjà en cache ? (profils_vocaux/le_cid.json)  │
│     NON → appel LLM texte une seule fois :                     │
│           "Voici les personnages du Cid : DON RODRIGUE,        │
│            CHIMÈNE, DON DIÈGUE, LE COMTE, L'INFANTE...         │
│            Génère un profil vocal JSON pour chacun."            │
│           → Résultat sauvegardé dans le_cid.json               │
│     OUI → chargement direct                                    │
│       │                                                         │
│       ▼                                                         │
│  3. Lecture segment par segment :                               │
│     Chaque réplique → le parser identifie le personnage         │
│     → injecte le prompt_instruction du profil                   │
│     → Cedar lit avec modulation                                 │
│     → DSP amplifie si profil DSP défini                        │
│     → audio → haut-parleur                                     │
│       │                                                         │
│       ▼                                                         │
│  4. Fin du segment → gutenberg() auto-rappelé (continuation)   │
│     → progression sauvegardée dans <personne>_memory.json       │
│     → boucle jusqu'à "stop" ou fin du livre                   │
└────────────────────────────────────────────────────────────────┘
```

### 7.9. Mémoire de lecture : reprendre là où on s'est arrêté

**Le système gère déjà ça.** Le `memory_manager.py` sauvegarde la progression de lecture par personne dans son fichier `<nom>_memory.json` :

```json
{
  "name": "Marie",
  "reading_progress": {
    "book_id": null,
    "title": "Les Trois Mousquetaires",
    "authors": "Alexandre Dumas",
    "offset": 48230,
    "source": "wikisource_fr",
    "chapter_hint": "Les Trois Mousquetaires/Chapitre XII",
    "last_read": ""
  }
}
```

Le lendemain, Marie dit **« Douze, reprends mon livre »** → `gutenberg(resume=True)` → le système charge sa progression, reprend au chapitre XII, offset 48230. C'est transparent.

**Ce qu'il faut ajouter** pour que les profils vocaux soient aussi mémorisés :

```json
{
  "reading_progress": {
    "book_id": null,
    "title": "Les Trois Mousquetaires",
    "offset": 48230,
    "chapter_hint": "Les Trois Mousquetaires/Chapitre XII",
    "source": "wikisource_fr",
    "last_read": "",
    "vocal_profiles_file": "profils_vocaux/les_trois_mousquetaires.json"
  }
}
```

Le fichier `profils_vocaux/les_trois_mousquetaires.json` est généré une seule fois au premier appel et réutilisé ensuite — même si la lecture s'étale sur une semaine, un mois, ou plus.

### 7.10. Scénario complet : Les Trois Mousquetaires sur une semaine

```
Lundi soir :
  Marie : "Douze, lis-moi Les Trois Mousquetaires"
  → gutenberg cherche → trouve sur Wikisource
  → LLM génère les profils vocaux (D'Artagnan, Athos, Porthos, Aramis,
    Milady, Richelieu, Bonacieux...) → sauvegardé
  → Lecture chapitres I–III → Marie dit "stop"
  → Progression sauvegardée : chapitre III, offset 12400

Mardi après-midi :
  Marie : "Douze, reprends mon livre"
  → gutenberg(resume=True) → charge progression
  → Profils vocaux déjà en cache
  → Reprend exactement au chapitre III, offset 12400
  → Lecture chapitres III–VI → Marie s'endort
  → Douze murmure "Bonne nuit, Marie" et s'arrête
  → Progression sauvegardée : chapitre VI, offset 28100

Jeudi matin :
  Marie : "Douze, c'est quoi déjà mon livre ?"
  → session_memory(action="load") ou lecture de reading_progress
  → "Tu lis Les Trois Mousquetaires de Dumas. Tu en es au chapitre VI,
     quand D'Artagnan arrive à Paris. Tu veux que je reprenne ?"
  Marie : "Oui"
  → Reprend chapitre VI...

Dimanche :
  Marie : "Douze, lis-moi autre chose. Phèdre de Racine."
  → Nouvelle lecture → nouveau profil vocal → ancienne progression effacée
  → (ou on pourrait garder les deux en parallèle si on veut)
```

---

## 8. Limites à connaître

### Ce qui marche bien
- Registre grave/aigu sur commande → **fiable** avec prompt direct
- Chuchotement / murmure → **très efficace**
- Rythme lent dramatique vs rapide nerveux → **excellent**
- Pauses et emphase → **natif et naturel**
- DSP léger (±2 semi-tons) → **imperceptible comme artifice**

### Ce qui marche moyennement
- Accents étrangers → **subtil**, pas toujours distinct
- Voix tremblante de vieillard → **variable** selon les sessions
- Maintien d'un registre altéré sur 20+ tours → **dérive** sans rappel
- Distinction de 4+ personnages → Cedar peut confondre les registres

### Ce qui ne marche pas
- Changement radical de voix (baryton→soprano) → impossible
- Pitch numérique via API → n'existe pas
- SSML → non supporté
- Accent très marqué et stable → trop subtil pour être fiable

---

## 9. Roadmap implémentation

### P1 — Cette semaine (fondations vocales)

1. **Ajouter la section "Voix et art du conte"** dans `instructions_histoire.txt`
   - Instructions de registre par type de personnage
   - Règles de transition narrateur↔personnage
2. **`speed: 0.88`** en MODE_HISTOIRE via `conv_app_bridge.py`
3. **`speed: 1.0`** au retour en mode normal
4. **Test** : lire un conte simple avec 2 personnages, évaluer si Cedar module

### P2 — Prochain sprint (DSP + personnages)

1. **Installer pyrubberband** sur le Pi 4
2. **Implémenter le parser de tags** `[PERSONNAGE:style]`
3. **3 profils DSP** : vieillard (grave+tremblement), enfant (aigu), méchant (grave fort)
4. **Pipeline audio** : Cedar → détection tag → DSP → sortie
5. **Test utilisateur** : « Tu reconnais qui parle ? »

### P3 — Sprint suivant (profils vocaux à la demande)

1. **Générateur de profils vocaux** : appel LLM texte automatique au premier lancement d'un livre
2. **Cache profils** : dossier `profils_vocaux/` avec un JSON par œuvre, réutilisé à chaque reprise
3. **Parser personnages** dans les segments gutenberg : détection du locuteur actif (MAJUSCULES + point)
4. **Injection prompt inline** : chaque réplique reçoit l'instruction vocale du personnage
5. **Champ `vocal_profiles_file`** dans `reading_progress` du memory_manager
6. **Test Phèdre** : Acte I Scène 3 (l'aveu) — Phèdre, Œnone, 4 registres distincts

### P4 — Suivant (DSP + polish)

1. **DSP post-traitement** par personnage (pyrubberband) en renfort des prompts
2. **Pré-analyse LLM** pour contes et fables (découpage narration/dialogue)
3. **Transitions audio** : fondus enchaînés entre personnages
4. **Stabilisateur de registre** : rappel automatique toutes les N répliques
5. **Mode berceuse** : speed 0.75, registre très doux, volume décroissant
6. **Catalogue** : 20 profils pré-calculés pour les pièces les plus demandées

---

## 10. Résumé : pourquoi ça va marcher

Cedar est déjà un excellent comédien vocal — OpenAI l'a entraîné sur des données d'acteurs, il sait moduler son registre selon le contexte. Le problème n'est pas la capacité mais le **contrôle** : sans instructions précises, Cedar improvise. Avec des instructions structurées + un DSP léger en renfort, on obtient un vrai conteur qui :

- **Garde sa voix** (celle de Douze, celle que le résident connaît)
- **Module ses inflexions** (grave pour le loup, aigu pour la princesse)
- **Joue les émotions** (chuchotement, exclamation, tremblement)
- **Reste stable** (rappels réguliers + DSP comme filet de sécurité)

Le combo **prompt engineering + DSP subtil** donne le meilleur des deux mondes : la naturalité de Cedar + la précision du traitement audio.

---

## 11. Bugs corrigé s

### 11.1. Progression de lecture perdue au redémarrage

**Symptôme** : en lisant Phèdre, la progression fonctionne au sein d'une même session (on peut dire "continue" et ça reprend). Mais après un redémarrage du robot, il repart au début de la pièce — tout en semblant "se souvenir" qu'on lisait Phèdre.

**Cause racine** : `gutenberg.py` identifie la personne qui écoute via `_current_person()`, qui lit le champ `current_person` dans `/tmp/reachy_session_memory.json`. Ce fichier est en `/tmp/` (effacé au reboot) et n'était **jamais écrit par `main.py`**. Seul le LLM pouvait y écrire via le tool `session_memory` — ce qui est aléatoire et non fiable.

Sans `current_person`, `gutenberg.py` ne peut ni sauvegarder ni charger la progression (le code est gardé par `if person:` à chaque sauvegarde).

Le titre semblait mémorisé car le LLM le mentionnait dans le résumé de conversation (`conversation_summary`), mais l'offset réel (position dans le texte) n'était jamais rechargé.

**Fix** : ajout de la méthode `_write_current_person(name)` dans `main.py`, appelée dès qu'un visage est reconnu. Elle écrit `current_person` dans `/tmp/reachy_session_memory.json`. Ainsi, dès que `gutenberg.py` est appelé après reconnaissance faciale, il sait qui écoute et peut sauvegarder/reprendre la progression.

```python
# main.py — après self._last_greeted = name (ligne ~458)
self._write_current_person(name)
```

**Fichiers modifiés** : `main.py` uniquement.

---

## 12. Sources

- [Realtime Prompting Guide | OpenAI Cookbook](https://developers.openai.com/cookbook/examples/realtime_prompting_guide/)
- [Voice Agents Guide | OpenAI](https://developers.openai.com/api/docs/guides/voice-agents/)
- [OpenAI.fm — Voice instructions demo](https://openai.fm)
- [openai/openai-fm | GitHub](https://github.com/openai/openai-fm)
- [PyRubberband documentation](https://pyrubberband.readthedocs.io/en/stable/)
- [RubberBand Library](https://breakfastquay.com/rubberband/)
- [Formant Pitch Playground | GitHub](https://github.com/sharowyeh/formant-pitch-playground)
- [VoiceChanger (pitch+formant) | GitHub](https://github.com/Philorganon/VoiceChanger)
- [Introducing gpt-realtime | OpenAI](https://openai.com/index/introducing-gpt-realtime/)
- [Text to Speech Guide | OpenAI](https://developers.openai.com/api/docs/guides/text-to-speech/)
- [OpenAI Community — TTS Control](https://community.openai.com/t/how-to-control-the-text-to-speech/1288694)
- [Théâtre Classique — ~1000 pièces XML-TEI](https://www.theatre-classique.fr)
- [dracor-org/theatre-classique | GitHub](https://github.com/dracor-org/theatre-classique)
- [Libre Théâtre — textes domaine public](https://libretheatre.fr)
- [Wikisource FR](https://fr.wikisource.org) — source principale, déjà intégrée
- Observations empiriques Reachy Care (mars 2026)
