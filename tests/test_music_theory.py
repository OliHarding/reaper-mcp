"""Tests for music_theory utilities."""

from __future__ import annotations

import pytest

from reaper_interface.music_theory import (
    expand_note_specs,
    get_scale_pitches,
    humanize_notes,
    parse_chord,
    parse_pitch,
    pitch_to_name,
    quantize_value,
    snap_pitch_to_scale,
)


def test_pitch_parsing_and_formatting():
    assert parse_pitch("C4") == 60
    assert parse_pitch("A4") == 69
    assert parse_pitch("F#3") == 54
    assert parse_pitch("Gb3") == 54
    assert parse_pitch("Bb2") == 46
    assert parse_pitch("C-1") == 0
    assert parse_pitch("kick") == 36
    assert parse_pitch("snare") == 38
    assert parse_pitch(72) == 72
    assert pitch_to_name(60) == "C4"
    assert pitch_to_name(69) == "A4"


def test_chords_and_inversions():
    assert parse_chord("C", octave=4) == [60, 64, 67]
    assert parse_chord("Cmaj7", octave=4) == [60, 64, 67, 71]
    assert parse_chord("Am", octave=4) == [69, 72, 76]
    assert parse_chord("C", octave=4, inversion=1) == [64, 67, 72]
    slash = parse_chord("C/E", octave=4)
    assert slash[0] == 52  # E3 bass
    assert slash[1:] == [60, 64, 67]


def test_scales_and_snap():
    c_maj = get_scale_pitches("C", "major", low_pitch=60, high_pitch=72)
    assert c_maj == [60, 62, 64, 65, 67, 69, 71, 72]
    assert snap_pitch_to_scale(61, "C", "major") in (60, 62)
    assert snap_pitch_to_scale(64, "C", "major") == 64


def test_expand_note_specs_with_strum():
    expanded = expand_note_specs(
        [
            {"chord": "Cmaj7", "octave": 4, "start_beat_rel": 0.0, "duration_beats": 4.0, "strum_beats": 0.05},
            {"note": "kick", "start_beat_rel": 0.0, "duration_beats": 0.25, "velocity": 120},
        ]
    )
    assert len(expanded) == 5
    assert [n["pitch"] for n in expanded[:4]] == [60, 64, 67, 71]
    assert pytest.approx(expanded[1]["start_beat_rel"]) == 0.05
    assert pytest.approx(expanded[3]["start_beat_rel"]) == 0.15
    assert expanded[4]["pitch"] == 36


def test_quantize_and_humanize():
    assert pytest.approx(quantize_value(0.47, 0.5, strength=1.0)) == 0.5
    assert pytest.approx(quantize_value(0.5, 0.5, strength=1.0, swing=0.2)) == 0.6
    notes = [{"pitch": 60, "start_beats": 1.0, "velocity": 100}]
    hum = humanize_notes(notes, timing_beats=0.03, velocity_jitter=10, seed=42)
    assert 0.97 <= hum[0]["start_beats"] <= 1.03
    assert 90 <= hum[0]["velocity"] <= 110
