"""
camera.py — Outil de vision pour Reachy Care.

Port exact du tool Pollen vanilla : prend un frame du CameraWorker,
l'encode en JPEG base64, et retourne {"b64_im": ...}.
Le handler dans openai_realtime.py envoie ensuite l'image à OpenAI
via conversation.item.create type=input_image.
"""

import base64
import logging
from typing import Any, Dict

import cv2

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)


class Camera(Tool):
    """Take a picture with the camera and ask a question about it."""

    name = "camera"
    description = "Take a picture with the camera and ask a question about it."
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask about the picture",
            },
        },
        "required": ["question"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        question = (kwargs.get("question") or "").strip()
        if not question:
            return {"error": "question must be a non-empty string"}

        logger.info("Tool call: camera question=%s", question[:120])

        camera_worker = getattr(deps.robot, "camera_worker", None) if deps.robot else None
        if camera_worker is None:
            logger.error("Camera worker not available")
            return {"error": "Camera worker not available"}

        frame = camera_worker.get_latest_frame()
        if frame is None:
            logger.error("No frame available from camera worker")
            return {"error": "No frame available"}

        success, buffer = cv2.imencode(".jpg", frame)
        if not success:
            return {"error": "Failed to encode frame as JPEG"}

        b64_encoded = base64.b64encode(buffer.tobytes()).decode("utf-8")
        logger.info("Camera frame encoded: %d bytes JPEG, %d bytes base64",
                     len(buffer), len(b64_encoded))
        return {"b64_im": b64_encoded}
