"""Tool loader with shim for Pollen imports.

Installs a fake `reachy_mini_conversation_app.tools.core_tools` module so that
existing tools in tools_for_conv_app/ can be imported without the Pollen SDK.
"""
import importlib
import logging
import sys
import types
from pathlib import Path

logger = logging.getLogger(__name__)


def _install_shim():
    """Shim so `from reachy_mini_conversation_app.tools.core_tools import Tool` uses our classes."""
    from .base import Tool, ToolDependencies

    pkg = types.ModuleType("reachy_mini_conversation_app")
    pkg.__path__ = []
    tools_pkg = types.ModuleType("reachy_mini_conversation_app.tools")
    tools_pkg.__path__ = []
    core = types.ModuleType("reachy_mini_conversation_app.tools.core_tools")
    core.Tool = Tool
    core.ToolDependencies = ToolDependencies

    sys.modules.setdefault("reachy_mini_conversation_app", pkg)
    sys.modules.setdefault("reachy_mini_conversation_app.tools", tools_pkg)
    sys.modules.setdefault("reachy_mini_conversation_app.tools.core_tools", core)


def load_tools(tools_dir: Path, tools_list: list[str]) -> dict:
    """Load tools by name from the tools directory.

    Returns dict mapping tool name -> Tool instance.
    Skips tools whose file doesn't exist (Pollen native tools).
    Logs a warning for tools that fail to import.
    """
    _install_shim()

    tools_dir_str = str(tools_dir)
    if tools_dir_str not in sys.path:
        sys.path.insert(0, tools_dir_str)

    loaded = {}
    for tool_name in tools_list:
        py_file = tools_dir / f"{tool_name}.py"
        if not py_file.exists():
            logger.debug("Tool %s: no file, skipping (Pollen native tool).", tool_name)
            continue
        try:
            mod = importlib.import_module(tool_name)
            from .base import Tool
            for attr_name in dir(mod):
                cls = getattr(mod, attr_name)
                if isinstance(cls, type) and issubclass(cls, Tool) and cls is not Tool:
                    instance = cls()
                    name = getattr(instance, "name", tool_name)
                    loaded[name] = instance
                    logger.info("Loaded tool: %s (%s)", name, py_file.name)
                    break
        except Exception as exc:
            logger.warning("Failed to load tool %s: %s", tool_name, exc)

    return loaded
