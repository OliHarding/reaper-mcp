"""Command-line interface (`reaper-cli`) for live REAPER control, MIDI editing, and FX management."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bridge_client import BridgeError, ReaperBridgeClient
from .fx_catalog import list_cached_presets, list_installed_fx
from .midi_files import (
    create_midi_file,
    edit_midi_file,
    export_reaper_to_midi_file,
    import_midi_file_to_reaper,
    inspect_midi_file,
)
from .music_theory import expand_note_specs


def _load_json_arg(json_str: str | None, json_file: str | None, default: Any = None) -> Any:
    if json_file:
        return json.loads(Path(json_file).read_text(encoding="utf-8"))
    if json_str:
        return json.loads(json_str)
    return default


def _parse_track_ref(val: str) -> int | str:
    if val.lstrip("-").isdigit():
        return int(val)
    return val


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reaper-cli", description="Control REAPER, MIDI files, and FX/Instruments.")
    sub = p.add_subparsers(dest="command", required=True)

    # Status / Ping
    sub.add_parser("status", help="Check bridge heartbeat and ping live REAPER.")

    # Project & Transport
    proj = sub.add_parser("project", help="Get active REAPER project overview.")
    proj.add_argument("--no-tracks", action="store_true")
    proj.add_argument("--no-fx", action="store_true")

    tempo = sub.add_parser("set-tempo", help="Set project tempo and optional time signature.")
    tempo.add_argument("bpm", type=float)
    tempo.add_argument("--num", type=int, default=None)
    tempo.add_argument("--denom", type=int, default=None)
    tempo.add_argument("--at-beats", type=float, default=None)

    trp = sub.add_parser("transport", help="Control REAPER transport & cursor.")
    trp.add_argument("--action", choices=["play", "stop", "pause", "record", "play_pause"], default=None)
    trp.add_argument("--cursor-beats", type=float, default=None)
    trp.add_argument("--loop-start", type=float, default=None)
    trp.add_argument("--loop-end", type=float, default=None)
    trp.add_argument("--repeat", choices=["on", "off"], default=None)

    # Tracks
    tl = sub.add_parser("tracks", help="List all tracks.")
    tl.add_argument("--master", action="store_true")
    tl.add_argument("--sends", action="store_true")

    tc = sub.add_parser("track-create", help="Create a track (optionally with instrument & FX).")
    tc.add_argument("--name", default="")
    tc.add_argument("--index", type=int, default=None)
    tc.add_argument("--instrument", default=None)
    tc.add_argument("--fx", nargs="*", default=None)
    tc.add_argument("--volume-db", type=float, default=None)
    tc.add_argument("--pan", type=float, default=None)
    tc.add_argument("--color", default=None)
    tc.add_argument("--open-ui", action="store_true")

    # Live MIDI
    mc = sub.add_parser("midi-create", help="Create a MIDI item on a track with optional notes JSON.")
    mc.add_argument("track")
    mc.add_argument("--start-beats", type=float, default=0.0)
    mc.add_argument("--length-beats", type=float, default=4.0)
    mc.add_argument("--name", default="")
    mc.add_argument("--notes-json", default=None)
    mc.add_argument("--notes-file", default=None)

    mr = sub.add_parser("midi-read", help="Read MIDI events from a track item.")
    mr.add_argument("track")
    mr.add_argument("--item", type=int, default=0)

    # FX & Instruments
    fxc = sub.add_parser("fx-catalog", help="Search installed VST3/VST2/CLAP/AU/JSFX/FXChains offline or live.")
    fxc.add_argument("--query", default=None)
    fxc.add_argument("--kind", choices=["all", "instrument", "effect", "fxchain"], default="all")
    fxc.add_argument("--format", dest="fmt", choices=["all", "vst3", "vst2", "clap", "au", "jsfx", "fxchain"], default="all")
    fxc.add_argument("--developer", default=None)
    fxc.add_argument("--live", action="store_true")
    fxc.add_argument("--limit", type=int, default=50)

    fxpc = sub.add_parser("fx-presets-catalog", help="Search REAPER's cached preset .ini files.")
    fxpc.add_argument("--query", default=None)
    fxpc.add_argument("--limit", type=int, default=50)

    fxl = sub.add_parser("fx-list", help="List FX loaded on a track.")
    fxl.add_argument("track")
    fxl.add_argument("--rec-fx", action="store_true")

    fxa = sub.add_parser("fx-add", help="Add an FX or instrument to a track.")
    fxa.add_argument("track")
    fxa.add_argument("fx_name")
    fxa.add_argument("--instrument", action="store_true")
    fxa.add_argument("--midi-in", action="store_true")
    fxa.add_argument("--preset", default=None)
    fxa.add_argument("--open-ui", action="store_true")

    fxp = sub.add_parser("fx-params", help="Inspect parameters of a loaded FX.")
    fxp.add_argument("track")
    fxp.add_argument("fx")
    fxp.add_argument("--search", default=None)
    fxp.add_argument("--limit", type=int, default=80)

    fxs = sub.add_parser("fx-set", help="Set FX parameters from JSON array.")
    fxs.add_argument("track")
    fxs.add_argument("fx")
    fxs.add_argument("--json", dest="json_str", default=None)
    fxs.add_argument("--json-file", default=None)

    # Standalone .mid files + import/export
    mi = sub.add_parser("mid-inspect", help="Inspect a .mid file on disk.")
    mi.add_argument("path")
    mi.add_argument("--summary-only", action="store_true")
    mi.add_argument("--track", type=int, default=None)

    mcr = sub.add_parser("mid-create", help="Create a .mid file on disk from JSON spec.")
    mcr.add_argument("output_path")
    mcr.add_argument("--bpm", type=float, default=120.0)
    mcr.add_argument("--json", dest="json_str", default=None, help="Tracks JSON array")
    mcr.add_argument("--json-file", default=None)

    med = sub.add_parser("mid-edit", help="Edit a .mid file on disk with JSON operations.")
    med.add_argument("input_path")
    med.add_argument("--output", default=None)
    med.add_argument("--json", dest="json_str", default=None, help="Operations JSON array")
    med.add_argument("--json-file", default=None)

    mimp = sub.add_parser("mid-import", help="Import a .mid file into live REAPER.")
    mimp.add_argument("midi_path")
    mimp.add_argument("--track", default=None)
    mimp.add_argument("--single-track", action="store_true")
    mimp.add_argument("--start-beats", type=float, default=0.0)
    mimp.add_argument("--import-tempo", action="store_true")
    mimp.add_argument("--instrument", default=None)

    mexp = sub.add_parser("mid-export", help="Export MIDI from live REAPER to a .mid file.")
    mexp.add_argument("output_path")
    mexp.add_argument("--tracks", nargs="*", default=None)

    # Generic direct bridge call / batch / Lua eval
    raw = sub.add_parser("call", help="Invoke any REAPER bridge handler directly.")
    raw.add_argument("cmd")
    raw.add_argument("--json", dest="json_str", default=None)
    raw.add_argument("--json-file", default=None)
    raw.add_argument("--undo-label", default=None)
    raw.add_argument("--timeout", type=float, default=None)

    bat = sub.add_parser("batch", help="Execute a JSON array of bridge commands in one Undo block.")
    bat.add_argument("--json", dest="json_str", default=None)
    bat.add_argument("--json-file", default=None)
    bat.add_argument("--undo-label", default="ReaperAgent Batch")

    evl = sub.add_parser("eval-lua", help="Execute Lua code inside REAPER and return result.")
    evl.add_argument("code")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = ReaperBridgeClient()

    try:
        cmd = args.command
        if cmd == "status":
            hb = client.get_heartbeat()
            if hb.get("connected"):
                _print({"connected": True, "heartbeat": hb, "live": client.ping(timeout_sec=3.0)})
            else:
                _print({"connected": False, "heartbeat": hb})
                return 2
        elif cmd == "project":
            _print(client.call("project_get_info", {"include_tracks": not args.no_tracks, "include_fx": not args.no_fx}))
        elif cmd == "set-tempo":
            payload: dict[str, Any] = {"bpm": args.bpm}
            if args.num is not None:
                payload["time_sig_num"] = args.num
            if args.denom is not None:
                payload["time_sig_denom"] = args.denom
            if args.at_beats is not None:
                payload["time_beats"] = args.at_beats
            _print(client.call("project_set_tempo", payload))
        elif cmd == "transport":
            payload = {}
            if args.action:
                payload["action"] = args.action
            if args.cursor_beats is not None:
                payload["cursor_beats"] = args.cursor_beats
            if args.loop_start is not None:
                payload["loop_start_beats"] = args.loop_start
            if args.loop_end is not None:
                payload["loop_end_beats"] = args.loop_end
            if args.repeat is not None:
                payload["repeat_enabled"] = args.repeat == "on"
            _print(client.call("transport_control", payload))
        elif cmd == "tracks":
            _print(client.call("track_list", {"include_fx": True, "include_master": args.master, "include_sends": args.sends}))
        elif cmd == "track-create":
            payload = {"name": args.name, "open_ui": args.open_ui}
            for k in ("index", "instrument", "fx", "volume_db", "pan", "color"):
                v = getattr(args, k)
                if v is not None:
                    payload[k] = v
            _print(client.call("track_create", payload))
        elif cmd == "midi-create":
            notes = _load_json_arg(args.notes_json, args.notes_file, default=[])
            payload = {
                "track": _parse_track_ref(args.track),
                "start_beats": args.start_beats,
                "length_beats": args.length_beats,
                "name": args.name,
                "notes": expand_note_specs(notes) if notes else [],
            }
            _print(client.call("item_create_midi", payload))
        elif cmd == "midi-read":
            _print(client.call("midi_get_events", {"track": _parse_track_ref(args.track), "item_index": args.item}))
        elif cmd == "fx-catalog":
            if args.live:
                _print(client.call("fx_enum_installed", {"query": args.query, "kind": args.kind, "format": args.fmt, "limit": args.limit}))
            else:
                _print(list_installed_fx(query=args.query, kind=args.kind, fmt=args.fmt, developer=args.developer, limit=args.limit))
        elif cmd == "fx-presets-catalog":
            _print(list_cached_presets(plugin_query=args.query, limit_per_plugin=args.limit))
        elif cmd == "fx-list":
            _print(client.call("fx_list", {"track": _parse_track_ref(args.track), "rec_fx": args.rec_fx}))
        elif cmd == "fx-add":
            payload = {
                "track": _parse_track_ref(args.track),
                "fx_name": args.fx_name,
                "prefer_instrument": args.instrument,
                "configure_midi_input": args.midi_in or args.instrument,
                "open_ui": args.open_ui,
            }
            if args.preset:
                payload["preset_name"] = args.preset
            _print(client.call("fx_add", payload, timeout_sec=30.0))
        elif cmd == "fx-params":
            _print(
                client.call(
                    "fx_get_params",
                    {"track": _parse_track_ref(args.track), "fx": _parse_track_ref(args.fx), "search": args.search, "limit": args.limit},
                )
            )
        elif cmd == "fx-set":
            params = _load_json_arg(args.json_str, args.json_file, default=[])
            _print(client.call("fx_set_params", {"track": _parse_track_ref(args.track), "fx": _parse_track_ref(args.fx), "params": params}))
        elif cmd == "mid-inspect":
            _print(inspect_midi_file(args.path, include_events=not args.summary_only, track_index=args.track))
        elif cmd == "mid-create":
            tracks = _load_json_arg(args.json_str, args.json_file, default=[])
            _print(create_midi_file(args.output_path, tracks, bpm=args.bpm))
        elif cmd == "mid-edit":
            ops = _load_json_arg(args.json_str, args.json_file, default=[])
            _print(edit_midi_file(args.input_path, ops, output_path=args.output))
        elif cmd == "mid-import":
            _print(
                import_midi_file_to_reaper(
                    client,
                    args.midi_path,
                    target_track=_parse_track_ref(args.track) if args.track is not None else None,
                    expand_tracks=not args.single_track,
                    start_beats=args.start_beats,
                    import_tempo=args.import_tempo,
                    instrument=args.instrument,
                )
            )
        elif cmd == "mid-export":
            tlist = [_parse_track_ref(t) for t in args.tracks] if args.tracks else None
            _print(export_reaper_to_midi_file(client, args.output_path, tracks=tlist))
        elif cmd == "call":
            payload = _load_json_arg(args.json_str, args.json_file, default={})
            _print(client.call(args.cmd, payload, undo_label=args.undo_label, timeout_sec=args.timeout))
        elif cmd == "batch":
            commands = _load_json_arg(args.json_str, args.json_file, default=[])
            _print(client.batch(commands, undo_label=args.undo_label))
        elif cmd == "eval-lua":
            _print(client.call("eval_lua", {"code": args.code}))
        return 0
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
