"""
activity_base.py — Classe de base pour les modules d'activité Reachy Care.

Chaque activité (échecs, dames, quiz...) peut optionnellement déclarer
un module Python qui hérite de ActivityModule.

Les méthodes on_enter(), on_exit(), tick() et handle_command() sont
appelées par main.py au moment approprié.
"""

import logging

logger = logging.getLogger(__name__)


class ActivityModule:
    """Classe de base pour les modules d'activité pluggables.

    Sous-classes :
        activities/echecs/module.py  → ChessActivityModule(ActivityModule)
        activities/dames/module.py   → CheckersActivityModule(ActivityModule)
    """

    name: str = ""

    def on_enter(self, context: str = "") -> None:
        """Appelé quand le mode est activé. context = topic/FEN/etc."""
        pass

    def on_exit(self) -> None:
        """Appelé quand on quitte ce mode."""
        pass

    def tick(self) -> None:
        """Appelé à chaque itération de la boucle principale (~5 Hz).

        Pour les activités qui ont besoin de polling (ex: pipeline vocal échecs).
        La plupart des activités n'ont pas besoin d'implémenter tick().
        """
        pass

    def handle_command(self, cmd: dict) -> bool:
        """Traite une commande IPC destinée à cette activité.

        Retourne True si la commande a été traitée, False sinon
        (la commande sera alors traitée par le handler générique de main.py).
        """
        return False
