"""End-to-end verification and optional non-destructive live smoke test for ReaperAgentBridge."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from reaper_interface.bridge_client import ReaperBridgeClient  # noqa: E402
from reaper_interface.config import get_config  # noqa: E402
from reaper_interface.fx_catalog import list_cached_presets, list_installed_fx  # noqa: E402
from reaper_interface.midi_files import create_midi_file, inspect_midi_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ReaperAgentBridge installation and live connectivity.")
    parser.add_argument(
        "--live-smoke-test",
        action="store_true",
        help="Run a non-destructive live test in REAPER (creates a temporary track/MIDI/FX, tests edits, then deletes the test track).",
    )
    args = parser.parse_args()

    cfg = get_config()
    bridge_lua = cfg.scripts_install_dir / "reaper_agent_bridge.lua"
    print("=== Installation Check ===")
    print(f"REAPER resource dir : {cfg.reaper_resource_dir} (exists={cfg.reaper_resource_dir.exists()})")
    print(f"Bridge Lua script   : {bridge_lua} (exists={bridge_lua.exists()})")
    print(f"IPC directory       : {cfg.ipc_root} (exists={cfg.ipc_root.exists()})")

    print("\n=== Offline FX Catalog Check ===")
    all_fx = list_installed_fx(limit=1)
    inst_fx = list_installed_fx(kind="instrument", limit=10)
    presets = list_cached_presets(limit_per_plugin=5)
    print(f"Total installed FX/instruments/chains: {all_fx['total_catalog_count']}")
    print(f"Instruments found: {inst_fx['matched_count']}")
    print("Sample instruments:", [x["reaper_add_name"] for x in inst_fx["items"][:6]])
    print(f"Preset .ini files found: {presets['matched_files']}")

    print("\n=== Live REAPER Connection Check ===")
    client = ReaperBridgeClient(cfg)
    hb = client.get_heartbeat()
    print(json.dumps(hb, indent=2))
    if not hb.get("connected"):
        print("\nBridge is NOT currently connected. Start or restart REAPER (or run reaper_agent_bridge.lua once).")
        return 2

    pong = client.ping()
    print("Live ping OK:", json.dumps(pong, indent=2))

    if args.live_smoke_test:
        print("\n=== Running Non-Destructive Live Smoke Test ===")
        tr = client.call("track_create", {"name": "__ReaperAgentSmokeTest__", "instrument": "ReaSynth", "fx": ["ReaEQ"]})
        tr_idx = tr["index"]
        try:
            print(f"Created temp track #{tr_idx} with FX:", [f["name"] for f in tr.get("fx", [])])
            params = client.call("fx_get_params", {"track": tr_idx, "fx": "ReaSynth", "limit": 10})
            print(f"ReaSynth params ({params['total_params']} total):", [(p["name"], p["formatted"]) for p in params["params"][:4]])
            edited = client.call(
                "fx_set_params",
                {"track": tr_idx, "fx": "ReaSynth", "params": [{"param": "Attack", "value_norm": 0.2}]},
            )
            print("Edited ReaSynth Attack:", edited["results"])

            item = client.call(
                "item_create_midi",
                {
                    "track": tr_idx,
                    "start_beats": 0,
                    "length_beats": 4,
                    "name": "SmokeTestClip",
                    "notes": [
                        {"pitch": 60, "start_beat_rel": 0.0, "duration_beats": 1.0, "velocity": 100},
                        {"pitch": 64, "start_beat_rel": 1.0, "duration_beats": 1.0, "velocity": 95},
                        {"pitch": 67, "start_beat_rel": 2.0, "duration_beats": 2.0, "velocity": 110},
                    ],
                },
            )
            evts = client.call("midi_get_events", {"item_guid": item["guid"]})
            print(f"Read back {evts['note_count']} MIDI notes:", [(n["note_name"], n["start_beat_rel"]) for n in evts["notes"]])

            with tempfile.TemporaryDirectory() as td:
                mid_out = Path(td) / "test.mid"
                create_midi_file(mid_out, [{"name": "Test", "notes": [{"chord": "Cmaj7", "start_beats": 0, "duration_beats": 4}]}])
                insp = inspect_midi_file(mid_out)
                print(f"Standalone .mid roundtrip OK: {insp['tracks'][0]['note_count']} notes")
        finally:
            client.call("track_delete", {"track": tr_idx})
            print(f"Cleaned up temp track #{tr_idx}.")
        print("Live smoke test PASSED!")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
