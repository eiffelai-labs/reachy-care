# reachy-care

Un compagnon robotique pour personnes âgées isolées à domicile, bâti sur **Reachy Mini** de Pollen Robotics / Hugging Face.

Développé par [EIFFEL AI](https://eiffelai.io), en partenariat de distribution avec TAVIE (SAS Longévie).

> ⚠️ **Statut sincère du projet : premier prototype, très jeune.**
> Ce dépôt contient ce qu'on a construit à ce jour : du code audio embarqué sur Raspberry Pi,
> dérivé de la `reachy_mini_conversation_app` de Pollen Robotics, et un premier assemblage
> d'outils autour d'OpenAI Realtime et Whisper. On y va étape par étape.
> Ce qui nous intéresse, c'est où ça va.

## Qui fait quoi

- Alexandre Ferran, co-fondateur d'EIFFEL AI, 33 ans de métier au théâtre (direction d'acteurs, régie d'orchestre à la Maison de la Radio, au CNSMD, à l'Opéra-Comique, aux Arts Florissants), écrit la vision et pilote la direction artistique et produit. Le code a été développé en pair-programming intensif avec les agents Claude d'Anthropic, à partir de cette vision, et relu session par session.
- L'équipe Pollen Robotics a créé le robot Reachy Mini et sa `reachy_mini_conversation_app`, dont ce projet est une dérivation. Leur travail est la fondation, pas un détail.
- L'équipe TAVIE (Longévie) prend en charge la distribution et la relation utilisateur en silver économie.

## Ce que le projet fait aujourd'hui

- Une boucle conversationnelle via **OpenAI Realtime**, greffée sur le SDK Reachy Mini, avec un contrôle d'attention (présence d'un visage, direction du regard, parole) pour éviter les déclenchements parasites dans un appartement vide.
- Un **mode histoire** très rudimentaire, qui lit un texte à voix haute via le TTS d'OpenAI, avec une première tentative de ponctuation expressive par prompting. C'est le point de départ, pas l'aboutissement.
- Quelques **activités** branchées sur la même conversation : une partie d'échecs vocale via Stockfish, une reconnaissance musicale, un jeu de questions.
- Une **reconnaissance faciale** locale (InsightFace `buffalo_s`), une **détection de chute** (MediaPipe Pose), une **reconnaissance de locuteur** (WeSpeaker).
- Un **mot-clé de réveil** «Hey Reachy», porté sur le canal ASR natif du circuit audio XMOS du Reachy Mini.
- Un **tableau de bord web** (Flask, port 8080) pour configurer le robot sur place. ⚠️ Ce dashboard n'a pas d'authentification dans l'état actuel, voir `modules/dashboard.py` pour les précautions avant tout usage réel.

## Où nous voulons aller, et pourquoi nous publions

Notre ambition de fond, c'est une **voix expressive dirigée**, pas une voix synthétique standard. Diriger une voix de synthèse comme on dirige un comédien : pitch, accent, débit, intensité, émotion paramètre par paramètre. Nous appelons ça, par paresse verbale et parce que ça nous aide à tenir le cap, la *direction vocale granulaire*.

**Ce dépôt ne contient pas encore ce moteur.** On y accède aujourd'hui par l'API d'ElevenLabs v3 dans nos tests internes, et nous cherchons des partenariats avec les équipes qui développent les TTS contrôlables de nouvelle génération : Mistral (Voxtral), Hugging Face (Parler-TTS), ElevenLabs, Pollen Robotics pour la coordination bas-niveau. Si vous travaillez sur ces briques et que le cas d'usage compagnon pour personnes âgées vous parle, écrivez-nous.

## Ce qui n'est PAS dans ce dépôt

Pour protéger la vie privée des personnes accompagnées et pour éviter d'exposer de l'infra qui n'est pas pertinente en open-source, plusieurs briques restent privées chez EIFFEL AI :

- Les visages, les prénoms, les journaux quotidiens des personnes âgées que Reachy Care accompagne (le dossier `known_faces/` est volontairement gitignored).
- Les profils de voix, personas narratifs et prompts systèmes que nous utilisons en interne (`external_profiles/`).
- L'infrastructure d'accès distant (VPN, tunnel HTTPS, configuration DNS) utilisée par TAVIE pour superviser les déploiements.
- Nos journaux de session, audits techniques internes, notes de debug quotidiennes.

Si vous voulez déployer une flotte de Reachy Care chez de vrais utilisateurs, contactez-nous. Nous ne prétendons pas fournir un produit clé-en-main ici : c'est de la R&D partagée.

## Matériel

- Un **Reachy Mini** de Pollen Robotics / Hugging Face. Plateforme robotique open-source (~299 $). https://huggingface.co/pollen-robotics
- Le Raspberry Pi 5 embarqué par Reachy Mini, avec son circuit audio XMOS.
- Une enceinte USB externe (nous utilisons aujourd'hui une Anker PowerConf S3 pour couvrir une chambre).

## Dépendances logicielles

- Python 3.11+
- `reachy_mini` SDK (Pollen Robotics, sur PyPI)
- `reachy_mini_conversation_app` (Pollen Robotics, utilisée comme fondation)
- Une clé API OpenAI (Realtime + Whisper)
- Une clé AudD facultative pour la reconnaissance musicale (https://audd.io)

Installation rapide :

```bash
git clone https://github.com/eiffelai-labs/reachy-care.git
cd reachy-care
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config_local.py.example config_local.py    # puis remplir les clés API
```

Le fichier `config_local.py` reste local et n'est jamais commité. Il surcharge les valeurs par défaut de `config.py` (OpenAI, Groq, Brave Search, Hugging Face, AudD, Telegram, LOCATION).

## Structure

| Dossier / fichier | Rôle |
|---|---|
| `main.py` | Boucle principale : vision, wake word, modes, dashboard embarqué |
| `conv_app_v2/` | Couche conversation, dérivée de `reachy_mini_conversation_app` de Pollen |
| `activities/` | Activités pluggables (échecs, musique, jeux de questions, mode histoire) |
| `library/` | Contenu narratif lu en mode histoire (privé, remplir avec vos textes) |
| `modules/` | Modules transverses (reconnaissance faciale, attention, mémoire, dashboard Flask) |
| `tools_for_conv_app/` | Outils exposés au LLM (tool use OpenAI Realtime) |
| `phase1_chess_vision/` | Pipeline de vision d'échiquier (early) |
| `resources/dashboard/` | Front-end du tableau de bord web (HTML + CSS + JS vanilla) |
| `resources/systemd/` | Unités systemd pour orchestrer les 3 services sur le Pi |
| `reachy_controller.py` | Service systemd Flask :8090, endpoints power / wifi / captive portal |
| `tests/` | Tests unitaires (embryon, à étoffer) |

## Vocabulaire interne

Quelques termes récurrents dans le code que le README ne documente pas autrement :

- **AttenLabs** : notre module d'attention visuelle (`modules/attention.py`). Il classifie à partir de la bbox du visage détecté si la personne regarde le robot (état `TO_COMPUTER`), regarde ailleurs (`TO_HUMAN`) ou est absente (`SILENT`). Ce n'est pas une dépendance tierce, c'est un module interne.
- **Gate** : on appelle gate les gardes qui coupent ou laissent passer un flux (audio, texte, décision d'activation). Par exemple la "speaking gate" empêche le micro de capter pendant que le robot parle, pour éviter les boucles d'écho.
- **AEC XMOS** : l'annulation d'écho matérielle intégrée au circuit audio XMOS du Reachy Mini. Elle fait une partie du boulot que ferait `WebRTC VAD` côté logiciel, en amont.

## Quelques documents pour comprendre

- [`ARCHITECTURE_HYBRIDE.md`](ARCHITECTURE_HYBRIDE.md), une note prospective sur une évolution envisagée Pi + workstation. Pas implémentée à ce jour, ne pas la lire comme l'architecture actuelle.
- [`docs/FONCTIONNEMENT_REACHY_CARE.md`](docs/FONCTIONNEMENT_REACHY_CARE.md), description du runtime réel sur le Pi.
- [`docs/GESTION_MOUVEMENTS_REACHY.md`](docs/GESTION_MOUVEMENTS_REACHY.md), notes sur les commandes moteur.
- [`docs/VOIX_CEDAR_MODE_HISTOIRE.md`](docs/VOIX_CEDAR_MODE_HISTOIRE.md), notes sur la voix Cedar et les premiers essais de mode histoire.

## Licence

Apache License 2.0. Voir [`LICENSE`](LICENSE).

Nous avons choisi Apache 2.0 parce qu'il est compatible avec la licence du code Pollen dont nous dérivons, et parce que le patent grant est utile pour un projet qui pourrait, un jour, toucher à des usages cliniques ou paramédicaux.

## Remerciements

- **Pollen Robotics** pour Reachy Mini et pour `reachy_mini_conversation_app`, sans lesquels ce projet n'existerait pas.
- **Hugging Face** pour avoir accueilli Pollen en avril 2025 et maintenu la plateforme ouverte, et pour le programme d'accès à OpenAI négocié pour ses membres.
- **Anthropic**, dont les agents Claude ont tapé l'essentiel du code avec nous, session après session.
- **OpenAI**, dont l'API Realtime rend possible une conversation fluide avec un robot embarqué.
- Tous les collègues de silver économie, ergothérapeutes, aides-soignantes, proches aidants qui nous ont aidés à calibrer ce que devait être un bon compagnon. Les premières utilisatrices sauront se reconnaître.

## Contact

Site : https://eiffelai.io
Contact : contact@eiffelai.io

Nous cherchons activement des partenariats techniques autour de la synthèse vocale contrôlable. Si vous êtes chez Mistral, Hugging Face, ElevenLabs, Pollen Robotics, ou ailleurs dans cet écosystème, n'hésitez pas.
