"""Tests de non-régression sur patch_source.py après désempilement.

Vérifie que :
- Le fichier est du Python syntaxiquement valide
- Le marker de version clean est présent
- Les patches critiques conservés existent encore
- Les patches à supprimer sont effectivement absents
- Les méthodes essentielles (bridge IPC) sont toujours dans REALTIME_METHODS
- Les méthodes supprimées ne sont plus dans REALTIME_METHODS
- Idempotence : les variables ne sont définies qu'une seule fois
"""

import ast
import re
from pathlib import Path

PATCH_SOURCE = Path(__file__).parent.parent / "patch_source.py"


def _read():
    return PATCH_SOURCE.read_text(encoding="utf-8")


def test_patch_source_is_valid_python():
    """patch_source.py doit rester du Python syntaxiquement valide."""
    src = _read()
    ast.parse(src)  # lève SyntaxError si invalide


def test_marker_is_clean_variant():
    """Le marker d'idempotence doit être la variante clean."""
    src = _read()
    assert 'MARKER_ALREADY_PATCHED = "reachy-care-events-clean"' in src, \
        "MARKER_ALREADY_PATCHED doit être 'reachy-care-events-clean' pour isoler du patch historique"


def test_bridge_init_marker_preserved():
    """Le patch d'init bridge IPC reste présent."""
    src = _read()
    assert "REALTIME_INIT_MARKER" in src
    assert "REALTIME_INIT_INJECTION" in src
    assert "_external_events" in src, "La queue bridge IPC doit être injectée"
    assert "_start_reachy_care_server" in src, "Le serveur HTTP bridge doit être injecté"


def test_attenlabs_gate_preserved():
    """AttenLabs gate doit rester dans les injections."""
    src = _read()
    assert "AttenLabs" in src, "Le log/gate AttenLabs doit rester présent"


def test_interrupt_response_false_preserved():
    """Le fix VAD interrupt_response=False reste."""
    src = _read()
    assert "interrupt_response" in src
    assert "reachy-care-vad-fix" in src


def test_wespeakerruntime_patch_preserved():
    """Le patch ARM wespeakerruntime reste."""
    src = _read()
    assert "wespeakerruntime" in src
    assert "kaldi_native_fbank" in src


def test_moves_backoff_preserved():
    """Le patch moves.py backoff gRPC reste."""
    src = _read()
    assert "moves.py" in src
    assert "backoff" in src.lower()


def test_asoundrc_fix_preserved():
    """Le fix audio_utils .asoundrc reste."""
    src = _read()
    assert "reachy-care-asoundrc-fix" in src


def test_pip_install_editable_preserved():
    """La réinstallation editable reste (sans --force-reinstall)."""
    src = _read()
    assert 'pip", "install", "-e"' in src, "pip install -e doit rester"
    assert "--force-reinstall" not in src, "INTERDIT : pip force-reinstall"


# --- patches à supprimer ---

def test_turn_det_threshold_patch_removed():
    """Le patch turn_detection threshold 0.9 est retiré."""
    src = _read()
    assert "REALTIME_TURN_DET_MARKER" not in src
    assert "REALTIME_TURN_DET_INJECTION" not in src
    assert '"threshold": 0.9' not in src


def test_fingerprint_patches_removed():
    """Les 2 patches Fingerprint TTS/STT sont retirés."""
    src = _read()
    assert "FINGERPRINT_TTS_MARKER" not in src
    assert "FINGERPRINT_STT_MARKER" not in src
    assert "FINGERPRINT_TTS_INJECTION" not in src
    assert "FINGERPRINT_STT_INJECTION" not in src
    assert "reachy-care-fingerprint" not in src
    assert "_is_own_voice" not in src
    assert "_tts_playing_queue" not in src


def test_receive_clear_buffer_patch_removed():
    """Le patch clear buffer 150ms est retiré."""
    src = _read()
    assert "RECEIVE_CLEAR_MARKER" not in src
    assert "RECEIVE_CLEAR_INJECTION" not in src
    assert "reachy-care-echo-clear" not in src
    assert "_reachy_clear_input_buffer" not in src


def test_idle_proactive_patch_removed():
    """Le patch idle signal proactif (notre ancien patch personnes-âgées) est retiré."""
    src = _read()
    assert "REALTIME_IDLE_MSG_MARKER" not in src
    assert "REALTIME_IDLE_INSTR_MARKER" not in src
    assert "REALTIME_IDLE_TIMEOUT_MARKER" not in src
    assert "personnes âgées" not in src, "Le prompt idle proactif doit être retiré"


def test_no_idle_signal_patch_present():
    """Un patch doit désactiver send_idle_signal vanilla.

    Sans ce patch, le code vanilla Pollen (emit() dans openai_realtime.py)
    appelle send_idle_signal toutes les 15s quand idle_duration > 15.0, ce
    qui force un tool_choice='required' au LLM et fait bouger le robot seul.
    Découverte pendant T14 julie-demo-clean + Issue GitHub Pollen #284.
    """
    src = _read()
    assert "reachy-care-no-idle-signal" in src, \
        "Le patch désactivant send_idle_signal doit être présent"
    assert "NO_IDLE_MARKER" in src
    assert "NO_IDLE_REPLACEMENT" in src


def test_hp_300hz_filter_removed():
    """Le filtre passe-haut 300 Hz est retiré."""
    src = _read()
    assert "passe-haut 300Hz" not in src
    assert "_cutoff = 300" not in src
    assert "highpass" not in src.lower()


def test_cedar_gate_removed():
    """Le Cedar gate est retiré."""
    src = _read()
    assert "_cedar_gate_audio" not in src
    assert "CedarGate" not in src
    assert "_cedar_audio_buffer" not in src


def test_language_fr_forced_removed():
    """La langue FR forcée dans transcription est retirée."""
    src = _read()
    assert "langue de transcription FR" not in src
    assert '"language": "fr"' not in src
    # auto-detect = ne rien passer ou None


def test_noise_reduction_disable_removed():
    """Le patch qui désactive noise_reduction est retiré."""
    src = _read()
    assert "désactiver l'appel noise_reduction" not in src
    assert "_reachy_set_noise_reduction" not in src


def test_custom_vad_wrappers_removed():
    """Les 4 wrappers VAD custom (bt/usb/sleep/wake) sont retirés."""
    src = _read()
    for method in ("_reachy_bt_vad", "_reachy_usb_vad", "_reachy_sleep_vad", "_reachy_wake_vad"):
        assert method not in src, f"{method} doit être retiré"


def test_no_duplicated_method_definitions_in_methods_string():
    """Pas de méthode définie en double dans REALTIME_METHODS (doublon audit K)."""
    src = _read()
    # On compte les def ... dans l'ensemble du fichier. Pour les méthodes injectées
    # dans REALTIME_METHODS, il ne doit y avoir qu'une seule occurrence.
    for method in ("_process_external_events", "schedule_external_event",
                   "schedule_session_update", "cancel_current_response"):
        count = len(re.findall(rf"def {method}\b", src))
        assert count == 1, f"{method} défini {count}× (attendu : 1)"


def test_line_count_reduced():
    """Le fichier doit avoir significativement diminué."""
    src = _read()
    n_lines = src.count("\n")
    assert n_lines < 1200, f"patch_source.py fait {n_lines} lignes (attendu < 1200, était 1553)"
    assert n_lines > 500, f"patch_source.py fait {n_lines} lignes (attendu > 500, sinon trop coupé)"
