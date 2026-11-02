-- Shared state and helpers of the latex2word Pandoc filter set.
--
-- The filter set is one Lua file (l2w_semantic.lua) that returns a list of
-- passes, so every pass shares this module's state. Python reads the JSON
-- report written at the end; the filters never abort the Pandoc run
-- themselves, so one run reports every problem at once.

local M = {}

M.config = {}
M.diags = {}

-- ---------------------------------------------------------------- diagnostics

function M.diag(code, severity, message, detail)
  M.diags[#M.diags + 1] = { code = code, severity = severity, message = message, detail = detail }
end

-- ---------------------------------------------------------------------- JSON

function M.read_json(path)
  local handle, err = io.open(path, 'rb')
  if not handle then error('cannot open ' .. tostring(path) .. ': ' .. tostring(err)) end
  local text = handle:read('a')
  handle:close()
  return pandoc.json.decode(text, false)
end

function M.write_json(path, value)
  local handle, err = io.open(path, 'wb')
  if not handle then error('cannot write ' .. tostring(path) .. ': ' .. tostring(err)) end
  handle:write(pandoc.json.encode(value))
  handle:close()
end

-- ------------------------------------------------------------------- numerals

local ROMAN = {
  { 1000, 'M' }, { 900, 'CM' }, { 500, 'D' }, { 400, 'CD' }, { 100, 'C' }, { 90, 'XC' },
  { 50, 'L' }, { 40, 'XL' }, { 10, 'X' }, { 9, 'IX' }, { 5, 'V' }, { 4, 'IV' }, { 1, 'I' },
}

-- Returns the numeral text, or nil plus a reason.
function M.numeral(n, style)
  n = math.tointeger(n) or n
  if n == 0 then return '0' end
  if n < 1 then return nil, 'counter value ' .. tostring(n) .. ' is not positive' end
  if style == 'arabic' then return tostring(n) end
  if style == 'Roman' or style == 'roman' then
    if n > 3999 then return nil, n .. ' exceeds the Roman numeral range' end
    local out, rest = {}, n
    for _, pair in ipairs(ROMAN) do
      while rest >= pair[1] do
        out[#out + 1] = pair[2]
        rest = rest - pair[1]
      end
    end
    local text = table.concat(out)
    if style == 'roman' then text = text:lower() end
    return text
  end
  if style == 'Alpha' or style == 'alpha' then
    if n > 26 then return nil, n .. ' exceeds the alphabetic range' end
    local letter = string.char(64 + n)
    if style == 'alpha' then letter = letter:lower() end
    return letter
  end
  return nil, 'unknown numeral style ' .. tostring(style)
end

local FORCED = { arabic = true, Roman = true, roman = true, Alpha = true, alpha = true }

-- Substitute {field} occurrences. `values` maps field names to text; forced
-- numeral fields ({Roman} ...) are rendered from values.count.
function M.render(template, values, count)
  return (template:gsub('{([^{}]*)}', function(name)
    if FORCED[name] then
      local text, why = M.numeral(count, name)
      if not text then
        M.diag('L2W-E-NUM-001', 'error', 'cannot render number in format "' .. template .. '"', why)
        return '?'
      end
      return text
    end
    local value = values[name]
    if value == nil then return '?' end
    return value
  end))
end

-- ---------------------------------------------------------------- inline text

-- Convert plain text into Str / Space / LineBreak inlines. A newline becomes a
-- LineBreak, a space becomes a Space (non-breaking spaces stay inside Str).
function M.text_inlines(text)
  local out = pandoc.Inlines({})
  local word = {}
  local function flush()
    if #word > 0 then
      out:insert(pandoc.Str(table.concat(word)))
      word = {}
    end
  end
  for char in text:gmatch(utf8.charpattern) do
    if char == ' ' then
      flush()
      out:insert(pandoc.Space())
    elseif char == '\n' then
      flush()
      out:insert(pandoc.LineBreak())
    else
      word[#word + 1] = char
    end
  end
  flush()
  return out
end

function M.sanitize_id(label)
  local id = label:gsub('[^%w_.:%-]', '_')
  if id == '' then id = 'l2w' end
  return id
end

return M
