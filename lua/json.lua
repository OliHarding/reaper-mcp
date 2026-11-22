-- Pure Lua JSON encoder/decoder for REAPER ReaScript (Lua 5.3/5.4)
-- Supports explicit empty arrays via json.array({}), safe NaN/Inf -> null,
-- full string escaping, and UTF-16 surrogate pair decoding.

local json = { _version = "1.0.0" }

local array_mt = { __is_json_array = true }
json.array_mt = array_mt

function json.array(tbl)
  tbl = tbl or {}
  return setmetatable(tbl, array_mt)
end

-------------------------------------------------------------------------------
-- Encode
-------------------------------------------------------------------------------

local escape_char_map = {
  ["\\"] = "\\\\",
  ['"']  = '\\"',
  ["\b"] = "\\b",
  ["\f"] = "\\f",
  ["\n"] = "\\n",
  ["\r"] = "\\r",
  ["\t"] = "\\t",
}

local function escape_char(c)
  return escape_char_map[c] or string.format("\\u%04x", c:byte())
end

local function encode_string(val)
  return '"' .. val:gsub('[%z\1-\31\\"]', escape_char) .. '"'
end

local function is_array(tbl)
  local mt = getmetatable(tbl)
  if mt and mt.__is_json_array then
    return true
  end
  local max_idx = 0
  local count = 0
  for k, _ in pairs(tbl) do
    if type(k) ~= "number" or k <= 0 or math.floor(k) ~= k then
      return false
    end
    if k > max_idx then max_idx = k end
    count = count + 1
  end
  if count == 0 then
    return false -- empty untagged table defaults to {}
  end
  return max_idx == count
end

local encode_val

local function encode_table(val, stack)
  if stack[val] then
    error("circular reference in JSON encoding")
  end
  stack[val] = true

  local res = {}
  if is_array(val) then
    for i = 1, #val do
      res[i] = encode_val(val[i], stack)
    end
    stack[val] = nil
    return "[" .. table.concat(res, ",") .. "]"
  else
    local i = 1
    for k, v in pairs(val) do
      if type(k) == "string" or type(k) == "number" then
        res[i] = encode_string(tostring(k)) .. ":" .. encode_val(v, stack)
        i = i + 1
      end
    end
    stack[val] = nil
    return "{" .. table.concat(res, ",") .. "}"
  end
end

encode_val = function(val, stack)
  local t = type(val)
  if val == nil then
    return "null"
  elseif t == "boolean" then
    return val and "true" or "false"
  elseif t == "number" then
    if val ~= val or val == math.huge or val == -math.huge then
      return "null"
    end
    if math.type and math.type(val) == "integer" then
      return tostring(val)
    end
    return string.format("%.14g", val)
  elseif t == "string" then
    return encode_string(val)
  elseif t == "table" then
    return encode_table(val, stack)
  else
    return encode_string(tostring(val))
  end
end

function json.encode(val)
  return encode_val(val, {})
end

-------------------------------------------------------------------------------
-- Decode
-------------------------------------------------------------------------------

local function create_set(...)
  local res = {}
  for i = 1, select("#", ...) do
    res[select(i, ...)] = true
  end
  return res
end

local space_chars  = create_set(" ", "\t", "\r", "\n")
local delim_chars  = create_set(" ", "\t", "\r", "\n", "]", "}", ",")
local escape_chars = create_set("\\", "/", '"', "b", "f", "n", "r", "t", "u")
local literals     = create_set("true", "false", "null")

local literal_map = {
  ["true"]  = true,
  ["false"] = false,
  ["null"]  = nil,
}

local escape_char_map_inv = {
  ["\\"] = "\\",
  ["/"]  = "/",
  ['"']  = '"',
  ["b"]  = "\b",
  ["f"]  = "\f",
  ["n"]  = "\n",
  ["r"]  = "\r",
  ["t"]  = "\t",
}

local function next_char(str, idx, set, negate)
  for i = idx, #str do
    if set[str:sub(i, i)] ~= negate then
      return i
    end
  end
  return #str + 1
end

local function decode_error(str, idx, msg)
  local line_count = 1
  local col_count = 1
  for i = 1, idx - 1 do
    col_count = col_count + 1
    if str:sub(i, i) == "\n" then
      line_count = line_count + 1
      col_count = 1
    end
  end
  error(string.format("%s at line %d col %d", msg, line_count, col_count))
end

local function codepoint_to_utf8(n)
  local f = math.floor
  if n <= 0x7f then
    return string.char(n)
  elseif n <= 0x7ff then
    return string.char(f(n / 64) + 192, n % 64 + 128)
  elseif n <= 0xffff then
    return string.char(f(n / 4096) + 224, f(n % 4096 / 64) + 128, n % 64 + 128)
  elseif n <= 0x10ffff then
    return string.char(
      f(n / 262144) + 240,
      f(n % 262144 / 4096) + 128,
      f(n % 4096 / 64) + 128,
      n % 64 + 128
    )
  end
  error(string.format("invalid unicode codepoint '%x'", n))
end

local function parse_unicode_escape(s)
  local n1 = tonumber(s:sub(1, 4), 16)
  local n2 = tonumber(s:sub(7, 10), 16)
  if n2 then
    return codepoint_to_utf8((n1 - 0xd800) * 0x400 + (n2 - 0xdc00) + 0x10000)
  else
    return codepoint_to_utf8(n1)
  end
end

local function parse_string(str, i)
  local res = {}
  local j = i + 1
  local k = j

  while j <= #str do
    local x = str:byte(j)
    if x < 32 then
      decode_error(str, j, "control character in string")
    elseif x == 92 then -- '\'
      res[#res + 1] = str:sub(k, j - 1)
      j = j + 1
      local c = str:sub(j, j)
      if c == "u" then
        local hex = str:match("^[dD][89aAbB]%x%x\\u%x%x%x%x", j + 1)
                 or str:match("^%x%x%x%x", j + 1)
                 or decode_error(str, j - 1, "invalid unicode escape in string")
        res[#res + 1] = parse_unicode_escape(hex)
        j = j + #hex
      else
        if not escape_chars[c] then
          decode_error(str, j - 1, "invalid escape char '" .. c .. "' in string")
        end
        res[#res + 1] = escape_char_map_inv[c]
      end
      k = j + 1
    elseif x == 34 then -- '"'
      res[#res + 1] = str:sub(k, j - 1)
      return table.concat(res), j + 1
    end
    j = j + 1
  end
  decode_error(str, i, "expected closing quote for string")
end

local function parse_number(str, i)
  local x = next_char(str, i, delim_chars)
  local s = str:sub(i, x - 1)
  local n = tonumber(s)
  if not n then
    decode_error(str, i, "invalid number '" .. s .. "'")
  end
  return n, x
end

local function parse_literal(str, i)
  local x = next_char(str, i, delim_chars)
  local word = str:sub(i, x - 1)
  if not literals[word] then
    decode_error(str, i, "invalid literal '" .. word .. "'")
  end
  return literal_map[word], x
end

local parse

local function parse_array(str, i)
  local res = {}
  local n = 1
  i = i + 1
  while true do
    local x
    i = next_char(str, i, space_chars, true)
    if str:sub(i, i) == "]" then
      i = i + 1
      break
    end
    x, i = parse(str, i)
    res[n] = x
    n = n + 1
    i = next_char(str, i, space_chars, true)
    local chr = str:sub(i, i)
    i = i + 1
    if chr == "]" then break end
    if chr ~= "," then decode_error(str, i, "expected ']' or ','") end
  end
  return res, i
end

local function parse_object(str, i)
  local res = {}
  i = i + 1
  while true do
    local key, val
    i = next_char(str, i, space_chars, true)
    if str:sub(i, i) == "}" then
      i = i + 1
      break
    end
    if str:sub(i, i) ~= '"' then
      decode_error(str, i, "expected string for key")
    end
    key, i = parse(str, i)
    i = next_char(str, i, space_chars, true)
    if str:sub(i, i) ~= ":" then
      decode_error(str, i, "expected ':' after key")
    end
    i = next_char(str, i + 1, space_chars, true)
    val, i = parse(str, i)
    res[key] = val
    i = next_char(str, i, space_chars, true)
    local chr = str:sub(i, i)
    i = i + 1
    if chr == "}" then break end
    if chr ~= "," then decode_error(str, i, "expected '}' or ','") end
  end
  return res, i
end

local char_func_map = {
  ['"'] = parse_string,
  ["0"] = parse_number,
  ["1"] = parse_number,
  ["2"] = parse_number,
  ["3"] = parse_number,
  ["4"] = parse_number,
  ["5"] = parse_number,
  ["6"] = parse_number,
  ["7"] = parse_number,
  ["8"] = parse_number,
  ["9"] = parse_number,
  ["-"] = parse_number,
  ["t"] = parse_literal,
  ["f"] = parse_literal,
  ["n"] = parse_literal,
  ["["] = parse_array,
  ["{"] = parse_object,
}

parse = function(str, idx)
  local chr = str:sub(idx, idx)
  local f = char_func_map[chr]
  if f then
    return f(str, idx)
  end
  decode_error(str, idx, "unexpected character '" .. chr .. "'")
end

function json.decode(str)
  if type(str) ~= "string" then
    error("expected argument of type string, got " .. type(str))
  end
  -- Strip UTF-8 BOM if present
  if str:sub(1, 3) == "\239\187\191" then
    str = str:sub(4)
  end
  local idx = next_char(str, 1, space_chars, true)
  local res, next_idx = parse(str, idx)
  next_idx = next_char(str, next_idx, space_chars, true)
  if next_idx <= #str then
    decode_error(str, next_idx, "trailing garbage")
  end
  return res
end

return json
