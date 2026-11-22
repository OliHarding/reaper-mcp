"""Tests for atomic file-queue IPC client, batching, error handling, and MIDI import/export."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reaper_interface.bridge_client import BridgeError, BridgeOfflineError, ReaperBridgeClient
from reaper_interface.config import ReaperConfig
from reaper_interface.midi_files import create_midi_file, export_reaper_to_midi_file, import_midi_file_to_reaper, inspect_midi_file


def _make_config(tmp_path: Path) -> ReaperConfig:
    ipc = tmp_path / "ipc"
    return ReaperConfig(
        reaper_resource_dir=tmp_path / "reaper",
        ipc_root=ipc,
        requests_dir=ipc / "requests",
        responses_dir=ipc / "responses",
        heartbeat_file=ipc / "heartbeat.json",
        scripts_install_dir=tmp_path / "reaper" / "Scripts" / "ReaperAgentBridge",
        default_timeout_sec=2.0,
        poll_interval_sec=0.005,
    )


def _start_mock_daemon(cfg: ReaperConfig, handler) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    cfg.requests_dir.mkdir(parents=True, exist_ok=True)
    cfg.responses_dir.mkdir(parents=True, exist_ok=True)
    cfg.heartbeat_file.write_text(json.dumps({"alive": True, "timestamp": time.time()}), encoding="utf-8")

    def loop():
        while not stop.is_set():
            for req_file in sorted(cfg.requests_dir.glob("req_*.json")):
                raw = req_file.read_text(encoding="utf-8")
                req_file.unlink(missing_ok=True)
                req = json.loads(raw)
                try:
                    data = handler(req["cmd"], req.get("args", {}))
                    resp = {"id": req["id"], "ok": True, "data": data}
                except Exception as exc:
                    resp = {"id": req["id"], "ok": False, "error": str(exc)}
                res_tmp = cfg.responses_dir / f"res_{req['id']}.tmp"
                res_dst = cfg.responses_dir / f"res_{req['id']}.json"
                res_tmp.write_text(json.dumps(resp), encoding="utf-8")
                os.replace(res_tmp, res_dst)
            time.sleep(0.005)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop, t


def test_ipc_roundtrip_and_errors(tmp_path: Path):
    cfg = _make_config(tmp_path)
    client = ReaperBridgeClient(cfg)

    def handle(cmd: str, args: dict[str, Any]):
        if cmd == "ping":
            return {"alive": True, "reaper_version": "7.22/mock"}
        if cmd == "fail_me":
            raise RuntimeError("boom inside REAPER")
        return {"echo": args}

    stop, t = _start_mock_daemon(cfg, handle)
    try:
        assert client.is_connected()
        assert client.ping()["reaper_version"] == "7.22/mock"
        assert client.call("echo", {"x": 42}) == {"echo": {"x": 42}}
        with pytest.raises(BridgeError, match="boom inside REAPER"):
            client.call("fail_me", {})
    finally:
        stop.set()
        t.join(timeout=1.0)


def test_ipc_offline_timeout(tmp_path: Path):
    cfg = _make_config(tmp_path)
    client = ReaperBridgeClient(cfg)
    assert not client.is_connected()
    with pytest.raises(BridgeOfflineError, match="not running"):
        client.call("ping", {}, timeout_sec=0.1)


def test_midi_import_and_export_via_mock_bridge(tmp_path: Path):
    cfg = _make_config(tmp_path)
    client = ReaperBridgeClient(cfg)

    mid_in = tmp_path / "in.mid"
    create_midi_file(
        mid_in,
        tracks=[{"name": "Piano", "notes": [{"chord": "Cmaj7", "start_beats": 0, "duration_beats": 4}]}],
        bpm=115.0,
    )

    recorded_batches: list[dict[str, Any]] = []

    def handle(cmd: str, args: dict[str, Any]):
        if cmd == "track_list":
            return {"count": 1, "tracks": [{"index": 0, "name": "Piano"}]}
        if cmd == "batch":
            recorded_batches.append(args)
            return {"count": len(args["commands"]), "results": []}
        if cmd == "project_get_info":
            return {"bpm": 115.0, "time_sig_num": 4, "time_sig_denom": 4}
        if cmd == "project_list_tempo_markers":
            return {"markers": []}
        if cmd == "item_list":
            return {"count": 1, "items": [{"guid": "{ITEM-1}", "is_midi": True, "mute": False}]}
        if cmd == "midi_get_events":
            return {
                "notes": [
                    {"pitch": 60, "velocity": 100, "channel": 0, "start_beats": 0.0, "end_beats": 2.0, "muted": False},
                    {"pitch": 67, "velocity": 95, "channel": 0, "start_beats": 2.0, "end_beats": 4.0, "muted": False},
                ],
                "cc": [],
                "text": [],
            }
        return {}

    stop, t = _start_mock_daemon(cfg, handle)
    try:
        imp = import_midi_file_to_reaper(client, mid_in, import_tempo=True, instrument="ReaSynth")
        assert imp["imported_tracks"] == 1
        assert len(recorded_batches) == 1
        cmds = [c["cmd"] for c in recorded_batches[0]["commands"]]
        assert cmds == ["project_set_tempo", "track_create", "item_create_midi"]

        mid_out = tmp_path / "out.mid"
        exp = export_reaper_to_midi_file(client, mid_out)
        assert exp["track_count"] == 1
        insp = inspect_midi_file(mid_out, include_events=True)
        assert [n["pitch"] for n in insp["tracks"][0]["notes"]] == [60, 67]
    finally:
        stop.set()
        t.join(timeout=1.0)
