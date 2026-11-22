"""Offline & cached catalog of installed REAPER FX, Instruments, FXChains, and Presets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ReaperConfig, get_config


@dataclass(frozen=True)
class FXEntry:
    name: str
    reaper_add_name: str
    format: str  # vst3 | vst2 | clap | jsfx | fxchain
    is_instrument: bool
    developer: str
    ident: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEV_PARENS_RE = re.compile(r"\(([^()]+)\)\s*(?:\(\d+\s*out\))?\s*$")
JSFX_LINE_RE = re.compile(r'^NAME\s+([^\s"]+|"[^"]+")\s+"(.*)"\s*$')


def _extract_developer(display_name: str) -> str:
    m = DEV_PARENS_RE.search(display_name)
    return m.group(1).strip() if m else ""


def _parse_vst_ini(path: Path) -> list[FXEntry]:
    if not path.exists():
        return []
    entries: list[FXEntry] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        parts = val.split(",", 2)
        if len(parts) < 3:
            # Failed scan or timestamp-only entry
            continue
        display_raw = parts[2].strip()
        if not display_raw or display_raw == "<SHELL>":
            continue
        is_inst = display_raw.endswith("!!!VSTi")
        display = display_raw[:-7].strip() if is_inst else display_raw
        is_vst3 = key.lower().endswith(".vst3")
        fmt = "vst3" if is_vst3 else "vst2"
        if is_vst3:
            prefix = "VST3i:" if is_inst else "VST3:"
        else:
            prefix = "VSTi:" if is_inst else "VST:"
        entries.append(
            FXEntry(
                name=display,
                reaper_add_name=f"{prefix} {display}",
                format=fmt,
                is_instrument=is_inst,
                developer=_extract_developer(display),
                ident=key,
            )
        )
    return entries


def _parse_clap_ini(path: Path) -> list[FXEntry]:
    if not path.exists():
        return []
    entries: list[FXEntry] = []
    current_file = ""
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_file = line[1:-1]
            continue
        if "=" not in line or line.startswith("_="):
            continue
        clap_id, val = line.split("=", 1)
        if "|" not in val:
            continue
        flag_str, display = val.split("|", 1)
        is_inst = flag_str.strip() == "1"
        display = display.strip()
        prefix = "CLAPi:" if is_inst else "CLAP:"
        entries.append(
            FXEntry(
                name=display,
                reaper_add_name=f"{prefix} {display}",
                format="clap",
                is_instrument=is_inst,
                developer=_extract_developer(display),
                ident=f"{current_file}:{clap_id}",
            )
        )
    return entries


def _parse_jsfx_ini(path: Path) -> list[FXEntry]:
    if not path.exists():
        return []
    entries: list[FXEntry] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("NAME "):
            continue
        m = JSFX_LINE_RE.match(line)
        if not m:
            continue
        rel_path = m.group(1).strip('"')
        display = m.group(2).strip()
        is_inst = "synth" in display.lower() or "synth" in rel_path.lower() or "drum" in rel_path.lower()
        entries.append(
            FXEntry(
                name=display,
                reaper_add_name=f"JS:{rel_path}",
                format="jsfx",
                is_instrument=is_inst,
                developer=_extract_developer(display),
                ident=rel_path,
            )
        )
    return entries


def _parse_au_ini(path: Path) -> list[FXEntry]:
    """Parse macOS REAPER Audio Unit cache (reaper-auplugins*.ini)."""
    if not path.exists():
        return []
    entries: list[FXEntry] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if not val or val.startswith("<"):
            continue
        # Format: "Vendor: Plugin Name=<timestamp><!flags or type>"
        # REAPER marks AU instruments with "aumu" or "!!!AUi" or "augn"
        val_l = val.lower()
        is_inst = ("aumu" in val_l) or ("augn" in val_l) or ("!!!aui" in key.lower()) or ("!!!aui" in val_l)
        clean_key = key.replace("!!!AUi", "").strip()
        if ":" in clean_key:
            vendor, pname = [p.strip() for p in clean_key.split(":", 1)]
            display = f"{pname} ({vendor})"
        else:
            vendor = _extract_developer(clean_key)
            display = clean_key
        prefix = "AUi:" if is_inst else "AU:"
        entries.append(
            FXEntry(
                name=display,
                reaper_add_name=f"{prefix} {clean_key}",
                format="au",
                is_instrument=is_inst,
                developer=vendor,
                ident=clean_key,
            )
        )
    return entries


def _scan_fxchains(chains_dir: Path) -> list[FXEntry]:
    if not chains_dir.exists():
        return []
    entries: list[FXEntry] = []
    for p in sorted(chains_dir.rglob("*.RfxChain")):
        rel = p.relative_to(chains_dir).as_posix()
        entries.append(
            FXEntry(
                name=p.stem,
                reaper_add_name=str(p),
                format="fxchain",
                is_instrument=False,
                developer="FXChain",
                ident=rel,
            )
        )
    return entries


def _collect_ini_entries(res: Path) -> list[FXEntry]:
    """Scan all platform-specific REAPER FX cache files (Windows, macOS arm64/x86_64, Linux) and deduplicate."""
    all_entries: list[FXEntry] = []
    seen: set[tuple[str, str]] = set()

    def _add(items: list[FXEntry]) -> None:
        for e in items:
            dedup_key = (e.format, e.name.lower())
            if dedup_key not in seen:
                seen.add(dedup_key)
                all_entries.append(e)

    # VST2 / VST3 (e.g. reaper-vstplugins_arm64.ini, reaper-vstplugins64.ini, reaper-vstplugins.ini)
    for p in sorted(res.glob("reaper-vstplugins*.ini")):
        _add(_parse_vst_ini(p))
    # CLAP (e.g. reaper-clap-arm64.ini, reaper-clap-x86_64.ini, reaper-clap-win64.ini)
    for p in sorted(res.glob("reaper-clap*.ini")):
        _add(_parse_clap_ini(p))
    # Audio Units on macOS (e.g. reaper-auplugins_arm64.ini, reaper-auplugins64.ini)
    for p in sorted(res.glob("reaper-auplugins*.ini")):
        _add(_parse_au_ini(p))
    # JSFX & FXChains
    _add(_parse_jsfx_ini(res / "reaper-jsfx.ini"))
    _add(_scan_fxchains(res / "FXChains"))
    return all_entries


def list_installed_fx(
    *,
    query: str | None = None,
    kind: str = "all",  # all | instrument | effect | fxchain
    fmt: str = "all",  # all | vst3 | vst2 | clap | au | jsfx | fxchain
    developer: str | None = None,
    offset: int = 0,
    limit: int = 100,
    config: ReaperConfig | None = None,
) -> dict[str, Any]:
    """Search installed FX/instruments directly from REAPER's plugin cache files (works even offline)."""
    cfg = config or get_config()
    all_entries = _collect_ini_entries(cfg.reaper_resource_dir)

    q = query.lower().strip() if query else None
    dev_q = developer.lower().strip() if developer else None
    fmt_l = fmt.lower().strip()
    kind_l = kind.lower().strip()

    matched: list[FXEntry] = []
    for e in all_entries:
        if fmt_l != "all" and e.format != fmt_l:
            continue
        if kind_l == "instrument" and not e.is_instrument:
            continue
        if kind_l == "effect" and (e.is_instrument or e.format == "fxchain"):
            continue
        if kind_l == "fxchain" and e.format != "fxchain":
            continue
        if dev_q and dev_q not in e.developer.lower():
            continue
        if q:
            hay = f"{e.name} {e.reaper_add_name} {e.developer} {e.ident}".lower()
            if q not in hay:
                continue
        matched.append(e)

    # Sort instruments and exact-start matches higher when querying
    if q:
        matched.sort(
            key=lambda e: (
                0 if e.name.lower().startswith(q) else 1,
                0 if e.format in ("vst3", "clap") else (1 if e.format == "vst2" else 2),
                e.name.lower(),
            )
        )
    else:
        matched.sort(key=lambda e: (e.format, e.name.lower()))

    page = matched[offset : offset + limit]
    return {
        "total_catalog_count": len(all_entries),
        "matched_count": len(matched),
        "offset": offset,
        "items": [e.to_dict() for e in page],
    }


def list_cached_presets(
    plugin_query: str | None = None,
    *,
    limit_per_plugin: int = 100,
    config: ReaperConfig | None = None,
) -> dict[str, Any]:
    """Read REAPER preset .ini files (both factory cached *-builtin.ini and user presets)."""
    cfg = config or get_config()
    presets_dir = cfg.reaper_resource_dir / "presets"
    if not presets_dir.exists():
        return {"plugins": []}

    q = plugin_query.lower().strip() if plugin_query else None
    results: list[dict[str, Any]] = []

    for ini_path in sorted(presets_dir.glob("*.ini")):
        stem = ini_path.stem
        if q and q not in stem.lower():
            continue
        is_builtin = stem.endswith("-builtin")
        presets: list[dict[str, Any]] = []
        lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()

        if is_builtin:
            in_factory = False
            for raw in lines:
                s = raw.strip()
                if s.startswith("["):
                    in_factory = s.lower() == "[factory]"
                    continue
                if in_factory and "=" in s:
                    k, v = s.split("=", 1)
                    if k.isdigit():
                        presets.append({"index": int(k) - 1, "name": v.strip()})
        else:
            current_section = ""
            for raw in lines:
                s = raw.strip()
                if s.startswith("[") and s.endswith("]"):
                    current_section = s[1:-1]
                    continue
                if current_section.lower().startswith("preset") and s.lower().startswith("name="):
                    idx_str = current_section[6:]
                    idx = int(idx_str) if idx_str.isdigit() else len(presets)
                    presets.append({"index": idx, "name": s.split("=", 1)[1].strip()})

        results.append(
            {
                "file": ini_path.name,
                "plugin": stem[:-8] if is_builtin else stem,
                "kind": "factory_cached" if is_builtin else "user",
                "preset_count": len(presets),
                "presets": presets[:limit_per_plugin],
            }
        )

    return {"matched_files": len(results), "plugins": results}
