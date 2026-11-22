"""Tests for standalone Standard MIDI File (.mid) creation, inspection, and editing."""

from __future__ import annotations

from pathlib import Path

import pytest

from reaper_interface.midi_files import create_midi_file, edit_midi_file, inspect_midi_file


def test_create_and_inspect_multitrack_midi(tmp_path: Path):
    mid_path = tmp_path / "demo.mid"
    summary = create_midi_file(
        mid_path,
        tracks=[
            {
                "name": "Chords",
                "channel": 0,
                "program": 0,
                "notes": [
                    {"chord": "Am9", "octave": 3, "start_beats": 0.0, "duration_beats": 4.0, "velocity": 90},
                    {"chord": "Fmaj7", "octave": 3, "start_beats": 4.0, "duration_beats": 4.0, "velocity": 85},
                ],
                "cc": [
                    {"kind": "cc", "cc": 64, "value": 127, "beats": 0.0},
                    {"kind": "cc", "cc": 64, "value": 0, "beats": 3.9},
                    {"kind": "pitch_bend", "value": 1024, "beats": 4.0},
                ],
            },
            {
                "name": "Drums",
                "channel": 9,
                "notes": [
                    {"note": "kick", "start_beats": 0.0, "duration_beats": 0.25, "velocity": 120},
                    {"note": "snare", "start_beats": 1.0, "duration_beats": 0.25, "velocity": 110},
                    {"note": "kick", "start_beats": 2.0, "duration_beats": 0.25, "velocity": 120},
                    {"note": "snare", "start_beats": 3.0, "duration_beats": 0.25, "velocity": 110},
                ],
            },
        ],
        bpm=128.0,
        time_sig_num=4,
        time_sig_denom=4,
    )
    assert summary["track_count"] == 2
    assert pytest.approx(summary["bpm"], abs=0.05) == 128.0
    assert summary["tracks"][0]["note_count"] == 9  # Am9 (5 notes) + Fmaj7 (4 notes)
    assert summary["tracks"][1]["note_count"] == 4

    full = inspect_midi_file(mid_path, include_events=True)
    assert full["tracks"][0]["name"] == "Chords"
    assert any(c["kind"] == "pitch_bend" and c["value"] == 1024 for c in full["tracks"][0]["cc"])
    assert [n["pitch"] for n in full["tracks"][1]["notes"]] == [36, 38, 36, 38]


def test_edit_midi_file_operations(tmp_path: Path):
    src = tmp_path / "src.mid"
    dst = tmp_path / "edited.mid"
    create_midi_file(
        src,
        tracks=[
            {
                "name": "Lead",
                "notes": [
                    {"note": "C4", "start_beats": 0.03, "duration_beats": 1.0, "velocity": 80},
                    {"note": "C#4", "start_beats": 1.02, "duration_beats": 1.0, "velocity": 80},
                    {"note": "E4", "start_beats": 2.0, "duration_beats": 1.0, "velocity": 80},
                ],
            }
        ],
        bpm=100.0,
    )

    edit_midi_file(
        src,
        output_path=dst,
        operations=[
            {"op": "set_tempo", "bpm": 140.0},
            {"op": "rename_track", "track_index": 0, "name": "Lead Synth"},
            {"op": "quantize", "track_index": 0, "grid_beats": 0.5, "strength": 1.0},
            {"op": "scale_snap", "track_index": 0, "root": "C", "scale": "major"},
            {"op": "transpose", "track_index": 0, "semitones": 12},
            {"op": "scale_velocity", "track_index": 0, "offset": 20},
            {"op": "add_cc_ramp", "track_index": 0, "cc": 1, "start_beats": 0.0, "end_beats": 3.0, "start_value": 10, "end_value": 120, "steps": 6},
            {"op": "add_track", "name": "Bass", "notes": [{"note": "C2", "start_beats": 0.0, "duration_beats": 4.0}]},
        ],
    )

    res = inspect_midi_file(dst, include_events=True)
    assert pytest.approx(res["bpm"], abs=0.05) == 140.0
    assert res["track_count"] == 2
    lead = res["tracks"][0]
    assert lead["name"] == "Lead Synth"
    assert [n["start_beats"] for n in lead["notes"]] == [0.0, 1.0, 2.0]
    assert all(n["velocity"] == 100 for n in lead["notes"])
    assert all(n["pitch"] >= 72 for n in lead["notes"])  # transposed up an octave
    assert len(lead["cc"]) == 6
    assert res["tracks"][1]["name"] == "Bass"
    assert res["tracks"][1]["notes"][0]["note_name"] == "C2"
