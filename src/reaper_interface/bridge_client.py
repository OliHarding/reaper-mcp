"""Atomic file-queue IPC client for communicating with ReaperAgentBridge inside REAPER."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .config import ReaperConfig, get_config


class BridgeError(RuntimeError):
    """Raised when REAPER returns an error executing a command."""


class BridgeOfflineError(BridgeError):
    """Raised when REAPER or ReaperAgentBridge is not responding."""


class ReaperBridgeClient:
    """Sends JSON commands to the running ReaperAgentBridge Lua daemon in REAPER."""

    def __init__(self, config: ReaperConfig | None = None) -> None:
        self.config = config or get_config()
        self.config.requests_dir.mkdir(parents=True, exist_ok=True)
        self.config.responses_dir.mkdir(parents=True, exist_ok=True)

    def get_heartbeat(self, max_age_sec: float = 5.0) -> dict[str, Any]:
        """Read heartbeat.json written by ReaperAgentBridge. Returns status dict."""
        hb_path = self.config.heartbeat_file
        for attempt in range(4):
            try:
                if not hb_path.exists():
                    return {
                        "connected": False,
                        "reason": "heartbeat_missing",
                        "ipc_root": str(self.config.ipc_root),
                        "script_path": str(self.config.scripts_install_dir / "reaper_agent_bridge.lua"),
                    }
                raw = hb_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                ts = float(data.get("timestamp", 0))
                mtime = hb_path.stat().st_mtime
                now = time.time()
                age = min(abs(now - ts), abs(now - mtime))
                data["age_sec"] = round(age, 2)
                data["connected"] = age <= max_age_sec
                if not data["connected"]:
                    data["reason"] = f"heartbeat_stale ({age:.1f}s old)"
                return data
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                if attempt < 3:
                    time.sleep(0.03)
                else:
                    return {
                        "connected": False,
                        "reason": "heartbeat_unreadable",
                        "ipc_root": str(self.config.ipc_root),
                    }
        return {"connected": False, "reason": "unknown"}

    def is_connected(self, max_age_sec: float = 5.0) -> bool:
        return bool(self.get_heartbeat(max_age_sec=max_age_sec).get("connected"))

    def call(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
        undo_label: str | None = None,
    ) -> Any:
        """Execute a single command inside REAPER and return its `data` payload."""
        timeout = timeout_sec if timeout_sec is not None else self.config.default_timeout_sec
        ts_ms = int(time.time() * 1000)
        req_id = f"{ts_ms}_{uuid.uuid4().hex[:10]}"

        payload: dict[str, Any] = {
            "id": req_id,
            "cmd": cmd,
            "args": args or {},
        }
        if undo_label:
            payload["undo_label"] = undo_label

        req_tmp = self.config.requests_dir / f"req_{req_id}.tmp"
        req_file = self.config.requests_dir / f"req_{req_id}.json"
        res_file = self.config.responses_dir / f"res_{req_id}.json"

        raw_json = json.dumps(payload, ensure_ascii=False)
        req_tmp.write_text(raw_json, encoding="utf-8")
        os.replace(req_tmp, req_file)

        deadline = time.monotonic() + timeout
        poll = self.config.poll_interval_sec

        while time.monotonic() < deadline:
            if res_file.exists():
                resp = self._read_and_cleanup_response(res_file)
                if resp is not None:
                    if not resp.get("ok"):
                        raise BridgeError(resp.get("error") or f"REAPER command '{cmd}' failed")
                    return resp.get("data")
            time.sleep(poll)

        # Clean up stale request file on timeout
        try:
            req_file.unlink(missing_ok=True)
        except OSError:
            pass

        hb = self.get_heartbeat()
        script_path = self.config.scripts_install_dir / "reaper_agent_bridge.lua"
        if not hb.get("connected"):
            raise BridgeOfflineError(
                f"REAPER Agent Bridge is not running or not responding (command '{cmd}' timed out after {timeout:.1f}s).\n"
                f"To start it:\n"
                f"  1. In REAPER: Actions -> Show action list (?) -> New action -> Load ReaScript -> "
                f"select '{script_path}' and Run it.\n"
                f"  2. Or restart REAPER (auto-start is installed in Scripts/__startup.lua)."
            )
        raise BridgeError(
            f"REAPER command '{cmd}' timed out after {timeout:.1f}s (bridge heartbeat was alive; "
            f"REAPER may have a modal dialog open or a heavy plugin loading)."
        )

    def batch(
        self,
        commands: list[dict[str, Any]],
        *,
        undo_label: str = "ReaperAgent Batch",
        continue_on_error: bool = False,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Execute multiple commands atomically in one REAPER defer tick and one Undo step."""
        return self.call(
            "batch",
            {"commands": commands, "continue_on_error": continue_on_error},
            timeout_sec=timeout_sec,
            undo_label=undo_label,
        )

    def ping(self, timeout_sec: float = 3.0) -> dict[str, Any]:
        return self.call("ping", {}, timeout_sec=timeout_sec)

    @staticmethod
    def _read_and_cleanup_response(res_file: Path) -> dict[str, Any] | None:
        for attempt in range(5):
            try:
                raw = res_file.read_text(encoding="utf-8")
                if not raw:
                    time.sleep(0.01)
                    continue
                data = json.loads(raw)
                try:
                    res_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return data
            except (PermissionError, json.JSONDecodeError):
                if attempt < 4:
                    time.sleep(0.015)
                else:
                    raise
            except FileNotFoundError:
                return None
        return None
