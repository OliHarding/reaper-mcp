---
name: reaper-interface
description: >-
  Control live REAPER DAW projects, inspect/create/edit Standard MIDI Files (.mid),
  and discover, load, and edit VST/VST3/CLAP/JSFX instruments, effects, presets,
  parameters, and automation envelopes via the ReaperAgentBridge MCP server or CLI.
---

# REAPER Interface Skill

Use this skill whenever the user wants to inspect, compose, mix, arrange, add instruments/FX, tweak synth or effect parameters, automate parameters, or read/edit `.mid` files in REAPER or on disk.

## Preferred Interfaces

1. **Native MCP tools (`reaper_*`, `midi_file_*`, `music_theory_helper`)** if the `reaper` MCP server is loaded in your toolset.
2. **CLI fallback (`uv run reaper-cli ...`)** run from this repository root if MCP tools aren't loaded yet in the current session.

## Common Workflows

### 1. Check Connection & Inspect Project
- Call `reaper_status` / `uv run reaper-cli status`.
- Call `reaper_project_info(include_tracks=True, include_fx=True)` to understand tempo, time signature, existing tracks, and loaded plugins.

### 2. Discover & Load Instruments / FX
- Search installed plugins first with `reaper_search_installed_fx(query=..., kind="instrument"|"effect", fmt="all"|"vst3"|"clap"|"vst2"|"jsfx")`.
- Create a track with an instrument ready to play MIDI:
  `reaper_create_track(name="Bass", instrument="Surge XT", fx=["ReaEQ", "ReaComp"])`
- Or add to an existing track:
  `reaper_fx_add(track="Bass", fx_name="CHOWTapeModel", open_ui=False)`

### 3. Edit Synth / FX Parameters & Presets
- Search parameters on an FX: `reaper_fx_get_params(track="Bass", fx="Surge XT", search="cutoff")`
- Edit parameters by index or name using normalized (`0.0–1.0`), raw, or formatted target (`"-6 dB"`, `"650 Hz"`, `"Saw"`):
  `reaper_fx_set_params(track="Bass", fx="Surge XT", params=[{"param": "Filter 1 Cutoff", "target_formatted": "800 Hz"}])`
- Browse cached presets: `reaper_search_cached_presets(plugin_query="ChowKick")`
- Switch presets: `reaper_fx_preset(track=0, fx=0, action="set", preset_name="Tight")`
- Load WAV samples into ReaSamplOmatic5000 via `reaper_fx_named_config(action="set", parms=[{"name": "FILE0", "value": "C:/path/kick.wav"}, {"name": "DONE", "value": ""}])`.
- Automate any FX parameter with `reaper_envelope(action="set", track=0, fx=0, param="Cutoff", points=[{"time_beats": 0, "value": 0.2}, {"time_beats": 16, "value": 0.9}])`.

### 4. Compose & Edit MIDI (Live REAPER or Standalone `.mid`)
- Create a MIDI clip with note names, drum names, or chord symbols:
  ```json
  [
    {"chord": "Am9", "octave": 3, "start_beat_rel": 0, "duration_beats": 4, "velocity": 88, "strum_beats": 0.03},
    {"chord": "Fmaj7", "octave": 3, "start_beat_rel": 4, "duration_beats": 4, "velocity": 84},
    {"note": "kick", "start_beat_rel": 0, "duration_beats": 0.25, "velocity": 115}
  ]
  ```
- Read existing MIDI: `reaper_midi_read(track=0, item_index=0)`
- Edit / delete / transpose / quantize / humanize: `reaper_midi_edit_notes`, `reaper_midi_delete_notes`, `reaper_midi_transform`, `reaper_midi_cc`.
- Standalone `.mid` files on disk: `midi_file_inspect`, `midi_file_create`, `midi_file_edit`.
- Move `.mid` between disk and REAPER with zero popups: `reaper_import_midi_file`, `reaper_export_midi_file`.

### 5. Reinstalling / Updating the REAPER Lua Bridge
If `lua/reaper_agent_bridge.lua` is updated, re-run:
```powershell
uv run python scripts/install_reaper_bridge.py
```
and re-run the script inside REAPER (it cleanly replaces the previous running instance automatically).
