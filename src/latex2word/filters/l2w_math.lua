-- Math normalisation for the DOCX writer.
--
-- Observed with Pandoc 3.12 (see docs/pandoc-ast-findings.md): texmath writes
-- aligned/alignedat/split/align rows as <m:eqArr> and emits every alignment
-- marker as a literal "&" text run. A legitimate "\&" produces the very same
-- run, so the two cannot be told apart after conversion. The structural
-- markers are therefore translated BEFORE the writer: each alignment
-- environment becomes an `array` with right/left columns, which texmath writes
-- as an OMML matrix without any literal "&". Row boundaries, cell contents and
-- column alignment are preserved; escaped "\&" is never touched.

local U = require('l2w_util')

local M = {}

-- Environments whose body is split on "&" (cells) and "\\" (rows).
local ALIGN_ENVS = {
  aligned = 'rl', alignedat = 'rl', split = 'rl',
  align = 'rl', ['align*'] = 'rl', alignat = 'rl', ['alignat*'] = 'rl',
  flalign = 'rl', ['flalign*'] = 'rl', eqnarray = 'rcl', ['eqnarray*'] = 'rcl',
}
-- Environments in which a bare "&" is structural and handled by texmath as a matrix.
local TABULAR = {
  array = true, matrix = true, pmatrix = true, bmatrix = true, Bmatrix = true,
  vmatrix = true, Vmatrix = true, smallmatrix = true, cases = true, dcases = true, subarray = true,
}
-- Display wrappers: numbered or not, single or multi row.
local WRAPPERS = {
  equation = { numbered = true, rows = false },
  ['equation*'] = { numbered = false, rows = false },
  displaymath = { numbered = false, rows = false },
  align = { numbered = true, rows = true, align = true },
  ['align*'] = { numbered = false, rows = true, align = true },
  alignat = { numbered = true, rows = true, align = true, arg = true },
  ['alignat*'] = { numbered = false, rows = true, align = true, arg = true },
  flalign = { numbered = true, rows = true, align = true },
  ['flalign*'] = { numbered = false, rows = true, align = true },
  eqnarray = { numbered = true, rows = true, align = true },
  ['eqnarray*'] = { numbered = false, rows = true, align = true },
  gather = { numbered = true, rows = true, name = 'gathered' },
  ['gather*'] = { numbered = false, rows = true, name = 'gathered' },
  multline = { numbered = true, rows = false, name = 'multlined' },
  ['multline*'] = { numbered = false, rows = false, name = 'multlined' },
}
local UNSUPPORTED = { 'intertext', 'shortintertext', 'tag' }

local function trim(s) return (s:gsub('^%s+', ''):gsub('%s+$', '')) end

-- Drop TeX comments (an unescaped % to the end of the line).
local function strip_comments(s)
  if not s:find('%', 1, true) then return s end
  local out, i, n = {}, 1, #s
  while i <= n do
    local c = s:sub(i, i)
    if c == '\\' then
      out[#out + 1] = s:sub(i, i + 1)
      i = i + 2
    elseif c == '%' then
      local eol = s:find('\n', i, true)
      if not eol then break end
      i = eol
    else
      out[#out + 1] = c
      i = i + 1
    end
  end
  return table.concat(out)
end

-- Position range of the \end{name} matching the \begin{name} whose body starts at `from`.
local function find_env_end(s, from, name)
  local depth, pos = 1, from
  local open = '\\begin{' .. name .. '}'
  local close = '\\end{' .. name .. '}'
  while true do
    local o = s:find(open, pos, true)
    local c = s:find(close, pos, true)
    if not c then return nil end
    if o and o < c then
      depth = depth + 1
      pos = o + #open
    else
      depth = depth - 1
      if depth == 0 then return c, c + #close - 1 end
      pos = c + #close
    end
  end
end

-- Split `s` at top-level row ("\\") or cell ("&") separators: not inside braces
-- and not inside a nested \begin ... \end.
--
-- A row break may carry a spacing argument ("\\[2pt]", "\\*[2pt]"). amsmath
-- looks for it with \new@ifnextchar, which does NOT skip spaces: in align,
-- aligned, gather, split, ... "\\ [x,y]" ends the row and starts the next one
-- with the content "[x,y]". Only the kernel environments (array, eqnarray) look
-- past spaces. `kernel_rows` selects that behaviour; by default the bracket is
-- consumed only when it follows the break immediately.
local function top_split(s, mode, kernel_rows)
  local parts, start, i, n = {}, 1, 1, #s
  local brace, env = 0, 0
  while i <= n do
    local c = s:sub(i, i)
    if c == '\\' then
      local nx = s:sub(i + 1, i + 1)
      if nx == '\\' then
        if mode == 'row' and brace == 0 and env == 0 then
          parts[#parts + 1] = s:sub(start, i - 1)
          i = i + 2
          if s:sub(i, i) == '*' then i = i + 1 end
          local j = i
          if kernel_rows then
            while s:sub(j, j):match('%s') do j = j + 1 end
          end
          if s:sub(j, j) == '[' then
            local close = s:find(']', j, true)
            if close then i = close + 1 end
          end
          start = i
        else
          i = i + 2
        end
      elseif s:sub(i, i + 5) == '\\begin' and s:find('^%s*{', i + 6) then
        env = env + 1
        i = i + 6
      elseif s:sub(i, i + 3) == '\\end' and s:find('^%s*{', i + 4) then
        env = math.max(0, env - 1)
        i = i + 4
      else
        i = i + 2
      end
    elseif c == '{' then
      brace = brace + 1
      i = i + 1
    elseif c == '}' then
      brace = math.max(0, brace - 1)
      i = i + 1
    elseif c == '&' and mode == 'cell' and brace == 0 and env == 0 then
      parts[#parts + 1] = s:sub(start, i - 1)
      start = i + 1
      i = i + 1
    else
      i = i + 1
    end
  end
  parts[#parts + 1] = s:sub(start)
  return parts
end

-- True for the LaTeX-kernel environments whose \\ skips spaces before "[".
local function kernel_rows_of(name)
  return name == 'array' or name == 'eqnarray' or name == 'eqnarray*'
end

local function split_rows(body, kernel_rows)
  local rows = top_split(body, 'row', kernel_rows)
  if #rows > 0 and trim(rows[#rows]) == '' then table.remove(rows) end
  return rows
end

-- A row whose text starts with "[" would be read as the spacing argument of the
-- preceding "\\" by the writer (texmath applies kernel array rules). An empty
-- group in front keeps the bracket as content.
local function guard_row_start(text)
  if text:sub(1, 1) == '[' then return '{}' .. text end
  return text
end

-- Column specification: `pattern` repeated up to `count` columns.
local function column_spec(pattern, count)
  local spec = {}
  for i = 1, count do
    local k = (i - 1) % #pattern + 1
    spec[#spec + 1] = pattern:sub(k, k)
  end
  return table.concat(spec)
end

-- texmath turns an `array` whose columns alternate right/left exactly into an
-- <m:eqArr> whose alignment markers are literal "&" text runs.
local function is_alternating(letters)
  if #letters < 2 then return false end
  for i = 1, #letters do
    if letters:sub(i, i) ~= ((i % 2 == 1) and 'r' or 'l') then return false end
  end
  return true
end

-- Read a braced group starting at `pos` (after optional whitespace); returns its content and the next position.
local function read_braced(s, pos)
  local i = pos
  while s:sub(i, i):match('%s') do i = i + 1 end
  if s:sub(i, i) ~= '{' then return nil end
  local depth, j = 0, i
  while j <= #s do
    local c = s:sub(j, j)
    if c == '\\' then
      j = j + 2
    else
      if c == '{' then
        depth = depth + 1
      elseif c == '}' then
        depth = depth - 1
        if depth == 0 then return s:sub(i + 1, j - 1), j + 1 end
      end
      j = j + 1
    end
  end
  return nil
end

-- Decide whether the environment `name` whose \begin ends at `e` must be rewritten.
-- Returns true, the body start and (for user-written arrays) the column specification.
local function rewrite_target(s, e, name)
  if ALIGN_ENVS[name] then
    local body_start = e + 1
    if name == 'alignedat' or name == 'alignat' or name == 'alignat*' then
      local _, arg_end = s:find('^%s*{[^{}]*}', body_start)
      if arg_end then body_start = arg_end + 1 end
    end
    return true, body_start, nil
  end
  if name == 'array' then
    local spec, after = read_braced(s, e + 1)
    if spec then
      local letters = spec:gsub('%b{}', ''):gsub('[^rlc]', '')
      if is_alternating(letters) then return true, after, spec, #letters end
    end
  end
  return false
end

-- Replace every alignment environment (and every right/left array) in `s` by an
-- `array` that texmath writes as a plain matrix. Returns the new text and the
-- number of environments rewritten.
local function normalize_inner(s)
  local out, pos, count = {}, 1, 0
  while true do
    local b, e, name = s:find('\\begin{([%a]+%*?)}', pos)
    local ok, body_start, user_spec, user_columns
    while b do
      ok, body_start, user_spec, user_columns = rewrite_target(s, e, name)
      if ok then break end
      out[#out + 1] = s:sub(pos, e) -- leave this environment verbatim; its inner text is scanned next
      pos = e + 1
      b, e, name = s:find('\\begin{([%a]+%*?)}', pos)
    end
    if not b then
      out[#out + 1] = s:sub(pos)
      break
    end
    local close_start, close_end = find_env_end(s, e + 1, name)
    if not close_start then
      out[#out + 1] = s:sub(pos)
      break
    end
    out[#out + 1] = s:sub(pos, b - 1)
    local body, n_inner = normalize_inner(s:sub(body_start, close_start - 1))
    local rows, columns = {}, user_columns or 1
    for _, row in ipairs(split_rows(body, kernel_rows_of(name))) do
      local cells = top_split(row, 'cell')
      for k, cell in ipairs(cells) do cells[k] = trim(cell) end
      cells[1] = guard_row_start(cells[1])
      columns = math.max(columns, #cells)
      rows[#rows + 1] = cells
    end
    local spec, padded = user_spec, user_spec ~= nil
    if not spec then
      local pattern = ALIGN_ENVS[name]
      if columns == 1 then pattern = 'r' end
      spec = column_spec(pattern, columns)
      padded = #spec >= 2 and spec:match('^[rl]+$') and is_alternating(spec)
    end
    -- A trailing empty centred column makes the array an ordinary OMML matrix while
    -- the real cells keep their right/left alignment.
    if padded then
      spec = spec .. 'c'
      for _, cells in ipairs(rows) do
        while #cells < columns + 1 do cells[#cells + 1] = '' end
      end
    end
    local rendered = {}
    for k, cells in ipairs(rows) do rendered[k] = table.concat(cells, ' & ') end
    out[#out + 1] = '\\begin{array}{' .. spec .. '} '
      .. table.concat(rendered, ' \\\\ ') .. ' \\end{array}'
    count = count + 1 + n_inner
    pos = close_end + 1
  end
  return table.concat(out), count
end

-- Verify no structural "&" is left outside a tabular environment; count "\&".
local function audit_ampersands(s)
  local stack, i, n = {}, 1, #s
  local literal, stray = 0, 0
  while i <= n do
    local c = s:sub(i, i)
    if c == '\\' then
      local nx = s:sub(i + 1, i + 1)
      if nx == '&' then
        literal = literal + 1
        i = i + 2
      else
        local name = s:match('^\\begin%s*{([^{}]*)}', i)
        if name then
          stack[#stack + 1] = name
          i = i + #s:match('^\\begin%s*{[^{}]*}', i)
        else
          local ename = s:match('^\\end%s*{([^{}]*)}', i)
          if ename then
            if #stack > 0 then table.remove(stack) end
            i = i + #s:match('^\\end%s*{[^{}]*}', i)
          else
            i = i + 2
          end
        end
      end
    elseif c == '&' then
      local top = stack[#stack]
      if not (top and TABULAR[top]) then stray = stray + 1 end
      i = i + 1
    else
      i = i + 1
    end
  end
  return literal, stray
end

-- Remove \label{..}, \nonumber and \notag from `text`; returns text, labels, nonumber.
local function strip_markers(text)
  local labels, nonumber = {}, false
  -- %b{} matches a balanced group, so a label such as eq:a_{1} is removed whole
  text = text:gsub('\\label%s*(%b{})', function(group)
    labels[#labels + 1] = trim(group:sub(2, -2))
    return ''
  end)
  local function drop(cmd)
    local found = false
    text = text:gsub('\\' .. cmd .. '%f[^%a]', function()
      found = true
      return ''
    end)
    return found
  end
  if drop('nonumber') then nonumber = true end
  if drop('notag') then nonumber = true end
  return text, labels, nonumber
end

local function unsupported_commands(text)
  local found = {}
  for _, cmd in ipairs(UNSUPPORTED) do
    if text:find('\\' .. cmd .. '%f[^%a]') then found[#found + 1] = '\\' .. cmd end
  end
  return found
end

-- Analyse one Math string.
--   result.tex        text to hand to the writer
--   result.numbered   true when the display carries an equation number
--   result.labels     labels attached to the numbered equation
--   result.errors     list of {code, message}
--   result.literal    number of escaped "\&" left in `tex`
--   result.aligned    alignment environments translated
function M.analyze(text, display)
  local result = { errors = {}, labels = {}, numbered = false, literal = 0, aligned = 0 }
  local function fail(code, message) result.errors[#result.errors + 1] = { code = code, message = message } end

  text = trim(strip_comments(text))
  for _, cmd in ipairs(unsupported_commands(text)) do
    fail('L2W-E-MATH-003', cmd .. ' is not supported in equations; split the equation or number it with \\label')
  end

  local wrapper_name, wrapper
  local body = text
  if display then
    local name = text:match('^\\begin%s*{([%a]+%*?)}')
    if name and WRAPPERS[name] then
      local head = text:match('^\\begin%s*{[%a]+%*?}')
      local close_start, close_end = find_env_end(text, #head + 1, name)
      if close_start and trim(text:sub(close_end + 1)) == '' then
        wrapper_name, wrapper = name, WRAPPERS[name]
        body = text:sub(#head + 1, close_start - 1)
        if wrapper.arg then
          local _, arg_end = body:find('^%s*{[^{}]*}')
          if arg_end then body = body:sub(arg_end + 1) end
        end
      end
    end
  end

  local numbered_rows = 0
  if wrapper and wrapper.rows then
    -- Multi-row environments: row markers decide which rows are numbered.
    local rows = split_rows(body, kernel_rows_of(wrapper_name))
    local kept, row_labels = {}, {}
    for _, row in ipairs(rows) do
      local stripped, labels, nonumber = strip_markers(row)
      local counted = wrapper.numbered and not nonumber and trim(stripped) ~= ''
      if counted then
        numbered_rows = numbered_rows + 1
        for _, label in ipairs(labels) do row_labels[#row_labels + 1] = label end
      elseif #labels > 0 then
        fail('L2W-E-LABEL-002', 'label "' .. labels[1] .. '" is attached to an unnumbered equation row')
      end
      kept[#kept + 1] = kernel_rows_of(wrapper_name) and stripped or guard_row_start(trim(stripped))
    end
    if numbered_rows > 1 then
      fail('L2W-E-MATH-003', 'the numbered multi-row environment "' .. wrapper_name
        .. '" has ' .. numbered_rows .. ' numbered rows; mark all but one with \\nonumber or use one equation per row')
    end
    result.labels = row_labels
    result.numbered = numbered_rows == 1
    body = table.concat(kept, ' \\\\ ')
    if wrapper.align then
      body = '\\begin{' .. (wrapper_name:gsub('%*$', '')) .. '} ' .. body .. ' \\end{' .. (wrapper_name:gsub('%*$', '')) .. '}'
    else
      body = '\\begin{' .. wrapper.name .. '} ' .. body .. ' \\end{' .. wrapper.name .. '}'
    end
  else
    local stripped, labels, nonumber = strip_markers(body)
    body = stripped
    if wrapper then
      result.numbered = wrapper.numbered and not nonumber
      if wrapper.name then
        body = '\\begin{' .. wrapper.name .. '} ' .. trim(body) .. ' \\end{' .. wrapper.name .. '}'
      end
    end
    if #labels > 0 then
      if result.numbered then
        result.labels = labels
      else
        fail('L2W-E-LABEL-002', 'label "' .. labels[1] .. '" is attached to an unnumbered equation')
      end
    end
  end

  local normalized, count = normalize_inner(trim(body))
  result.aligned = count
  local literal, stray = audit_ampersands(normalized)
  result.literal = literal
  if stray > 0 then
    fail('L2W-E-MATH-001', stray .. ' alignment marker(s) "&" outside an array-like environment would appear '
      .. 'as literal text in the Word equation; the environment cannot be normalised safely')
  end
  result.tex = trim(normalized)
  return result
end

return M
