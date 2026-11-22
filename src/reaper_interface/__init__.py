"""REAPER Agent Interface — MCP server, CLI, Python client, MIDI engine, and FX catalog."""

from .bridge_client import BridgeError, BridgeOfflineError, ReaperBridgeClient
from .config import ReaperConfig, get_config

__all__ = [
    "BridgeError",
    "BridgeOfflineError",
    "ReaperBridgeClient",
    "ReaperConfig",
    "get_config",
]
__version__ = "0.1.0"
