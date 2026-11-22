"""FastMCP server exposing REAPER control, MIDI file editing, and FX/Instrument management."""

from __future__ import annotations

import json
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - fallback for mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from .bridge_client import ReaperBridgeClient
from .fx_catalog import list_cached_presets, list_installed_fx
from .midi_files import (
    create_midi_file,
    edit_midi_file,
    export_reaper_to_midi_file,
    import_midi_file_to_reaper,
    inspect_midi_file,
)
from .music_theory import expand_note_specs, get_scale_pitches, parse_chord, pitch_to_name

mcp = MCPServer(
    "reaper",
    instructions=(
        "Full interface for controlling live REAPER DAW sessions, inspecting/creating/editing "
        "Standard MIDI Files (.mid), and discovering, inserting, and editing VST/VST3/CLAP/JSFX "
        "instruments & effects, presets, parameters, and automation envelopes."
    ),
)

_client = ReaperBridgeClient()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------------
# 1. Bridge Status, Project & Transport
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_status() -> str:
    """Check connection status to REAPER (heartbeat + live ping if connected)."""
    hb = _client.get_heartbeat()
    if hb.get("connected"):
        try:
            live = _client.ping(timeout_sec=3.0)
            return _dumps({"connected": True, "heartbeat": hb, "live": live})
        except Exception as exc:
            return _dumps({"connected": False, "heartbeat": hb, "error": str(exc)})
    return _dumps({"connected": False, "heartbeat": hb})


@mcp.tool()
def reaper_project_info(include_tracks: bool = True, include_fx: bool = True, include_sends: bool = False) -> str:
    """Get active REAPER project state: name, path, BPM, time sig, cursor, loop, play state, and tracks."""
    res = _client.call(
        "project_get_info",
        {"include_tracks": include_tracks, "include_fx": include_fx, "include_sends": include_sends},
    )
    return _dumps(res)


@mcp.tool()
def reaper_set_tempo(
    bpm: float,
    time_sig_num: int | None = None,
    time_sig_denom: int | None = None,
    time_beats: float | None = None,
    linear: bool = False,
) -> str:
    """Set project BPM and optional time signature (at project start or at a specific beat position)."""
    args: dict[str, Any] = {"bpm": bpm, "linear": linear}
    if time_sig_num is not None:
        args["time_sig_num"] = time_sig_num
    if time_sig_denom is not None:
        args["time_sig_denom"] = time_sig_denom
    if time_beats is not None:
        args["time_beats"] = time_beats
    return _dumps(_client.call("project_set_tempo", args, undo_label=f"Set Tempo {bpm} BPM"))


@mcp.tool()
def reaper_transport(
    action: str | None = None,
    cursor_beats: float | None = None,
    cursor_sec: float | None = None,
    loop_start_beats: float | None = None,
    loop_end_beats: float | None = None,
    repeat_enabled: bool | None = None,
) -> str:
    """Control REAPER transport ('play', 'stop', 'pause', 'record', 'play_pause'), edit cursor, and loop range."""
    args: dict[str, Any] = {}
    if action is not None:
        args["action"] = action
    if cursor_beats is not None:
        args["cursor_beats"] = cursor_beats
    if cursor_sec is not None:
        args["cursor_sec"] = cursor_sec
    if loop_start_beats is not None:
        args["loop_start_beats"] = loop_start_beats
    if loop_end_beats is not None:
        args["loop_end_beats"] = loop_end_beats
    if repeat_enabled is not None:
        args["repeat_enabled"] = repeat_enabled
    return _dumps(_client.call("transport_control", args))


@mcp.tool()
def reaper_markers(
    action: str = "list",
    name: str = "",
    position_beats: float | None = None,
    end_beats: float | None = None,
    is_region: bool = False,
    color: str | None = None,
    marker_id: int | None = None,
) -> str:
    """List, add, or delete project markers and regions. action: 'list' | 'add' | 'delete'."""
    if action == "list":
        return _dumps(_client.call("marker_list", {}))
    if action == "add":
        args: dict[str, Any] = {"name": name, "is_region": is_region}
        if position_beats is not None:
            args["position_beats"] = position_beats
        if end_beats is not None:
            args["end_beats"] = end_beats
        if color:
            args["color"] = color
        if marker_id is not None:
            args["id"] = marker_id
        return _dumps(_client.call("marker_add", args, undo_label=f"Add Marker {name}"))
    if action == "delete":
        if marker_id is None:
            raise ValueError("marker_id is required to delete a marker")
        return _dumps(_client.call("marker_delete", {"id": marker_id, "is_region": is_region}))
    raise ValueError(f"Unknown marker action: {action}")


@mcp.tool()
def reaper_project_file(action: str, path: str | None = None, new_tab: bool = False, no_prompt: bool = False) -> str:
    """Save, open, or create a REAPER project. action: 'save' | 'open' | 'new'."""
    if action == "save":
        return _dumps(_client.call("project_save", {"save_as": False}))
    if action == "open":
        if not path:
            raise ValueError("path is required for action='open'")
        return _dumps(_client.call("project_open", {"path": path, "no_prompt": no_prompt}))
    if action == "new":
        return _dumps(_client.call("project_new", {"new_tab": new_tab}))
    raise ValueError(f"Unknown project file action: {action}")


# -----------------------------------------------------------------------------
# 2. Tracks & Routing
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_list_tracks(include_fx: bool = True, include_sends: bool = False, include_master: bool = False) -> str:
    """List all tracks in the active REAPER project (including mixer levels, mute/solo, FX, and sends)."""
    return _dumps(
        _client.call(
            "track_list",
            {"include_fx": include_fx, "include_sends": include_sends, "include_master": include_master},
        )
    )


@mcp.tool()
def reaper_create_track(
    name: str = "",
    index: int | None = None,
    instrument: str | None = None,
    fx: list[str] | None = None,
    volume_db: float | None = None,
    pan: float | None = None,
    color: str | None = None,
    rec_arm: bool | None = None,
    open_ui: bool = False,
) -> str:
    """Create a new track in REAPER, optionally adding an instrument plugin (auto-configured for MIDI) and FX chain."""
    args: dict[str, Any] = {"name": name, "open_ui": open_ui}
    if index is not None:
        args["index"] = index
    if instrument:
        args["instrument"] = instrument
    if fx:
        args["fx"] = fx
    if volume_db is not None:
        args["volume_db"] = volume_db
    if pan is not None:
        args["pan"] = pan
    if color:
        args["color"] = color
    if rec_arm is not None:
        args["rec_arm"] = rec_arm
    return _dumps(_client.call("track_create", args, undo_label=f"Create Track {name or 'New'}"))


@mcp.tool()
def reaper_edit_track(
    track: int | str,
    name: str | None = None,
    volume_db: float | None = None,
    pan: float | None = None,
    mute: bool | None = None,
    solo: int | bool | None = None,
    rec_arm: bool | None = None,
    rec_mon: int | bool | None = None,
    midi_input_all: bool | None = None,
    color: str | None = None,
    folder_depth: int | None = None,
) -> str:
    """Edit properties of a REAPER track (accepts 0-based index, 'master', track name, or GUID)."""
    args: dict[str, Any] = {"track": track}
    for k, v in (
        ("name", name),
        ("volume_db", volume_db),
        ("pan", pan),
        ("mute", mute),
        ("solo", solo),
        ("rec_arm", rec_arm),
        ("rec_mon", rec_mon),
        ("color", color),
        ("folder_depth", folder_depth),
    ):
        if v is not None:
            args[k] = v
    if midi_input_all:
        args["midi_input"] = "all"
    return _dumps(_client.call("track_edit", args, undo_label=f"Edit Track {track}"))


@mcp.tool()
def reaper_delete_track(track: int | str) -> str:
    """Delete a track by 0-based index, name, or GUID."""
    return _dumps(_client.call("track_delete", {"track": track}, undo_label=f"Delete Track {track}"))


@mcp.tool()
def reaper_track_send(
    action: str,
    track: int | str,
    dest_track: int | str | None = None,
    send_index: int | None = None,
    volume_db: float | None = None,
    pan: float | None = None,
    mute: bool | None = None,
    send_mode: int | None = None,
) -> str:
    """Manage audio/MIDI sends on a track. action: 'list' | 'add' | 'edit' | 'delete'.
    send_mode: 0=Post-Fader, 1=Pre-FX, 3=Pre-Fader."""
    if action == "list":
        return _dumps(_client.call("track_list_sends", {"track": track}))
    if action == "add":
        if dest_track is None:
            raise ValueError("dest_track is required for action='add'")
        args: dict[str, Any] = {"src_track": track, "dest_track": dest_track}
        if volume_db is not None:
            args["volume_db"] = volume_db
        if pan is not None:
            args["pan"] = pan
        if mute is not None:
            args["mute"] = mute
        if send_mode is not None:
            args["send_mode"] = send_mode
        return _dumps(_client.call("track_add_send", args, undo_label="Add Track Send"))
    if action == "edit":
        if send_index is None:
            raise ValueError("send_index is required for action='edit'")
        args = {"src_track": track, "send_index": send_index}
        if volume_db is not None:
            args["volume_db"] = volume_db
        if pan is not None:
            args["pan"] = pan
        if mute is not None:
            args["mute"] = mute
        if send_mode is not None:
            args["send_mode"] = send_mode
        return _dumps(_client.call("track_edit_send", args, undo_label="Edit Track Send"))
    if action == "delete":
        if send_index is None:
            raise ValueError("send_index is required for action='delete'")
        return _dumps(_client.call("track_delete_send", {"src_track": track, "send_index": send_index}))
    raise ValueError(f"Unknown send action: {action}")


# -----------------------------------------------------------------------------
# 3. Media Items & Live REAPER MIDI
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_list_items(track: int | str | None = None) -> str:
    """List media items (MIDI and audio) on a specific track or across the whole project."""
    args: dict[str, Any] = {}
    if track is not None:
        args["track"] = track
    return _dumps(_client.call("item_list", args))


@mcp.tool()
def reaper_create_midi_item(
    track: int | str,
    start_beats: float = 0.0,
    length_beats: float = 4.0,
    name: str = "",
    notes: list[dict[str, Any]] | None = None,
    cc: list[dict[str, Any]] | None = None,
    loop: bool = False,
    color: str | None = None,
) -> str:
    """Create a new MIDI item on a track and optionally populate it with notes, chords, and CC events.
    Each note supports: pitch (int) OR note ('C4', 'F#3', 'kick') OR chord ('Cmaj7', 'Am9', optional octave/inversion/strum_beats),
    start_beat_rel (beats from item start) or start_beats (project beat), duration_beats, velocity (1-127), channel (0-15)."""
    args: dict[str, Any] = {
        "track": track,
        "start_beats": start_beats,
        "length_beats": length_beats,
        "name": name,
        "loop": loop,
    }
    if color:
        args["color"] = color
    if notes:
        args["notes"] = expand_note_specs(notes)
    if cc:
        args["cc"] = cc
    return _dumps(_client.call("item_create_midi", args, undo_label=f"Create MIDI Item {name}"))


@mcp.tool()
def reaper_edit_item(
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    position_beats: float | None = None,
    length_beats: float | None = None,
    name: str | None = None,
    mute: bool | None = None,
    loop: bool | None = None,
    volume_db: float | None = None,
    playrate: float | None = None,
    pitch: float | None = None,
    color: str | None = None,
) -> str:
    """Edit a media item's position, length, name, mute, loop, volume, playrate, pitch, or color."""
    args: dict[str, Any] = {"item_index": item_index}
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    for k, v in (
        ("position_beats", position_beats),
        ("length_beats", length_beats),
        ("name", name),
        ("mute", mute),
        ("loop", loop),
        ("volume_db", volume_db),
        ("playrate", playrate),
        ("pitch", pitch),
        ("color", color),
    ):
        if v is not None:
            args[k] = v
    return _dumps(_client.call("item_edit", args, undo_label="Edit Media Item"))


@mcp.tool()
def reaper_delete_item(track: int | str | None = None, item_index: int = 0, item_guid: str | None = None) -> str:
    """Delete a media item from a track."""
    args: dict[str, Any] = {"item_index": item_index}
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    return _dumps(_client.call("item_delete", args, undo_label="Delete Media Item"))


@mcp.tool()
def reaper_midi_read(
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    include_notes: bool = True,
    include_cc: bool = True,
    include_text: bool = True,
) -> str:
    """Read all MIDI notes, CCs, pitch bends, program changes, and text events from a REAPER MIDI item."""
    args: dict[str, Any] = {
        "item_index": item_index,
        "include_notes": include_notes,
        "include_cc": include_cc,
        "include_text": include_text,
    }
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    return _dumps(_client.call("midi_get_events", args))


@mcp.tool()
def reaper_midi_insert_notes(
    notes: list[dict[str, Any]],
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    clear_existing: bool = False,
) -> str:
    """Insert (or replace) MIDI notes/chords in an existing REAPER MIDI take.
    Supports 'pitch' (0-127), 'note' ('C4', 'Bb3', 'kick'), or 'chord' ('Cmaj7', 'Dm9')."""
    args: dict[str, Any] = {
        "item_index": item_index,
        "notes": expand_note_specs(notes),
        "clear_existing": clear_existing,
    }
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    return _dumps(_client.call("midi_insert_notes", args, undo_label="Insert MIDI Notes"))


@mcp.tool()
def reaper_midi_edit_notes(
    edits: list[dict[str, Any]],
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
) -> str:
    """Edit specific MIDI notes by their index from reaper_midi_read (pitch/note, start/end/duration, velocity, channel, muted)."""
    args: dict[str, Any] = {"item_index": item_index, "edits": edits}
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    return _dumps(_client.call("midi_edit_notes", args, undo_label="Edit MIDI Notes"))


@mcp.tool()
def reaper_midi_delete_notes(
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    indices: list[int] | None = None,
    pitch: int | str | None = None,
    min_pitch: int | str | None = None,
    max_pitch: int | str | None = None,
    start_beat_rel: float | None = None,
    end_beat_rel: float | None = None,
    channel: int | None = None,
) -> str:
    """Delete MIDI notes in a REAPER take by explicit indices OR by pitch/time/channel filters."""
    args: dict[str, Any] = {"item_index": item_index}
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    for k, v in (
        ("indices", indices),
        ("pitch", pitch),
        ("min_pitch", min_pitch),
        ("max_pitch", max_pitch),
        ("start_beat_rel", start_beat_rel),
        ("end_beat_rel", end_beat_rel),
        ("channel", channel),
    ):
        if v is not None:
            args[k] = v
    return _dumps(_client.call("midi_delete_notes", args, undo_label="Delete MIDI Notes"))


@mcp.tool()
def reaper_midi_transform(
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    transpose: int = 0,
    quantize_beats: float | None = None,
    quantize_strength: float = 1.0,
    humanize_beats: float = 0.0,
    humanize_velocity: int = 0,
    velocity_scale: float = 1.0,
    velocity_offset: int = 0,
    velocity_set: int | None = None,
    shift_beats: float = 0.0,
    speed_scale: float | None = None,
) -> str:
    """Transform MIDI notes in a REAPER take: transpose, quantize, humanize, adjust velocity, shift, or scale speed."""
    args: dict[str, Any] = {
        "item_index": item_index,
        "transpose": transpose,
        "quantize_strength": quantize_strength,
        "humanize_beats": humanize_beats,
        "humanize_velocity": humanize_velocity,
        "velocity_scale": velocity_scale,
        "velocity_offset": velocity_offset,
        "shift_beats": shift_beats,
    }
    if track is not None:
        args["track"] = track
    if item_guid is not None:
        args["item_guid"] = item_guid
    if quantize_beats is not None:
        args["quantize_beats"] = quantize_beats
    if velocity_set is not None:
        args["velocity_set"] = velocity_set
    if speed_scale is not None:
        args["speed_scale"] = speed_scale
    return _dumps(_client.call("midi_transform_notes", args, undo_label="Transform MIDI Notes"))


@mcp.tool()
def reaper_midi_cc(
    action: str,
    track: int | str | None = None,
    item_index: int = 0,
    item_guid: str | None = None,
    events: list[dict[str, Any]] | None = None,
    cc: int | None = None,
    start_beat_rel: float | None = None,
    end_beat_rel: float | None = None,
    start_value: int = 0,
    end_value: int = 127,
    steps: int = 16,
    curve: float = 1.0,
    channel: int = 0,
    clear_existing: bool = False,
) -> str:
    """Insert, ramp, or delete MIDI CC / Pitch Bend / Program Change events inside a REAPER MIDI take.
    action: 'insert' (uses events list) | 'ramp' (uses cc, start/end_beat_rel, start/end_value, steps, curve) | 'delete' (uses cc/channel)."""
    base: dict[str, Any] = {"item_index": item_index}
    if track is not None:
        base["track"] = track
    if item_guid is not None:
        base["item_guid"] = item_guid

    if action == "insert":
        base["events"] = events or []
        base["clear_existing"] = clear_existing
        return _dumps(_client.call("midi_insert_cc", base, undo_label="Insert MIDI CC"))
    if action == "ramp":
        base.update(
            {
                "cc": cc if cc is not None else 1,
                "start_beat_rel": start_beat_rel if start_beat_rel is not None else 0.0,
                "end_beat_rel": end_beat_rel if end_beat_rel is not None else 4.0,
                "start_value": start_value,
                "end_value": end_value,
                "steps": steps,
                "curve": curve,
                "channel": channel,
            }
        )
        return _dumps(_client.call("midi_insert_cc_ramp", base, undo_label="Insert MIDI CC Ramp"))
    if action == "delete":
        if cc is not None:
            base["cc"] = cc
        return _dumps(_client.call("midi_delete_cc", base, undo_label="Delete MIDI CC"))
    raise ValueError(f"Unknown midi_cc action: {action}")


# -----------------------------------------------------------------------------
# 4. Standalone .mid Files + Non-Blocking REAPER Import / Export
# -----------------------------------------------------------------------------


@mcp.tool()
def midi_file_inspect(
    path: str,
    include_events: bool = True,
    track_index: int | None = None,
    max_events_per_track: int = 400,
) -> str:
    """Inspect a standalone Standard MIDI File (.mid) on disk (works even when REAPER is closed)."""
    return _dumps(
        inspect_midi_file(
            path,
            include_events=include_events,
            track_index=track_index,
            max_events_per_track=max_events_per_track,
        )
    )


@mcp.tool()
def midi_file_create(
    output_path: str,
    tracks: list[dict[str, Any]],
    bpm: float = 120.0,
    time_sig_num: int = 4,
    time_sig_denom: int = 4,
    ticks_per_beat: int = 480,
) -> str:
    """Create a multi-track Standard MIDI File (.mid) on disk from note names, chords, drum names, and CCs."""
    return _dumps(
        create_midi_file(
            output_path,
            tracks,
            bpm=bpm,
            time_sig_num=time_sig_num,
            time_sig_denom=time_sig_denom,
            ticks_per_beat=ticks_per_beat,
        )
    )


@mcp.tool()
def midi_file_edit(
    input_path: str,
    operations: list[dict[str, Any]],
    output_path: str | None = None,
) -> str:
    """Edit a standalone .mid file with composable operations:
    set_tempo, set_time_sig, rename_track, add_track, remove_track, add_notes, replace_notes,
    delete_notes, transpose, scale_snap, quantize, humanize, scale_velocity, shift_time,
    scale_speed, add_cc, add_cc_ramp, delete_cc."""
    return _dumps(edit_midi_file(input_path, operations, output_path=output_path))


@mcp.tool()
def reaper_import_midi_file(
    midi_path: str,
    target_track: int | str | None = None,
    expand_tracks: bool = True,
    start_beats: float = 0.0,
    import_tempo: bool = False,
    instrument: str | None = None,
) -> str:
    """Import a .mid file into live REAPER (zero modal dialogs; optionally expands tracks, sets tempo, and loads an instrument)."""
    return _dumps(
        import_midi_file_to_reaper(
            _client,
            midi_path,
            target_track=target_track,
            expand_tracks=expand_tracks,
            start_beats=start_beats,
            import_tempo=import_tempo,
            instrument=instrument,
        )
    )


@mcp.tool()
def reaper_export_midi_file(
    output_path: str,
    tracks: list[int | str] | None = None,
    ticks_per_beat: int = 480,
) -> str:
    """Export MIDI tracks/items and project tempo/time-sig map from live REAPER to a .mid file on disk."""
    return _dumps(export_reaper_to_midi_file(_client, output_path, tracks=tracks, ticks_per_beat=ticks_per_beat))


# -----------------------------------------------------------------------------
# 5. FX & Instruments (Discovery, Insertion, Parameters, Presets, Chunks)
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_search_installed_fx(
    query: str | None = None,
    kind: str = "all",
    fmt: str = "all",
    developer: str | None = None,
    use_live_reaper: bool = False,
    offset: int = 0,
    limit: int = 60,
) -> str:
    """Search installed VST3, VST2, CLAP, AU (macOS), JSFX plugins, instruments, and FXChains.
    kind: 'all' | 'instrument' | 'effect' | 'fxchain'. fmt: 'all' | 'vst3' | 'vst2' | 'clap' | 'au' | 'jsfx' | 'fxchain'."""
    if use_live_reaper:
        return _dumps(
            _client.call(
                "fx_enum_installed",
                {"query": query, "kind": kind, "format": fmt, "offset": offset, "limit": limit},
            )
        )
    return _dumps(
        list_installed_fx(
            query=query,
            kind=kind,
            fmt=fmt,
            developer=developer,
            offset=offset,
            limit=limit,
        )
    )


@mcp.tool()
def reaper_search_cached_presets(plugin_query: str | None = None, limit_per_plugin: int = 100) -> str:
    """Search REAPER's cached factory and user preset lists (.ini files in REAPER/presets) without opening plugins."""
    return _dumps(list_cached_presets(plugin_query=plugin_query, limit_per_plugin=limit_per_plugin))


@mcp.tool()
def reaper_fx_list(track: int | str, rec_fx: bool = False) -> str:
    """List all FX and instruments currently loaded on a track (or 'master', or Input FX with rec_fx=True)."""
    return _dumps(_client.call("fx_list", {"track": track, "rec_fx": rec_fx}))


@mcp.tool()
def reaper_fx_add(
    track: int | str,
    fx_name: str,
    prefer_instrument: bool = False,
    position: int | None = None,
    rec_fx: bool = False,
    configure_midi_input: bool = False,
    preset_name: str | None = None,
    preset_index: int | None = None,
    params: list[dict[str, Any]] | None = None,
    open_ui: bool = False,
) -> str:
    """Add an FX or instrument plugin (VST3/VST2/CLAP/JSFX/FXChain) to a track with smart name matching.
    Optionally applies a preset, sets initial parameters, configures MIDI input, and opens the floating UI."""
    args: dict[str, Any] = {
        "track": track,
        "fx_name": fx_name,
        "prefer_instrument": prefer_instrument,
        "rec_fx": rec_fx,
        "configure_midi_input": configure_midi_input,
        "open_ui": open_ui,
    }
    if position is not None:
        args["position"] = position
    if preset_name is not None:
        args["preset_name"] = preset_name
    if preset_index is not None:
        args["preset_index"] = preset_index
    if params:
        args["params"] = params
    return _dumps(_client.call("fx_add", args, undo_label=f"Add FX {fx_name}", timeout_sec=30.0))


@mcp.tool()
def reaper_fx_get_params(
    track: int | str,
    fx: int | str,
    search: str | None = None,
    offset: int = 0,
    limit: int = 80,
    rec_fx: bool = False,
) -> str:
    """List parameters of an FX or instrument with normalized values, raw ranges, and formatted display strings (e.g. '440 Hz', '-6 dB').
    Use `search` to filter large synths (e.g. 'cutoff', 'attack', 'reverb', 'osc')."""
    args: dict[str, Any] = {"track": track, "fx": fx, "offset": offset, "limit": limit, "rec_fx": rec_fx}
    if search:
        args["search"] = search
    return _dumps(_client.call("fx_get_params", args))


@mcp.tool()
def reaper_fx_set_params(
    track: int | str,
    fx: int | str,
    params: list[dict[str, Any]],
    rec_fx: bool = False,
) -> str:
    """Edit one or many parameters on an FX or instrument.
    Each item in `params` takes:
      - `param`: int index OR parameter name string (exact or substring match, or ':wet', ':bypass', ':delta')
      - ONE of: `value_norm` (0.0-1.0), `value_raw` (plugin native range), OR `target_formatted` (e.g. '-12 dB', '800 Hz', 'Saw')."""
    return _dumps(
        _client.call(
            "fx_set_params",
            {"track": track, "fx": fx, "params": params, "rec_fx": rec_fx},
            undo_label=f"Edit FX Params ({fx})",
        )
    )


@mcp.tool()
def reaper_fx_configure(
    track: int | str,
    fx: int | str,
    enabled: bool | None = None,
    offline: bool | None = None,
    wet: float | None = None,
    delta: bool | None = None,
    open_ui: bool | None = None,
    show_chain: bool | None = None,
    rename: str | None = None,
    rec_fx: bool = False,
) -> str:
    """Configure an FX slot: bypass/enable, offline, wet/dry mix (0.0-1.0), delta solo, open/close UI window, or rename."""
    args: dict[str, Any] = {"track": track, "fx": fx, "rec_fx": rec_fx}
    for k, v in (
        ("enabled", enabled),
        ("offline", offline),
        ("wet", wet),
        ("delta", delta),
        ("open_ui", open_ui),
        ("show_chain", show_chain),
        ("rename", rename),
    ):
        if v is not None:
            args[k] = v
    return _dumps(_client.call("fx_configure", args, undo_label=f"Configure FX {fx}"))


@mcp.tool()
def reaper_fx_preset(
    track: int | str,
    fx: int | str,
    action: str = "get",
    preset_name: str | None = None,
    preset_index: int | None = None,
    navigate: int | None = None,
    scan_limit: int = 64,
    scan_offset: int = 0,
    rec_fx: bool = False,
) -> str:
    """Get, set, navigate, or scan presets on a loaded FX/instrument.
    action: 'get' | 'set' (uses preset_name, preset_index, or navigate +1/-1) | 'scan' (lists factory presets)."""
    base: dict[str, Any] = {"track": track, "fx": fx, "rec_fx": rec_fx}
    if action == "get":
        return _dumps(_client.call("fx_get_preset", base))
    if action == "set":
        if preset_name is not None:
            base["preset_name"] = preset_name
        elif preset_index is not None:
            base["preset_index"] = preset_index
        elif navigate is not None:
            base["navigate"] = navigate
        else:
            raise ValueError("Provide preset_name, preset_index, or navigate for action='set'")
        return _dumps(_client.call("fx_set_preset", base, undo_label="Change FX Preset"))
    if action == "scan":
        base["limit"] = scan_limit
        base["offset"] = scan_offset
        return _dumps(_client.call("fx_scan_presets", base, timeout_sec=30.0))
    raise ValueError(f"Unknown preset action: {action}")


@mcp.tool()
def reaper_fx_named_config(
    track: int | str,
    fx: int | str,
    action: str = "get",
    parm_name: str | None = None,
    value: str | None = None,
    parms: list[dict[str, str]] | None = None,
    rec_fx: bool = False,
) -> str:
    """Get or set REAPER TrackFX NamedConfigParm values (e.g. ReaSamplOmatic5000 FILE0/DONE to load WAV samples,
    ReaEQ BANDTYPE0/BANDFREQ0/BANDGAIN0/BANDQ0, or plugin state chunks)."""
    base: dict[str, Any] = {"track": track, "fx": fx, "rec_fx": rec_fx}
    if action == "get":
        if not parm_name:
            raise ValueError("parm_name is required for action='get'")
        base["parm_name"] = parm_name
        return _dumps(_client.call("fx_get_named_config_parm", base))
    if action == "set":
        if parms is not None:
            base["parms"] = parms
        else:
            if not parm_name or value is None:
                raise ValueError("Provide parm_name and value (or parms list) for action='set'")
            base["parm_name"] = parm_name
            base["value"] = value
        return _dumps(_client.call("fx_set_named_config_parm", base, undo_label="Set FX Config Parm"))
    raise ValueError(f"Unknown named_config action: {action}")


@mcp.tool()
def reaper_fx_manage(
    action: str,
    track: int | str,
    fx: int | str,
    dest_track: int | str | None = None,
    dest_index: int | None = None,
    rec_fx: bool = False,
) -> str:
    """Delete, reorder (move), or copy an FX/instrument between slots or tracks.
    action: 'delete' | 'move' | 'copy'."""
    if action == "delete":
        return _dumps(_client.call("fx_delete", {"track": track, "fx": fx, "rec_fx": rec_fx}, undo_label="Delete FX"))
    if action == "move":
        if dest_track is not None and dest_track != track:
            return _dumps(
                _client.call(
                    "fx_copy",
                    {"src_track": track, "dest_track": dest_track, "fx": fx, "dest_index": dest_index, "move": True},
                    undo_label="Move FX",
                )
            )
        if dest_index is None:
            raise ValueError("dest_index is required for action='move' on same track")
        return _dumps(
            _client.call("fx_move", {"track": track, "fx": fx, "dest_index": dest_index, "rec_fx": rec_fx}, undo_label="Reorder FX")
        )
    if action == "copy":
        if dest_track is None:
            dest_track = track
        return _dumps(
            _client.call(
                "fx_copy",
                {"src_track": track, "dest_track": dest_track, "fx": fx, "dest_index": dest_index, "move": False},
                undo_label="Copy FX",
            )
        )
    raise ValueError(f"Unknown fx_manage action: {action}")


# -----------------------------------------------------------------------------
# 6. Automation Envelopes
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_envelope(
    action: str,
    track: int | str,
    fx: int | str | None = None,
    param: int | str | None = None,
    envelope_name: str | None = None,
    points: list[dict[str, Any]] | None = None,
    clear_existing: bool = False,
    start_beats: float | None = None,
    end_beats: float | None = None,
) -> str:
    """List, read, write, or clear track or FX parameter automation envelopes.
    action: 'list' | 'get' | 'set' | 'clear'.
    For FX parameter automation, pass `fx` and `param` (envelope is auto-created on 'set').
    Each point in `points`: {time_beats (or time_sec), value, shape (0=linear..5=bezier), tension}."""
    base: dict[str, Any] = {"track": track}
    if fx is not None:
        base["fx"] = fx
    if param is not None:
        base["param"] = param
    if envelope_name is not None:
        base["envelope_name"] = envelope_name

    if action == "list":
        return _dumps(_client.call("envelope_list", {"track": track}))
    if action == "get":
        return _dumps(_client.call("envelope_get_points", base))
    if action == "set":
        base["points"] = points or []
        base["clear_existing"] = clear_existing
        return _dumps(_client.call("envelope_set_points", base, undo_label="Edit Automation Envelope"))
    if action == "clear":
        if start_beats is not None:
            base["start_beats"] = start_beats
        if end_beats is not None:
            base["end_beats"] = end_beats
        return _dumps(_client.call("envelope_clear", base, undo_label="Clear Automation Envelope"))
    raise ValueError(f"Unknown envelope action: {action}")


# -----------------------------------------------------------------------------
# 7. Actions, Undo/Redo, Batch, Raw Lua & Music Theory Helper
# -----------------------------------------------------------------------------


@mcp.tool()
def reaper_action(command_id: int | str, section: int = 0) -> str:
    """Run any REAPER action by numeric ID (e.g. 40044) or named command ID (e.g. '_SWS_...')."""
    return _dumps(_client.call("action_run", {"command_id": command_id, "section": section}))


@mcp.tool()
def reaper_undo_redo(action: str = "undo") -> str:
    """Undo or redo the last action in REAPER. action: 'undo' | 'redo'."""
    if action == "undo":
        return _dumps(_client.call("undo", {}))
    if action == "redo":
        return _dumps(_client.call("redo", {}))
    raise ValueError("action must be 'undo' or 'redo'")


@mcp.tool()
def reaper_batch(commands: list[dict[str, Any]], undo_label: str = "ReaperAgent Batch") -> str:
    """Execute multiple bridge commands atomically in one REAPER tick and one Undo step.
    Each command is {'cmd': '<handler_name>', 'args': {...}}."""
    return _dumps(_client.batch(commands, undo_label=undo_label))


@mcp.tool()
def reaper_eval_lua(code: str, undo_label: str = "ReaperAgent Lua") -> str:
    """Advanced escape hatch: execute arbitrary ReaScript Lua code inside REAPER's main thread and return its JSON-serializable result."""
    return _dumps(_client.call("eval_lua", {"code": code}, undo_label=undo_label))


@mcp.tool()
def music_theory_helper(
    chord: str | None = None,
    octave: int = 4,
    inversion: int = 0,
    scale_root: str | None = None,
    scale_name: str | None = None,
) -> str:
    """Helper to resolve chord symbols ('Cmaj7', 'F#m9', 'G7/B') or scale pitches to MIDI note numbers and note names."""
    out: dict[str, Any] = {}
    if chord:
        pitches = parse_chord(chord, octave=octave, inversion=inversion)
        out["chord"] = {"symbol": chord, "pitches": pitches, "notes": [pitch_to_name(p) for p in pitches]}
    if scale_root and scale_name:
        pitches = get_scale_pitches(scale_root, scale_name, low_pitch=48, high_pitch=84)
        out["scale"] = {"root": scale_root, "scale": scale_name, "pitches": pitches, "notes": [pitch_to_name(p) for p in pitches]}
    return _dumps(out)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
