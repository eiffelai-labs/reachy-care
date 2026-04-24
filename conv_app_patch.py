"""
conv_app_patch.py — Script de patch chirurgical pour reachy_mini_conversation_app.

À exécuter UNE SEULE FOIS sur le robot Reachy (ou la machine de développement)
pour intégrer le support des événements externes Reachy Care dans
reachy_mini_conversation_app.

Ce script est IDEMPOTENT : il peut être relancé sans risque si le patch
est déjà appliqué.

Modifications apportées
-----------------------
1. openai_realtime.py
   - Ajoute self._external_events et self._asyncio_loop dans __init__
   - Ajoute la création de la tâche _process_external_events dans
     _run_realtime_session(), juste après `async with self.connection:`
   - Ajoute les méthodes _process_external_events() et schedule_external_event()

2. main.py
   - Enregistre le handler dans le bridge Reachy Care après son instanciation

Usage
-----
    python conv_app_patch.py

Prérequis
---------
    pip install reachy-mini-conversation-app
    OU
    le code source présent dans /home/pollen/reachy_mini_conversation_app/src/
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# Marqueurs d'idempotence : si ces chaînes sont présentes, le patch est déjà là
# ---------------------------------------------------------------------------
_PATCH_MARKER_INIT   = "_external_events: asyncio.Queue = asyncio.Queue()"
_PATCH_MARKER_TASK   = "asyncio.create_task(self._process_external_events()"
_PATCH_MARKER_METHOD = "async def _process_external_events(self)"
_PATCH_MARKER_SCHED  = "async def schedule_external_event(self"
_PATCH_MARKER_BRIDGE = "conv_app_bridge"


# ---------------------------------------------------------------------------
# Code injecté dans openai_realtime.py
# ---------------------------------------------------------------------------

# Lignes à insérer dans __init__, après la dernière ligne existante d'initialisation
# (détectée par la présence de "self.connection" dans __init__)
_INIT_INJECTION = textwrap.dedent("""\
        # --- Reachy Care patch ---
        self._external_events: asyncio.Queue = asyncio.Queue()
        self._asyncio_loop = None
        # --- fin patch ---
""")

# Lignes à insérer dans _run_realtime_session(), juste après `async with self.connection:`
_SESSION_INJECTION = textwrap.dedent("""\
            # --- Reachy Care patch ---
            self._asyncio_loop = asyncio.get_event_loop()
            asyncio.create_task(self._process_external_events(), name="reachy-care-events")
            # --- fin patch ---
""")

# Deux méthodes à ajouter à la classe (avant la dernière ligne du fichier ou juste
# avant une méthode non-indentée de fermeture de classe)
_METHODS_INJECTION = textwrap.dedent("""\

    # --- Reachy Care patch ---

    async def _process_external_events(self) -> None:
        \"\"\"
        Tâche asyncio qui consomme en continu la queue _external_events
        et injecte chaque événement dans la session OpenAI Realtime.

        Tourne en arrière-plan pendant toute la durée de la session.
        \"\"\"
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.info("[Reachy Care] _process_external_events démarrée.")
        try:
            while True:
                text, response_instructions = await self._external_events.get()
                try:
                    _log.debug("[Reachy Care] Injection événement : %s", text[:80])
                    await self.connection.conversation.item.create(
                        item={
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        }
                    )
                    await self.connection.response.create(
                        response={
                            "instructions": response_instructions,
                        }
                    )
                except Exception as _exc:
                    _log.error("[Reachy Care] Erreur injection événement : %s", _exc)
                finally:
                    self._external_events.task_done()
        except asyncio.CancelledError:
            _log.info("[Reachy Care] _process_external_events annulée proprement.")

    async def schedule_external_event(self, text: str, response_instructions: str) -> None:
        \"\"\"
        Ajoute un événement externe à la queue pour injection dans la session.

        Thread-safe : peut être appelé depuis un thread synchrone via
        asyncio.run_coroutine_threadsafe(handler.schedule_external_event(...), loop).

        Paramètres
        ----------
        text                  : str
            Message à injecter comme tour utilisateur.
        response_instructions : str
            Instructions de réponse à passer à l'API OpenAI Realtime.
        \"\"\"
        await self._external_events.put((text, response_instructions))

    # --- fin patch ---
""")


# ---------------------------------------------------------------------------
# Code injecté dans main.py
# ---------------------------------------------------------------------------

_MAIN_INJECTION = textwrap.dedent("""\
    # --- Reachy Care patch ---
    try:
        import sys as _sys
        import os as _os
        # Ajouter le répertoire Reachy Care au path si nécessaire
        _reachy_care_path = _os.environ.get(
            "REACHY_CARE_PATH", "/home/pollen/reachy_care"
        )
        if _reachy_care_path not in _sys.path:
            _sys.path.insert(0, _reachy_care_path)
        from conv_app_bridge import bridge as _rc_bridge
        _rc_bridge.register_handler(handler)
        print("[Reachy Care] Bridge enregistré avec succès.")
    except ImportError as _e:
        print(f"[Reachy Care] AVERTISSEMENT : impossible d'importer conv_app_bridge : {_e}")
        print("[Reachy Care] Le bridge ne sera pas actif pour cette session.")
    # --- fin patch ---
""")


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _find_package_file(package_name: str, filename: str) -> Path | None:
    """
    Cherche un fichier appartenant à un package Python installé.

    Essaie d'abord via `pip show`, puis via importlib.
    Retourne le Path absolu si trouvé, None sinon.
    """
    # Tentative 1 : pip show
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "-f", package_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            location = None
            files_section = False
            for line in lines:
                if line.startswith("Location:"):
                    location = line.split(":", 1)[1].strip()
                if line.strip() == "Files:":
                    files_section = True
                    continue
                if files_section and filename in line:
                    rel_path = line.strip()
                    if location:
                        candidate = Path(location) / rel_path
                        if candidate.exists():
                            return candidate
    except Exception:
        pass

    # Tentative 2 : importlib — localise le package dans sys.path
    for path_entry in sys.path:
        candidate = Path(path_entry) / filename
        if candidate.exists():
            return candidate
        # Cherche récursivement jusqu'à 4 niveaux (src layouts)
        for depth in range(1, 5):
            pattern = "/".join(["*"] * depth) + "/" + filename
            matches = list(Path(path_entry).glob(pattern))
            if matches:
                return matches[0]

    return None


def _find_source_file(filename: str, search_roots: list[str]) -> Path | None:
    """
    Cherche un fichier source dans les répertoires standards de reachy_mini_conversation_app.
    """
    for root in search_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        # Cherche récursivement
        matches = list(root_path.rglob(filename))
        if matches:
            return matches[0]
    return None


def _backup(path: Path) -> Path:
    """Crée une copie de sauvegarde .bak (écrase si existe déjà)."""
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    print(f"  Backup créé : {bak}")
    return bak


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _abort(message: str) -> None:
    print(f"\nERREUR : {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Patch de openai_realtime.py
# ---------------------------------------------------------------------------

def patch_openai_realtime(filepath: Path) -> None:
    """
    Applique les trois injections dans openai_realtime.py :
    1. Dans __init__ : _external_events et _asyncio_loop
    2. Dans _run_realtime_session : création de la tâche
    3. À la fin de la classe : les deux méthodes async
    """
    content = _read(filepath)

    # ---- Vérification idempotence ----
    already_patched = all(
        marker in content
        for marker in [_PATCH_MARKER_INIT, _PATCH_MARKER_TASK, _PATCH_MARKER_METHOD, _PATCH_MARKER_SCHED]
    )
    if already_patched:
        print("  openai_realtime.py : patch déjà appliqué, rien à faire.")
        return

    _backup(filepath)
    original = content

    # ---- Injection 1 : dans __init__ ----
    # On cherche la fin du bloc __init__ en détectant la dernière assignation
    # self.XXX = YYY avant la prochaine définition de méthode `def `.
    # Stratégie : insérer juste avant la première ligne `    def ` qui suit `def __init__`.
    if _PATCH_MARKER_INIT not in content:
        # Trouver la position de `def __init__` puis la prochaine `    def `
        init_pos = content.find("    def __init__")
        if init_pos == -1:
            init_pos = content.find("def __init__")
        if init_pos == -1:
            print("  AVERTISSEMENT : __init__ introuvable dans openai_realtime.py — injection ignorée.")
        else:
            # Chercher la prochaine `    def ` (méthode suivante dans la classe)
            next_def_pos = content.find("\n    def ", init_pos + 1)
            if next_def_pos == -1:
                # Pas de méthode suivante : insérer avant la fin du fichier
                next_def_pos = len(content)
            # Insérer juste avant cette position
            content = content[:next_def_pos] + "\n" + _INIT_INJECTION + content[next_def_pos:]
            print("  openai_realtime.py : injection __init__ OK.")
    else:
        print("  openai_realtime.py : injection __init__ déjà présente.")

    # ---- Injection 2 : dans _run_realtime_session ----
    if _PATCH_MARKER_TASK not in content:
        # Chercher `async with self.connection:` dans le contexte de _run_realtime_session
        anchor = "async with self.connection:"
        pos = content.find(anchor)
        if pos == -1:
            print("  AVERTISSEMENT : 'async with self.connection:' introuvable — injection session ignorée.")
        else:
            # Trouver la fin de cette ligne
            end_of_line = content.find("\n", pos)
            if end_of_line == -1:
                end_of_line = len(content)
            content = content[:end_of_line + 1] + _SESSION_INJECTION + content[end_of_line + 1:]
            print("  openai_realtime.py : injection _run_realtime_session OK.")
    else:
        print("  openai_realtime.py : injection _run_realtime_session déjà présente.")

    # ---- Injection 3 : méthodes à la fin de la classe ----
    if _PATCH_MARKER_METHOD not in content:
        # Insérer avant la dernière ligne non-vide du fichier
        # (on suppose que la classe se termine à la fin du fichier ou avant une autre classe)
        content = content.rstrip() + "\n" + _METHODS_INJECTION
        print("  openai_realtime.py : injection méthodes OK.")
    else:
        print("  openai_realtime.py : méthodes déjà présentes.")

    if content != original:
        _write(filepath, content)
        print(f"  openai_realtime.py : fichier mis à jour ({filepath}).")
    else:
        print("  openai_realtime.py : aucune modification nécessaire.")


# ---------------------------------------------------------------------------
# Patch de main.py
# ---------------------------------------------------------------------------

def patch_main(filepath: Path) -> None:
    """
    Ajoute l'enregistrement du handler dans le bridge Reachy Care,
    juste après l'instanciation du handler dans main.py.
    """
    content = _read(filepath)

    if _PATCH_MARKER_BRIDGE in content:
        print("  main.py : patch bridge déjà appliqué, rien à faire.")
        return

    _backup(filepath)
    original = content

    # Chercher la ligne où le handler est instancié
    # Patterns courants : `handler = OpenaiRealtimeHandler(` ou `handler = ...Handler(`
    import re
    pattern = re.compile(r"([ \t]*handler\s*=\s*\w*[Hh]andler\w*\s*\([^\n]*\n)")
    match = pattern.search(content)

    if not match:
        # Fallback : chercher `handler = ` tout court
        pattern2 = re.compile(r"([ \t]*handler\s*=\s*[^\n]+\n)")
        match = pattern2.search(content)

    if not match:
        print(
            "  AVERTISSEMENT : instanciation du handler introuvable dans main.py.\n"
            "  Ajout du patch à la fin du fichier (à vérifier manuellement)."
        )
        content = content.rstrip() + "\n\n" + _MAIN_INJECTION
    else:
        insert_pos = match.end()
        content = content[:insert_pos] + _MAIN_INJECTION + content[insert_pos:]
        print("  main.py : injection bridge OK (après instanciation du handler).")

    if content != original:
        _write(filepath, content)
        print(f"  main.py : fichier mis à jour ({filepath}).")
    else:
        print("  main.py : aucune modification nécessaire.")


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("  Reachy Care — Patch de reachy_mini_conversation_app")
    print("=" * 70)

    # Répertoires de recherche (dans l'ordre de priorité)
    search_roots = [
        "/home/pollen/reachy_mini_conversation_app/src",
        "/home/pollen/reachy_mini_conversation_app",
        os.path.expanduser("~/reachy_mini_conversation_app/src"),
        os.path.expanduser("~/reachy_mini_conversation_app"),
    ]

    # Ajouter les paths du venv courant
    for p in sys.path:
        if "reachy" in p.lower() or "pollen" in p.lower():
            search_roots.append(p)

    # ---- Trouver openai_realtime.py ----
    print("\n[1/2] Recherche de openai_realtime.py …")

    realtime_path = _find_package_file("reachy-mini-conversation-app", "openai_realtime.py")

    if realtime_path is None:
        realtime_path = _find_source_file("openai_realtime.py", search_roots)

    if realtime_path is None:
        _abort(
            "openai_realtime.py introuvable.\n\n"
            "Vérifiez que reachy-mini-conversation-app est installé :\n"
            "  pip install reachy-mini-conversation-app\n"
            "OU que le code source est présent dans l'un de ces répertoires :\n"
            + "\n".join(f"  - {r}" for r in search_roots)
        )

    print(f"  Trouvé : {realtime_path}")
    patch_openai_realtime(realtime_path)

    # ---- Trouver main.py ----
    print("\n[2/2] Recherche de main.py (reachy_mini_conversation_app) …")

    # Chercher main.py dans le même répertoire que openai_realtime.py en priorité
    main_path = realtime_path.parent / "main.py"
    if not main_path.exists():
        main_path = _find_package_file("reachy-mini-conversation-app", "main.py")
    if main_path is None or not main_path.exists():
        main_path = _find_source_file("main.py", search_roots)

    if main_path is None or not main_path.exists():
        print(
            "  AVERTISSEMENT : main.py introuvable.\n"
            "  Le bridge ne sera pas enregistré automatiquement.\n"
            "  Ajoutez manuellement les lignes suivantes dans main.py,\n"
            "  juste après l'instanciation du handler :\n"
        )
        print(_MAIN_INJECTION)
    else:
        print(f"  Trouvé : {main_path}")
        patch_main(main_path)

    print("\n" + "=" * 70)
    print("  Patch terminé avec succès.")
    print("=" * 70)
    print()
    print("Prochaines étapes :")
    print("  1. Copier external_profiles/reachy_care/ vers /home/pollen/reachy_care/external_profiles/")
    print("  2. Copier .env.example vers /home/pollen/reachy_mini_conversation_app/.env")
    print("     et renseigner OPENAI_API_KEY")
    print("  3. Redémarrer reachy_mini_conversation_app")
    print("  4. Lancer Reachy Care : python /home/pollen/reachy_care/start.py")
    print()


if __name__ == "__main__":
    main()
