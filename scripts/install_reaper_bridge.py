"""Cross-platform installer for ReaperAgentBridge (REAPER Lua daemon + __startup.lua + MCP client config).

Works on macOS (Apple Silicon & Intel), Windows, and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "src"))

from reaper_interface.config import get_config  # noqa: E402

START_MARKER = "-- >>> ReaperAgentBridge auto-start (managed) >>>"
END_MARKER = "-- <<< ReaperAgentBridge auto-start (managed) <<<"


def find_venv_python() -> Path:
    candidates = [
        WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        WORKSPACE_ROOT / ".venv" / "bin" / "python",          # macOS / Linux
        WORKSPACE_ROOT / ".venv" / "bin" / "python3",         # macOS / Linux fallback
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return Path(sys.executable)


def install_lua_scripts(config_dir: Path) -> Path:
    lua_src = WORKSPACE_ROOT / "lua"
    config_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("json.lua", "reaper_agent_bridge.lua", "reaper_agent_bridge_ui.lua"):
        shutil.copy2(lua_src / fname, config_dir / fname)
    return config_dir / "reaper_agent_bridge.lua"


def update_startup_lua(reaper_resource_dir: Path, bridge_lua_path: Path) -> Path:
    startup_path = reaper_resource_dir / "Scripts" / "__startup.lua"
    startup_path.parent.mkdir(parents=True, exist_ok=True)
    existing = startup_path.read_text(encoding="utf-8") if startup_path.exists() else ""

    managed_block = (
        f"{START_MARKER}\n"
        f"reaper.defer(function() dofile([[{bridge_lua_path}]]) end)\n"
        f"{END_MARKER}\n"
    )

    if START_MARKER in existing and END_MARKER in existing:
        before = existing.split(START_MARKER, 1)[0]
        after = existing.split(END_MARKER, 1)[1].lstrip("\r\n")
        new_content = before.rstrip("\r\n")
        if new_content:
            new_content += "\n\n"
        new_content += managed_block
        if after:
            new_content += "\n" + after
    else:
        base = existing.rstrip("\r\n")
        new_content = (base + "\n\n" + managed_block) if base else managed_block

    startup_path.write_text(new_content, encoding="utf-8")
    return startup_path


def get_mcp_config_paths(client: str) -> list[tuple[str, Path, bool]]:
    """Return list of (client_label, config_path, include_disabled_flag)."""
    home = Path.home()
    if sys.platform == "darwin":
        claude_path = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        claude_path = appdata / "Claude" / "claude_desktop_config.json"
    else:
        claude_path = home / ".config" / "Claude" / "claude_desktop_config.json"

    all_targets = {
        "antigravity": ("Antigravity", home / ".gemini" / "config" / "mcp_config.json", True),
        "claude-desktop": ("Claude Desktop", claude_path, False),
        "cursor": ("Cursor", home / ".cursor" / "mcp.json", False),
    }

    if client in all_targets:
        return [all_targets[client]]
    if client == "all":
        return list(all_targets.values())
    if client == "auto":
        # Register for any detected client directories; fall back to Antigravity if none found
        detected = [t for t in all_targets.values() if t[1].parent.exists()]
        return detected if detected else [all_targets["antigravity"]]
    raise ValueError(f"Unsupported client: {client}")


def register_mcp_config(mcp_config_path: Path, python_exe: Path, *, include_disabled: bool = False) -> Path:
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
    if mcp_config_path.exists():
        raw = mcp_config_path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
    else:
        data = {}
    servers = data.setdefault("mcpServers", {})
    entry: dict[str, object] = {
        "command": str(python_exe),
        "args": ["-m", "reaper_interface.mcp_server"],
        "env": {
            "PYTHONPATH": str(WORKSPACE_ROOT / "src"),
        },
    }
    if include_disabled:
        entry["disabled"] = False
    servers["reaper"] = entry
    mcp_config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return mcp_config_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install ReaperAgentBridge into REAPER and register the MCP server (macOS / Windows / Linux)."
    )
    parser.add_argument(
        "--client",
        choices=["antigravity", "claude-desktop", "cursor", "auto", "all"],
        default="antigravity",
        help="Which MCP client config to register in (default: antigravity; use 'claude-desktop', 'cursor', 'auto', or 'all').",
    )
    parser.add_argument("--skip-mcp", action="store_true", help="Do not modify any MCP client config file.")
    parser.add_argument("--skip-startup", action="store_true", help="Do not modify REAPER Scripts/__startup.lua.")
    args = parser.parse_args()

    cfg = get_config()
    bridge_path = install_lua_scripts(cfg.scripts_install_dir)
    print(f"[OK] Installed Lua bridge to: {bridge_path}")

    if not args.skip_startup:
        startup_path = update_startup_lua(cfg.reaper_resource_dir, bridge_path)
        print(f"[OK] Updated auto-start hook: {startup_path}")

    if not args.skip_mcp:
        venv_python = find_venv_python()
        for label, mcp_path, include_disabled in get_mcp_config_paths(args.client):
            register_mcp_config(mcp_path, venv_python, include_disabled=include_disabled)
            print(f"[OK] Registered 'reaper' MCP server for {label} in: {mcp_path}")

    cfg.requests_dir.mkdir(parents=True, exist_ok=True)
    cfg.responses_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OK] IPC directory ready: {cfg.ipc_root}")
    print("\nNext step:")
    print("  1. Restart REAPER (the bridge auto-starts via Scripts/__startup.lua).")
    print("     Or without restarting REAPER: Actions (?) -> New action -> Load ReaScript ->")
    print(f"     select '{bridge_path}' -> Run.")
    print("  2. Restart or reload your MCP client (Antigravity / Claude Desktop / Cursor).")
    print("  3. Verify with: uv run python scripts/verify_bridge.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
