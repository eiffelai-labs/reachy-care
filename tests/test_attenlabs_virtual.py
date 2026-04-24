"""
test_attenlabs_virtual.py — Terrain virtuel AttenLabs (sans Pi, sans caméra)
============================================================================
Teste les 3 blocs AttenLabs sur Mac :
  Bloc 1 : classifieur SILENT/TO_HUMAN/TO_COMPUTER (heuristique bbox)
  Bloc 2 : état d'attention en mémoire partagée (simulé)
  Bloc 3 : gate STT dans le handler de transcription (eval du code injecté)

Lance avec : python -m pytest tests/test_attenlabs_virtual.py -v
         ou : python tests/test_attenlabs_virtual.py
"""
import sys
import os
import time
import math
import types
import unittest

# ---------------------------------------------------------------------------
# Helpers : simuler les objets dont _compute_attention a besoin
# ---------------------------------------------------------------------------

def _make_mock_face(bbox, det_score=0.95):
    """Crée un objet face InsightFace minimal (duck-typing)."""
    face = types.SimpleNamespace()
    face.bbox = bbox          # [x1, y1, x2, y2]
    face.det_score = det_score
    return face


def _make_mock_app(faces):
    """Mock de recognizer._app.get(frame) → liste de faces."""
    app = types.SimpleNamespace()
    app.get = lambda frame: faces
    return app


def _make_mock_recognizer(faces):
    recognizer = types.SimpleNamespace()
    recognizer._app = _make_mock_app(faces)
    return recognizer


def _make_mock_frame(width=640, height=480):
    """Frame numpy factice (shape suffit, pas de pixels réels)."""
    import numpy as np
    return np.zeros((height, width, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Classe de test minimale qui implémente _compute_attention
# (copie exacte de ce qui a été inséré dans main.py)
# ---------------------------------------------------------------------------

class _MockConfig:
    ATTENTION_HEADING_MAX = 0.25
    ATTENTION_SIZE_MIN = 0.10


class _MockReachyCare:
    """Stub de ReachyCare avec seulement ce qu'AttenLabs utilise."""
    def __init__(self, recognizer=None):
        self.recognizer = recognizer
        self._attention_state = "SILENT"
        self._attention_history = []

    def _compute_attention(self, frame) -> str:
        import numpy as np
        if self.recognizer is None or frame is None:
            return "SILENT"
        try:
            faces = self.recognizer._app.get(frame)
        except Exception:
            return "SILENT"
        if not faces:
            return "SILENT"
        best = max(faces, key=lambda f: f.det_score)
        bbox = best.bbox
        frame_w = max(int(frame.shape[1]), 1)
        center_x = (float(bbox[0]) + float(bbox[2])) / 2.0
        bbox_w = float(bbox[2]) - float(bbox[0])
        heading = abs(center_x / frame_w - 0.5)
        size_ratio = bbox_w / frame_w
        heading_max = getattr(_MockConfig, "ATTENTION_HEADING_MAX", 0.25)
        size_min = getattr(_MockConfig, "ATTENTION_SIZE_MIN", 0.10)
        if heading < heading_max and size_ratio > size_min:
            return "TO_COMPUTER"
        return "TO_HUMAN"

    def _run_attention_update(self, frame, push_callback=None):
        """Simule la boucle AttenLabs de run() — anti-flicker + callback."""
        _att_new = self._compute_attention(frame)
        self._attention_history.append(_att_new)
        if len(self._attention_history) > 3:
            self._attention_history.pop(0)
        if (len(self._attention_history) >= 3
                and len(set(self._attention_history[-3:])) == 1
                and self._attention_history[-1] != self._attention_state):
            prev = self._attention_state
            self._attention_state = self._attention_history[-1]
            if push_callback:
                push_callback(self._attention_state)
            return True, prev, self._attention_state
        return False, self._attention_state, self._attention_state


# ---------------------------------------------------------------------------
# Tests Bloc 1 — Classifieur bbox
# ---------------------------------------------------------------------------

class TestComputeAttention(unittest.TestCase):

    def _rc(self, faces):
        return _MockReachyCare(recognizer=_make_mock_recognizer(faces))

    def test_no_recognizer_returns_silent(self):
        rc = _MockReachyCare(recognizer=None)
        frame = _make_mock_frame()
        self.assertEqual(rc._compute_attention(frame), "SILENT")

    def test_no_frame_returns_silent(self):
        rc = self._rc([_make_mock_face([220, 100, 420, 380])])
        self.assertEqual(rc._compute_attention(None), "SILENT")

    def test_no_faces_detected_returns_silent(self):
        rc = self._rc([])
        frame = _make_mock_frame()
        self.assertEqual(rc._compute_attention(frame), "SILENT")

    def test_face_centered_close_returns_to_computer(self):
        """Visage centré (heading≈0) et grand (size>0.10) → TO_COMPUTER."""
        # Frame 640px. Visage centré 180-460 = center_x=320, bbox_w=280
        # heading = |320/640 - 0.5| = 0.0  < 0.25 ✓
        # size_ratio = 280/640 = 0.4375 > 0.10 ✓
        faces = [_make_mock_face([180, 80, 460, 400])]
        rc = self._rc(faces)
        frame = _make_mock_frame(640, 480)
        self.assertEqual(rc._compute_attention(frame), "TO_COMPUTER")

    def test_face_off_center_returns_to_human(self):
        """Visage décalé sur le côté → TO_HUMAN (regarde ailleurs)."""
        # Frame 640px. Visage à droite 500-600 = center_x=550, bbox_w=100
        # heading = |550/640 - 0.5| = |0.859-0.5| = 0.359 > 0.25 ✗
        faces = [_make_mock_face([500, 80, 600, 300])]
        rc = self._rc(faces)
        frame = _make_mock_frame(640, 480)
        self.assertEqual(rc._compute_attention(frame), "TO_HUMAN")

    def test_face_centered_but_too_small_returns_to_human(self):
        """Visage centré mais trop petit (loin) → TO_HUMAN."""
        # Frame 640px. Visage centré 300-340 = center_x=320, bbox_w=40
        # heading = 0.0 ✓ mais size_ratio = 40/640 = 0.0625 < 0.10 ✗
        faces = [_make_mock_face([300, 200, 340, 280])]
        rc = self._rc(faces)
        frame = _make_mock_frame(640, 480)
        self.assertEqual(rc._compute_attention(frame), "TO_HUMAN")

    def test_best_face_selected_by_det_score(self):
        """Si plusieurs visages, prend celui avec det_score le plus élevé."""
        # Visage 1 : décentré, score=0.99
        # Visage 2 : centré, score=0.50 → non sélectionné
        face_offcenter = _make_mock_face([500, 80, 600, 300], det_score=0.99)
        face_centered  = _make_mock_face([180, 80, 460, 400], det_score=0.50)
        rc = self._rc([face_offcenter, face_centered])
        frame = _make_mock_frame(640, 480)
        # Le visage avec det_score=0.99 est décentré → TO_HUMAN
        self.assertEqual(rc._compute_attention(frame), "TO_HUMAN")

    def test_app_exception_returns_silent(self):
        """Si _app.get() lève une exception → SILENT (pas de crash)."""
        recognizer = types.SimpleNamespace()
        recognizer._app = types.SimpleNamespace()
        recognizer._app.get = lambda frame: (_ for _ in ()).throw(RuntimeError("camera error"))
        rc = _MockReachyCare(recognizer=recognizer)
        frame = _make_mock_frame()
        self.assertEqual(rc._compute_attention(frame), "SILENT")


# ---------------------------------------------------------------------------
# Tests Bloc 1 — Anti-flicker (3 prédictions consécutives)
# ---------------------------------------------------------------------------

class TestAntiFlicker(unittest.TestCase):

    def _rc_centered(self):
        """Robot avec visage centré → TO_COMPUTER."""
        return _MockReachyCare(
            recognizer=_make_mock_recognizer([_make_mock_face([180, 80, 460, 400])])
        )

    def _rc_silent(self):
        """Robot sans visage → SILENT."""
        return _MockReachyCare(recognizer=_make_mock_recognizer([]))

    def test_no_transition_before_3_frames(self):
        """État ne change pas avant 3 prédictions consécutives identiques."""
        rc = self._rc_centered()
        frame = _make_mock_frame(640, 480)
        changed, *_ = rc._run_attention_update(frame)
        self.assertFalse(changed)  # 1 frame → pas encore
        changed, *_ = rc._run_attention_update(frame)
        self.assertFalse(changed)  # 2 frames → pas encore

    def test_transition_after_3_frames(self):
        """État change exactement à la 3e prédiction consécutive identique."""
        rc = self._rc_centered()
        frame = _make_mock_frame(640, 480)
        self.assertEqual(rc._attention_state, "SILENT")
        rc._run_attention_update(frame)  # 1
        rc._run_attention_update(frame)  # 2
        changed, prev, new = rc._run_attention_update(frame)  # 3
        self.assertTrue(changed)
        self.assertEqual(prev, "SILENT")
        self.assertEqual(new, "TO_COMPUTER")
        self.assertEqual(rc._attention_state, "TO_COMPUTER")

    def test_mixed_predictions_reset_counter(self):
        """Prédictions non consécutives ne déclenchent pas de transition."""
        faces_centered  = [_make_mock_face([180, 80, 460, 400])]  # TO_COMPUTER
        faces_offcenter = [_make_mock_face([500, 80, 600, 300])]  # TO_HUMAN
        rc = _MockReachyCare(recognizer=_make_mock_recognizer(faces_centered))
        frame = _make_mock_frame(640, 480)
        rc._run_attention_update(frame)  # TO_COMPUTER
        # Changer les faces pour simuler tête tournée
        rc.recognizer = _make_mock_recognizer(faces_offcenter)
        rc._run_attention_update(frame)  # TO_HUMAN — rompt la séquence
        rc.recognizer = _make_mock_recognizer(faces_centered)
        changed, *_ = rc._run_attention_update(frame)  # TO_COMPUTER mais historique mixte
        self.assertFalse(changed)  # [TC, TH, TC] → pas homogène
        self.assertEqual(rc._attention_state, "SILENT")  # aucune transition

    def test_push_callback_called_on_transition(self):
        """Le callback (bridge.set_attention) est appelé lors d'une transition."""
        pushes = []
        rc = self._rc_centered()
        frame = _make_mock_frame(640, 480)
        for _ in range(3):
            rc._run_attention_update(frame, push_callback=pushes.append)
        self.assertEqual(pushes, ["TO_COMPUTER"])

    def test_no_push_if_no_state_change(self):
        """Pas de push si l'état ne change pas (déjà dans le bon état)."""
        rc = self._rc_centered()
        rc._attention_state = "TO_COMPUTER"  # déjà là
        frame = _make_mock_frame(640, 480)
        pushes = []
        for _ in range(3):
            rc._run_attention_update(frame, push_callback=pushes.append)
        self.assertEqual(pushes, [])  # aucune transition → aucun push


# ---------------------------------------------------------------------------
# Tests Bloc 2 — IPC /attention handler (simulation du code patch_source.py)
# ---------------------------------------------------------------------------

class TestAttentionIPCHandler(unittest.TestCase):
    """Simule le handler do_POST /attention de patch_source.py."""

    def _handle_attention_post(self, self_obj, body):
        """Copie exacte de la logique injectée dans do_POST."""
        import logging as _log_mod
        _log = _log_mod.getLogger("test_ipc")
        _att_state = body.get("state", "SILENT")
        if _att_state in ("SILENT", "TO_HUMAN", "TO_COMPUTER"):
            self_obj._attention_state = _att_state
        else:
            pass  # log warning

    def test_set_to_computer(self):
        obj = types.SimpleNamespace(_attention_state="SILENT")
        self._handle_attention_post(obj, {"state": "TO_COMPUTER"})
        self.assertEqual(obj._attention_state, "TO_COMPUTER")

    def test_set_to_human(self):
        obj = types.SimpleNamespace(_attention_state="SILENT")
        self._handle_attention_post(obj, {"state": "TO_HUMAN"})
        self.assertEqual(obj._attention_state, "TO_HUMAN")

    def test_set_silent(self):
        obj = types.SimpleNamespace(_attention_state="TO_COMPUTER")
        self._handle_attention_post(obj, {"state": "SILENT"})
        self.assertEqual(obj._attention_state, "SILENT")

    def test_invalid_state_ignored(self):
        """État inconnu ne modifie pas l'état courant."""
        obj = types.SimpleNamespace(_attention_state="TO_COMPUTER")
        self._handle_attention_post(obj, {"state": "UNKNOWN"})
        self.assertEqual(obj._attention_state, "TO_COMPUTER")

    def test_missing_state_defaults_to_silent(self):
        """body sans clé 'state' → SILENT (via .get default)."""
        obj = types.SimpleNamespace(_attention_state="TO_COMPUTER")
        self._handle_attention_post(obj, {})
        self.assertEqual(obj._attention_state, "SILENT")


# ---------------------------------------------------------------------------
# Tests Bloc 3 — Gate STT AttenLabs (logique du handler transcription)
# ---------------------------------------------------------------------------

class TestAttenLabsSTTGate(unittest.TestCase):
    """Simule la logique du gate AttenLabs dans REALTIME_SPEECH_STARTED_INJECTION."""

    def _apply_gate(self, stt_text, attention_state, is_echo_already=False):
        """
        Copie de la logique gate AttenLabs :
        Si not _is_echo et _att_state != TO_COMPUTER → _is_echo = True.
        Retourne _is_echo final.
        """
        _is_echo = is_echo_already
        _stt_text = stt_text
        if not _is_echo and _stt_text:
            _att_state = attention_state
            if _att_state != "TO_COMPUTER":
                _is_echo = True
        return _is_echo

    def test_to_computer_allows_response(self):
        """Attention=TO_COMPUTER → réponse autorisée (pas d'écho)."""
        result = self._apply_gate("Bonjour, tu vas bien ?", "TO_COMPUTER")
        self.assertFalse(result)

    def test_silent_blocks_response(self):
        """Attention=SILENT → réponse bloquée."""
        result = self._apply_gate("Bonjour, tu vas bien ?", "SILENT")
        self.assertTrue(result)

    def test_to_human_blocks_response(self):
        """Attention=TO_HUMAN → réponse bloquée."""
        result = self._apply_gate("Bonjour, tu vas bien ?", "TO_HUMAN")
        self.assertTrue(result)

    def test_already_echoed_not_double_blocked(self):
        """Si _is_echo déjà True (autre filtre), le gate ne change rien."""
        result = self._apply_gate("Bonjour", "TO_COMPUTER", is_echo_already=True)
        self.assertTrue(result)  # restes True (other filter caught it)

    def test_empty_stt_not_blocked(self):
        """STT vide → gate ne s'applique pas (stt_text falsy)."""
        result = self._apply_gate("", "SILENT")
        self.assertFalse(result)  # gate ignoré si texte vide

    def test_fallback_silent_when_no_attr(self):
        """getattr(self, '_attention_state', 'SILENT') → SILENT si pas initialisé."""
        obj = types.SimpleNamespace()  # pas d'attribut _attention_state
        _att_state = getattr(obj, "_attention_state", "SILENT")
        self.assertEqual(_att_state, "SILENT")
        result = self._apply_gate("Test", _att_state)
        self.assertTrue(result)

    def _apply_gate_with_speaking(self, stt_text, attention_state, is_speaking):
        """Gate AttenLabs avec vérification _reachy_response_free."""
        _is_echo = False
        if not _is_echo and stt_text:
            _att_state = attention_state
            if _att_state != "TO_COMPUTER":
                if not is_speaking:
                    _is_echo = True
        return _is_echo

    def test_gate_bypassed_when_robot_speaking(self):
        """AttenLabs ne coupe pas une réponse en cours (mode histoire)."""
        # Attention=SILENT mais robot parle (is_speaking=True) → gate bypassé
        result = self._apply_gate_with_speaking("chapitre deux", "SILENT", is_speaking=True)
        self.assertFalse(result)  # lecture non interrompue

    def test_gate_bypassed_when_robot_speaking_to_human(self):
        """Même comportement en TO_HUMAN : réponse en cours → non interrompue."""
        result = self._apply_gate_with_speaking("il était une fois", "TO_HUMAN", is_speaking=True)
        self.assertFalse(result)

    def test_gate_active_when_robot_idle(self):
        """Robot silencieux → gate AttenLabs actif normalement."""
        result = self._apply_gate_with_speaking("bonjour", "SILENT", is_speaking=False)
        self.assertTrue(result)

    def test_gate_active_to_computer_regardless_of_speaking(self):
        """TO_COMPUTER → jamais bloqué, même si robot parle."""
        result = self._apply_gate_with_speaking("bonjour", "TO_COMPUTER", is_speaking=True)
        self.assertFalse(result)
        result = self._apply_gate_with_speaking("bonjour", "TO_COMPUTER", is_speaking=False)
        self.assertFalse(result)  # bloqué car SILENT


# ---------------------------------------------------------------------------
# Tests d'intégration — Scénarios complets démo
# ---------------------------------------------------------------------------

class TestDemoScenarios(unittest.TestCase):
    """Simule les 4 critères de succès de la démo."""

    def _make_rc(self, face_bbox=None):
        if face_bbox:
            rc = _MockReachyCare(
                recognizer=_make_mock_recognizer([_make_mock_face(face_bbox)])
            )
        else:
            rc = _MockReachyCare(recognizer=_make_mock_recognizer([]))
        return rc

    def _stabilize(self, rc, frame, n=3):
        """Envoie n frames pour stabiliser l'état."""
        pushes = []
        for _ in range(n):
            rc._run_attention_update(frame, push_callback=pushes.append)
        return pushes

    def test_scenario_julie_regards_robot_robot_repond(self):
        """Alexandre/Julie regarde le robot → TO_COMPUTER → réponse autorisée."""
        rc = self._make_rc(face_bbox=[180, 80, 460, 400])  # centré
        frame = _make_mock_frame(640, 480)
        self._stabilize(rc, frame, 3)
        self.assertEqual(rc._attention_state, "TO_COMPUTER")
        # Gate STT → autorisé
        gate = rc._attention_state != "TO_COMPUTER"
        self.assertFalse(gate)  # robot répond

    def test_scenario_julie_detourne_regard_robot_se_tait(self):
        """Julie se tourne → TO_HUMAN → réponse bloquée."""
        rc = self._make_rc(face_bbox=[500, 80, 600, 300])  # décentré
        frame = _make_mock_frame(640, 480)
        self._stabilize(rc, frame, 3)
        self.assertEqual(rc._attention_state, "TO_HUMAN")
        gate = rc._attention_state != "TO_COMPUTER"
        self.assertTrue(gate)  # robot se tait

    def test_scenario_personne_partie_robot_silencieux(self):
        """Personne partie → SILENT → réponse bloquée."""
        rc = self._make_rc(face_bbox=None)  # pas de visage
        frame = _make_mock_frame(640, 480)
        self._stabilize(rc, frame, 3)
        self.assertEqual(rc._attention_state, "SILENT")
        gate = rc._attention_state != "TO_COMPUTER"
        self.assertTrue(gate)  # robot silencieux

    def test_scenario_retour_regard_robot_repond_nouveau(self):
        """Julie revient vers robot → TO_COMPUTER restored après 3 frames."""
        # Commence avec visage décentré
        rc = self._make_rc(face_bbox=[500, 80, 600, 300])
        frame = _make_mock_frame(640, 480)
        self._stabilize(rc, frame, 3)
        self.assertEqual(rc._attention_state, "TO_HUMAN")
        # Julie se retourne vers le robot
        rc.recognizer = _make_mock_recognizer([_make_mock_face([180, 80, 460, 400])])
        pushes = self._stabilize(rc, frame, 3)
        self.assertEqual(rc._attention_state, "TO_COMPUTER")
        self.assertIn("TO_COMPUTER", pushes)  # transition poussée vers conv_app

    def test_scenario_echo_bt_bloque(self):
        """Écho BT → STT reçu SANS que Julie regarde le robot → bloqué."""
        # Julie regarde ailleurs (TO_HUMAN)
        attention_state = "TO_HUMAN"
        stt_text = "Qu'est-ce qui germe dans ton esprit ?"  # écho french
        _is_echo = False
        if not _is_echo and stt_text:
            if attention_state != "TO_COMPUTER":
                _is_echo = True
        self.assertTrue(_is_echo)  # écho bloqué


if __name__ == "__main__":
    print("=" * 60)
    print("AttenLabs Virtual Test Suite — Reachy Care")
    print("=" * 60)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestComputeAttention,
        TestAntiFlicker,
        TestAttentionIPCHandler,
        TestAttenLabsSTTGate,
        TestDemoScenarios,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
