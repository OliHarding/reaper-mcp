-- ReaperAgentBridge: High-reliability IPC bridge daemon for REAPER 7+
-- Runs non-blockingly via reaper.defer() inside REAPER's main UI thread.
-- Communicates via atomic JSON files in %LOCALAPPDATA%\ReaperAgentBridge\ipc
-- (avoiding OneDrive sync locks, port conflicts, and Python DLL requirements).

local BRIDGE_VERSION = "0.1.0"
local EXT_SECTION = "ReaperAgentBridge"

-- Resolve script directory reliably regardless of REAPER working directory
local script_path = debug.getinfo(1, "S").source
if script_path:sub(1, 1) == "@" then
  script_path = script_path:sub(2)
end
local script_dir = script_path:match("^(.*[/\\])") or ""

local json = dofile(script_dir .. "json.lua")

-- Allow REAPER 7.03+ to terminate previous instance cleanly if re-launched
if reaper.set_action_options then
  reaper.set_action_options(1)
end

-- Unique instance token so an older defer loop exits automatically if restarted
local INSTANCE_TOKEN = string.format("%d_%d", math.floor(reaper.time_precise() * 1000), math.random(100000, 999999))
reaper.SetExtState(EXT_SECTION, "instance_token", INSTANCE_TOKEN, false)
reaper.SetExtState(EXT_SECTION, "ui_visible", "0", false)

-------------------------------------------------------------------------------
-- Paths & File Utilities
-------------------------------------------------------------------------------

local SEP = package.config:sub(1, 1)

local function join_path(a, b)
  if a:sub(-1) == "/" or a:sub(-1) == "\\" then
    return a .. b
  end
  return a .. SEP .. b
end

local function get_ipc_root()
  local env_override = os.getenv("REAPER_AGENT_IPC_DIR")
  if env_override and #env_override > 0 then
    return env_override
  end
  local local_appdata = os.getenv("LOCALAPPDATA")
  if local_appdata and #local_appdata > 0 then
    return join_path(join_path(local_appdata, "ReaperAgentBridge"), "ipc")
  end
  return join_path(reaper.GetResourcePath(), "ReaperAgentBridgeIPC")
end

local IPC_ROOT      = get_ipc_root()
local REQUESTS_DIR  = join_path(IPC_ROOT, "requests")
local RESPONSES_DIR = join_path(IPC_ROOT, "responses")
local HEARTBEAT_FILE = join_path(IPC_ROOT, "heartbeat.json")

reaper.RecursiveCreateDirectory(REQUESTS_DIR, 0)
reaper.RecursiveCreateDirectory(RESPONSES_DIR, 0)

local function read_file(path)
  local f, err = io.open(path, "rb")
  if not f then return nil, err end
  local content = f:read("*a")
  f:close()
  return content
end

local function write_file_atomic(target_path, content, overwrite_existing)
  local tmp_path = target_path .. ".tmp." .. tostring(math.random(10000, 99999))
  local f, err = io.open(tmp_path, "wb")
  if not f then
    return false, err
  end
  f:write(content)
  f:flush()
  f:close()
  -- On Windows, os.rename fails if target exists; on POSIX (macOS/Linux), os.rename is natively atomic
  if overwrite_existing and SEP == "\\" then
    os.remove(target_path)
  end
  local ok, ren_err = os.rename(tmp_path, target_path)
  if not ok then
    os.remove(tmp_path)
    return false, ren_err
  end
  return true
end

-------------------------------------------------------------------------------
-- Music / Conversion Helpers
-------------------------------------------------------------------------------

local NOTE_NAMES = { "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B" }
local NOTE_NAME_TO_SEMITONE = {
  ["c"] = 0, ["b#"] = 0,
  ["c#"] = 1, ["db"] = 1,
  ["d"] = 2,
  ["d#"] = 3, ["eb"] = 3,
  ["e"] = 4, ["fb"] = 4,
  ["f"] = 5, ["e#"] = 5,
  ["f#"] = 6, ["gb"] = 6,
  ["g"] = 7,
  ["g#"] = 8, ["ab"] = 8,
  ["a"] = 9,
  ["a#"] = 10, ["bb"] = 10,
  ["b"] = 11, ["cb"] = 11,
}

local function pitch_to_name(pitch)
  pitch = math.floor(tonumber(pitch) or 60)
  local octave = math.floor(pitch / 12) - 1
  local idx = (pitch % 12) + 1
  return NOTE_NAMES[idx] .. tostring(octave)
end

local function parse_pitch(val)
  if type(val) == "number" then
    local p = math.floor(val + 0.5)
    if p < 0 then p = 0 elseif p > 127 then p = 127 end
    return p
  elseif type(val) == "string" then
    local num = tonumber(val)
    if num then return parse_pitch(num) end
    local note_part, oct_part = val:match("^%s*([A-Ga-g][#b]?)(%-?%d+)%s*$")
    if note_part and oct_part then
      local semi = NOTE_NAME_TO_SEMITONE[note_part:lower()]
      local oct = tonumber(oct_part)
      if semi and oct then
        local p = (oct + 1) * 12 + semi
        if p < 0 then p = 0 elseif p > 127 then p = 127 end
        return p
      end
    end
  end
  error("Invalid MIDI pitch or note name: " .. tostring(val))
end

local LOG10 = math.log(10)

local function linear_to_db(v)
  if not v or v <= 1e-10 then return -150.0 end
  return 20.0 * (math.log(v) / LOG10)
end

local function db_to_linear(db)
  if not db or db <= -150.0 then return 0.0 end
  return 10.0 ^ (db / 20.0)
end

local function native_color_to_hex(native_col)
  if not native_col or native_col == 0 then return nil end
  local r, g, b = reaper.ColorFromNative(native_col)
  return string.format("#%02X%02X%02X", r, g, b)
end

local function hex_to_native_color(hex)
  if not hex or type(hex) ~= "string" then return nil end
  local clean = hex:gsub("^#", "")
  if #clean ~= 6 then return nil end
  local r = tonumber(clean:sub(1, 2), 16)
  local g = tonumber(clean:sub(3, 4), 16)
  local b = tonumber(clean:sub(5, 6), 16)
  if not (r and g and b) then return nil end
  return reaper.ColorToNative(r, g, b)
end

local function resolve_time_sec(args, sec_key, beats_key, default_val)
  if args[sec_key] ~= nil then
    return tonumber(args[sec_key])
  elseif args[beats_key] ~= nil then
    return reaper.TimeMap2_QNToTime(0, tonumber(args[beats_key]))
  end
  return default_val
end

local function resolve_ppq(take, item_start_qn, obj, field_prefix)
  -- Supports <prefix>ppq, <prefix>beats (project QN), <prefix>beat_rel (beats relative to item start), <prefix>sec
  local ppq_k = field_prefix .. "ppq"
  local qn_k  = field_prefix .. "beats"
  local qn2_k = field_prefix .. "qn"
  local rel_k = field_prefix .. "beat_rel"
  local sec_k = field_prefix .. "sec"

  if obj[ppq_k] ~= nil then
    return tonumber(obj[ppq_k])
  elseif obj[rel_k] ~= nil then
    return reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn + tonumber(obj[rel_k]))
  elseif obj[qn_k] ~= nil then
    return reaper.MIDI_GetPPQPosFromProjQN(take, tonumber(obj[qn_k]))
  elseif obj[qn2_k] ~= nil then
    return reaper.MIDI_GetPPQPosFromProjQN(take, tonumber(obj[qn2_k]))
  elseif obj[sec_k] ~= nil then
    return reaper.MIDI_GetPPQPosFromProjTime(take, tonumber(obj[sec_k]))
  end
  return nil
end

-------------------------------------------------------------------------------
-- Track / Item / Take / FX Resolution Helpers
-------------------------------------------------------------------------------

local function resolve_track(ref, allow_master)
  if ref == nil then
    error("Missing required 'track' parameter")
  end
  if ref == -1 or (type(ref) == "string" and ref:lower() == "master") then
    if allow_master == false then
      error("Master track is not supported for this operation")
    end
    return reaper.GetMasterTrack(0), -1
  end
  if type(ref) == "number" then
    local idx = math.floor(ref)
    local tr = reaper.GetTrack(0, idx)
    if not tr then
      error(string.format("Track index %d out of range (total tracks: %d)", idx, reaper.CountTracks(0)))
    end
    return tr, idx
  end
  if type(ref) == "string" then
    local cnt = reaper.CountTracks(0)
    -- 1. Try GUID match
    if ref:sub(1, 1) == "{" and ref:sub(-1) == "}" then
      for i = 0, cnt - 1 do
        local tr = reaper.GetTrack(0, i)
        if reaper.GetTrackGUID(tr) == ref then
          return tr, i
        end
      end
    end
    -- 2. Try exact case-insensitive name match
    local ref_l = ref:lower()
    for i = 0, cnt - 1 do
      local tr = reaper.GetTrack(0, i)
      local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
      if name:lower() == ref_l then
        return tr, i
      end
    end
    -- 3. Try substring match
    for i = 0, cnt - 1 do
      local tr = reaper.GetTrack(0, i)
      local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
      if name:lower():find(ref_l, 1, true) then
        return tr, i
      end
    end
    error("Track not found: " .. ref)
  end
  error("Invalid track reference type: " .. type(ref))
end

local function resolve_item(args)
  if args.item_guid then
    local cnt = reaper.CountMediaItems(0)
    for i = 0, cnt - 1 do
      local it = reaper.GetMediaItem(0, i)
      local _, guid = reaper.GetSetMediaItemInfo_String(it, "GUID", "", false)
      if guid == args.item_guid then
        local tr = reaper.GetMediaItem_Track(it)
        return it, tr
      end
    end
    error("Item not found with GUID: " .. tostring(args.item_guid))
  end
  local tr = resolve_track(args.track, false)
  local item_idx = tonumber(args.item_index or 0)
  local it = reaper.GetTrackMediaItem(tr, math.floor(item_idx))
  if not it then
    error(string.format("Item index %d not found on track (count: %d)", item_idx, reaper.CountTrackMediaItems(tr)))
  end
  return it, tr
end

local function resolve_take(args, require_midi)
  local item, tr = resolve_item(args)
  local take
  if args.take_index ~= nil then
    take = reaper.GetTake(item, math.floor(tonumber(args.take_index)))
  else
    take = reaper.GetActiveTake(item)
  end
  if not take then
    error("No valid take found on media item")
  end
  if require_midi and not reaper.TakeIsMIDI(take) then
    error("Target take is not a MIDI take")
  end
  return take, item, tr
end

local function fx_slot(fx_idx, rec_fx)
  if rec_fx then
    return 0x1000000 | fx_idx
  end
  return fx_idx
end

local function resolve_fx_index(tr, fx_ref, rec_fx)
  local count = rec_fx and reaper.TrackFX_GetRecCount(tr) or reaper.TrackFX_GetCount(tr)
  if type(fx_ref) == "number" then
    local idx = math.floor(fx_ref)
    if idx < 0 or idx >= count then
      error(string.format("FX index %d out of range (count: %d)", idx, count))
    end
    return idx, fx_slot(idx, rec_fx)
  elseif type(fx_ref) == "string" then
    local q = fx_ref:lower()
    -- GUID check
    if fx_ref:sub(1, 1) == "{" and fx_ref:sub(-1) == "}" then
      for i = 0, count - 1 do
        local slot = fx_slot(i, rec_fx)
        if reaper.TrackFX_GetFXGUID(tr, slot) == fx_ref then
          return i, slot
        end
      end
    end
    -- Exact match first
    for i = 0, count - 1 do
      local slot = fx_slot(i, rec_fx)
      local _, name = reaper.TrackFX_GetFXName(tr, slot)
      if name:lower() == q then
        return i, slot
      end
    end
    -- Substring match
    for i = 0, count - 1 do
      local slot = fx_slot(i, rec_fx)
      local _, name = reaper.TrackFX_GetFXName(tr, slot)
      if name:lower():find(q, 1, true) then
        return i, slot
      end
    end
    error("FX not found on track: " .. fx_ref)
  end
  error("Invalid fx reference: " .. tostring(fx_ref))
end

local function resolve_param_index(tr, slot, param_ref)
  local num = reaper.TrackFX_GetNumParams(tr, slot)
  if type(param_ref) == "number" then
    local idx = math.floor(param_ref)
    if idx < 0 or idx >= num then
      error(string.format("Parameter index %d out of range (count: %d)", idx, num))
    end
    return idx
  elseif type(param_ref) == "string" then
    -- Support special REAPER parameter idents like ":wet", ":bypass", ":delta"
    if param_ref:sub(1, 1) == ":" then
      local special = reaper.TrackFX_GetParamFromIdent(tr, slot, param_ref)
      if special >= 0 then return special end
    end
    local q = param_ref:lower()
    -- Exact case-insensitive match first
    for i = 0, num - 1 do
      local _, pname = reaper.TrackFX_GetParamName(tr, slot, i)
      if pname:lower() == q then
        return i
      end
    end
    -- Substring match
    for i = 0, num - 1 do
      local _, pname = reaper.TrackFX_GetParamName(tr, slot, i)
      if pname:lower():find(q, 1, true) then
        return i
      end
    end
    error("Parameter not found on FX: " .. param_ref)
  end
  error("Invalid parameter reference: " .. tostring(param_ref))
end

-------------------------------------------------------------------------------
-- Serialization Helpers
-------------------------------------------------------------------------------

local function serialize_fx(tr, idx, rec_fx)
  local slot = fx_slot(idx, rec_fx)
  local _, display_name = reaper.TrackFX_GetFXName(tr, slot)
  local _, actual_name  = reaper.TrackFX_GetNamedConfigParm(tr, slot, "fx_name")
  local _, fx_type      = reaper.TrackFX_GetNamedConfigParm(tr, slot, "fx_type")
  local _, fx_ident     = reaper.TrackFX_GetNamedConfigParm(tr, slot, "fx_ident")
  local _, preset_name  = reaper.TrackFX_GetPreset(tr, slot)
  local preset_idx, preset_cnt = reaper.TrackFX_GetPresetIndex(tr, slot)
  local wet_idx = reaper.TrackFX_GetParamFromIdent(tr, slot, ":wet")
  local wet_val = nil
  if wet_idx >= 0 then
    wet_val = reaper.TrackFX_GetParamNormalized(tr, slot, wet_idx)
  end
  return {
    index = idx,
    rec_fx = rec_fx and true or false,
    guid = reaper.TrackFX_GetFXGUID(tr, slot),
    name = display_name,
    actual_name = actual_name ~= "" and actual_name or display_name,
    fx_type = fx_type,
    fx_ident = fx_ident,
    enabled = reaper.TrackFX_GetEnabled(tr, slot),
    offline = reaper.TrackFX_GetOffline(tr, slot),
    open = reaper.TrackFX_GetOpen(tr, slot),
    preset_name = preset_name,
    preset_index = preset_idx,
    preset_count = preset_cnt,
    param_count = reaper.TrackFX_GetNumParams(tr, slot),
    wet = wet_val,
  }
end

local function serialize_track(tr, idx, include_fx, include_sends)
  local is_master = (idx == -1)
  local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  if is_master and (not name or name == "") then name = "Master" end
  local vol = reaper.GetMediaTrackInfo_Value(tr, "D_VOL")
  local pan = reaper.GetMediaTrackInfo_Value(tr, "D_PAN")
  local mute = reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") ~= 0
  local solo = math.floor(reaper.GetMediaTrackInfo_Value(tr, "I_SOLO"))
  local rec_arm = reaper.GetMediaTrackInfo_Value(tr, "I_RECARM") ~= 0
  local rec_mon = math.floor(reaper.GetMediaTrackInfo_Value(tr, "I_RECMON"))
  local rec_input = math.floor(reaper.GetMediaTrackInfo_Value(tr, "I_RECINPUT"))
  local folder_depth = math.floor(reaper.GetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH"))
  local selected = reaper.GetMediaTrackInfo_Value(tr, "I_SELECTED") ~= 0
  local col = reaper.GetTrackColor(tr)
  local fx_cnt = reaper.TrackFX_GetCount(tr)
  local rec_fx_cnt = reaper.TrackFX_GetRecCount(tr)

  local out = {
    index = idx,
    is_master = is_master,
    guid = reaper.GetTrackGUID(tr),
    name = name,
    volume_linear = vol,
    volume_db = linear_to_db(vol),
    pan = pan,
    mute = mute,
    solo = solo,
    rec_arm = rec_arm,
    rec_mon = rec_mon,
    rec_input = rec_input,
    folder_depth = folder_depth,
    selected = selected,
    color = native_color_to_hex(col),
    fx_count = fx_cnt,
    rec_fx_count = rec_fx_cnt,
    item_count = is_master and 0 or reaper.CountTrackMediaItems(tr),
  }

  if include_fx then
    local fx_list = json.array({})
    for i = 0, fx_cnt - 1 do
      fx_list[#fx_list + 1] = serialize_fx(tr, i, false)
    end
    out.fx = fx_list
  end

  if include_sends and not is_master then
    local sends = json.array({})
    local n_sends = reaper.GetTrackNumSends(tr, 0)
    for s = 0, n_sends - 1 do
      local _, dest_name = reaper.GetTrackSendName(tr, s)
      local svol = reaper.GetTrackSendInfo_Value(tr, 0, s, "D_VOL")
      local dest_idx = nil
      if reaper.BR_GetMediaTrackSendInfo_Track then
        local dtr = reaper.BR_GetMediaTrackSendInfo_Track(tr, 0, s, 1)
        if dtr then
          dest_idx = math.floor(reaper.GetMediaTrackInfo_Value(dtr, "IP_TRACKNUMBER")) - 1
        end
      end
      sends[#sends + 1] = {
        index = s,
        dest_name = dest_name,
        dest_track_index = dest_idx,
        volume_db = linear_to_db(svol),
        pan = reaper.GetTrackSendInfo_Value(tr, 0, s, "D_PAN"),
        mute = reaper.GetTrackSendInfo_Value(tr, 0, s, "B_MUTE") ~= 0,
        send_mode = math.floor(reaper.GetTrackSendInfo_Value(tr, 0, s, "I_SENDMODE")),
        src_chan = math.floor(reaper.GetTrackSendInfo_Value(tr, 0, s, "I_SRCCHAN")),
        dst_chan = math.floor(reaper.GetTrackSendInfo_Value(tr, 0, s, "I_DSTCHAN")),
        midi_flags = math.floor(reaper.GetTrackSendInfo_Value(tr, 0, s, "I_MIDIFLAGS")),
      }
    end
    out.sends = sends
  end

  return out
end

local function serialize_item(item, item_idx, track_idx)
  local _, guid = reaper.GetSetMediaItemInfo_String(item, "GUID", "", false)
  local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
  local pos_qn = reaper.TimeMap2_timeToQN(0, pos)
  local end_qn = reaper.TimeMap2_timeToQN(0, pos + len)
  local take = reaper.GetActiveTake(item)
  local take_name = take and reaper.GetTakeName(take) or ""
  local is_midi = take and reaper.TakeIsMIDI(take) or false
  local note_cnt, cc_cnt, text_cnt = 0, 0, 0
  local source_file = nil
  local take_fx_cnt = 0
  if take then
    take_fx_cnt = reaper.TakeFX_GetCount(take)
    if is_midi then
      _, note_cnt, cc_cnt, text_cnt = reaper.MIDI_CountEvts(take)
    else
      local src = reaper.GetMediaItemTake_Source(take)
      if src then
        local fn = reaper.GetMediaSourceFileName(src)
        if fn and fn ~= "" then source_file = fn end
      end
    end
  end
  return {
    index = item_idx,
    track_index = track_idx,
    guid = guid,
    name = take_name,
    is_midi = is_midi,
    position_sec = pos,
    length_sec = len,
    end_sec = pos + len,
    position_beats = pos_qn,
    length_beats = end_qn - pos_qn,
    end_beats = end_qn,
    mute = reaper.GetMediaItemInfo_Value(item, "B_MUTE") ~= 0,
    loop = reaper.GetMediaItemInfo_Value(item, "B_LOOPSRC") ~= 0,
    volume_db = linear_to_db(reaper.GetMediaItemInfo_Value(item, "D_VOL")),
    fade_in_sec = reaper.GetMediaItemInfo_Value(item, "D_FADEINLEN"),
    fade_out_sec = reaper.GetMediaItemInfo_Value(item, "D_FADEOUTLEN"),
    selected = reaper.GetMediaItemInfo_Value(item, "B_UISEL") ~= 0,
    take_count = reaper.CountTakes(item),
    take_fx_count = take_fx_cnt,
    midi_note_count = note_cnt,
    midi_cc_count = cc_cnt,
    midi_text_count = text_cnt,
    source_file = source_file,
  }
end

-------------------------------------------------------------------------------
-- Smart FX Name Resolution & Formatted Value Search
-------------------------------------------------------------------------------

local function find_installed_fx_match(query, prefer_instrument)
  if not reaper.EnumInstalledFX then return nil end
  local q = query:lower()
  local exact_matches = {}
  local partial_matches = {}
  local i = 0
  while true do
    local ok, name, ident = reaper.EnumInstalledFX(i)
    if not ok then break end
    local nl = name:lower()
    -- Strip leading format prefix like "VST3i: " or "CLAP: " for matching
    local stripped = nl:gsub("^[%w%s]+:%s*", "")
    local stripped_no_dev = stripped:gsub("%s*%([^%)]*%)%s*$", "")
    local is_inst = nl:match("^vst3?i:") ~= nil or nl:match("^clapi:") ~= nil or nl:match("^aui:") ~= nil

    if nl == q or stripped == q or stripped_no_dev == q then
      exact_matches[#exact_matches + 1] = { name = name, ident = ident, is_inst = is_inst }
    elseif nl:find(q, 1, true) then
      partial_matches[#partial_matches + 1] = { name = name, ident = ident, is_inst = is_inst }
    end
    i = i + 1
  end

  local function pick_best(list)
    if #list == 0 then return nil end
    if prefer_instrument then
      for _, m in ipairs(list) do
        if m.is_inst then return m.name end
      end
    end
    -- Prefer VST3/CLAP over VST2 if multiple matches
    for _, m in ipairs(list) do
      if m.name:match("^VST3") or m.name:match("^CLAP") then return m.name end
    end
    return list[1].name
  end

  return pick_best(exact_matches) or pick_best(partial_matches)
end

local function extract_first_number(str)
  if not str then return nil end
  local low = str:lower()
  if low:find("-inf", 1, true) then return -1e9 end
  if low:find("inf", 1, true) then return 1e9 end
  local num = str:match("([%-+]?%d+%.?%d*)")
  if not num then return nil end
  local val = tonumber(num)
  if not val then return nil end
  if low:find("khz", 1, true) then
    val = val * 1000.0
  end
  return val
end

local function set_param_by_formatted_target(tr, slot, param_idx, target_str)
  local orig_norm = reaper.TrackFX_GetParamNormalized(tr, slot, param_idx)
  local target_num = extract_first_number(tostring(target_str))
  local target_low = tostring(target_str):lower()

  -- Probe endpoints
  reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, 0.0)
  local _, f0 = reaper.TrackFX_GetFormattedParamValue(tr, slot, param_idx)
  reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, 1.0)
  local _, f1 = reaper.TrackFX_GetFormattedParamValue(tr, slot, param_idx)

  if f0:lower() == target_low then
    reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, 0.0)
    return true, 0.0, f0
  end
  if f1:lower() == target_low then
    reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, 1.0)
    return true, 1.0, f1
  end

  -- Numeric binary search if both ends parse numerically and differ
  local n0 = extract_first_number(f0)
  local n1 = extract_first_number(f1)
  if target_num and n0 and n1 and n0 ~= n1 then
    local ascending = (n1 > n0)
    local lo, hi = 0.0, 1.0
    local best_norm = orig_norm
    local best_diff = math.huge
    local best_fmt = f0
    for _ = 1, 22 do
      local mid = (lo + hi) * 0.5
      reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, mid)
      local _, fmid = reaper.TrackFX_GetFormattedParamValue(tr, slot, param_idx)
      local nmid = extract_first_number(fmid)
      if not nmid then break end
      local diff = math.abs(nmid - target_num)
      if diff < best_diff then
        best_diff = diff
        best_norm = mid
        best_fmt = fmid
      end
      if diff < 1e-4 then break end
      if ascending then
        if nmid < target_num then lo = mid else hi = mid end
      else
        if nmid > target_num then lo = mid else hi = mid end
      end
    end
    reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, best_norm)
    return true, best_norm, best_fmt
  end

  -- Discrete/enum scan (64 steps)
  local steps = 64
  for s = 0, steps do
    local v = s / steps
    reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, v)
    local _, fv = reaper.TrackFX_GetFormattedParamValue(tr, slot, param_idx)
    if fv:lower() == target_low or fv:lower():find(target_low, 1, true) then
      return true, v, fv
    end
  end

  -- Restore on failure
  reaper.TrackFX_SetParamNormalized(tr, slot, param_idx, orig_norm)
  return false, orig_norm, "No matching formatted value found for: " .. tostring(target_str)
end

-------------------------------------------------------------------------------
-- Command Handlers
-------------------------------------------------------------------------------

local handlers = {}
local START_TIME = reaper.time_precise()
local COMMAND_LOG = {}
local MAX_LOG = 50
local UI_CTX = nil

local function log_command(cmd, ok, elapsed_ms, err_msg)
  COMMAND_LOG[#COMMAND_LOG + 1] = {
    time = os.date("%H:%M:%S"),
    cmd = cmd,
    ok = ok,
    ms = elapsed_ms,
    err = err_msg,
  }
  if #COMMAND_LOG > MAX_LOG then
    table.remove(COMMAND_LOG, 1)
  end
end

-- 1. System / Project / Transport --------------------------------------------

handlers.ping = function()
  local _, proj_fn = reaper.EnumProjects(-1)
  local ts_num, ts_den, bpm = reaper.TimeMap_GetTimeSigAtTime(0, 0)
  return {
    alive = true,
    bridge_version = BRIDGE_VERSION,
    reaper_version = reaper.GetAppVersion(),
    uptime_sec = reaper.time_precise() - START_TIME,
    project_name = reaper.GetProjectName(0),
    project_path = proj_fn,
    bpm = bpm,
    time_sig_num = ts_num,
    time_sig_denom = ts_den,
    track_count = reaper.CountTracks(0),
    play_state = reaper.GetPlayState(),
    ipc_root = IPC_ROOT,
  }
end

handlers.bridge_ui_toggle = function(args)
  local cur = reaper.GetExtState(EXT_SECTION, "ui_visible")
  local next_v
  if args and args.visible ~= nil then
    next_v = args.visible and "1" or "0"
  else
    next_v = (cur == "1") and "0" or "1"
  end
  reaper.SetExtState(EXT_SECTION, "ui_visible", next_v, false)
  return { ui_visible = (next_v == "1") }
end

handlers.project_get_info = function(args)
  local _, proj_fn = reaper.EnumProjects(-1)
  local ts_num, ts_den, bpm = reaper.TimeMap_GetTimeSigAtTime(0, 0)
  local loop_start, loop_end = reaper.GetSet_LoopTimeRange(false, true, 0, 0, false)
  local sel_start, sel_end = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
  local ps = reaper.GetPlayState()
  local info = {
    project_name = reaper.GetProjectName(0),
    project_path = proj_fn,
    media_path = reaper.GetProjectPath(),
    project_length_sec = reaper.GetProjectLength(0),
    project_length_beats = reaper.TimeMap2_timeToQN(0, reaper.GetProjectLength(0)),
    bpm = bpm,
    master_bpm = reaper.Master_GetTempo(),
    time_sig_num = ts_num,
    time_sig_denom = ts_den,
    cursor_sec = reaper.GetCursorPosition(),
    cursor_beats = reaper.TimeMap2_timeToQN(0, reaper.GetCursorPosition()),
    play_position_sec = reaper.GetPlayPosition(),
    play_state = ps,
    is_playing = (ps & 1) ~= 0,
    is_paused = (ps & 2) ~= 0,
    is_recording = (ps & 4) ~= 0,
    repeat_enabled = reaper.GetSetRepeat(-1) ~= 0,
    loop_start_sec = loop_start,
    loop_end_sec = loop_end,
    loop_start_beats = reaper.TimeMap2_timeToQN(0, loop_start),
    loop_end_beats = reaper.TimeMap2_timeToQN(0, loop_end),
    time_sel_start_sec = sel_start,
    time_sel_end_sec = sel_end,
    track_count = reaper.CountTracks(0),
    is_dirty = reaper.IsProjectDirty(0) ~= 0,
  }
  if args and args.include_tracks then
    local tracks = json.array({})
    for i = 0, reaper.CountTracks(0) - 1 do
      tracks[#tracks + 1] = serialize_track(reaper.GetTrack(0, i), i, args.include_fx, args.include_sends)
    end
    info.tracks = tracks
  end
  return info
end

handlers.project_set_tempo = function(args)
  local bpm = tonumber(args.bpm)
  if not bpm or bpm <= 0 then error("Invalid bpm") end
  local time_sec = resolve_time_sec(args, "time_sec", "time_beats", nil)
  if time_sec ~= nil then
    local num = tonumber(args.time_sig_num or 0)
    local den = tonumber(args.time_sig_denom or 0)
    local linear = args.linear and true or false
    reaper.SetTempoTimeSigMarker(0, -1, time_sec, -1, -1, bpm, num, den, linear)
  else
    reaper.SetCurrentBPM(0, bpm, true)
    local cnt = reaper.CountTempoTimeSigMarkers(0)
    local cur_num, cur_den = reaper.TimeMap_GetTimeSigAtTime(0, 0)
    local num = tonumber(args.time_sig_num or cur_num)
    local den = tonumber(args.time_sig_denom or cur_den)
    if cnt > 0 then
      local ok, tpos = reaper.GetTempoTimeSigMarker(0, 0)
      if ok and math.abs(tpos) <= 1e-6 then
        reaper.SetTempoTimeSigMarker(0, 0, 0, -1, -1, bpm, num, den, false)
      elseif args.time_sig_num or args.time_sig_denom then
        reaper.SetTempoTimeSigMarker(0, -1, 0, -1, -1, bpm, num, den, false)
      end
    elseif args.time_sig_num or args.time_sig_denom then
      reaper.SetTempoTimeSigMarker(0, -1, 0, -1, -1, bpm, num, den, false)
    end
  end
  reaper.UpdateTimeline()
  local ts_num, ts_den, new_bpm = reaper.TimeMap_GetTimeSigAtTime(0, time_sec or 0)
  return { bpm = new_bpm, time_sig_num = ts_num, time_sig_denom = ts_den }
end

handlers.project_list_tempo_markers = function()
  local cnt = reaper.CountTempoTimeSigMarkers(0)
  local markers = json.array({})
  for i = 0, cnt - 1 do
    local ok, timepos, measurepos, beatpos, bpm, num, den, linear = reaper.GetTempoTimeSigMarker(0, i)
    if ok then
      markers[#markers + 1] = {
        index = i,
        time_sec = timepos,
        time_beats = reaper.TimeMap2_timeToQN(0, timepos),
        measure = measurepos,
        beat = beatpos,
        bpm = bpm,
        time_sig_num = num,
        time_sig_denom = den,
        linear = linear,
      }
    end
  end
  return { markers = markers }
end

handlers.project_save = function(args)
  reaper.Main_SaveProject(0, (args and args.save_as) and true or false)
  local _, proj_fn = reaper.EnumProjects(-1)
  return { saved = true, project_path = proj_fn }
end

handlers.project_open = function(args)
  if not args.path then error("Missing 'path'") end
  local target = args.no_prompt and ("noprompt:" .. args.path) or args.path
  reaper.Main_openProject(target)
  return handlers.project_get_info({})
end

handlers.project_new = function(args)
  if args and args.new_tab then
    reaper.Main_OnCommand(40859, 0) -- New project tab
  else
    reaper.Main_OnCommand(40023, 0) -- New project
  end
  return handlers.project_get_info({})
end

handlers.transport_control = function(args)
  local action = args.action
  if action == "play" then
    reaper.OnPlayButton()
  elseif action == "stop" then
    reaper.OnStopButton()
  elseif action == "pause" then
    reaper.OnPauseButton()
  elseif action == "record" then
    reaper.CSurf_OnRecord()
  elseif action == "play_pause" then
    reaper.Main_OnCommand(40073, 0)
  end

  if args.cursor_sec ~= nil or args.cursor_beats ~= nil then
    local t = resolve_time_sec(args, "cursor_sec", "cursor_beats", 0)
    reaper.SetEditCurPos(t, args.move_view ~= false, args.seek_play and true or false)
  end

  if args.loop_start_sec ~= nil or args.loop_start_beats ~= nil then
    local ls = resolve_time_sec(args, "loop_start_sec", "loop_start_beats", 0)
    local le = resolve_time_sec(args, "loop_end_sec", "loop_end_beats", ls + 4)
    reaper.GetSet_LoopTimeRange(true, true, ls, le, false)
  end

  if args.repeat_enabled ~= nil then
    reaper.GetSetRepeat(args.repeat_enabled and 1 or 0)
  end

  local ps = reaper.GetPlayState()
  return {
    play_state = ps,
    is_playing = (ps & 1) ~= 0,
    is_paused = (ps & 2) ~= 0,
    is_recording = (ps & 4) ~= 0,
    cursor_sec = reaper.GetCursorPosition(),
    cursor_beats = reaper.TimeMap2_timeToQN(0, reaper.GetCursorPosition()),
    play_position_sec = reaper.GetPlayPosition(),
    repeat_enabled = reaper.GetSetRepeat(-1) ~= 0,
  }
end

handlers.marker_list = function()
  local total = reaper.CountProjectMarkers(0)
  local markers = json.array({})
  for i = 0, total - 1 do
    local ok, isrgn, pos, rgnend, name, idxnum, col = reaper.EnumProjectMarkers3(0, i)
    if ok > 0 then
      markers[#markers + 1] = {
        enum_index = i,
        id = idxnum,
        is_region = isrgn,
        name = name,
        position_sec = pos,
        position_beats = reaper.TimeMap2_timeToQN(0, pos),
        end_sec = isrgn and rgnend or nil,
        end_beats = isrgn and reaper.TimeMap2_timeToQN(0, rgnend) or nil,
        color = native_color_to_hex(col),
      }
    end
  end
  return { markers = markers }
end

handlers.marker_add = function(args)
  local pos = resolve_time_sec(args, "position_sec", "position_beats", reaper.GetCursorPosition())
  local isrgn = args.is_region and true or false
  local rgnend = isrgn and resolve_time_sec(args, "end_sec", "end_beats", pos + 4) or 0
  local col = 0
  if args.color then
    local nc = hex_to_native_color(args.color)
    if nc then col = nc | 0x1000000 end
  end
  local id = reaper.AddProjectMarker2(0, isrgn, pos, rgnend, args.name or "", tonumber(args.id or -1), col)
  return { id = id, is_region = isrgn, position_sec = pos, end_sec = isrgn and rgnend or nil }
end

handlers.marker_delete = function(args)
  local ok = reaper.DeleteProjectMarker(0, math.floor(tonumber(args.id)), args.is_region and true or false)
  return { deleted = ok }
end

handlers.action_run = function(args)
  local cmd_id = args.command_id
  if type(cmd_id) == "string" then
    local num = tonumber(cmd_id)
    if num then
      cmd_id = math.floor(num)
    else
      cmd_id = reaper.NamedCommandLookup(cmd_id)
    end
  end
  if not cmd_id or cmd_id == 0 then
    error("Action command_id not found: " .. tostring(args.command_id))
  end
  local section = math.floor(tonumber(args.section or 0))
  if section == 0 then
    reaper.Main_OnCommand(cmd_id, 0)
  else
    reaper.Main_OnCommandEx(cmd_id, 0, 0)
  end
  return { executed = true, resolved_id = cmd_id }
end

handlers.undo = function()
  local ok = reaper.Undo_DoUndo2(0) ~= 0
  return { undone = ok }
end

handlers.redo = function()
  local ok = reaper.Undo_DoRedo2(0) ~= 0
  return { redone = ok }
end

-- 2. Tracks & Routing --------------------------------------------------------

handlers.track_list = function(args)
  args = args or {}
  local tracks = json.array({})
  if args.include_master then
    tracks[#tracks + 1] = serialize_track(reaper.GetMasterTrack(0), -1, args.include_fx, false)
  end
  local cnt = reaper.CountTracks(0)
  for i = 0, cnt - 1 do
    tracks[#tracks + 1] = serialize_track(reaper.GetTrack(0, i), i, args.include_fx, args.include_sends)
  end
  return { count = cnt, tracks = tracks }
end

handlers.track_get = function(args)
  local tr, idx = resolve_track(args.track, true)
  return serialize_track(tr, idx, args.include_fx ~= false, args.include_sends ~= false)
end

local function apply_track_properties(tr, args)
  if args.name ~= nil then
    reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", tostring(args.name), true)
  end
  if args.volume_db ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "D_VOL", db_to_linear(tonumber(args.volume_db)))
  elseif args.volume_linear ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "D_VOL", tonumber(args.volume_linear))
  end
  if args.pan ~= nil then
    local p = tonumber(args.pan)
    if p < -1 then p = -1 elseif p > 1 then p = 1 end
    reaper.SetMediaTrackInfo_Value(tr, "D_PAN", p)
  end
  if args.mute ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "B_MUTE", args.mute and 1 or 0)
  end
  if args.solo ~= nil then
    local s = args.solo
    if type(s) == "boolean" then s = s and 1 or 0 end
    reaper.SetMediaTrackInfo_Value(tr, "I_SOLO", math.floor(tonumber(s)))
  end
  if args.rec_arm ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "I_RECARM", args.rec_arm and 1 or 0)
  end
  if args.rec_mon ~= nil then
    local m = args.rec_mon
    if type(m) == "boolean" then m = m and 1 or 0 end
    reaper.SetMediaTrackInfo_Value(tr, "I_RECMON", math.floor(tonumber(m)))
  end
  if args.midi_input == true or args.midi_input == "all" then
    -- All MIDI inputs (63), all channels (0)
    reaper.SetMediaTrackInfo_Value(tr, "I_RECINPUT", 4096 | (63 << 5) | 0)
  elseif type(args.rec_input) == "number" then
    reaper.SetMediaTrackInfo_Value(tr, "I_RECINPUT", math.floor(args.rec_input))
  end
  if args.folder_depth ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH", math.floor(tonumber(args.folder_depth)))
  end
  if args.selected ~= nil then
    reaper.SetMediaTrackInfo_Value(tr, "I_SELECTED", args.selected and 1 or 0)
  end
  if args.color ~= nil then
    if args.color == "" or args.color == false then
      reaper.SetMediaTrackInfo_Value(tr, "I_CUSTOMCOLOR", 0)
    else
      local nc = hex_to_native_color(args.color)
      if nc then reaper.SetTrackColor(tr, nc) end
    end
  end
end

handlers.track_create = function(args)
  args = args or {}
  local cnt = reaper.CountTracks(0)
  local idx = args.index ~= nil and math.floor(tonumber(args.index)) or cnt
  if idx < 0 then idx = 0 elseif idx > cnt then idx = cnt end
  reaper.InsertTrackAtIndex(idx, true)
  local tr = reaper.GetTrack(0, idx)
  apply_track_properties(tr, args)

  -- Optional immediate instrument / FX insertion
  local added_fx = json.array({})
  if args.instrument then
    local res = handlers.fx_add({
      track = idx,
      fx_name = args.instrument,
      prefer_instrument = true,
      configure_midi_input = args.configure_midi_input ~= false,
      open_ui = args.open_ui,
    })
    added_fx[#added_fx + 1] = res
  end
  if type(args.fx) == "table" then
    for _, fxn in ipairs(args.fx) do
      local res = handlers.fx_add({ track = idx, fx_name = fxn })
      added_fx[#added_fx + 1] = res
    end
  end

  reaper.TrackList_AdjustWindows(false)
  local info = serialize_track(tr, idx, true, false)
  if #added_fx > 0 then info.added_fx = added_fx end
  return info
end

handlers.track_edit = function(args)
  local tr, idx = resolve_track(args.track, true)
  apply_track_properties(tr, args)
  reaper.TrackList_AdjustWindows(false)
  return serialize_track(tr, idx, false, false)
end

handlers.track_delete = function(args)
  local tr, idx = resolve_track(args.track, false)
  local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  reaper.DeleteTrack(tr)
  reaper.TrackList_AdjustWindows(false)
  return { deleted = true, index = idx, name = name }
end

handlers.track_list_sends = function(args)
  local tr, idx = resolve_track(args.track, false)
  return serialize_track(tr, idx, false, true)
end

handlers.track_add_send = function(args)
  local src_tr = resolve_track(args.src_track or args.track, false)
  local dst_tr = resolve_track(args.dest_track, false)
  local send_idx = reaper.CreateTrackSend(src_tr, dst_tr)
  if send_idx < 0 then error("Failed to create track send") end
  if args.volume_db ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_VOL", db_to_linear(tonumber(args.volume_db)))
  end
  if args.pan ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_PAN", tonumber(args.pan))
  end
  if args.mute ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "B_MUTE", args.mute and 1 or 0)
  end
  if args.send_mode ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_SENDMODE", math.floor(tonumber(args.send_mode)))
  end
  if args.midi_flags ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_MIDIFLAGS", math.floor(tonumber(args.midi_flags)))
  end
  if args.src_chan ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_SRCCHAN", math.floor(tonumber(args.src_chan)))
  end
  if args.dst_chan ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_DSTCHAN", math.floor(tonumber(args.dst_chan)))
  end
  return { send_index = send_idx }
end

handlers.track_edit_send = function(args)
  local src_tr = resolve_track(args.src_track or args.track, false)
  local send_idx = math.floor(tonumber(args.send_index))
  if args.volume_db ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_VOL", db_to_linear(tonumber(args.volume_db)))
  end
  if args.pan ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "D_PAN", tonumber(args.pan))
  end
  if args.mute ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "B_MUTE", args.mute and 1 or 0)
  end
  if args.send_mode ~= nil then
    reaper.SetTrackSendInfo_Value(src_tr, 0, send_idx, "I_SENDMODE", math.floor(tonumber(args.send_mode)))
  end
  return { updated = true, send_index = send_idx }
end

handlers.track_delete_send = function(args)
  local src_tr = resolve_track(args.src_track or args.track, false)
  local ok = reaper.RemoveTrackSend(src_tr, 0, math.floor(tonumber(args.send_index)))
  return { deleted = ok }
end

-- 3. Media Items & Takes -----------------------------------------------------

handlers.item_list = function(args)
  args = args or {}
  local items = json.array({})
  if args.track ~= nil then
    local tr, tidx = resolve_track(args.track, false)
    local cnt = reaper.CountTrackMediaItems(tr)
    for i = 0, cnt - 1 do
      items[#items + 1] = serialize_item(reaper.GetTrackMediaItem(tr, i), i, tidx)
    end
  else
    local tcnt = reaper.CountTracks(0)
    for t = 0, tcnt - 1 do
      local tr = reaper.GetTrack(0, t)
      local cnt = reaper.CountTrackMediaItems(tr)
      for i = 0, cnt - 1 do
        items[#items + 1] = serialize_item(reaper.GetTrackMediaItem(tr, i), i, t)
      end
    end
  end
  return { count = #items, items = items }
end

handlers.item_get = function(args)
  local item, tr = resolve_item(args)
  local tidx = math.floor(reaper.GetMediaTrackInfo_Value(tr, "IP_TRACKNUMBER")) - 1
  local cnt = reaper.CountTrackMediaItems(tr)
  local iidx = 0
  for i = 0, cnt - 1 do
    if reaper.GetTrackMediaItem(tr, i) == item then iidx = i; break end
  end
  return serialize_item(item, iidx, tidx)
end

handlers.item_create_midi = function(args)
  local tr, tidx = resolve_track(args.track, false)
  local use_qn = (args.start_beats ~= nil or args.length_beats ~= nil or args.end_beats ~= nil)
                 and (args.start_sec == nil and args.length_sec == nil and args.end_sec == nil)
  local item
  if use_qn then
    local start_qn = tonumber(args.start_beats or 0)
    local end_qn = args.end_beats ~= nil and tonumber(args.end_beats) or (start_qn + tonumber(args.length_beats or 4))
    item = reaper.CreateNewMIDIItemInProj(tr, start_qn, end_qn, true)
  else
    local start_s = resolve_time_sec(args, "start_sec", "start_beats", 0)
    local end_s
    if args.end_sec ~= nil or args.end_beats ~= nil then
      end_s = resolve_time_sec(args, "end_sec", "end_beats", start_s + 2)
    elseif args.length_sec ~= nil then
      end_s = start_s + tonumber(args.length_sec)
    else
      local start_qn = reaper.TimeMap2_timeToQN(0, start_s)
      end_s = reaper.TimeMap2_QNToTime(0, start_qn + tonumber(args.length_beats or 4))
    end
    item = reaper.CreateNewMIDIItemInProj(tr, start_s, end_s, false)
  end

  if not item then error("Failed to create MIDI item") end

  local take = reaper.GetActiveTake(item)
  if args.name and take then
    reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", tostring(args.name), true)
  end
  if args.loop ~= nil then
    reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", args.loop and 1 or 0)
  end
  if args.color then
    local nc = hex_to_native_color(args.color)
    if nc then reaper.SetMediaItemInfo_Value(item, "I_CUSTOMCOLOR", nc | 0x1000000) end
  end

  -- Optionally populate notes/CCs right away
  local _, guid = reaper.GetSetMediaItemInfo_String(item, "GUID", "", false)
  if type(args.notes) == "table" and #args.notes > 0 then
    handlers.midi_insert_notes({ item_guid = guid, notes = args.notes })
  end
  if type(args.cc) == "table" and #args.cc > 0 then
    handlers.midi_insert_cc({ item_guid = guid, events = args.cc })
  end

  local cnt = reaper.CountTrackMediaItems(tr)
  local iidx = cnt - 1
  for i = 0, cnt - 1 do
    if reaper.GetTrackMediaItem(tr, i) == item then iidx = i; break end
  end
  return serialize_item(item, iidx, tidx)
end

handlers.item_insert_media = function(args)
  if not args.path then error("Missing 'path'") end
  local tr, tidx = resolve_track(args.track, false)
  -- Save cursor & selection
  local prev_cursor = reaper.GetCursorPosition()
  local pos = resolve_time_sec(args, "position_sec", "position_beats", prev_cursor)
  reaper.SetOnlyTrackSelected(tr)
  reaper.SetEditCurPos(pos, false, false)
  -- Mode 0 = add to current track
  reaper.InsertMedia(args.path, 0)
  reaper.SetEditCurPos(prev_cursor, false, false)
  local cnt = reaper.CountTrackMediaItems(tr)
  if cnt > 0 then
    return serialize_item(reaper.GetTrackMediaItem(tr, cnt - 1), cnt - 1, tidx)
  end
  return { inserted = true }
end

handlers.item_edit = function(args)
  local item, tr = resolve_item(args)
  if args.position_sec ~= nil or args.position_beats ~= nil then
    local pos = resolve_time_sec(args, "position_sec", "position_beats", 0)
    reaper.SetMediaItemInfo_Value(item, "D_POSITION", pos)
  end
  if args.length_sec ~= nil then
    reaper.SetMediaItemInfo_Value(item, "D_LENGTH", tonumber(args.length_sec))
  elseif args.length_beats ~= nil then
    local cur_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local start_qn = reaper.TimeMap2_timeToQN(0, cur_pos)
    local end_sec = reaper.TimeMap2_QNToTime(0, start_qn + tonumber(args.length_beats))
    reaper.SetMediaItemInfo_Value(item, "D_LENGTH", end_sec - cur_pos)
  end
  if args.mute ~= nil then
    reaper.SetMediaItemInfo_Value(item, "B_MUTE", args.mute and 1 or 0)
  end
  if args.loop ~= nil then
    reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", args.loop and 1 or 0)
  end
  if args.volume_db ~= nil then
    reaper.SetMediaItemInfo_Value(item, "D_VOL", db_to_linear(tonumber(args.volume_db)))
  end
  if args.fade_in_sec ~= nil then
    reaper.SetMediaItemInfo_Value(item, "D_FADEINLEN", tonumber(args.fade_in_sec))
  end
  if args.fade_out_sec ~= nil then
    reaper.SetMediaItemInfo_Value(item, "D_FADEOUTLEN", tonumber(args.fade_out_sec))
  end
  if args.color ~= nil then
    local nc = hex_to_native_color(args.color)
    if nc then reaper.SetMediaItemInfo_Value(item, "I_CUSTOMCOLOR", nc | 0x1000000) end
  end
  local take = reaper.GetActiveTake(item)
  if take then
    if args.name ~= nil then
      reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", tostring(args.name), true)
    end
    if args.playrate ~= nil then
      reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", tonumber(args.playrate))
    end
    if args.pitch ~= nil then
      reaper.SetMediaItemTakeInfo_Value(take, "D_PITCH", tonumber(args.pitch))
    end
  end
  reaper.MarkTrackItemsDirty(tr, item)
  return handlers.item_get(args)
end

handlers.item_split = function(args)
  local item, tr = resolve_item(args)
  local pos = resolve_time_sec(args, "position_sec", "position_beats", nil)
  if not pos then error("Missing 'position_sec' or 'position_beats'") end
  local right = reaper.SplitMediaItem(item, pos)
  if not right then error("Split failed (position may be outside item bounds)") end
  local _, left_guid = reaper.GetSetMediaItemInfo_String(item, "GUID", "", false)
  local _, right_guid = reaper.GetSetMediaItemInfo_String(right, "GUID", "", false)
  return { left_item_guid = left_guid, right_item_guid = right_guid, split_sec = pos }
end

handlers.item_delete = function(args)
  local item, tr = resolve_item(args)
  local ok = reaper.DeleteTrackMediaItem(tr, item)
  return { deleted = ok }
end

-- 4. MIDI Editing (Live REAPER Takes) ----------------------------------------

handlers.midi_get_events = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local _, note_cnt, cc_cnt, text_cnt = reaper.MIDI_CountEvts(take)

  local notes = json.array({})
  if args.include_notes ~= false then
    for i = 0, note_cnt - 1 do
      local ok, sel, muted, sp, ep, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
      if ok then
        local sqn = reaper.MIDI_GetProjQNFromPPQPos(take, sp)
        local eqn = reaper.MIDI_GetProjQNFromPPQPos(take, ep)
        notes[#notes + 1] = {
          index = i,
          pitch = pitch,
          note_name = pitch_to_name(pitch),
          velocity = vel,
          channel = chan,
          start_beats = sqn,
          end_beats = eqn,
          start_beat_rel = sqn - item_start_qn,
          duration_beats = eqn - sqn,
          start_sec = reaper.MIDI_GetProjTimeFromPPQPos(take, sp),
          end_sec = reaper.MIDI_GetProjTimeFromPPQPos(take, ep),
          start_ppq = sp,
          end_ppq = ep,
          muted = muted,
          selected = sel,
        }
      end
    end
  end

  local cc_events = json.array({})
  if args.include_cc ~= false then
    for i = 0, cc_cnt - 1 do
      local ok, sel, muted, ppq, chanmsg, chan, msg2, msg3 = reaper.MIDI_GetCC(take, i)
      if ok then
        local _, shape, tension = reaper.MIDI_GetCCShape(take, i)
        local qn = reaper.MIDI_GetProjQNFromPPQPos(take, ppq)
        local evt = {
          index = i,
          ppq = ppq,
          beats = qn,
          beat_rel = qn - item_start_qn,
          sec = reaper.MIDI_GetProjTimeFromPPQPos(take, ppq),
          chanmsg = chanmsg,
          channel = chan,
          msg2 = msg2,
          msg3 = msg3,
          shape = shape,
          tension = tension,
          muted = muted,
          selected = sel,
        }
        if chanmsg == 0xB0 then
          evt.kind = "cc"
          evt.cc = msg2
          evt.value = msg3
        elseif chanmsg == 0xE0 then
          evt.kind = "pitch_bend"
          evt.value = ((msg3 << 7) | msg2) - 8192
        elseif chanmsg == 0xC0 then
          evt.kind = "program_change"
          evt.program = msg2
        elseif chanmsg == 0xD0 then
          evt.kind = "channel_pressure"
          evt.value = msg2
        elseif chanmsg == 0xA0 then
          evt.kind = "poly_aftertouch"
          evt.pitch = msg2
          evt.value = msg3
        else
          evt.kind = "other"
        end
        cc_events[#cc_events + 1] = evt
      end
    end
  end

  local text_events = json.array({})
  if args.include_text ~= false then
    for i = 0, text_cnt - 1 do
      local ok, sel, muted, ppq, evt_type, msg = reaper.MIDI_GetTextSysexEvt(take, i)
      if ok then
        local qn = reaper.MIDI_GetProjQNFromPPQPos(take, ppq)
        text_events[#text_events + 1] = {
          index = i,
          ppq = ppq,
          beats = qn,
          beat_rel = qn - item_start_qn,
          type = evt_type,
          text = msg,
          muted = muted,
          selected = sel,
        }
      end
    end
  end

  return {
    item_start_beats = item_start_qn,
    note_count = note_cnt,
    cc_count = cc_cnt,
    text_count = text_cnt,
    notes = notes,
    cc = cc_events,
    text = text_events,
  }
end

handlers.midi_clear = function(args)
  local take, item, tr = resolve_take(args, true)
  local _, note_cnt, cc_cnt, text_cnt = reaper.MIDI_CountEvts(take)
  reaper.MIDI_DisableSort(take)
  local clear_notes = (args.notes ~= false)
  local clear_cc = (args.cc ~= false)
  local clear_text = (args.text == true)
  if clear_notes then
    for i = note_cnt - 1, 0, -1 do reaper.MIDI_DeleteNote(take, i) end
  end
  if clear_cc then
    for i = cc_cnt - 1, 0, -1 do reaper.MIDI_DeleteCC(take, i) end
  end
  if clear_text then
    for i = text_cnt - 1, 0, -1 do reaper.MIDI_DeleteTextSysexEvt(take, i) end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { cleared_notes = clear_notes and note_cnt or 0, cleared_cc = clear_cc and cc_cnt or 0 }
end

handlers.midi_insert_notes = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local notes = args.notes
  if type(notes) ~= "table" then error("Missing 'notes' array") end

  reaper.MIDI_DisableSort(take)

  if args.clear_existing then
    local _, note_cnt = reaper.MIDI_CountEvts(take)
    for i = note_cnt - 1, 0, -1 do
      reaper.MIDI_DeleteNote(take, i)
    end
  end

  local inserted = 0
  for _, n in ipairs(notes) do
    local pitch = parse_pitch(n.pitch ~= nil and n.pitch or n.note)
    local start_ppq = resolve_ppq(take, item_start_qn, n, "start_")
    if not start_ppq then
      -- Also allow shorthand: beat_rel or beats
      if n.beat_rel ~= nil then
        start_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn + tonumber(n.beat_rel))
      elseif n.beats ~= nil then
        start_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, tonumber(n.beats))
      else
        start_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn)
      end
    end

    local end_ppq = resolve_ppq(take, item_start_qn, n, "end_")
    if not end_ppq then
      local start_qn = reaper.MIDI_GetProjQNFromPPQPos(take, start_ppq)
      if n.duration_beats ~= nil then
        end_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, start_qn + tonumber(n.duration_beats))
      elseif n.duration_sec ~= nil then
        local start_sec = reaper.MIDI_GetProjTimeFromPPQPos(take, start_ppq)
        end_ppq = reaper.MIDI_GetPPQPosFromProjTime(take, start_sec + tonumber(n.duration_sec))
      elseif n.duration_ppq ~= nil then
        end_ppq = start_ppq + tonumber(n.duration_ppq)
      else
        end_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, start_qn + 0.5)
      end
    end
    if end_ppq <= start_ppq then end_ppq = start_ppq + 1 end

    local vel = math.floor(tonumber(n.velocity or 100))
    if vel < 1 then vel = 1 elseif vel > 127 then vel = 127 end
    local chan = math.floor(tonumber(n.channel or 0))
    if chan < 0 then chan = 0 elseif chan > 15 then chan = 15 end
    local sel = n.selected and true or false
    local muted = n.muted and true or false

    if reaper.MIDI_InsertNote(take, sel, muted, start_ppq, end_ppq, chan, pitch, vel, true) then
      inserted = inserted + 1
    end
  end

  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  local _, total_notes = reaper.MIDI_CountEvts(take)
  return { inserted = inserted, total_notes = total_notes }
end

handlers.midi_edit_notes = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local edits = args.edits
  if type(edits) ~= "table" then error("Missing 'edits' array") end

  reaper.MIDI_DisableSort(take)
  local updated = 0
  for _, e in ipairs(edits) do
    local idx = math.floor(tonumber(e.index))
    local ok, sel, muted, sp, ep, chan, pitch, vel = reaper.MIDI_GetNote(take, idx)
    if ok then
      if e.selected ~= nil then sel = e.selected and true or false end
      if e.muted ~= nil then muted = e.muted and true or false end
      if e.channel ~= nil then chan = math.max(0, math.min(15, math.floor(tonumber(e.channel)))) end
      if e.pitch ~= nil or e.note ~= nil then pitch = parse_pitch(e.pitch ~= nil and e.pitch or e.note) end
      if e.velocity ~= nil then vel = math.max(1, math.min(127, math.floor(tonumber(e.velocity)))) end

      local new_sp = resolve_ppq(take, item_start_qn, e, "start_")
      if new_sp then sp = new_sp end
      local new_ep = resolve_ppq(take, item_start_qn, e, "end_")
      if new_ep then
        ep = new_ep
      elseif e.duration_beats ~= nil then
        local sqn = reaper.MIDI_GetProjQNFromPPQPos(take, sp)
        ep = reaper.MIDI_GetPPQPosFromProjQN(take, sqn + tonumber(e.duration_beats))
      elseif e.duration_sec ~= nil then
        local ssec = reaper.MIDI_GetProjTimeFromPPQPos(take, sp)
        ep = reaper.MIDI_GetPPQPosFromProjTime(take, ssec + tonumber(e.duration_sec))
      end
      if ep <= sp then ep = sp + 1 end

      if reaper.MIDI_SetNote(take, idx, sel, muted, sp, ep, chan, pitch, vel, true) then
        updated = updated + 1
      end
    end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { updated = updated }
end

handlers.midi_delete_notes = function(args)
  local take, item, tr = resolve_take(args, true)
  local _, note_cnt = reaper.MIDI_CountEvts(take)
  local to_delete = {}

  if type(args.indices) == "table" then
    for _, idx in ipairs(args.indices) do
      to_delete[math.floor(tonumber(idx))] = true
    end
  else
    -- Filter-based deletion
    local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
    local min_pitch = args.min_pitch and parse_pitch(args.min_pitch) or nil
    local max_pitch = args.max_pitch and parse_pitch(args.max_pitch) or nil
    local exact_pitch = args.pitch and parse_pitch(args.pitch) or nil
    local chan_filter = args.channel ~= nil and math.floor(tonumber(args.channel)) or nil
    local min_qn = args.start_beats ~= nil and tonumber(args.start_beats) or (args.start_beat_rel ~= nil and (item_start_qn + tonumber(args.start_beat_rel)) or nil)
    local max_qn = args.end_beats ~= nil and tonumber(args.end_beats) or (args.end_beat_rel ~= nil and (item_start_qn + tonumber(args.end_beat_rel)) or nil)

    for i = 0, note_cnt - 1 do
      local ok, _, _, sp, _, chan, pitch = reaper.MIDI_GetNote(take, i)
      if ok then
        local sqn = reaper.MIDI_GetProjQNFromPPQPos(take, sp)
        local match = true
        if exact_pitch and pitch ~= exact_pitch then match = false end
        if min_pitch and pitch < min_pitch then match = false end
        if max_pitch and pitch > max_pitch then match = false end
        if chan_filter and chan ~= chan_filter then match = false end
        if min_qn and sqn < min_qn then match = false end
        if max_qn and sqn > max_qn then match = false end
        if match then to_delete[i] = true end
      end
    end
  end

  reaper.MIDI_DisableSort(take)
  local deleted = 0
  for i = note_cnt - 1, 0, -1 do
    if to_delete[i] then
      if reaper.MIDI_DeleteNote(take, i) then
        deleted = deleted + 1
      end
    end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  local _, rem = reaper.MIDI_CountEvts(take)
  return { deleted = deleted, remaining_notes = rem }
end

handlers.midi_transform_notes = function(args)
  local take, item, tr = resolve_take(args, true)
  local _, note_cnt = reaper.MIDI_CountEvts(take)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)

  local transpose = tonumber(args.transpose or 0)
  local vel_offset = tonumber(args.velocity_offset or 0)
  local vel_scale = tonumber(args.velocity_scale or 1.0)
  local vel_set = args.velocity_set and math.floor(tonumber(args.velocity_set)) or nil
  local quantize_grid = args.quantize_beats and tonumber(args.quantize_beats) or nil
  local quantize_strength = tonumber(args.quantize_strength or 1.0)
  local humanize_beats = tonumber(args.humanize_beats or 0)
  local humanize_vel = tonumber(args.humanize_velocity or 0)
  local speed_scale = args.speed_scale and tonumber(args.speed_scale) or nil
  local shift_beats = tonumber(args.shift_beats or 0)

  reaper.MIDI_DisableSort(take)
  local modified = 0
  for i = 0, note_cnt - 1 do
    local ok, sel, muted, sp, ep, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
    if ok and (not args.selected_only or sel) then
      local sqn = reaper.MIDI_GetProjQNFromPPQPos(take, sp)
      local eqn = reaper.MIDI_GetProjQNFromPPQPos(take, ep)
      local dur = eqn - sqn

      if transpose ~= 0 then
        pitch = math.max(0, math.min(127, pitch + math.floor(transpose)))
      end

      if vel_set then
        vel = vel_set
      else
        vel = math.floor(vel * vel_scale + vel_offset + 0.5)
      end
      if humanize_vel > 0 then
        vel = vel + math.random(-math.floor(humanize_vel), math.floor(humanize_vel))
      end
      vel = math.max(1, math.min(127, vel))

      if speed_scale and speed_scale > 0 then
        local rel = sqn - item_start_qn
        sqn = item_start_qn + rel / speed_scale
        dur = dur / speed_scale
      end

      if quantize_grid and quantize_grid > 0 then
        local rel = sqn - item_start_qn
        local snapped = math.floor(rel / quantize_grid + 0.5) * quantize_grid
        sqn = item_start_qn + rel + (snapped - rel) * quantize_strength
      end

      if shift_beats ~= 0 then
        sqn = sqn + shift_beats
      end

      if humanize_beats > 0 then
        local jitter = (math.random() * 2 - 1) * humanize_beats
        sqn = math.max(item_start_qn, sqn + jitter)
      end

      local new_sp = reaper.MIDI_GetPPQPosFromProjQN(take, sqn)
      local new_ep = reaper.MIDI_GetPPQPosFromProjQN(take, sqn + math.max(0.01, dur))
      reaper.MIDI_SetNote(take, i, sel, muted, new_sp, new_ep, chan, pitch, vel, true)
      modified = modified + 1
    end
  end

  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { modified = modified }
end

handlers.midi_insert_cc = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local events = args.events
  if type(events) ~= "table" then error("Missing 'events' array") end

  reaper.MIDI_DisableSort(take)
  if args.clear_existing then
    local _, _, cc_cnt = reaper.MIDI_CountEvts(take)
    for i = cc_cnt - 1, 0, -1 do reaper.MIDI_DeleteCC(take, i) end
  end

  local inserted = 0
  for _, ev in ipairs(events) do
    local ppq = resolve_ppq(take, item_start_qn, ev, "")
    if not ppq then
      ppq = reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn)
    end
    local chan = math.max(0, math.min(15, math.floor(tonumber(ev.channel or 0))))
    local kind = ev.kind or "cc"
    local chanmsg, msg2, msg3
    if kind == "pitch_bend" then
      chanmsg = 0xE0
      local v = math.max(-8192, math.min(8191, math.floor(tonumber(ev.value or 0)))) + 8192
      msg2 = v & 0x7F
      msg3 = (v >> 7) & 0x7F
    elseif kind == "program_change" then
      chanmsg = 0xC0
      msg2 = math.max(0, math.min(127, math.floor(tonumber(ev.program or ev.value or 0))))
      msg3 = 0
    elseif kind == "channel_pressure" then
      chanmsg = 0xD0
      msg2 = math.max(0, math.min(127, math.floor(tonumber(ev.value or 0))))
      msg3 = 0
    elseif kind == "poly_aftertouch" then
      chanmsg = 0xA0
      msg2 = parse_pitch(ev.pitch or 60)
      msg3 = math.max(0, math.min(127, math.floor(tonumber(ev.value or 0))))
    else
      chanmsg = 0xB0
      msg2 = math.max(0, math.min(127, math.floor(tonumber(ev.cc or 1))))
      msg3 = math.max(0, math.min(127, math.floor(tonumber(ev.value or 0))))
    end

    local sel = ev.selected and true or false
    local muted = ev.muted and true or false
    if reaper.MIDI_InsertCC(take, sel, muted, ppq, chanmsg, chan, msg2, msg3) then
      inserted = inserted + 1
      if ev.shape ~= nil then
        local _, _, new_cc_cnt = reaper.MIDI_CountEvts(take)
        reaper.MIDI_SetCCShape(take, new_cc_cnt - 1, math.floor(tonumber(ev.shape)), tonumber(ev.tension or 0), true)
      end
    end
  end

  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { inserted = inserted }
end

handlers.midi_insert_cc_ramp = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local cc_num = math.max(0, math.min(127, math.floor(tonumber(args.cc or 1))))
  local chan = math.max(0, math.min(15, math.floor(tonumber(args.channel or 0))))
  local start_ppq = resolve_ppq(take, item_start_qn, args, "start_") or reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn)
  local end_ppq = resolve_ppq(take, item_start_qn, args, "end_") or reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn + 4)
  local start_val = tonumber(args.start_value or 0)
  local end_val = tonumber(args.end_value or 127)
  local steps = math.max(2, math.floor(tonumber(args.steps or 16)))
  local curve = tonumber(args.curve or 1.0) -- 1=linear, >1 exponential, <1 logarithmic

  reaper.MIDI_DisableSort(take)
  local inserted = 0
  for s = 0, steps - 1 do
    local t = s / (steps - 1)
    local tc = t ^ curve
    local ppq = start_ppq + (end_ppq - start_ppq) * t
    local val = math.max(0, math.min(127, math.floor(start_val + (end_val - start_val) * tc + 0.5)))
    if reaper.MIDI_InsertCC(take, false, false, ppq, 0xB0, chan, cc_num, val) then
      inserted = inserted + 1
    end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { inserted = inserted, cc = cc_num }
end

handlers.midi_delete_cc = function(args)
  local take, item, tr = resolve_take(args, true)
  local _, _, cc_cnt = reaper.MIDI_CountEvts(take)
  local cc_filter = args.cc ~= nil and math.floor(tonumber(args.cc)) or nil
  local chan_filter = args.channel ~= nil and math.floor(tonumber(args.channel)) or nil
  local idx_set = nil
  if type(args.indices) == "table" then
    idx_set = {}
    for _, v in ipairs(args.indices) do idx_set[math.floor(tonumber(v))] = true end
  end

  reaper.MIDI_DisableSort(take)
  local deleted = 0
  for i = cc_cnt - 1, 0, -1 do
    local ok, _, _, _, chanmsg, chan, msg2 = reaper.MIDI_GetCC(take, i)
    if ok then
      local match = true
      if idx_set then
        match = idx_set[i] == true
      else
        if cc_filter ~= nil and (chanmsg ~= 0xB0 or msg2 ~= cc_filter) then match = false end
        if chan_filter ~= nil and chan ~= chan_filter then match = false end
      end
      if match and reaper.MIDI_DeleteCC(take, i) then
        deleted = deleted + 1
      end
    end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { deleted = deleted }
end

handlers.midi_insert_text = function(args)
  local take, item, tr = resolve_take(args, true)
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_start_qn = reaper.TimeMap2_timeToQN(0, item_pos)
  local events = args.events
  if type(events) ~= "table" then error("Missing 'events' array") end
  reaper.MIDI_DisableSort(take)
  local inserted = 0
  for _, ev in ipairs(events) do
    local ppq = resolve_ppq(take, item_start_qn, ev, "") or reaper.MIDI_GetPPQPosFromProjQN(take, item_start_qn)
    local evt_type = math.floor(tonumber(ev.type or 1)) -- 1=text, 5=lyric, 6=marker, 7=cue
    local text = tostring(ev.text or "")
    if reaper.MIDI_InsertTextSysexEvt(take, ev.selected and true or false, ev.muted and true or false, ppq, evt_type, text) then
      inserted = inserted + 1
    end
  end
  reaper.MIDI_Sort(take)
  reaper.MarkTrackItemsDirty(tr, item)
  return { inserted = inserted }
end

-- 5. FX & Instruments --------------------------------------------------------

handlers.fx_enum_installed = function(args)
  args = args or {}
  if not reaper.EnumInstalledFX then
    error("reaper.EnumInstalledFX requires REAPER 7.0+")
  end
  local query = args.query and args.query:lower() or nil
  local kind = args.kind or "all" -- all | instrument | effect
  local fmt = args.format and args.format:lower() or "all"
  local offset = math.floor(tonumber(args.offset or 0))
  local limit = math.floor(tonumber(args.limit or 100))

  local matched = {}
  local i = 0
  while true do
    local ok, name, ident = reaper.EnumInstalledFX(i)
    if not ok then break end
    local nl = name:lower()
    local is_inst = nl:match("^vst3?i:") ~= nil or nl:match("^clapi:") ~= nil or nl:match("^aui:") ~= nil
    local ftype = "other"
    if nl:match("^vst3i?:") then ftype = "vst3"
    elseif nl:match("^vsti?:") then ftype = "vst2"
    elseif nl:match("^clapi?:") then ftype = "clap"
    elseif nl:match("^js:") then ftype = "jsfx"
    elseif nl:match("^aui?:") then ftype = "au"
    elseif nl:match("^dx") then ftype = "dx"
    end

    local keep = true
    if query and not nl:find(query, 1, true) then keep = false end
    if kind == "instrument" and not is_inst then keep = false end
    if kind == "effect" and is_inst then keep = false end
    if fmt ~= "all" and ftype ~= fmt then keep = false end

    if keep then
      matched[#matched + 1] = {
        name = name,
        ident = ident,
        is_instrument = is_inst,
        format = ftype,
      }
    end
    i = i + 1
  end

  local page = json.array({})
  for k = offset + 1, math.min(#matched, offset + limit) do
    page[#page + 1] = matched[k]
  end
  return { total_installed = i, matched_count = #matched, offset = offset, items = page }
end

handlers.fx_list = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local count = rec_fx and reaper.TrackFX_GetRecCount(tr) or reaper.TrackFX_GetCount(tr)
  local list = json.array({})
  for i = 0, count - 1 do
    list[#list + 1] = serialize_fx(tr, i, rec_fx)
  end
  return { count = count, rec_fx = rec_fx, fx = list }
end

handlers.fx_add = function(args)
  local tr = resolve_track(args.track, true)
  local fx_name = args.fx_name
  if not fx_name or fx_name == "" then error("Missing 'fx_name'") end
  local rec_fx = args.rec_fx and true or false
  local instantiate = -1 -- default: always add a new instance at end
  if args.reuse_existing then
    instantiate = 1
  elseif args.position ~= nil then
    instantiate = -1000 - math.floor(tonumber(args.position))
  end

  local idx = reaper.TrackFX_AddByName(tr, fx_name, rec_fx, instantiate)
  local resolved_name = fx_name
  if idx < 0 then
    -- Smart resolution against REAPER's installed FX list
    local candidate = find_installed_fx_match(fx_name, args.prefer_instrument)
    if candidate then
      resolved_name = candidate
      idx = reaper.TrackFX_AddByName(tr, candidate, rec_fx, instantiate)
    end
  end
  if idx < 0 then
    error("Could not load FX/instrument: " .. fx_name .. ". Check installed FX with fx_enum_installed.")
  end

  local slot = fx_slot(idx, rec_fx)

  -- Optional MIDI input setup when adding an instrument
  if args.configure_midi_input then
    reaper.SetMediaTrackInfo_Value(tr, "I_RECINPUT", 4096 | (63 << 5) | 0) -- All MIDI inputs, all channels
    reaper.SetMediaTrackInfo_Value(tr, "I_RECMON", 1)
  end

  if args.preset_name then
    reaper.TrackFX_SetPreset(tr, slot, tostring(args.preset_name))
  elseif args.preset_index ~= nil then
    reaper.TrackFX_SetPresetByIndex(tr, slot, math.floor(tonumber(args.preset_index)))
  end

  if args.open_ui then
    reaper.TrackFX_Show(tr, slot, 3) -- float window
  end

  -- Apply initial params if supplied
  if type(args.params) == "table" and #args.params > 0 then
    handlers.fx_set_params({
      track = args.track,
      fx = idx,
      rec_fx = rec_fx,
      params = args.params,
    })
  end

  local info = serialize_fx(tr, idx, rec_fx)
  info.requested_name = fx_name
  info.resolved_name = resolved_name
  return info
end

handlers.fx_delete = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local _, name = reaper.TrackFX_GetFXName(tr, slot)
  local ok = reaper.TrackFX_Delete(tr, slot)
  return { deleted = ok, index = idx, name = name }
end

handlers.fx_move = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local _, src_slot = resolve_fx_index(tr, args.fx, rec_fx)
  local dest_idx = math.floor(tonumber(args.dest_index))
  local dest_slot = fx_slot(dest_idx, rec_fx)
  reaper.TrackFX_CopyToTrack(tr, src_slot, tr, dest_slot, true)
  return handlers.fx_list({ track = args.track, rec_fx = rec_fx })
end

handlers.fx_copy = function(args)
  local src_tr = resolve_track(args.src_track or args.track, true)
  local dst_tr = resolve_track(args.dest_track, true)
  local src_rec = args.src_rec_fx and true or false
  local dst_rec = args.dest_rec_fx and true or false
  local _, src_slot = resolve_fx_index(src_tr, args.fx, src_rec)
  local dst_cnt = dst_rec and reaper.TrackFX_GetRecCount(dst_tr) or reaper.TrackFX_GetCount(dst_tr)
  local dst_idx = args.dest_index ~= nil and math.floor(tonumber(args.dest_index)) or dst_cnt
  local dst_slot = fx_slot(dst_idx, dst_rec)
  reaper.TrackFX_CopyToTrack(src_tr, src_slot, dst_tr, dst_slot, args.move and true or false)
  return { copied = true, moved = args.move and true or false, dest_index = dst_idx }
end

handlers.fx_configure = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)

  if args.enabled ~= nil then
    reaper.TrackFX_SetEnabled(tr, slot, args.enabled and true or false)
  end
  if args.bypass ~= nil then
    reaper.TrackFX_SetEnabled(tr, slot, not args.bypass)
  end
  if args.offline ~= nil then
    reaper.TrackFX_SetOffline(tr, slot, args.offline and true or false)
  end
  if args.open_ui ~= nil then
    reaper.TrackFX_Show(tr, slot, args.open_ui and 3 or 2)
  end
  if args.show_chain ~= nil then
    reaper.TrackFX_Show(tr, slot, args.show_chain and 1 or 0)
  end
  if args.wet ~= nil then
    local wet_idx = reaper.TrackFX_GetParamFromIdent(tr, slot, ":wet")
    if wet_idx >= 0 then
      local w = math.max(0.0, math.min(1.0, tonumber(args.wet)))
      reaper.TrackFX_SetParamNormalized(tr, slot, wet_idx, w)
    end
  end
  if args.delta ~= nil then
    local d_idx = reaper.TrackFX_GetParamFromIdent(tr, slot, ":delta")
    if d_idx >= 0 then
      reaper.TrackFX_SetParamNormalized(tr, slot, d_idx, args.delta and 1.0 or 0.0)
    end
  end
  if args.rename ~= nil then
    reaper.TrackFX_SetNamedConfigParm(tr, slot, "renamed_name", tostring(args.rename))
  end

  return serialize_fx(tr, idx, rec_fx)
end

handlers.fx_get_params = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local num = reaper.TrackFX_GetNumParams(tr, slot)
  local search = args.search and args.search:lower() or nil
  local offset = math.floor(tonumber(args.offset or 0))
  local limit = math.floor(tonumber(args.limit or 100))

  local matched = {}
  for p = 0, num - 1 do
    local _, pname = reaper.TrackFX_GetParamName(tr, slot, p)
    if not search or pname:lower():find(search, 1, true) then
      local v_norm = reaper.TrackFX_GetParamNormalized(tr, slot, p)
      local v_raw, min_v, max_v, mid_v = reaper.TrackFX_GetParamEx(tr, slot, p)
      local _, formatted = reaper.TrackFX_GetFormattedParamValue(tr, slot, p)
      matched[#matched + 1] = {
        index = p,
        name = pname,
        value_norm = v_norm,
        value_raw = v_raw,
        min_raw = min_v,
        max_raw = max_v,
        mid_raw = mid_v,
        formatted = formatted,
      }
    end
  end

  local page = json.array({})
  for k = offset + 1, math.min(#matched, offset + limit) do
    page[#page + 1] = matched[k]
  end

  local _, fx_name = reaper.TrackFX_GetFXName(tr, slot)
  return {
    fx_index = idx,
    fx_name = fx_name,
    total_params = num,
    matched_count = #matched,
    offset = offset,
    params = page,
  }
end

handlers.fx_set_params = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local params = args.params
  if type(params) ~= "table" then error("Missing 'params' array") end

  local results = json.array({})
  for _, p in ipairs(params) do
    local pidx = resolve_param_index(tr, slot, p.param ~= nil and p.param or (p.index ~= nil and p.index or p.name))
    local ok = false
    local note = nil
    if p.value_norm ~= nil then
      local v = math.max(0.0, math.min(1.0, tonumber(p.value_norm)))
      ok = reaper.TrackFX_SetParamNormalized(tr, slot, pidx, v)
    elseif p.value_raw ~= nil then
      ok = reaper.TrackFX_SetParam(tr, slot, pidx, tonumber(p.value_raw))
    elseif p.target_formatted ~= nil then
      ok, _, note = set_param_by_formatted_target(tr, slot, pidx, p.target_formatted)
    else
      error("Each parameter edit must include 'value_norm', 'value_raw', or 'target_formatted'")
    end

    local _, pname = reaper.TrackFX_GetParamName(tr, slot, pidx)
    local new_norm = reaper.TrackFX_GetParamNormalized(tr, slot, pidx)
    local new_raw = reaper.TrackFX_GetParamEx(tr, slot, pidx)
    local _, formatted = reaper.TrackFX_GetFormattedParamValue(tr, slot, pidx)
    results[#results + 1] = {
      index = pidx,
      name = pname,
      ok = ok,
      value_norm = new_norm,
      value_raw = new_raw,
      formatted = formatted,
      note = note,
    }
  end

  return { fx_index = idx, results = results }
end

handlers.fx_get_named_config_parm = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local _, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local ok, val = reaper.TrackFX_GetNamedConfigParm(tr, slot, tostring(args.parm_name))
  return { ok = ok, parm_name = args.parm_name, value = val }
end

handlers.fx_set_named_config_parm = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local _, slot = resolve_fx_index(tr, args.fx, rec_fx)
  -- Support setting either a single parm_name/value or an array of {name, value}
  if type(args.parms) == "table" then
    local results = json.array({})
    for _, kv in ipairs(args.parms) do
      local ok = reaper.TrackFX_SetNamedConfigParm(tr, slot, tostring(kv.name), tostring(kv.value))
      results[#results + 1] = { name = kv.name, value = kv.value, ok = ok }
    end
    return { results = results }
  end
  local ok = reaper.TrackFX_SetNamedConfigParm(tr, slot, tostring(args.parm_name), tostring(args.value))
  return { ok = ok, parm_name = args.parm_name, value = args.value }
end

handlers.fx_get_preset = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local ok, name = reaper.TrackFX_GetPreset(tr, slot)
  local pidx, pcnt = reaper.TrackFX_GetPresetIndex(tr, slot)
  return { fx_index = idx, ok = ok, preset_name = name, preset_index = pidx, preset_count = pcnt }
end

handlers.fx_set_preset = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local ok = false
  if args.preset_name ~= nil then
    ok = reaper.TrackFX_SetPreset(tr, slot, tostring(args.preset_name))
  elseif args.preset_index ~= nil then
    ok = reaper.TrackFX_SetPresetByIndex(tr, slot, math.floor(tonumber(args.preset_index)))
  elseif args.navigate ~= nil then
    ok = reaper.TrackFX_NavigatePresets(tr, slot, math.floor(tonumber(args.navigate)))
  else
    error("Provide 'preset_name', 'preset_index', or 'navigate'")
  end
  local _, name = reaper.TrackFX_GetPreset(tr, slot)
  local pidx, pcnt = reaper.TrackFX_GetPresetIndex(tr, slot)
  return { fx_index = idx, ok = ok, preset_name = name, preset_index = pidx, preset_count = pcnt }
end

handlers.fx_scan_presets = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local idx, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local orig_idx, total = reaper.TrackFX_GetPresetIndex(tr, slot)
  local _, orig_name = reaper.TrackFX_GetPreset(tr, slot)
  local max_scan = math.min(total, math.floor(tonumber(args.limit or 64)))
  local start_idx = math.floor(tonumber(args.offset or 0))
  local presets = json.array({})
  for i = start_idx, math.min(total - 1, start_idx + max_scan - 1) do
    if reaper.TrackFX_SetPresetByIndex(tr, slot, i) then
      local _, pname = reaper.TrackFX_GetPreset(tr, slot)
      presets[#presets + 1] = { index = i, name = pname }
    end
  end
  -- Restore original preset
  if orig_idx >= 0 then
    reaper.TrackFX_SetPresetByIndex(tr, slot, orig_idx)
  elseif orig_name and orig_name ~= "" then
    reaper.TrackFX_SetPreset(tr, slot, orig_name)
  end
  return { fx_index = idx, total_presets = total, current_index = orig_idx, current_name = orig_name, presets = presets }
end

handlers.fx_get_chunk = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local _, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local ok, chunk = reaper.TrackFX_GetNamedConfigParm(tr, slot, "vst_chunk")
  local kind = "vst_chunk"
  if not ok or chunk == "" then
    ok, chunk = reaper.TrackFX_GetNamedConfigParm(tr, slot, "clap_chunk")
    kind = "clap_chunk"
  end
  return { ok = ok, kind = kind, chunk = chunk }
end

handlers.fx_set_chunk = function(args)
  local tr = resolve_track(args.track, true)
  local rec_fx = args.rec_fx and true or false
  local _, slot = resolve_fx_index(tr, args.fx, rec_fx)
  local kind = args.kind or "vst_chunk"
  local ok = reaper.TrackFX_SetNamedConfigParm(tr, slot, kind, tostring(args.chunk))
  return { ok = ok, kind = kind }
end

-- Take FX --------------------------------------------------------------------

handlers.take_fx_list = function(args)
  local take = resolve_take(args, false)
  local cnt = reaper.TakeFX_GetCount(take)
  local list = json.array({})
  for i = 0, cnt - 1 do
    local _, name = reaper.TakeFX_GetFXName(take, i)
    local _, preset = reaper.TakeFX_GetPreset(take, i)
    list[#list + 1] = {
      index = i,
      name = name,
      enabled = reaper.TakeFX_GetEnabled(take, i),
      preset_name = preset,
      param_count = reaper.TakeFX_GetNumParams(take, i),
    }
  end
  return { count = cnt, fx = list }
end

handlers.take_fx_add = function(args)
  local take = resolve_take(args, false)
  local idx = reaper.TakeFX_AddByName(take, tostring(args.fx_name), -1)
  if idx < 0 then
    local cand = find_installed_fx_match(tostring(args.fx_name), false)
    if cand then idx = reaper.TakeFX_AddByName(take, cand, -1) end
  end
  if idx < 0 then error("Could not add Take FX: " .. tostring(args.fx_name)) end
  local _, name = reaper.TakeFX_GetFXName(take, idx)
  return { index = idx, name = name }
end

handlers.take_fx_get_params = function(args)
  local take = resolve_take(args, false)
  local fx_idx = math.floor(tonumber(args.fx))
  local num = reaper.TakeFX_GetNumParams(take, fx_idx)
  local search = args.search and args.search:lower() or nil
  local list = json.array({})
  for p = 0, num - 1 do
    local _, pname = reaper.TakeFX_GetParamName(take, fx_idx, p)
    if not search or pname:lower():find(search, 1, true) then
      local vn = reaper.TakeFX_GetParamNormalized(take, fx_idx, p)
      local _, fmt = reaper.TakeFX_GetFormattedParamValue(take, fx_idx, p)
      list[#list + 1] = { index = p, name = pname, value_norm = vn, formatted = fmt }
    end
  end
  return { fx_index = fx_idx, total_params = num, params = list }
end

handlers.take_fx_set_params = function(args)
  local take = resolve_take(args, false)
  local fx_idx = math.floor(tonumber(args.fx))
  local num = reaper.TakeFX_GetNumParams(take, fx_idx)
  local results = json.array({})
  for _, p in ipairs(args.params or {}) do
    local pidx = nil
    if type(p.param) == "number" then pidx = math.floor(p.param)
    elseif type(p.index) == "number" then pidx = math.floor(p.index)
    else
      local q = tostring(p.param or p.name):lower()
      for i = 0, num - 1 do
        local _, pn = reaper.TakeFX_GetParamName(take, fx_idx, i)
        if pn:lower() == q or pn:lower():find(q, 1, true) then pidx = i; break end
      end
    end
    if not pidx then error("TakeFX parameter not found") end
    local ok = reaper.TakeFX_SetParamNormalized(take, fx_idx, pidx, tonumber(p.value_norm))
    local _, fmt = reaper.TakeFX_GetFormattedParamValue(take, fx_idx, pidx)
    results[#results + 1] = { index = pidx, ok = ok, formatted = fmt }
  end
  return { results = results }
end

handlers.take_fx_delete = function(args)
  local take = resolve_take(args, false)
  local ok = reaper.TakeFX_Delete(take, math.floor(tonumber(args.fx)))
  return { deleted = ok }
end

-- 6. Automation Envelopes ----------------------------------------------------

local function resolve_envelope(args, create_if_missing)
  local tr = resolve_track(args.track, true)
  if args.fx ~= nil and args.param ~= nil then
    local _, slot = resolve_fx_index(tr, args.fx, args.rec_fx and true or false)
    local pidx = resolve_param_index(tr, slot, args.param)
    local env = reaper.GetFXEnvelope(tr, slot, pidx, create_if_missing and true or false)
    if not env then error("FX envelope not found (set create=true to create it)") end
    return env, tr
  elseif args.envelope_name then
    local env = reaper.GetTrackEnvelopeByName(tr, tostring(args.envelope_name))
    if not env and create_if_missing then
      -- Show common track envelopes by command if needed
      error("Track envelope not visible/found: " .. tostring(args.envelope_name))
    end
    if not env then error("Track envelope not found: " .. tostring(args.envelope_name)) end
    return env, tr
  elseif args.envelope_index ~= nil then
    local env = reaper.GetTrackEnvelope(tr, math.floor(tonumber(args.envelope_index)))
    if not env then error("Envelope index out of range") end
    return env, tr
  end
  error("Specify either (fx + param), envelope_name, or envelope_index")
end

handlers.envelope_list = function(args)
  local tr = resolve_track(args.track, true)
  local cnt = reaper.CountTrackEnvelopes(tr)
  local list = json.array({})
  for i = 0, cnt - 1 do
    local env = reaper.GetTrackEnvelope(tr, i)
    local _, name = reaper.GetEnvelopeName(env)
    list[#list + 1] = {
      index = i,
      name = name,
      point_count = reaper.CountEnvelopePoints(env),
      scaling_mode = reaper.GetEnvelopeScalingMode(env),
    }
  end
  return { count = cnt, envelopes = list }
end

handlers.envelope_get_points = function(args)
  local env = resolve_envelope(args, false)
  local _, name = reaper.GetEnvelopeName(env)
  local scale_mode = reaper.GetEnvelopeScalingMode(env)
  local cnt = reaper.CountEnvelopePoints(env)
  local pts = json.array({})
  for i = 0, cnt - 1 do
    local ok, time, value, shape, tension, selected = reaper.GetEnvelopePoint(env, i)
    if ok then
      pts[#pts + 1] = {
        index = i,
        time_sec = time,
        time_beats = reaper.TimeMap2_timeToQN(0, time),
        value = reaper.ScaleFromEnvelopeMode(scale_mode, value),
        raw_value = value,
        shape = shape,
        tension = tension,
        selected = selected,
      }
    end
  end
  return { name = name, point_count = cnt, points = pts }
end

handlers.envelope_set_points = function(args)
  local env = resolve_envelope(args, true)
  local scale_mode = reaper.GetEnvelopeScalingMode(env)
  if args.clear_existing then
    reaper.DeleteEnvelopePointRange(env, -1e9, 1e9)
  end
  local inserted = 0
  for _, pt in ipairs(args.points or {}) do
    local t = resolve_time_sec(pt, "time_sec", "time_beats", 0)
    local val = reaper.ScaleToEnvelopeMode(scale_mode, tonumber(pt.value))
    local shape = math.floor(tonumber(pt.shape or 0))
    local tension = tonumber(pt.tension or 0)
    local sel = pt.selected and true or false
    if reaper.InsertEnvelopePoint(env, t, val, shape, tension, sel, true) then
      inserted = inserted + 1
    end
  end
  reaper.Envelope_SortPoints(env)
  reaper.TrackList_AdjustWindows(false)
  return { inserted = inserted, total_points = reaper.CountEnvelopePoints(env) }
end

handlers.envelope_clear = function(args)
  local env = resolve_envelope(args, false)
  local start_s = resolve_time_sec(args, "start_sec", "start_beats", -1e9)
  local end_s = resolve_time_sec(args, "end_sec", "end_beats", 1e9)
  reaper.DeleteEnvelopePointRange(env, start_s, end_s)
  reaper.Envelope_SortPoints(env)
  return { cleared = true, remaining_points = reaper.CountEnvelopePoints(env) }
end

-- 7. Batch & Raw Lua Escape Hatch --------------------------------------------

handlers.eval_lua = function(args)
  local code = args.code
  if type(code) ~= "string" or #code == 0 then error("Missing 'code'") end
  local fn, compile_err = load(code, "reaper_agent_eval", "t")
  if not fn then
    -- Try wrapping as a return expression
    fn, compile_err = load("return " .. code, "reaper_agent_eval", "t")
  end
  if not fn then
    error("Lua compile error: " .. tostring(compile_err))
  end
  local result = fn(args.args)
  return { result = result }
end

local READ_ONLY_CMDS = {
  ping = true,
  project_get_info = true,
  project_list_tempo_markers = true,
  marker_list = true,
  track_list = true,
  track_get = true,
  track_list_sends = true,
  item_list = true,
  item_get = true,
  midi_get_events = true,
  fx_enum_installed = true,
  fx_list = true,
  fx_get_params = true,
  fx_get_named_config_parm = true,
  fx_get_preset = true,
  fx_get_chunk = true,
  take_fx_list = true,
  take_fx_get_params = true,
  envelope_list = true,
  envelope_get_points = true,
  bridge_ui_toggle = true,
}

handlers.batch = function(args)
  local commands = args.commands
  if type(commands) ~= "table" then error("Missing 'commands' array") end
  local results = json.array({})
  for idx, step in ipairs(commands) do
    local cmd = step.cmd
    local fn = handlers[cmd]
    if not fn then
      error(string.format("Batch step %d: unknown command '%s'", idx, tostring(cmd)))
    end
    local ok, res = xpcall(function() return fn(step.args or {}) end, debug.traceback)
    if not ok then
      if args.continue_on_error then
        results[#results + 1] = { index = idx, cmd = cmd, ok = false, error = tostring(res) }
      else
        error(string.format("Batch step %d (%s) failed: %s", idx, tostring(cmd), tostring(res)))
      end
    else
      results[#results + 1] = { index = idx, cmd = cmd, ok = true, data = res }
    end
  end
  return { count = #results, results = results }
end

-------------------------------------------------------------------------------
-- Request Execution & IPC Loop
-------------------------------------------------------------------------------

local function execute_request(req)
  local cmd = req.cmd
  local fn = handlers[cmd]
  if not fn then
    return { id = req.id, ok = false, error = "Unknown command: " .. tostring(cmd) }
  end

  local t0 = reaper.time_precise()
  local is_mutating = not READ_ONLY_CMDS[cmd]
  local undo_label = req.undo_label or ("ReaperAgent: " .. tostring(cmd))

  if is_mutating then
    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)
  end

  local ok, result = xpcall(function()
    return fn(req.args or {})
  end, debug.traceback)

  if is_mutating then
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock(undo_label, -1)
    reaper.UpdateArrange()
  end

  local elapsed_ms = (reaper.time_precise() - t0) * 1000.0
  log_command(cmd, ok, elapsed_ms, ok and nil or tostring(result))

  if ok then
    return { id = req.id, ok = true, data = result, elapsed_ms = elapsed_ms }
  else
    return { id = req.id, ok = false, error = tostring(result), elapsed_ms = elapsed_ms }
  end
end

local last_heartbeat = 0.0

local function update_heartbeat()
  local now = reaper.time_precise()
  if now - last_heartbeat < 1.0 then return end
  last_heartbeat = now
  local _, proj_fn = reaper.EnumProjects(-1)
  local payload = json.encode({
    alive = true,
    timestamp = os.time(),
    uptime_sec = now - START_TIME,
    bridge_version = BRIDGE_VERSION,
    reaper_version = reaper.GetAppVersion(),
    project_name = reaper.GetProjectName(0),
    project_path = proj_fn,
    track_count = reaper.CountTracks(0),
    instance_token = INSTANCE_TOKEN,
  })
  write_file_atomic(HEARTBEAT_FILE, payload, true)
end

local function process_queue()
  -- CRITICAL: Passing -1 resets REAPER's EnumerateFiles directory cache!
  reaper.EnumerateFiles(REQUESTS_DIR, -1)
  local files = {}
  local i = 0
  while true do
    local fn = reaper.EnumerateFiles(REQUESTS_DIR, i)
    if not fn then break end
    if fn:sub(-5) == ".json" and fn:sub(1, 4) == "req_" then
      files[#files + 1] = fn
    end
    i = i + 1
  end
  if #files == 0 then return end

  table.sort(files)
  local max_per_tick = 8
  for k = 1, math.min(#files, max_per_tick) do
    local fn = files[k]
    local full_path = join_path(REQUESTS_DIR, fn)
    local raw = read_file(full_path)
    os.remove(full_path)
    if raw and #raw > 0 then
      local dec_ok, req = pcall(json.decode, raw)
      local resp
      if not dec_ok or type(req) ~= "table" then
        resp = { id = fn, ok = false, error = "Invalid request JSON: " .. tostring(req) }
      else
        resp = execute_request(req)
      end
      local res_id = (type(req) == "table" and req.id) or fn:gsub("^req_", ""):gsub("%.json$", "")
      local resp_path = join_path(RESPONSES_DIR, "res_" .. tostring(res_id) .. ".json")
      write_file_atomic(resp_path, json.encode(resp), true)
    end
  end
end

-- Optional ReaImGui status window (only shown when ui_visible == "1")
local function render_ui_if_visible()
  if reaper.GetExtState(EXT_SECTION, "ui_visible") ~= "1" then
    UI_CTX = nil
    return
  end
  if not reaper.ImGui_CreateContext then return end
  if not UI_CTX then
    UI_CTX = reaper.ImGui_CreateContext("ReaperAgentBridge Status")
  end
  reaper.ImGui_SetNextWindowSize(UI_CTX, 480, 320, reaper.ImGui_Cond_FirstUseEver())
  local visible, open = reaper.ImGui_Begin(UI_CTX, "ReaperAgentBridge v" .. BRIDGE_VERSION, true)
  if visible then
    reaper.ImGui_Text(UI_CTX, "Status: RUNNING")
    reaper.ImGui_Text(UI_CTX, "REAPER: " .. reaper.GetAppVersion())
    reaper.ImGui_Text(UI_CTX, "IPC Dir: " .. IPC_ROOT)
    reaper.ImGui_Separator(UI_CTX)
    reaper.ImGui_Text(UI_CTX, string.format("Recent Commands (%d):", #COMMAND_LOG))
    if reaper.ImGui_BeginChild(UI_CTX, "cmd_log", 0, 0) then
      for idx = #COMMAND_LOG, 1, -1 do
        local entry = COMMAND_LOG[idx]
        local status = entry.ok and "OK" or "ERR"
        reaper.ImGui_Text(UI_CTX, string.format("[%s] %s (%.1fms) - %s", entry.time, entry.cmd, entry.ms, status))
        if entry.err then
          reaper.ImGui_TextWrapped(UI_CTX, "  " .. entry.err)
        end
      end
      reaper.ImGui_EndChild(UI_CTX)
    end
    reaper.ImGui_End(UI_CTX)
  end
  if not open then
    reaper.SetExtState(EXT_SECTION, "ui_visible", "0", false)
    UI_CTX = nil
  end
end

local function main_loop()
  -- Exit cleanly if a newer bridge instance took over
  if reaper.GetExtState(EXT_SECTION, "instance_token") ~= INSTANCE_TOKEN then
    return
  end

  pcall(update_heartbeat)
  pcall(process_queue)
  pcall(render_ui_if_visible)

  reaper.defer(main_loop)
end

-- Write immediate initial heartbeat and start loop
update_heartbeat()
reaper.defer(main_loop)
