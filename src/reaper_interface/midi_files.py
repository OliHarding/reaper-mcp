"""Standalone Standard MIDI File (.mid) engine + non-blocking REAPER import/export."""

from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import mido

from .bridge_client import ReaperBridgeClient
from .music_theory import (
    expand_note_specs,
    humanize_notes,
    parse_pitch,
    pitch_to_name,
    quantize_value,
    snap_pitch_to_scale,
)


def _ticks_to_beats(ticks: int, tpb: int) -> float:
    return round(ticks / float(tpb), 6)


def _beats_to_ticks(beats: float, tpb: int) -> int:
    return max(0, int(round(float(beats) * tpb)))


def _extract_track_data(track: mido.MidiTrack, tpb: int) -> dict[str, Any]:
    abs_tick = 0
    track_name = track.name or ""
    active_notes: dict[tuple[int, int], deque[tuple[int, int]]] = defaultdict(deque)
    notes: list[dict[str, Any]] = []
    cc_events: list[dict[str, Any]] = []
    text_events: list[dict[str, Any]] = []
    tempo_changes: list[dict[str, Any]] = []
    time_sigs: list[dict[str, Any]] = []
    key_sigs: list[dict[str, Any]] = []
    channels: set[int] = set()
    programs: set[int] = set()

    for msg in track:
        abs_tick += msg.time
        beat = _ticks_to_beats(abs_tick, tpb)
        if msg.is_meta:
            if msg.type == "track_name":
                track_name = msg.name
            elif msg.type == "set_tempo":
                tempo_changes.append(
                    {"beats": beat, "tick": abs_tick, "bpm": round(mido.tempo2bpm(msg.tempo), 4), "tempo_us": msg.tempo}
                )
            elif msg.type == "time_signature":
                time_sigs.append(
                    {"beats": beat, "tick": abs_tick, "numerator": msg.numerator, "denominator": msg.denominator}
                )
            elif msg.type == "key_signature":
                key_sigs.append({"beats": beat, "tick": abs_tick, "key": msg.key})
            elif msg.type in ("text", "lyrics", "marker", "cue_marker"):
                text_events.append({"beats": beat, "tick": abs_tick, "type": msg.type, "text": msg.text})
            continue

        if hasattr(msg, "channel"):
            channels.add(msg.channel)

        if msg.type == "note_on" and msg.velocity > 0:
            active_notes[(msg.channel, msg.note)].append((abs_tick, msg.velocity))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if active_notes[key]:
                start_tick, vel = active_notes[key].popleft()
                dur_ticks = max(1, abs_tick - start_tick)
                s_beat = _ticks_to_beats(start_tick, tpb)
                e_beat = _ticks_to_beats(start_tick + dur_ticks, tpb)
                notes.append(
                    {
                        "pitch": msg.note,
                        "note_name": pitch_to_name(msg.note),
                        "velocity": vel,
                        "channel": msg.channel,
                        "start_beats": s_beat,
                        "end_beats": e_beat,
                        "duration_beats": round(e_beat - s_beat, 6),
                        "start_tick": start_tick,
                        "end_tick": start_tick + dur_ticks,
                    }
                )
        elif msg.type == "control_change":
            cc_events.append(
                {"kind": "cc", "beats": beat, "tick": abs_tick, "channel": msg.channel, "cc": msg.control, "value": msg.value}
            )
        elif msg.type == "pitchwheel":
            cc_events.append(
                {"kind": "pitch_bend", "beats": beat, "tick": abs_tick, "channel": msg.channel, "value": msg.pitch}
            )
        elif msg.type == "program_change":
            programs.add(msg.program)
            cc_events.append(
                {"kind": "program_change", "beats": beat, "tick": abs_tick, "channel": msg.channel, "program": msg.program}
            )
        elif msg.type == "aftertouch":
            cc_events.append(
                {"kind": "channel_pressure", "beats": beat, "tick": abs_tick, "channel": msg.channel, "value": msg.value}
            )
        elif msg.type == "polytouch":
            cc_events.append(
                {
                    "kind": "poly_aftertouch",
                    "beats": beat,
                    "tick": abs_tick,
                    "channel": msg.channel,
                    "pitch": msg.note,
                    "value": msg.value,
                }
            )

    # Flush any unclosed hanging notes at end of track
    end_tick = max(abs_tick, 1)
    for (chan, pitch), q in active_notes.items():
        while q:
            start_tick, vel = q.popleft()
            dur_ticks = max(1, end_tick - start_tick)
            s_beat = _ticks_to_beats(start_tick, tpb)
            e_beat = _ticks_to_beats(start_tick + dur_ticks, tpb)
            notes.append(
                {
                    "pitch": pitch,
                    "note_name": pitch_to_name(pitch),
                    "velocity": vel,
                    "channel": chan,
                    "start_beats": s_beat,
                    "end_beats": e_beat,
                    "duration_beats": round(e_beat - s_beat, 6),
                    "start_tick": start_tick,
                    "end_tick": start_tick + dur_ticks,
                }
            )

    notes.sort(key=lambda n: (n["start_tick"], n["pitch"], n["channel"]))
    pitches = [n["pitch"] for n in notes]
    return {
        "name": track_name,
        "channels": sorted(channels),
        "programs": sorted(programs),
        "note_count": len(notes),
        "cc_count": len(cc_events),
        "text_count": len(text_events),
        "pitch_min": min(pitches) if pitches else None,
        "pitch_max": max(pitches) if pitches else None,
        "pitch_range": f"{pitch_to_name(min(pitches))}..{pitch_to_name(max(pitches))}" if pitches else None,
        "end_beats": _ticks_to_beats(abs_tick, tpb),
        "notes": notes,
        "cc": cc_events,
        "text": text_events,
        "tempo_changes": tempo_changes,
        "time_signatures": time_sigs,
        "key_signatures": key_sigs,
    }


def inspect_midi_file(
    path: str | Path,
    *,
    include_events: bool = True,
    track_index: int | None = None,
    max_events_per_track: int | None = None,
) -> dict[str, Any]:
    """Parse a .mid file and return structured musical info (tempo, time sigs, tracks, notes, CCs)."""
    p = Path(path)
    mid = mido.MidiFile(p)
    tpb = mid.ticks_per_beat

    all_tempos: list[dict[str, Any]] = []
    all_ts: list[dict[str, Any]] = []
    all_ks: list[dict[str, Any]] = []
    tracks_out: list[dict[str, Any]] = []
    max_end_beats = 0.0

    for idx, tr in enumerate(mid.tracks):
        info = _extract_track_data(tr, tpb)
        all_tempos.extend(info.pop("tempo_changes"))
        all_ts.extend(info.pop("time_signatures"))
        all_ks.extend(info.pop("key_signatures"))
        max_end_beats = max(max_end_beats, info["end_beats"])

        if track_index is not None and idx != track_index:
            continue

        entry: dict[str, Any] = {"index": idx, **info}
        if not include_events:
            entry.pop("notes", None)
            entry.pop("cc", None)
            entry.pop("text", None)
        elif max_events_per_track is not None:
            if len(entry["notes"]) > max_events_per_track:
                entry["notes_truncated"] = True
                entry["notes"] = entry["notes"][:max_events_per_track]
            if len(entry["cc"]) > max_events_per_track:
                entry["cc_truncated"] = True
                entry["cc"] = entry["cc"][:max_events_per_track]
        tracks_out.append(entry)

    all_tempos.sort(key=lambda x: x["tick"])
    all_ts.sort(key=lambda x: x["tick"])
    if not all_tempos:
        all_tempos = [{"beats": 0.0, "tick": 0, "bpm": 120.0, "tempo_us": 500000}]
    if not all_ts:
        all_ts = [{"beats": 0.0, "tick": 0, "numerator": 4, "denominator": 4}]

    return {
        "path": str(p.resolve()),
        "file_type": mid.type,
        "ticks_per_beat": tpb,
        "track_count": len(mid.tracks),
        "duration_beats": round(max_end_beats, 6),
        "duration_sec": round(mid.length, 4),
        "bpm": all_tempos[0]["bpm"],
        "tempo_changes": all_tempos,
        "time_signatures": all_ts,
        "key_signatures": all_ks,
        "tracks": tracks_out,
    }


def _build_mido_file(
    tracks_data: list[dict[str, Any]],
    *,
    tempo_changes: list[dict[str, Any]] | None = None,
    time_signatures: list[dict[str, Any]] | None = None,
    ticks_per_beat: int = 480,
) -> mido.MidiFile:
    mid = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)

    # Track 0: tempo & time-signature conductor track if multiple tracks or explicit meta
    meta_events: list[tuple[int, int, mido.MetaMessage]] = []
    for ts in time_signatures or [{"beats": 0.0, "numerator": 4, "denominator": 4}]:
        tick = _beats_to_ticks(ts.get("beats", 0.0), ticks_per_beat)
        meta_events.append(
            (
                tick,
                0,
                mido.MetaMessage(
                    "time_signature",
                    numerator=int(ts.get("numerator", 4)),
                    denominator=int(ts.get("denominator", 4)),
                    time=0,
                ),
            )
        )
    for tc in tempo_changes or [{"beats": 0.0, "bpm": 120.0}]:
        tick = _beats_to_ticks(tc.get("beats", 0.0), ticks_per_beat)
        bpm = float(tc.get("bpm", 120.0))
        meta_events.append((tick, 1, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0)))

    meta_events.sort(key=lambda x: (x[0], x[1]))

    # If there is a single track, place meta events on that track; otherwise use a conductor track or Track 0
    for t_idx, tdata in enumerate(tracks_data):
        tr = mido.MidiTrack()
        events: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []

        if t_idx == 0:
            events.extend(meta_events)

        name = tdata.get("name")
        if name:
            events.append((0, 0, mido.MetaMessage("track_name", name=str(name), time=0)))

        if tdata.get("program") is not None:
            ch = max(0, min(15, int(tdata.get("channel", 0))))
            events.append((0, 2, mido.Message("program_change", channel=ch, program=int(tdata["program"]), time=0)))

        # Notes
        raw_notes = expand_note_specs(tdata.get("notes", []))
        for n in raw_notes:
            pitch = parse_pitch(n.get("pitch", n.get("note", 60)))
            vel = max(1, min(127, int(n.get("velocity", 100))))
            ch = max(0, min(15, int(n.get("channel", tdata.get("channel", 0)))))
            start_beats = float(n.get("start_beats", n.get("start_beat_rel", n.get("beats", 0.0))))
            if n.get("end_beats") is not None:
                end_beats = float(n["end_beats"])
            else:
                end_beats = start_beats + float(n.get("duration_beats", 0.5))
            s_tick = _beats_to_ticks(start_beats, ticks_per_beat)
            e_tick = max(s_tick + 1, _beats_to_ticks(end_beats, ticks_per_beat))
            # Order priority at same tick: note_off (4) before note_on (5)
            events.append((s_tick, 5, mido.Message("note_on", channel=ch, note=pitch, velocity=vel, time=0)))
            events.append((e_tick, 4, mido.Message("note_off", channel=ch, note=pitch, velocity=0, time=0)))

        # CC / pitch bend / program change
        for ev in tdata.get("cc", []):
            tick = _beats_to_ticks(float(ev.get("beats", ev.get("beat_rel", 0.0))), ticks_per_beat)
            ch = max(0, min(15, int(ev.get("channel", tdata.get("channel", 0)))))
            kind = ev.get("kind", "cc")
            if kind == "pitch_bend":
                val = max(-8192, min(8191, int(ev.get("value", 0))))
                events.append((tick, 3, mido.Message("pitchwheel", channel=ch, pitch=val, time=0)))
            elif kind == "program_change":
                prog = max(0, min(127, int(ev.get("program", ev.get("value", 0)))))
                events.append((tick, 2, mido.Message("program_change", channel=ch, program=prog, time=0)))
            elif kind == "channel_pressure":
                val = max(0, min(127, int(ev.get("value", 0))))
                events.append((tick, 3, mido.Message("aftertouch", channel=ch, value=val, time=0)))
            elif kind == "poly_aftertouch":
                val = max(0, min(127, int(ev.get("value", 0))))
                p = parse_pitch(ev.get("pitch", 60))
                events.append((tick, 3, mido.Message("polytouch", channel=ch, note=p, value=val, time=0)))
            else:
                cc_num = max(0, min(127, int(ev.get("cc", 1))))
                val = max(0, min(127, int(ev.get("value", 0))))
                events.append((tick, 3, mido.Message("control_change", channel=ch, control=cc_num, value=val, time=0)))

        # Text / Lyrics / Markers
        for tev in tdata.get("text", []):
            tick = _beats_to_ticks(float(tev.get("beats", 0.0)), ticks_per_beat)
            mtype = tev.get("type", "text")
            if mtype not in ("text", "lyrics", "marker", "cue_marker"):
                mtype = "text"
            events.append((tick, 1, mido.MetaMessage(mtype, text=str(tev.get("text", "")), time=0)))

        events.sort(key=lambda x: (x[0], x[1]))
        prev_tick = 0
        for abs_tick, _, msg in events:
            delta = max(0, abs_tick - prev_tick)
            tr.append(msg.copy(time=delta))
            prev_tick = abs_tick

        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)

    if not mid.tracks:
        tr = mido.MidiTrack()
        for _, _, msg in meta_events:
            tr.append(msg)
        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)

    return mid


def _save_mido_atomic(mid: mido.MidiFile, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    mid.save(tmp)
    os.replace(tmp, output_path)


def create_midi_file(
    output_path: str | Path,
    tracks: list[dict[str, Any]],
    *,
    bpm: float = 120.0,
    time_sig_num: int = 4,
    time_sig_denom: int = 4,
    ticks_per_beat: int = 480,
    tempo_changes: list[dict[str, Any]] | None = None,
    time_signatures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a Standard MIDI File (.mid) from structured track/note/chord/CC definitions."""
    out = Path(output_path)
    tc = tempo_changes or [{"beats": 0.0, "bpm": bpm}]
    ts = time_signatures or [{"beats": 0.0, "numerator": time_sig_num, "denominator": time_sig_denom}]
    mid = _build_mido_file(tracks, tempo_changes=tc, time_signatures=ts, ticks_per_beat=ticks_per_beat)
    _save_mido_atomic(mid, out)
    return inspect_midi_file(out, include_events=False)


def edit_midi_file(
    input_path: str | Path,
    operations: list[dict[str, Any]],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply a sequence of musical edit operations to an existing .mid file."""
    src = Path(input_path)
    dst = Path(output_path) if output_path else src
    parsed = inspect_midi_file(src, include_events=True)
    tpb = parsed["ticks_per_beat"]
    tempos = list(parsed["tempo_changes"])
    time_sigs = list(parsed["time_signatures"])
    tracks = list(parsed["tracks"])

    def _target_tracks(op: dict[str, Any]) -> list[dict[str, Any]]:
        tidx = op.get("track_index")
        if tidx is None:
            return tracks
        idx = int(tidx)
        if not (0 <= idx < len(tracks)):
            raise ValueError(f"track_index {idx} out of range (0..{len(tracks)-1})")
        return [tracks[idx]]

    for op in operations:
        kind = op.get("op")
        if kind == "set_tempo":
            bpm = float(op["bpm"])
            at_beat = float(op.get("beats", 0.0))
            tempos = [t for t in tempos if abs(float(t["beats"]) - at_beat) > 1e-6]
            tempos.append({"beats": at_beat, "bpm": bpm})
            tempos.sort(key=lambda t: float(t["beats"]))
        elif kind == "set_time_sig":
            at_beat = float(op.get("beats", 0.0))
            time_sigs = [t for t in time_sigs if abs(float(t["beats"]) - at_beat) > 1e-6]
            time_sigs.append(
                {"beats": at_beat, "numerator": int(op["numerator"]), "denominator": int(op["denominator"])}
            )
            time_sigs.sort(key=lambda t: float(t["beats"]))
        elif kind == "rename_track":
            for tr in _target_tracks(op):
                tr["name"] = str(op["name"])
        elif kind == "add_track":
            tracks.append(
                {
                    "name": op.get("name", f"Track {len(tracks) + 1}"),
                    "channel": int(op.get("channel", 0)),
                    "program": op.get("program"),
                    "notes": expand_note_specs(op.get("notes", [])),
                    "cc": list(op.get("cc", [])),
                    "text": list(op.get("text", [])),
                }
            )
        elif kind == "remove_track":
            idx = int(op["track_index"])
            tracks.pop(idx)
        elif kind == "add_notes":
            new_notes = expand_note_specs(op.get("notes", []))
            for tr in _target_tracks(op):
                tr["notes"].extend(new_notes)
        elif kind == "replace_notes":
            new_notes = expand_note_specs(op.get("notes", []))
            for tr in _target_tracks(op):
                tr["notes"] = list(new_notes)
        elif kind == "delete_notes":
            min_p = parse_pitch(op["min_pitch"]) if op.get("min_pitch") is not None else None
            max_p = parse_pitch(op["max_pitch"]) if op.get("max_pitch") is not None else None
            exact_p = parse_pitch(op["pitch"]) if op.get("pitch") is not None else None
            min_b = float(op["start_beats"]) if op.get("start_beats") is not None else None
            max_b = float(op["end_beats"]) if op.get("end_beats") is not None else None
            ch = int(op["channel"]) if op.get("channel") is not None else None
            for tr in _target_tracks(op):
                kept = []
                for n in tr["notes"]:
                    match = True
                    if exact_p is not None and n["pitch"] != exact_p:
                        match = False
                    if min_p is not None and n["pitch"] < min_p:
                        match = False
                    if max_p is not None and n["pitch"] > max_p:
                        match = False
                    if min_b is not None and n["start_beats"] < min_b:
                        match = False
                    if max_b is not None and n["start_beats"] > max_b:
                        match = False
                    if ch is not None and n.get("channel", 0) != ch:
                        match = False
                    if not match:
                        kept.append(n)
                tr["notes"] = kept
        elif kind == "transpose":
            semi = int(op["semitones"])
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    n["pitch"] = max(0, min(127, int(n["pitch"]) + semi))
                    n["note_name"] = pitch_to_name(n["pitch"])
        elif kind == "scale_snap":
            root = str(op["root"])
            scale = str(op.get("scale", "major"))
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    n["pitch"] = snap_pitch_to_scale(int(n["pitch"]), root, scale)
                    n["note_name"] = pitch_to_name(n["pitch"])
        elif kind == "quantize":
            grid = float(op["grid_beats"])
            strength = float(op.get("strength", 1.0))
            swing = float(op.get("swing", 0.0))
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    dur = float(n.get("duration_beats", n.get("end_beats", 0.5) - n["start_beats"]))
                    new_s = quantize_value(float(n["start_beats"]), grid, strength=strength, swing=swing)
                    n["start_beats"] = max(0.0, new_s)
                    n["end_beats"] = n["start_beats"] + max(0.01, dur)
                    n["duration_beats"] = max(0.01, dur)
        elif kind == "humanize":
            t_jit = float(op.get("timing_beats", 0.02))
            v_jit = int(op.get("velocity_jitter", 8))
            seed = op.get("seed")
            for tr in _target_tracks(op):
                tr["notes"] = humanize_notes(tr["notes"], timing_beats=t_jit, velocity_jitter=v_jit, seed=seed)
        elif kind == "scale_velocity":
            scale = float(op.get("scale", 1.0))
            offset = int(op.get("offset", 0))
            fixed = op.get("set_value")
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    if fixed is not None:
                        v = int(fixed)
                    else:
                        v = int(round(int(n.get("velocity", 100)) * scale + offset))
                    n["velocity"] = max(1, min(127, v))
        elif kind == "shift_time":
            delta = float(op["delta_beats"])
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    dur = float(n.get("duration_beats", n.get("end_beats", 0.5) - n["start_beats"]))
                    n["start_beats"] = max(0.0, float(n["start_beats"]) + delta)
                    n["end_beats"] = n["start_beats"] + dur
                for ev in tr["cc"]:
                    ev["beats"] = max(0.0, float(ev["beats"]) + delta)
        elif kind == "scale_speed":
            factor = float(op["factor"])  # >1 = faster (shorter), <1 = slower (longer)
            if factor <= 0:
                raise ValueError("scale_speed factor must be > 0")
            for tr in _target_tracks(op):
                for n in tr["notes"]:
                    n["start_beats"] = float(n["start_beats"]) / factor
                    dur = float(n.get("duration_beats", n.get("end_beats", 0.5) - n["start_beats"])) / factor
                    n["duration_beats"] = max(0.01, dur)
                    n["end_beats"] = n["start_beats"] + n["duration_beats"]
                for ev in tr["cc"]:
                    ev["beats"] = float(ev["beats"]) / factor
        elif kind == "add_cc":
            for tr in _target_tracks(op):
                tr["cc"].extend(op.get("events", []))
        elif kind == "add_cc_ramp":
            cc_num = int(op.get("cc", 1))
            ch = int(op.get("channel", 0))
            sb = float(op.get("start_beats", 0.0))
            eb = float(op.get("end_beats", 4.0))
            sv = float(op.get("start_value", 0))
            ev_val = float(op.get("end_value", 127))
            steps = max(2, int(op.get("steps", 16)))
            curve = float(op.get("curve", 1.0))
            ramp = []
            for s in range(steps):
                t = s / (steps - 1)
                val = max(0, min(127, int(round(sv + (ev_val - sv) * (t**curve)))))
                ramp.append({"kind": "cc", "cc": cc_num, "channel": ch, "beats": sb + (eb - sb) * t, "value": val})
            for tr in _target_tracks(op):
                tr["cc"].extend(ramp)
        elif kind == "delete_cc":
            cc_filter = int(op["cc"]) if op.get("cc") is not None else None
            for tr in _target_tracks(op):
                if cc_filter is None:
                    tr["cc"] = []
                else:
                    tr["cc"] = [e for e in tr["cc"] if not (e.get("kind") == "cc" and e.get("cc") == cc_filter)]
        else:
            raise ValueError(f"Unknown MIDI edit operation: {kind!r}")

    mid = _build_mido_file(tracks, tempo_changes=tempos, time_signatures=time_sigs, ticks_per_beat=tpb)
    _save_mido_atomic(mid, dst)
    return inspect_midi_file(dst, include_events=False)


def import_midi_file_to_reaper(
    client: ReaperBridgeClient,
    midi_path: str | Path,
    *,
    target_track: int | str | None = None,
    expand_tracks: bool = True,
    start_beats: float = 0.0,
    import_tempo: bool = False,
    instrument: str | None = None,
) -> dict[str, Any]:
    """Import a .mid file directly into REAPER with ZERO modal popups."""
    parsed = inspect_midi_file(midi_path, include_events=True)
    non_empty = [t for t in parsed["tracks"] if t["note_count"] > 0 or t["cc_count"] > 0]
    if not non_empty:
        return {"imported_tracks": 0, "message": "MIDI file had no note or CC events."}

    commands: list[dict[str, Any]] = []

    if import_tempo:
        for idx, tc in enumerate(parsed["tempo_changes"]):
            t_args: dict[str, Any] = {"bpm": tc["bpm"]}
            if idx > 0 or float(tc["beats"]) > 0:
                t_args["time_beats"] = start_beats + float(tc["beats"])
            if idx == 0 and parsed["time_signatures"]:
                t_args["time_sig_num"] = parsed["time_signatures"][0]["numerator"]
                t_args["time_sig_denom"] = parsed["time_signatures"][0]["denominator"]
            commands.append({"cmd": "project_set_tempo", "args": t_args})

    created_summary: list[dict[str, Any]] = []
    if not expand_tracks or (target_track is not None and len(non_empty) == 1):
        # Merge all non-empty tracks onto target_track (or create one new track)
        dest_ref = target_track
        if dest_ref is None:
            tr_info = client.call(
                "track_create",
                {
                    "name": Path(midi_path).stem,
                    "instrument": instrument,
                },
                undo_label=f"Import MIDI: {Path(midi_path).name}",
            )
            dest_ref = tr_info["index"]

        all_notes: list[dict[str, Any]] = []
        all_cc: list[dict[str, Any]] = []
        max_len = 4.0
        for tr in non_empty:
            max_len = max(max_len, float(tr["end_beats"]))
            for n in tr["notes"]:
                all_notes.append(
                    {
                        "pitch": n["pitch"],
                        "velocity": n["velocity"],
                        "channel": n["channel"],
                        "start_beat_rel": n["start_beats"],
                        "duration_beats": n["duration_beats"],
                    }
                )
            for ev in tr["cc"]:
                c = dict(ev)
                c["beat_rel"] = c.pop("beats", 0.0)
                all_cc.append(c)

        commands.append(
            {
                "cmd": "item_create_midi",
                "args": {
                    "track": dest_ref,
                    "start_beats": start_beats,
                    "length_beats": max(1.0, max_len),
                    "name": Path(midi_path).stem,
                    "notes": all_notes,
                    "cc": all_cc,
                },
            }
        )
        res = client.batch(commands, undo_label=f"Import MIDI: {Path(midi_path).name}")
        return {"imported_tracks": 1, "target_track": dest_ref, "note_count": len(all_notes), "batch": res}

    # Expand each MIDI track onto its own new REAPER track
    existing = client.call("track_list", {})
    base_idx = existing["count"]
    for offset, tr in enumerate(non_empty):
        tr_idx = base_idx + offset
        tr_name = tr["name"] or f"{Path(midi_path).stem} Track {tr['index'] + 1}"
        commands.append(
            {
                "cmd": "track_create",
                "args": {"index": tr_idx, "name": tr_name, "instrument": instrument},
            }
        )
        notes_payload = [
            {
                "pitch": n["pitch"],
                "velocity": n["velocity"],
                "channel": n["channel"],
                "start_beat_rel": n["start_beats"],
                "duration_beats": n["duration_beats"],
            }
            for n in tr["notes"]
        ]
        cc_payload = []
        for ev in tr["cc"]:
            c = dict(ev)
            c["beat_rel"] = c.pop("beats", 0.0)
            cc_payload.append(c)

        commands.append(
            {
                "cmd": "item_create_midi",
                "args": {
                    "track": tr_idx,
                    "start_beats": start_beats,
                    "length_beats": max(1.0, float(tr["end_beats"])),
                    "name": tr_name,
                    "notes": notes_payload,
                    "cc": cc_payload,
                },
            }
        )
        created_summary.append({"track_index": tr_idx, "name": tr_name, "notes": len(notes_payload), "cc": len(cc_payload)})

    client.batch(commands, undo_label=f"Import MIDI: {Path(midi_path).name}")
    return {"imported_tracks": len(created_summary), "tracks": created_summary}


def export_reaper_to_midi_file(
    client: ReaperBridgeClient,
    output_path: str | Path,
    *,
    tracks: list[int | str] | None = None,
    ticks_per_beat: int = 480,
) -> dict[str, Any]:
    """Export MIDI tracks/items + tempo map from live REAPER to a Standard MIDI File (.mid)."""
    proj = client.call("project_get_info", {})
    tmarkers = client.call("project_list_tempo_markers", {}).get("markers", [])
    tempo_changes: list[dict[str, Any]] = []
    time_sigs: list[dict[str, Any]] = []
    for tm in tmarkers:
        if tm.get("bpm", 0) > 0:
            tempo_changes.append({"beats": tm["time_beats"], "bpm": tm["bpm"]})
        if tm.get("time_sig_num", 0) > 0 and tm.get("time_sig_denom", 0) > 0:
            time_sigs.append(
                {"beats": tm["time_beats"], "numerator": tm["time_sig_num"], "denominator": tm["time_sig_denom"]}
            )
    if not tempo_changes:
        tempo_changes = [{"beats": 0.0, "bpm": proj["bpm"]}]
    if not time_sigs:
        time_sigs = [{"beats": 0.0, "numerator": proj["time_sig_num"], "denominator": proj["time_sig_denom"]}]

    if tracks is None:
        all_tracks = client.call("track_list", {}).get("tracks", [])
        target_track_infos = all_tracks
    else:
        target_track_infos = [client.call("track_get", {"track": t, "include_fx": False, "include_sends": False}) for t in tracks]

    midi_tracks: list[dict[str, Any]] = []
    for tr_info in target_track_infos:
        items = client.call("item_list", {"track": tr_info["index"]}).get("items", [])
        midi_items = [it for it in items if it.get("is_midi") and not it.get("mute")]
        if not midi_items:
            continue
        merged_notes: list[dict[str, Any]] = []
        merged_cc: list[dict[str, Any]] = []
        merged_text: list[dict[str, Any]] = []

        for it in midi_items:
            evts = client.call("midi_get_events", {"item_guid": it["guid"]})
            for n in evts.get("notes", []):
                if n.get("muted"):
                    continue
                merged_notes.append(
                    {
                        "pitch": n["pitch"],
                        "velocity": n["velocity"],
                        "channel": n["channel"],
                        "start_beats": max(0.0, float(n["start_beats"])),
                        "end_beats": max(0.01, float(n["end_beats"])),
                    }
                )
            for c in evts.get("cc", []):
                if c.get("muted"):
                    continue
                merged_cc.append(
                    {
                        "kind": c.get("kind", "cc"),
                        "beats": max(0.0, float(c["beats"])),
                        "channel": c.get("channel", 0),
                        "cc": c.get("cc"),
                        "value": c.get("value"),
                        "program": c.get("program"),
                        "pitch": c.get("pitch"),
                    }
                )
            for tx in evts.get("text", []):
                merged_text.append({"beats": max(0.0, float(tx["beats"])), "type": "text", "text": tx.get("text", "")})

        if merged_notes or merged_cc:
            midi_tracks.append(
                {
                    "name": tr_info.get("name") or f"Track {tr_info['index'] + 1}",
                    "notes": merged_notes,
                    "cc": merged_cc,
                    "text": merged_text,
                }
            )

    return create_midi_file(
        output_path,
        midi_tracks,
        ticks_per_beat=ticks_per_beat,
        tempo_changes=tempo_changes,
        time_signatures=time_sigs,
    )
