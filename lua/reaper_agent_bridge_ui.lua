-- Toggle the ReaperAgentBridge status/command log window (requires ReaImGui)
local EXT_SECTION = "ReaperAgentBridge"
local cur = reaper.GetExtState(EXT_SECTION, "ui_visible")
reaper.SetExtState(EXT_SECTION, "ui_visible", (cur == "1") and "0" or "1", false)
