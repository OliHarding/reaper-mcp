"""Configuration & cross-platform path discovery for REAPER Agent Interface."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReaperConfig:
    reaper_resource_dir: Path
    ipc_root: Path
    requests_dir: Path
    responses_dir: Path
    heartbeat_file: Path
    scripts_install_dir: Path
    default_timeout_sec: float = 15.0
    poll_interval_sec: float = 0.015


def get_reaper_resource_dir() -> Path:
    override = os.environ.get("REAPER_RESOURCE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "REAPER"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    # Linux / other POSIX
    home = Path.home()
    mac_dir = home / "Library" / "Application Support" / "REAPER"
    if mac_dir.exists():
        return mac_dir
    return home / ".config" / "REAPER"


def get_ipc_root() -> Path:
    override = os.environ.get("REAPER_AGENT_IPC_DIR")
    if override:
        return Path(override).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "ReaperAgentBridge" / "ipc"
    return get_reaper_resource_dir() / "ReaperAgentBridgeIPC"


def get_config() -> ReaperConfig:
    resource_dir = get_reaper_resource_dir()
    ipc_root = get_ipc_root()
    return ReaperConfig(
        reaper_resource_dir=resource_dir,
        ipc_root=ipc_root,
        requests_dir=ipc_root / "requests",
        responses_dir=ipc_root / "responses",
        heartbeat_file=ipc_root / "heartbeat.json",
        scripts_install_dir=resource_dir / "Scripts" / "ReaperAgentBridge",
    )
