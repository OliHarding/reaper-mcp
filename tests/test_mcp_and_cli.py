"""Tests verifying MCP server tool registration and CLI offline commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reaper_interface.cli import main as cli_main
from reaper_interface.mcp_server import mcp


def test_mcp_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "reaper_status",
        "reaper_project_info",
        "reaper_set_tempo",
        "reaper_transport",
        "reaper_create_track",
        "reaper_create_midi_item",
        "reaper_midi_read",
        "reaper_midi_insert_notes",
        "reaper_midi_transform",
        "reaper_midi_cc",
        "midi_file_inspect",
        "midi_file_create",
        "midi_file_edit",
        "reaper_import_midi_file",
        "reaper_export_midi_file",
        "reaper_search_installed_fx",
        "reaper_fx_add",
        "reaper_fx_get_params",
        "reaper_fx_set_params",
        "reaper_fx_preset",
        "reaper_fx_named_config",
        "reaper_envelope",
        "reaper_batch",
        "reaper_eval_lua",
    }
    missing = expected - names
    assert not missing, f"Missing expected MCP tools: {missing}"


def test_cli_mid_create_inspect_edit(tmp_path: Path, capsys):
    mid = tmp_path / "cli_test.mid"
    rc = cli_main(
        [
            "mid-create",
            str(mid),
            "--bpm",
            "124",
            "--json",
            json.dumps([{"name": "Synth", "notes": [{"chord": "Dm7", "start_beats": 0, "duration_beats": 4}]}]),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tracks"][0]["note_count"] == 4

    rc2 = cli_main(["mid-inspect", str(mid)])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert len(out2["tracks"][0]["notes"]) == 4
