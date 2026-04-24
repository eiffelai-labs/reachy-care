# library/ — contenu narratif pour le mode histoire

Ce dossier contient les textes que Reachy Care peut lire à voix haute en mode
histoire.

Le contenu de la bibliothèque que nous utilisons en interne (un roman en cours
d'écriture d'Alexandre Ferran) est volontairement privé et ne fait pas partie
de ce dépôt. Placez ici vos propres fichiers, un dossier par œuvre, avec des
chapitres en Markdown (`01-chapitre.md`, `02-chapitre.md`, etc.).

Exemples de sources en domaine public facilement réutilisables pour démarrer :

- [Project Gutenberg, catalogue francophone](https://www.gutenberg.org/browse/languages/fr)
- [Wikisource, œuvres en français](https://fr.wikisource.org)

Le chargement de la bibliothèque est assuré par
`tools_for_conv_app/galileo_library.py` (nom hérité du projet d'Alexandre,
fonctionne avec n'importe quel dossier d'œuvres organisé en chapitres
Markdown).

Note : le mode histoire est encore très rudimentaire, c'est le prochain gros
chantier. On lit le texte via le TTS d'OpenAI avec une pauvre ponctuation
expressive par prompting. L'objectif à terme est une **direction vocale
granulaire** (pitch, accent, débit, intensité, émotion paramètre par
paramètre), qui demande des moteurs TTS contrôlables que nous ne maîtrisons
pas encore pleinement.
