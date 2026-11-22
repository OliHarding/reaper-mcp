# REAPER Agent Interface — Workspace Instructions

This workspace provides a complete interface for controlling **REAPER**, editing **MIDI** (both live in REAPER and standalone `.mid` files), and discovering/adding/editing **FX & Instruments** (VST3, VST2, CLAP, JSFX, FXChains, presets, parameters, and automation envelopes).

## How to Use

1. **MCP Server (`reaper`)** — Registered in `~/.gemini/config/mcp_config.json` (stdio FastMCP server). When loaded, use the `reaper_*` and `midi_file_*` MCP tools directly.
2. **Immediate CLI (`reaper-cli`)** — Available at any time without restarting Antigravity:
   ```powershell
   uv run reaper-cli status
   uv run reaper-cli project
   uv run reaper-cli fx-catalog --kind instrument --query "Surge"
   uv run reaper-cli track-create --name "Lead Synth" --instrument "Surge XT"
   uv run reaper-cli call <handler_name> --json '{...}'
   ```

## Architecture

- **REAPER Lua Bridge**: `lua/reaper_agent_bridge.lua` + `lua/json.lua` (installed to REAPER's `Scripts/ReaperAgentBridge/` directory and auto-started via `Scripts/__startup.lua`).
- **IPC Queue**: Atomic JSON file queue (Windows: `%LOCALAPPDATA%\ReaperAgentBridge\ipc`; macOS: `~/Library/Application Support/REAPER/ReaperAgentBridgeIPC`; Linux: `~/.config/REAPER/ReaperAgentBridgeIPC` — kept outside cloud-synced folders to prevent sync locks).
- **Offline MIDI & FX Engine**: `src/reaper_interface/midi_files.py` and `src/reaper_interface/fx_catalog.py` work even if REAPER is closed.

## Best Practices When Editing REAPER

- Always check `reaper_status` (or `uv run reaper-cli status`) first. If the bridge is offline, tell the user to restart REAPER or run `ReaperAgentBridge/reaper_agent_bridge.lua` once from REAPER's Actions list.
- For large synths (Surge XT, Massive X, Kontakt, Dexed, LABS, Opus), always pass `search` to `reaper_fx_get_params` (e.g., `search="cutoff"`, `search="attack"`, `search="reverb"`) instead of dumping all parameters.
- When setting FX parameters, prefer `target_formatted` (e.g., `"-12 dB"`, `"800 Hz"`, `"Saw"`) or `value_norm` (`0.0`–`1.0`) and verify the returned `formatted` value.
- Prefer `reaper_batch` when building multi-track / multi-step arrangements so the user gets a single `Ctrl+Z` Undo step.
- For `.mid` import into REAPER, always use `reaper_import_midi_file` (never `InsertMedia` on multi-track `.mid` files, which pops a blocking modal dialog).
