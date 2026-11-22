"""Music theory, note-name parsing, chord & scale expansion, and MIDI rhythm helpers."""

from __future__ import annotations

import random
import re
from typing import Any

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

NOTE_TO_SEMITONE: dict[str, int] = {
    "c": 0, "b#": 0,
    "c#": 1, "db": 1,
    "d": 2,
    "d#": 3, "eb": 3,
    "e": 4, "fb": 4,
    "f": 5, "e#": 5,
    "f#": 6, "gb": 6,
    "g": 7,
    "g#": 8, "ab": 8,
    "a": 9,
    "a#": 10, "bb": 10,
    "b": 11, "cb": 11,
}

NOTE_RE = re.compile(r"^\s*([A-Ga-g])([#b]?)(-?\d+)\s*$")
CHORD_RE = re.compile(r"^\s*([A-Ga-g][#b]?)([^/]*?)(?:/([A-Ga-g][#b]?))?\s*$")

CHORD_INTERVALS: dict[str, list[int]] = {
    "": [0, 4, 7],
    "maj": [0, 4, 7],
    "M": [0, 4, 7],
    "m": [0, 3, 7],
    "min": [0, 3, 7],
    "-": [0, 3, 7],
    "dim": [0, 3, 6],
    "aug": [0, 4, 8],
    "+": [0, 4, 8],
    "5": [0, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "sus": [0, 5, 7],
    "6": [0, 4, 7, 9],
    "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10],
    "dom7": [0, 4, 7, 10],
    "maj7": [0, 4, 7, 11],
    "M7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10],
    "min7": [0, 3, 7, 10],
    "mMaj7": [0, 3, 7, 11],
    "dim7": [0, 3, 6, 9],
    "m7b5": [0, 3, 6, 10],
    "hdim7": [0, 3, 6, 10],
    "7sus4": [0, 5, 7, 10],
    "add9": [0, 4, 7, 14],
    "madd9": [0, 3, 7, 14],
    "9": [0, 4, 7, 10, 14],
    "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14],
    "11": [0, 4, 7, 10, 14, 17],
    "m11": [0, 3, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21],
    "maj13": [0, 4, 7, 11, 14, 21],
}

SCALES: dict[str, list[int]] = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "ionian": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "natural_minor": [0, 2, 3, 5, 7, 8, 10],
    "aeolian": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
    "whole_tone": [0, 2, 4, 6, 8, 10],
    "chromatic": list(range(12)),
}

# General MIDI drum map shorthand (channel 9 / 10 in 1-based)
GM_DRUMS: dict[str, int] = {
    "kick": 36, "bd": 36, "kick2": 35,
    "rim": 37, "sidestick": 37,
    "snare": 38, "sd": 38, "snare2": 40,
    "clap": 39,
    "low_tom": 41, "mid_tom": 45, "high_tom": 48,
    "hh": 42, "chh": 42, "closed_hat": 42,
    "phh": 44, "pedal_hat": 44,
    "ohh": 46, "open_hat": 46,
    "crash": 49, "crash2": 57,
    "ride": 51, "ride_bell": 53,
    "tambourine": 54, "cowbell": 56, "shaker": 70,
}


def pitch_to_name(pitch: int) -> str:
    """Convert MIDI pitch (0-127) to standard note name (C4 = 60)."""
    p = max(0, min(127, int(pitch)))
    octave = (p // 12) - 1
    return f"{NOTE_NAMES[p % 12]}{octave}"


def parse_pitch(value: int | str) -> int:
    """Parse MIDI integer, standard note name ('C4', 'F#3', 'Bb2'), or GM drum name ('kick', 'snare')."""
    if isinstance(value, (int, float)):
        return max(0, min(127, int(round(value))))
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return max(0, min(127, int(s)))
    low = s.lower()
    if low in GM_DRUMS:
        return GM_DRUMS[low]
    m = NOTE_RE.match(s)
    if not m:
        raise ValueError(f"Invalid MIDI pitch or note name: {value!r}")
    letter, accidental, oct_str = m.groups()
    semi = NOTE_TO_SEMITONE[(letter + accidental).lower()]
    octave = int(oct_str)
    pitch = (octave + 1) * 12 + semi
    if not (0 <= pitch <= 127):
        raise ValueError(f"MIDI pitch out of range (0-127): {value!r} -> {pitch}")
    return pitch


def parse_chord(symbol: str, octave: int = 4, inversion: int = 0) -> list[int]:
    """Expand a chord symbol (e.g., 'Cmaj7', 'F#m', 'Bb7/D', 'Dm9') to MIDI pitches."""
    m = CHORD_RE.match(symbol)
    if not m:
        raise ValueError(f"Cannot parse chord symbol: {symbol!r}")
    root_str, quality_raw, bass_str = m.groups()
    root_semi = NOTE_TO_SEMITONE[root_str.lower()]
    quality = quality_raw.strip()

    intervals = CHORD_INTERVALS.get(quality)
    if intervals is None:
        intervals = CHORD_INTERVALS.get(quality.lower())
    if intervals is None:
        raise ValueError(f"Unknown chord quality {quality!r} in {symbol!r}")

    root_midi = (octave + 1) * 12 + root_semi
    pitches = [root_midi + iv for iv in intervals]

    for _ in range(max(0, inversion)):
        if pitches:
            pitches.append(pitches.pop(0) + 12)

    if bass_str:
        bass_semi = NOTE_TO_SEMITONE[bass_str.lower()]
        bass_midi = (octave) * 12 + bass_semi
        while bass_midi >= pitches[0]:
            bass_midi -= 12
        pitches.insert(0, bass_midi)

    return [max(0, min(127, p)) for p in pitches]


def get_scale_pitches(root: str, scale: str = "major", low_pitch: int = 0, high_pitch: int = 127) -> list[int]:
    """Return all MIDI pitches in a given scale across [low_pitch, high_pitch]."""
    root_semi = NOTE_TO_SEMITONE.get(root.strip().lower())
    if root_semi is None:
        raise ValueError(f"Invalid scale root: {root!r}")
    intervals = SCALES.get(scale.strip().lower())
    if intervals is None:
        raise ValueError(f"Unknown scale {scale!r}. Available: {sorted(SCALES.keys())}")
    allowed = {(root_semi + iv) % 12 for iv in intervals}
    return [p for p in range(max(0, low_pitch), min(127, high_pitch) + 1) if (p % 12) in allowed]


def snap_pitch_to_scale(pitch: int, root: str, scale: str = "major") -> int:
    """Snap a MIDI pitch to the nearest pitch in the specified scale."""
    scale_pitches = get_scale_pitches(root, scale)
    return min(scale_pitches, key=lambda sp: (abs(sp - pitch), sp))


def expand_note_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand high-level note specs (supporting 'pitch', 'note', 'chord', or 'pitches' lists)
    into flat note dicts normalized with integer 'pitch'."""
    out: list[dict[str, Any]] = []
    for item in specs:
        base = dict(item)
        pitches: list[int] = []
        if "chord" in base and base["chord"] is not None:
            chord_sym = str(base.pop("chord"))
            octv = int(base.pop("octave", 4))
            inv = int(base.pop("inversion", 0))
            pitches = parse_chord(chord_sym, octave=octv, inversion=inv)
        elif "pitches" in base and base["pitches"] is not None:
            pitches = [parse_pitch(p) for p in base.pop("pitches")]
        elif "notes" in base and base["notes"] is not None:
            pitches = [parse_pitch(p) for p in base.pop("notes")]
        else:
            raw = base.pop("pitch", base.pop("note", 60))
            pitches = [parse_pitch(raw)]

        strum = float(base.pop("strum_beats", 0.0))
        for i, p in enumerate(pitches):
            note_dict = dict(base)
            note_dict["pitch"] = p
            if strum and i > 0:
                for k in ("start_beat_rel", "start_beats", "beat_rel", "beats"):
                    if k in note_dict and note_dict[k] is not None:
                        note_dict[k] = float(note_dict[k]) + strum * i
                        break
            out.append(note_dict)
    return out


def quantize_value(val: float, grid: float, strength: float = 1.0, swing: float = 0.0) -> float:
    """Quantize a beat value to a grid with optional strength (0..1) and swing (0..0.5)."""
    if grid <= 0:
        return val
    step = round(val / grid)
    target = step * grid
    if swing and (step % 2 == 1):
        target += grid * swing
    return val + (target - val) * max(0.0, min(1.0, strength))


def humanize_notes(
    notes: list[dict[str, Any]],
    timing_beats: float = 0.0,
    velocity_jitter: int = 0,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Return a copy of notes with subtle timing and velocity humanization."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for n in notes:
        c = dict(n)
        if timing_beats > 0:
            jitter = rng.uniform(-timing_beats, timing_beats)
            if "start_beats" in c:
                c["start_beats"] = max(0.0, float(c["start_beats"]) + jitter)
            elif "start_beat_rel" in c:
                c["start_beat_rel"] = max(0.0, float(c["start_beat_rel"]) + jitter)
        if velocity_jitter > 0:
            vel = int(c.get("velocity", 100)) + rng.randint(-velocity_jitter, velocity_jitter)
            c["velocity"] = max(1, min(127, vel))
        out.append(c)
    return out
