# REAPER MCP (`reaper-interface`)

A cross-platform **Model Context Protocol (MCP) server**, CLI, Python API, and lightweight **REAPER Lua bridge** that lets AI agents (Antigravity, Claude Desktop, Cursor, Claude Code, etc.) control live **REAPER** projects, create/edit **MIDI** (both live in REAPER and standalone `.mid` files), and discover, insert, edit, and automate **VST3 / VST2 / CLAP / AU / JSFX** instruments & effects.

Works on **macOS (Apple Silicon & Intel)**, **Windows**, and **Linux** with **zero** Python DLL setup inside REAPER and **zero** port conflicts.

---

## Features (42 MCP Tools)

- **Project & Transport**: Inspect project state, set tempo & time signatures, play/stop/pause/record, move cursor, set loop ranges, manage markers & regions, save/open/new projects, undo/redo, run any REAPER/SWS action, atomic batching (single `Cmd+Z`/`Ctrl+Z` undo step), and `reaper_eval_lua`.
- **Tracks & Routing**: Create, edit, and delete tracks (volume dB, pan, mute, solo, record arm, monitoring, MIDI input, color, folders) and manage sends/receives.
- **Live MIDI & Standalone `.mid` Files**:
  - Create/read/edit/delete/transform MIDI notes (supports note names like `C4`, `F#3`, GM drums like `kick`/`snare`/`hh`, and chords like `Cmaj7`, `Am9`, `C/E` with optional strum).
  - MIDI CC / Pitch Bend / Expression / Sustain / Aftertouch automation (`reaper_midi_cc` with discrete events, curve shapes, and smooth ramps).
  - Standalone `.mid` file inspection, creation, and editing (even with REAPER closed), plus non-blocking `.mid` import/export with REAPER (no modal popups).
- **FX & Instruments**:
  - Offline & live search across installed **VST3, VST2, CLAP, Audio Units (AU/AUi on macOS), JSFX, and FXChains**, plus cached factory/user presets.
  - Add instruments/FX with smart name matching, search/inspect parameters, set parameters by normalized value (`0.0–1.0`), raw value, or formatted display target (`"-12 dB"`, `"800 Hz"`, `"Saw"`), switch/scan presets, configure bypass/wet-dry/UI/rename/move/copy/delete, edit `NamedConfigParm` (e.g., ReaEQ bands, loading WAVs into ReaSamplOmatic5000), and automate any FX parameter via track envelopes (`reaper_envelope`).

---

## Architecture

```
MCP Client (Antigravity / Claude Desktop / Cursor / Claude Code)
   │  stdio MCP (42 tools)
   ▼
reaper_interface.mcp_server (Python 3.10+)
   ├── Offline Engine (works even when REAPER is closed)
   │    ├── midi_files.py   — Standard MIDI File (.mid) inspect/create/edit + theory
   │    └── fx_catalog.py   — Fast search over VST3/VST2/CLAP/AU/JSFX/FXChains & presets
   │
   └── Atomic File-Queue IPC (zero ports, zero REAPER Python DLL required)
        ├── macOS:   ~/Library/Application Support/REAPER/ReaperAgentBridgeIPC
        ├── Windows: %LOCALAPPDATA%\ReaperAgentBridge\ipc
        └── Linux:   ~/.config/REAPER/ReaperAgentBridgeIPC
             │
             ▼
        reaper_agent_bridge.lua (runs non-blockingly via reaper.defer() in REAPER)
```

---

## Installation on macOS (Apple Silicon & Intel)

### Prerequisites
- **REAPER 7+** (recommended; REAPER 6.x works for most features except `EnumInstalledFX`)
- **Python 3.10+** and **[uv](https://docs.astral.sh/uv/)** (install `uv` if needed):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### Step 1: Clone & Install Dependencies
```bash
git clone https://github.com/OliHarding/reaper-mcp.git
cd reaper-mcp
uv sync
```

### Step 2: Install the REAPER Lua Bridge & Register the MCP Server
Pick the `--client` flag for your AI app (`antigravity`, `claude-desktop`, `cursor`, `auto`, or `all`):

```bash
# For Claude Desktop on Mac:
uv run python scripts/install_reaper_bridge.py --client claude-desktop

# For Antigravity on Mac:
uv run python scripts/install_reaper_bridge.py --client antigravity

# For Cursor on Mac:
uv run python scripts/install_reaper_bridge.py --client cursor

# Or auto-detect installed clients:
uv run python scripts/install_reaper_bridge.py --client auto
```

*(For **Claude Code**, run with `--skip-mcp` and register via `claude mcp add`:)*
```bash
uv run python scripts/install_reaper_bridge.py --skip-mcp
claude mcp add reaper -- "$(pwd)/.venv/bin/python" -m reaper_interface.mcp_server
```

What the installer does:
1. Copies `json.lua`, `reaper_agent_bridge.lua`, and `reaper_agent_bridge_ui.lua` to `~/Library/Application Support/REAPER/Scripts/ReaperAgentBridge/`.
2. Appends a managed auto-start block to `~/Library/Application Support/REAPER/Scripts/__startup.lua` (preserving any existing startup scripts).
3. Adds the `"reaper"` server entry to your chosen MCP client config file (preserving all existing MCP servers).

### Step 3: Restart REAPER & Your MCP Client
1. **Restart REAPER** (the Lua bridge auto-starts silently in the background via `__startup.lua`).
   - *Without restarting REAPER*: Press `?` (**Actions → Show action list**) → **New action… → Load ReaScript…** → select `~/Library/Application Support/REAPER/Scripts/ReaperAgentBridge/reaper_agent_bridge.lua` → **Run**.
2. **Restart (or reload MCP servers in)** Claude Desktop / Antigravity / Cursor.

### Step 4: Verify Connection
```bash
uv run python scripts/verify_bridge.py
# Optional non-destructive live test (creates a temp track, tests MIDI + FX, then deletes it):
uv run python scripts/verify_bridge.py --live-smoke-test
```

### macOS Notes
- **Custom/Portable REAPER installs**: If your REAPER resource folder isn't at `~/Library/Application Support/REAPER`, set `REAPER_RESOURCE_DIR="/path/to/your/REAPER"` in both your shell and the MCP server's `env` block.
- **Audio Units (AU/AUi)**: Supported natively alongside VST3, VST2, CLAP, and JSFX.
- **macOS Local Permissions**: Because the bridge only uses local files inside `~/Library/Application Support/REAPER`, no firewall or network permissions are required.

---

## Installation on Windows

```powershell
git clone https://github.com/OliHarding/reaper-mcp.git
cd reaper-mcp
uv sync
uv run python scripts/install_reaper_bridge.py --client antigravity   # or claude-desktop / cursor / auto
```

Then restart REAPER (or run `%APPDATA%\REAPER\Scripts\ReaperAgentBridge\reaper_agent_bridge.lua` once from REAPER's Actions list) and reload your MCP client.

Verify with:
```powershell
uv run python scripts/verify_bridge.py
```

---

## Optional Live Command Log Window (ReaImGui)

If you have ReaImGui installed in REAPER, you can watch incoming AI commands live inside REAPER by running `ReaperAgentBridge/reaper_agent_bridge_ui.lua` from REAPER's Actions list, or via CLI:
```bash
uv run reaper-cli call bridge_ui_toggle
```

---

## CLI Quick Reference

```bash
# Check live connection & project overview
uv run reaper-cli status
uv run reaper-cli project

# Search installed instruments & FX
uv run reaper-cli fx-catalog --kind instrument
uv run reaper-cli fx-catalog --query "ReaEQ"
uv run reaper-cli fx-presets-catalog --query "ChowKick"

# Create a track with an instrument + FX
uv run reaper-cli track-create --name "Lead" --instrument "Surge XT" --fx "ReaEQ"

# Inspect & edit FX parameters (supports value_norm, value_raw, or target_formatted)
uv run reaper-cli fx-params "Lead" "Surge XT" --search "cutoff"
uv run reaper-cli fx-set "Lead" "ReaEQ" --json '[{"param": "Freq-Low Shelf", "target_formatted": "120 Hz"}]'

# Create MIDI clips with chords & notes
uv run reaper-cli midi-create "Lead" --length-beats 8 --notes-json '[{"chord":"Am9","octave":4,"start_beat_rel":0,"duration_beats":4},{"chord":"Fmaj7","octave":4,"start_beat_rel":4,"duration_beats":4}]'

# Standalone .mid files & REAPER import/export
uv run reaper-cli mid-inspect song.mid
uv run reaper-cli mid-import song.mid --import-tempo
uv run reaper-cli mid-export exported.mid
```

---

## Development & Tests

```bash
uv sync --extra dev
uv run pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
