-- latex2word semantic filter set (Pandoc 3.12 AST).
--
-- Passes, in order, all sharing one Lua state:
--   0  read the JSON configuration (metadata key l2w-config)
--   1  manual bibliography: <l2wbibliography> divs -> numbered entries
--   0b front matter: keyword environments named by the profile get a paragraph style
--   2  numbering: headings, figures, tables, equations (+ math normalisation)
--   3  cross-references, citations (manual mode), leftover raw TeX
--   4  write the JSON report
--
-- Design facts this relies on are recorded in docs/pandoc-ast-findings.md.

local dir = PANDOC_SCRIPT_FILE and PANDOC_SCRIPT_FILE:match('^(.*)[/\\][^/\\]*$') or '.'
package.path = dir .. '/?.lua;' .. package.path

local U = require('l2w_util')
local MATH = require('l2w_math')

local S = {
  sec = { 0, 0, 0, 0, 0, 0 },
  count = { figures = 0, tables = 0, equations = 0 },
  labels = {}, label_order = {}, dests = {},
  sequences = { figures = {}, tables = {}, equations = {}, sections = {} },
  unnumbered = {},
  refs = {},
  citations = {}, citation_order = {},
  raw = {},
  keywords = {},
  math = { display = 0, inline = 0, aligned = 0, literal = 0 },
  bibliography = { entries = {}, order = {} },
  source_label_set = nil,
  appendix = false, -- set by the <l2wappendix> marker that replaces the appendix command in the staged copy
}
local C -- configuration, set by pass 0

local function stringify(x) return pandoc.utils.stringify(x) end

local function excerpt(text)
  text = text:gsub('%s+', ' ')
  if #text > 80 then text = text:sub(1, 77) .. '...' end
  return text
end

-- ------------------------------------------------------------------ registry

local function is_source_label(id)
  if id == nil or id == '' then return false end
  if S.source_label_set == nil then return true end
  return S.source_label_set[id] == true
end

local function register(label, entry)
  if S.labels[label] then
    U.diag('L2W-E-LABEL-001', 'error', 'duplicate label "' .. label .. '"',
      'first target is a ' .. S.labels[label].kind .. '; also defined on a ' .. entry.kind)
    return false
  end
  entry.label = label
  entry.refs = 0
  S.labels[label] = entry
  S.label_order[#S.label_order + 1] = label
  return true
end

local function make_dest(label)
  local base = U.sanitize_id(label)
  local dest, k = base, 1
  while S.dests[dest] do
    k = k + 1
    dest = base .. '-' .. k
  end
  S.dests[dest] = true
  return dest
end

-- --------------------------------------------------------------- number text

local function numeral_text(n, style)
  local text, why = U.numeral(n, style)
  if not text then
    U.diag('L2W-E-NUM-001', 'error', 'cannot write counter value ' .. tostring(n) .. ' as "' .. tostring(style) .. '"', why)
    return '?'
  end
  return text
end

local function appendix_spec()
  if S.appendix then return C.numbering.sections.appendix end
  return nil
end

local function section_values()
  local values = {}
  for k = 1, 3 do
    local level = C.numbering.sections['level' .. k]
    if k == 1 and appendix_spec() ~= nil then level = appendix_spec() end
    values['n' .. k] = numeral_text(S.sec[k], level and level.numeral or 'arabic')
  end
  return values
end

local function kind_texts(kind, count)
  local spec = C.numbering[kind]
  local values = section_values()
  values.n = numeral_text(count, spec.numeral)
  return {
    display = U.render(spec.format, values, count),
    ref = U.render(spec.ref, values, count),
    reference = U.render(spec.reference, values, count),
  }
end

local function reset_counters(level)
  for _, kind in ipairs({ 'figures', 'tables', 'equations' }) do
    local policy = C.numbering[kind].reset
    if (policy == 'section' and level <= 1) or (policy == 'subsection' and level <= 2) then
      S.count[kind] = 0
    end
  end
end

-- Inlines for "<label><separator>" where the label is a bookmark target.
local function label_inlines(display, separator, dest)
  local label = pandoc.Span(U.text_inlines(display), dest and pandoc.Attr(dest) or pandoc.Attr())
  local out = pandoc.Inlines({ label })
  out:extend(U.text_inlines(separator or ''))
  return out
end

local function prefix_caption(caption_long, prefix)
  local first = caption_long[1]
  if first ~= nil and (first.t == 'Plain' or first.t == 'Para') then
    local content = pandoc.Inlines(prefix)
    content:extend(first.content)
    caption_long[1] = pandoc.Plain(content)
  else
    caption_long:insert(1, pandoc.Plain(prefix))
  end
  return caption_long
end

-- ------------------------------------------------------------------- pass 0

local pass_config = {
  Pandoc = function(doc)
    local path = doc.meta['l2w-config']
    if path == nil then error('latex2word: metadata key l2w-config is required') end
    C = U.read_json(stringify(path))
    U.config = C
    if C.source_labels then
      S.source_label_set = {}
      for _, label in ipairs(C.source_labels) do S.source_label_set[label] = true end
    end
    doc.meta['l2w-config'] = nil
    return doc
  end,
}

-- ------------------------------------------------------------------- pass 0b

-- Environments the profile declares as the keyword list (frontmatter.keywords.environments) become Divs with a
-- custom paragraph style; the style name comes from the configuration, never from this file.
local pass_frontmatter = {
  Div = function(div)
    local fm = C.frontmatter
    if fm == nil or fm.keywords_style == nil then return nil end
    for _, class in ipairs(div.classes) do
      for _, env in ipairs(fm.keyword_environments or {}) do
        if class == env then
          S.keywords[#S.keywords + 1] = stringify(div.content)
          div.attributes['custom-style'] = fm.keywords_style
          return div
        end
      end
    end
    return nil
  end,
}

-- ------------------------------------------------------------------- pass 1

local BIB_PREFIX = 'l2w-bib-'

local function build_manual_bibliography(div)
  local manual = C.bibliography.manual
  local entries = {}
  for _, item in ipairs(div.content) do
    if item.t == 'Div' and item.classes:includes('l2wbibitem') then
      local inner = item.content[1]
      local key = inner and inner.t == 'Div' and inner.identifier:sub(1, #BIB_PREFIX) == BIB_PREFIX
        and inner.identifier:sub(#BIB_PREFIX + 1)
      if key then
        local n = #S.bibliography.order + 1
        local label = U.render(manual.entry_label, { n = tostring(n) }, n)
        local blocks = pandoc.Blocks(inner.content)
        local first = blocks[1]
        local content = pandoc.Inlines(U.text_inlines(label .. ' '))
        if first ~= nil and (first.t == 'Para' or first.t == 'Plain') then
          content:extend(first.content)
          blocks[1] = pandoc.Para(content)
        else
          blocks:insert(1, pandoc.Para(content))
        end
        local dest = 'ref-' .. U.sanitize_id(key)
        if S.bibliography.entries[key] then
          U.diag('L2W-E-BIB-003', 'error', 'duplicate bibliography key "' .. key .. '"')
        else
          S.bibliography.entries[key] = { n = n, dest = dest }
          S.bibliography.order[n] = key
          entries[#entries + 1] = pandoc.Div(blocks, pandoc.Attr(dest, { 'csl-entry' }))
        end
      else
        U.diag('L2W-E-BIB-004', 'error', 'a manual bibliography item has no key; it cannot be cited')
      end
    end
  end
  return pandoc.Div(entries, pandoc.Attr('refs', { 'references', 'csl-bib-body' }))
end

local pass_bibliography = {
  traverse = 'topdown',
  Div = function(div)
    if not div.classes:includes('l2wbibliography') then return nil end
    if C.bibliography.mode ~= 'manual' then
      U.diag('L2W-E-BIB-005', 'error', 'an inline bibliography is present but the bibliography mode is "'
        .. tostring(C.bibliography.mode) .. '"')
      return nil
    end
    local refs = build_manual_bibliography(div)
    local heading = C.bibliography.heading
    if heading and heading ~= '' then
      local title = pandoc.Header(1, U.text_inlines(heading), pandoc.Attr('', { 'unnumbered' }))
      return pandoc.Blocks({ title, refs }), false
    end
    return refs, false
  end,
}

-- ------------------------------------------------------------------- pass 2

local function trim_inlines(list)
  local first, last = 1, #list
  local function blank(i) return list[i].t == 'Space' or list[i].t == 'SoftBreak' or list[i].t == 'LineBreak' end
  while first <= last and blank(first) do first = first + 1 end
  while last >= first and blank(last) do last = last - 1 end
  local out = pandoc.Inlines({})
  for i = first, last do out:insert(list[i]) end
  return out
end

local function analyze_math(el)
  local display = el.mathtype == 'DisplayMath'
  local a = MATH.analyze(el.text, display)
  if display then S.math.display = S.math.display + 1 else S.math.inline = S.math.inline + 1 end
  S.math.aligned = S.math.aligned + a.aligned
  for _, e in ipairs(a.errors) do U.diag(e.code, 'error', e.message, excerpt(el.text)) end
  if #a.errors > 0 then
    S.math.literal = S.math.literal + select(2, el.text:gsub('\\&', ''))
    return nil, a
  end
  S.math.literal = S.math.literal + a.literal
  return pandoc.Math(el.mathtype, a.tex), a
end

local function equation_block(math_el, analysis)
  local spec = C.numbering.equations
  S.count.equations = S.count.equations + 1
  local n = S.count.equations
  local texts = kind_texts('equations', n)
  local dest
  for index, label in ipairs(analysis.labels) do
    if index == 1 then dest = make_dest(label) end
    register(label, {
      kind = 'equation', numbered = true, number = n, display = texts.display,
      ref_text = texts.ref, reference_text = texts.reference, dest = dest,
    })
  end
  S.sequences.equations[#S.sequences.equations + 1] = {
    n = n, text = texts.display, label = analysis.labels[1], appendix = S.appendix or nil,
  }
  -- The DOCX writer puts a display equation in a paragraph of its own, so the
  -- number is its own paragraph as well; both carry a style hook for later layout.
  local math_inlines = pandoc.Inlines({ math_el })
  if dest then
    math_inlines = pandoc.Inlines({ pandoc.Span({ math_el }, pandoc.Attr(dest, { 'l2w-equation' })) })
  end
  local function styled(block, style)
    return pandoc.Div({ block }, pandoc.Attr('', {}, { ['custom-style'] = style }))
  end
  return pandoc.Blocks({
    styled(pandoc.Para(math_inlines), 'Equation'),
    styled(pandoc.Para(U.text_inlines(texts.display)), 'Equation Number'),
  })
end

-- ---------------------------------------------------------------- page breaks
-- <l2wpagebreak> replaces \clearpage, \newpage, \cleardoublepage. A run of them becomes one empty
-- paragraph that starts a new page; it is dropped when the next block is a level-1 heading whose
-- style already starts a new page (C.page_breaks.before_level1), so no blank page appears.
local PAGE_BREAK = '<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="0" w:line="20" '
  .. 'w:lineRule="exact"/></w:pPr></w:p>'

local function is_page_break(block)
  return block ~= nil and block.t == 'Div' and block.classes:includes('l2wpagebreak')
end

-- <l2wpn-FORMAT> (\pagenumbering) and <l2wpage-N> (\setcounter{page}{N}) start a Word section with
-- its own page numbering. A run of them becomes one empty paragraph carrying a private bookmark
-- _l2wpn_<FORMAT|keep>_<START|keep>; the layout pass turns it into a next-page section break.
local function pagination_marker(block)
  if block == nil or block.t ~= 'Div' or #block.classes ~= 1 then return nil end
  local class = block.classes[1]
  local fmt = class:match('^l2wpn%-(%a+)$')
  if fmt then return { format = fmt } end
  local start = class:match('^l2wpage%-(%d+)$')
  if start then return { start = start } end
  return nil
end

local pagination_bookmark_id = 1900000000

local function pagination_block(spec)
  pagination_bookmark_id = pagination_bookmark_id + 1
  local name = '_l2wpn_' .. (spec.format or 'keep') .. '_' .. (spec.start or (spec.format and '1' or 'keep'))
  local id = tostring(pagination_bookmark_id)
  return pandoc.RawBlock('openxml', '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
    .. '</w:pPr><w:bookmarkStart w:id="' .. id .. '" w:name="' .. name .. '"/><w:bookmarkEnd w:id="' .. id .. '"/></w:p>')
end

-- \null followed by a page break before any content: TeX ships out a page holding only the empty box, i.e. a
-- blank first sheet. Word ignores a page break before the first paragraph, so the sheet is made by a paragraph
-- that holds only a page break (its mark, 1 pt high, lands invisibly at the top of the next page).
local BLANK_PAGE = '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/><w:rPr>'
  .. '<w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr><w:r><w:rPr><w:sz w:val="2"/></w:rPr><w:br w:type="page"/></w:r></w:p>'

local function is_null(block)
  return block ~= nil and block.t == 'Div' and block.classes:includes('l2wnull')
end

local function resolve_page_breaks(blocks)
  local out = pandoc.Blocks({})
  local i = 1
  local content_seen = false
  while i <= #blocks do
    local block = blocks[i]
    if is_null(block) then
      if not content_seen and is_page_break(blocks[i + 1]) then
        while is_page_break(blocks[i + 1]) do i = i + 1 end
        out:insert(pandoc.RawBlock('openxml', BLANK_PAGE))
        content_seen = true
      end
      -- elsewhere an empty box changes nothing visible: dropped
    elseif is_page_break(block) then
      while is_page_break(blocks[i + 1]) do i = i + 1 end
      -- \clearpage\addcontentsline{..}\chapter*{..} (or a generated list title): the invisible contents entries
      -- between the break and a page-breaking heading move after the heading, so they land on its page (their
      -- TOC page number) and the break is not doubled
      local j = i + 1
      while blocks[j] ~= nil and blocks[j].t == 'Div' and blocks[j].classes:includes('l2wcontentsline') do j = j + 1 end
      local after = blocks[j]
      if j > i + 1 and (C.page_breaks or {}).before_level1 and after ~= nil and after.t == 'Header' and after.level == 1 then
        out:insert(after)
        for k = i + 1, j - 1 do out:insert(blocks[k]) end
        content_seen = true
        i = j + 1
        goto continue
      end
      local nxt = blocks[i + 1]
      local heading_breaks = (C.page_breaks or {}).before_level1 and nxt ~= nil and nxt.t == 'Header' and nxt.level == 1
      -- a section break (\pagenumbering) right after already starts a new page
      if nxt ~= nil and not heading_breaks and pagination_marker(nxt) == nil then
        out:insert(pandoc.RawBlock('openxml', PAGE_BREAK))
      end
    elseif pagination_marker(block) ~= nil then
      local spec = {}
      while pagination_marker(blocks[i]) ~= nil do
        local m = pagination_marker(blocks[i])
        -- order matters as in LaTeX: \pagenumbering resets the counter to 1, \setcounter{page} sets it
        if m.format then spec.format, spec.start = m.format, '1' end
        if m.start then spec.start = m.start end
        i = i + 1
      end
      i = i - 1
      while is_page_break(blocks[i + 1]) do i = i + 1 end  -- \pagenumbering{..}\clearpage: one break
      out:insert(pagination_block(spec))
    else
      out:insert(block)
      content_seen = true
    end
    i = i + 1
    ::continue::
  end
  return out
end

local pass_page_breaks = {
  Blocks = resolve_page_breaks,
}

-- ---------------------------------------------------------------- figure notes
-- A figure environment whose text continues after the image (a note or source line under the
-- caption) arrives as a Figure with extra blocks; Word lays those out beside the image in a table.
-- They are moved after the figure, which is where they stand in the typeset document.
local function is_image_block(block)
  if block.t ~= 'Plain' and block.t ~= 'Para' then return false end
  local has_image = false
  for _, el in ipairs(block.content) do
    if el.t == 'Image' then
      has_image = true
    elseif not (el.t == 'Space' or el.t == 'SoftBreak' or el.t == 'LineBreak') then
      return false
    end
  end
  return has_image
end

local pass_figure_notes = {
  Figure = function(fig)
    local images, notes = pandoc.Blocks({}), pandoc.Blocks({})
    local seen_image = false
    for _, block in ipairs(fig.content) do
      if is_image_block(block) then
        seen_image = true
        images:insert(block)
      elseif seen_image then
        notes:insert(block)
      else
        images:insert(block)
      end
    end
    if #notes == 0 or not seen_image then return nil end
    fig.content = images
    local out = pandoc.Blocks({ fig })
    out:extend(notes)
    return out
  end,
}

-- ---------------------------------------------------------- base-less scripts
-- Text followed by $_{95}$ or $^{*}$ (e.g. "IC$_{95}$"): the math has no base, so the OMML writer
-- invents a zero-width-space base. When the script is plain text it is typeset as an ordinary
-- subscript/superscript of the preceding text instead, which is what LaTeX shows; anything with
-- commands (other than \mathrm/\text/\textrm wrappers) stays math.
-- symbol commands with a plain Unicode text equivalent (footnote-style marks)
local SCRIPT_SYMBOLS = {
  dagger = '\u{2020}', ddagger = '\u{2021}', ast = '*', star = '\u{22C6}', circ = '\u{2218}', prime = '\u{2032}',
  S = '\u{00A7}',
}

local function plain_script(text)
  local kind, body = text:match('^%s*([_^])%s*(.-)%s*$')
  if kind == nil then return nil end
  local inner = body:match('^{(.*)}$') or body
  inner = inner:match('^\\mathrm%s*{(.*)}$') or inner:match('^\\text%s*{(.*)}$') or inner:match('^\\textrm%s*{(.*)}$') or inner
  local symbol = inner:match('^%s*\\(%a+)%s*$')
  if symbol and SCRIPT_SYMBOLS[symbol] then return kind, SCRIPT_SYMBOLS[symbol] end
  if inner == '' or inner:find('[\\{}_^$&#%%~]') or #inner > 12 then return nil end
  if body == inner and #inner > 1 then return nil end  -- $_ab$: only the first character is the script in TeX
  return kind, inner
end

local pass_scripts = {
  Math = function(el)
    if el.mathtype ~= 'InlineMath' then return nil end
    local kind, inner = plain_script(el.text)
    if kind == nil then return nil end
    local content = pandoc.Inlines({ pandoc.Str(inner) })
    return kind == '_' and pandoc.Subscript(content) or pandoc.Superscript(content)
  end,
}

-- ------------------------------------------------------------- heading labels
-- \section{T}\label{a}\label{b}: Pandoc makes "a" the heading id and turns "b" into an empty
-- Span{label=b} at the start of the next paragraph. Such label-only spans directly after a
-- heading are moved onto the heading (attribute l2w-aliases) so both labels name it.

local function is_label_span(el)
  return el.t == 'Span' and #el.content == 0 and el.attributes['label'] ~= nil
end

local function collect_heading_labels(blocks)
  local i = 1
  while i < #blocks do
    local h, nxt = blocks[i], blocks[i + 1]
    if h.t == 'Header' and (nxt.t == 'Para' or nxt.t == 'Plain') then
      local labels = {}
      local k = 1
      while k <= #nxt.content do
        local el = nxt.content[k]
        if is_label_span(el) then
          labels[#labels + 1] = el.attributes['label']
        elseif not (el.t == 'Space' or el.t == 'SoftBreak') then
          break
        end
        k = k + 1
      end
      if #labels > 0 then
        local existing = h.attributes['l2w-aliases']
        h.attributes['l2w-aliases'] = (existing and (existing .. ',') or '') .. table.concat(labels, ',')
        local rest = pandoc.Inlines({})
        for j = k, #nxt.content do rest:insert(nxt.content[j]) end
        if #rest == 0 then
          blocks:remove(i + 1)
        else
          nxt.content = rest
        end
      end
    end
    i = i + 1
  end
  return blocks
end

local pass_heading_labels = {
  Blocks = collect_heading_labels,
}

-- ------------------------------------------------------------ bibliography place
-- <l2wrefs> replaces \printbibliography in the staged copy: it becomes the "refs" division where
-- citeproc puts the entries (otherwise they land after the appendices), preceded by the heading.
-- With a refs division Pandoc ignores reference-section-title, so the heading is written here.
local refs_marker_found = false

-- <l2wtoc>/<l2wlof>/<l2wlot> replace \tableofcontents, \listoffigures, \listoftables: a heading and
-- a Word TOC field. Headings feed the table of contents; the figure and table lists collect the
-- caption paragraphs by style (their numbers are literal text, not SEQ fields).
local function xml_escape(text)
  return (text:gsub('&', '&amp;'):gsub('<', '&lt;'):gsub('>', '&gt;'):gsub('"', '&quot;'))
end

local function toc_field(instruction, placeholder)
  local run = function(inner) return '<w:r>' .. inner .. '</w:r>' end
  return pandoc.RawBlock('openxml', '<w:p>'
    .. run('<w:fldChar w:fldCharType="begin" w:dirty="true"/>')
    .. run('<w:instrText xml:space="preserve"> ' .. xml_escape(instruction) .. ' </w:instrText>')
    .. run('<w:fldChar w:fldCharType="separate"/>')
    .. run('<w:t xml:space="preserve">' .. xml_escape(placeholder) .. '</w:t>')
    .. run('<w:fldChar w:fldCharType="end"/>')
    .. '</w:p>')
end

local CONTENTS_KINDS = { l2wtoc = 'toc', l2wlof = 'lof', l2wlot = 'lot' }

local function contents_block(kind)
  local contents = C.contents or {}
  local titles = contents.titles or {}
  local instruction
  if kind == 'toc' then
    local depth = 0
    for k = 1, 6 do if C.numbering.sections['level' .. k] ~= nil then depth = k end end
    -- \l 1-9: Word only collects TC entries (manual \addcontentsline{toc}) with this switch; TC entries of a
    -- custom list carry an \f identifier and stay out
    instruction = 'TOC \\o "1-' .. math.max(1, math.min(depth, 3)) .. '" \\h \\z \\u \\l 1-9'
  elseif kind == 'lof' then
    instruction = 'TOC \\h \\z \\t "' .. (contents.lof_style or 'Image Caption') .. ',' .. string.format('%d', C.list_level or 1) .. '"'
  else
    instruction = 'TOC \\h \\z \\t "' .. (contents.lot_style or 'Table Caption') .. ',' .. string.format('%d', C.list_level or 1) .. '"'
  end
  local blocks = pandoc.Blocks({})
  if (titles[kind] or '') ~= '' then
    blocks:insert(pandoc.Header(1, U.text_inlines(titles[kind]), pandoc.Attr('', { 'unnumbered' })))
  end
  blocks:insert(toc_field(instruction, contents.placeholder or 'Update this field (F9).'))
  return blocks
end

local function centered(div)
  -- \begin{center}: its paragraphs take the "Centered" style (centred, no first-line indent)
  local content = pandoc.Blocks({})
  for _, block in ipairs(div.content) do
    if block.t == 'Para' or block.t == 'Plain' then
      content:insert(pandoc.Div({ block }, pandoc.Attr('', {}, { ['custom-style'] = 'Centered' })))
    else
      content:insert(block)
    end
  end
  div.content = content
  return div
end

local function custom_list_by_letter(letter)
  for _, entry in pairs(C.custom_lists or {}) do
    if entry.letter == letter then return entry end
  end
  return nil
end

local function custom_list_block(letter)
  -- \listof<name> of a tocloft list: its title and a TOC field over the TC entries with identifier <letter>
  local entry = custom_list_by_letter(letter)
  local blocks = pandoc.Blocks({})
  if entry and (entry.title or '') ~= '' then
    blocks:insert(pandoc.Header(1, U.text_inlines(entry.title), pandoc.Attr('', { 'unnumbered' })))
  end
  blocks:insert(toc_field('TOC \\h \\z \\f ' .. letter, (C.contents or {}).placeholder or 'Update this field (F9).'))
  return blocks
end

-- Front-matter typesetting markers (structure_rewrite.plan_typesetting_rewrites):
--   l2wsize-<cmd>   {\Large ...}: its paragraphs' text takes the character style "Size <cmd>" (size from the profile)
--   l2wvspace-<tw>  \vspace{..}: an empty paragraph of exactly that height (paragraph mark 1 pt)
--   l2whrule        \hrulefill on its own line: an empty paragraph with a bottom border across the text width
local function sized(div, command)
  local out = pandoc.Blocks({})
  for _, block in ipairs(div.content) do
    if block.t == 'Para' or block.t == 'Plain' then
      local span = pandoc.Span(block.content, pandoc.Attr('', {}, { ['custom-style'] = 'Size ' .. command }))
      out:insert(block.t == 'Para' and pandoc.Para({ span }) or pandoc.Plain({ span }))
    else
      out:insert(block)
    end
  end
  return out
end

local function spacer(twips)
  local height = math.max(20, math.floor(twips))
  return pandoc.RawBlock('openxml', '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="' .. height
    .. '" w:lineRule="exact"/><w:ind w:firstLine="0"/><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr></w:p>')
end

-- the bookmark _l2wrule_<n> lets the OOXML pass (ooxml/headings.py) start the next paragraph at the margin:
-- "\hrulefill\\ Name\\ Role" is one LaTeX paragraph, and the text after the line break is not indented
local rule_count = 0
local function rule_block()
  rule_count = rule_count + 1
  local id = string.format('%d', 1920000000 + rule_count)
  return pandoc.RawBlock('openxml', '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" w:color="auto"/>'
    .. '</w:pBdr><w:spacing w:before="0" w:after="0"/><w:ind w:firstLine="0"/></w:pPr><w:bookmarkStart w:id="' .. id
    .. '" w:name="_l2wrule_' .. rule_count .. '"/><w:bookmarkEnd w:id="' .. id .. '"/></w:p>')
end

-- l2wnote-<size>-<align>-<indent>-<skip twips>-<f|o>: the note block of a figure/table, inside a float or after a
-- table (structure_rewrite.plan_note_rewrites).
-- Its paragraphs take the style "Caption Note"; the private bookmark _l2wnote_<semantics>_<n> in the first one hands
-- the source declarations to the OOXML pass (ooxml/notes.py), which removes it.
local note_count = 0
local function note_block(div, class)
  note_count = note_count + 1
  local id = string.format('%d', 1930000000 + note_count)
  local name = '_' .. class:gsub('%-', '_') .. '_' .. note_count
  local mark = pandoc.RawInline('openxml', '<w:bookmarkStart w:id="' .. id .. '" w:name="' .. name
    .. '"/><w:bookmarkEnd w:id="' .. id .. '"/>')
  local marked = false
  local content = div.content:walk({
    Para = function(p)
      if marked then return nil end
      marked = true
      p.content:insert(1, mark)
      return p
    end,
    Plain = function(p)
      if marked then return nil end
      marked = true
      p.content:insert(1, mark)
      return p
    end,
  })
  if not marked then return content end
  return pandoc.Div(content, pandoc.Attr('', {}, { ['custom-style'] = 'Caption Note' }))
end

local pass_bibliography_place = {
  Div = function(div)
    if #div.classes == 1 then
      local class = div.classes[1]
      local command = class:match('^l2wsize%-(%a+)$')
      if command then return sized(div, command) end
      local twips = class:match('^l2wvspace%-(%d+)$')
      if twips then return spacer(tonumber(twips)) end
      if class == 'l2whrule' then return rule_block() end
      if class:match('^l2wnote%-%a+%-%a%-%a%-%d+%-%a$') then return note_block(div, class) end
    end
    for class, kind in pairs(CONTENTS_KINDS) do
      if div.classes:includes(class) then return contents_block(kind) end
    end
    local list_letter = #div.classes == 1 and div.classes[1]:match('^l2wlistof%-(%u)$')
    if list_letter then return custom_list_block(list_letter) end
    if div.classes:includes('center') then return centered(div) end
    if not div.classes:includes('l2wrefs') then return nil end
    if C.bibliography.mode ~= 'citeproc' or refs_marker_found then return {} end
    refs_marker_found = true
    local blocks = pandoc.Blocks({})
    local heading = C.bibliography.heading or ''
    if heading ~= '' then
      -- biblatex heading=bibintoc (and friends) puts the bibliography in the TOC; its other headings do not
      local attributes = C.bibliography_in_toc ~= false and { ['l2w-listed'] = '1' } or {}
      blocks:insert(pandoc.Header(1, U.text_inlines(heading), pandoc.Attr('', { 'unnumbered' }, attributes)))
    end
    blocks:insert(pandoc.Div({}, pandoc.Attr('refs')))
    return blocks
  end,
  Pandoc = function(doc)
    if refs_marker_found then
      doc.meta['reference-section-title'] = nil
      return doc
    end
    return nil
  end,
}

local unlisted_count = 0

local pass_numbering = {
  traverse = 'topdown',

  RawBlock = function(raw)
    -- the page-number format in effect (pagination markers): a manual TOC entry under \pagenumbering{gobble}
    -- shows no page number, as in LaTeX
    local fmt = raw.format == 'openxml' and raw.text:match('_l2wpn_(%a+)_')
    if fmt and fmt ~= 'keep' then S.page_format = fmt end
    return nil
  end,

  Div = function(div)
    if div.classes:includes('l2wcontentsline') then
      -- \addcontentsline{ext}{unit}{text}: a TC field (no visible text) at this place.
      --   toc         -> a bare TC at the unit's TOC level: the main TOC field collects it (LaTeX lists exactly
      --                  these entries plus numbered headings; starred headings are unlisted, see Header)
      --   custom list -> TC with the list identifier, collected by the list's TOC \f field
      --   lof / lot   -> dropped: Word builds those lists from the captions
      local ext, unit, numbered, parts = '', '', false, {}
      for _, block in ipairs(div.content) do
        if block.t == 'Div' and block.classes:includes('l2wcontentsext') then
          ext = stringify(block)
        elseif block.t == 'Div' and block.classes:includes('l2wcontentsunit') then
          unit = stringify(block)
        elseif block.t == 'Div' and block.classes:includes('l2wnumberline') then
          numbered = true
        else
          parts[#parts + 1] = stringify(block)
        end
      end
      local list = (C.custom_lists or {})[ext]
      local switches
      if list ~= nil then
        switches = '\\f ' .. list.letter .. ' \\l ' .. string.format('%d', C.list_level or 1)
      elseif ext == 'toc' then
        local level = (C.toc_levels or {})[unit] or 1
        switches = '\\l ' .. string.format('%d', level) .. (S.page_format == 'none' and ' \\n' or '')
      else
        return {}
      end
      local text = table.concat(parts, ' '):gsub('^%s+', ''):gsub('%s+$', '')
      if numbered then
        -- \numberline{\thechapter}: the current top-level number in its own numeral style (A, B, ... after \appendix)
        local level = appendix_spec() or C.numbering.sections.level1
        text = numeral_text(S.sec[1], level and level.numeral or 'arabic') .. '  ' .. text
      end
      if text == '' then return {} end
      local escaped = xml_escape(text:gsub('"', "'"))
      return pandoc.RawBlock('openxml', '<w:p><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
        .. '</w:pPr><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TC "'
        .. escaped .. '" ' .. switches .. ' </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/>'
        .. '</w:r></w:p>'), false
    end
    if not div.classes:includes('l2wappendix') then return nil end
    if C.numbering.sections.appendix ~= nil then
      S.appendix = true
      for k = 1, 6 do S.sec[k] = 0 end
      reset_counters(1)
    else
      U.diag('L2W-W-NUM-020', 'warning',
        '\\appendix: the profile defines no numbering.sections.appendix; appendix chapters continue the main numbering')
    end
    return {}
  end,

  Header = function(h)
    local sections = C.numbering.sections
    local id = h.identifier
    local label = is_source_label(id) and id or nil
    local title = stringify(h.content)
    -- further \label commands of the same heading (see pass_heading_labels) name the same target
    local aliases = {}
    for alias in (h.attributes['l2w-aliases'] or ''):gmatch('[^,]+') do aliases[#aliases + 1] = alias end
    h.attributes['l2w-aliases'] = nil
    if label == nil and #aliases > 0 and id == '' then
      label = table.remove(aliases, 1)
      h.identifier, id = label, label
    end
    local function register_all(fields)
      if label then register(label, fields) end
      for _, alias in ipairs(aliases) do
        local copy = {}
        for k, v in pairs(fields) do copy[k] = v end
        register(alias, copy)
      end
    end
    if h.classes:includes('unnumbered') then
      S.unnumbered[#S.unnumbered + 1] = { level = h.level, title = title, label = label }
      register_all({ kind = 'section', numbered = false, dest = id })
      -- a starred heading is not a TOC entry in LaTeX (only \addcontentsline adds one): a private bookmark
      -- tells the layout pass to give it the unlisted variant of its heading style
      if h.attributes['l2w-listed'] == nil then
        unlisted_count = unlisted_count + 1
        local mark = tostring(1910000000 + unlisted_count)
        h.content:insert(1, pandoc.RawInline('openxml', '<w:bookmarkStart w:id="' .. mark .. '" w:name="_l2wunlisted_'
          .. unlisted_count .. '"/><w:bookmarkEnd w:id="' .. mark .. '"/>'))
      end
      h.attributes['l2w-listed'] = nil
      return h
    end
    local level = sections['level' .. h.level]
    local in_appendix = h.level == 1 and appendix_spec() ~= nil
    if in_appendix then level = appendix_spec() end
    if not sections.enabled or level == nil then
      register_all({ kind = 'section', numbered = false, dest = id })
      return h
    end
    S.sec[h.level] = S.sec[h.level] + 1
    for k = h.level + 1, 6 do S.sec[k] = 0 end
    reset_counters(h.level)
    local values = section_values()
    values.n = numeral_text(S.sec[h.level], level.numeral)
    local count = S.sec[h.level]
    local display = U.render(level.format, values, count)
    local ref_text = U.render(level.ref, values, count)
    local reference_text = U.render(level.reference, values, count)
    local content = U.text_inlines(display .. sections.separator)
    content:extend(h.content)
    h.content = content
    S.sequences.sections[#S.sequences.sections + 1] = {
      level = h.level, n = count, text = display, title = title, label = label, appendix = S.appendix or nil,
    }
    register_all({
      kind = 'section', numbered = true, number = count, display = display,
      ref_text = ref_text, reference_text = reference_text, dest = id, level = h.level,
    })
    return h
  end,

  Figure = function(fig)
    local spec = C.numbering.figures
    local label = fig.identifier ~= '' and fig.identifier or nil
    if not spec.enabled then return nil end
    local long = fig.caption.long
    if #long == 0 then
      if label then register(label, { kind = 'figure', numbered = false, dest = label }) end
      return nil
    end
    S.count.figures = S.count.figures + 1
    local n = S.count.figures
    local texts = kind_texts('figures', n)
    local dest = label and make_dest(label) or nil
    fig.caption.long = prefix_caption(long, label_inlines(texts.display, spec.separator, dest))
    if label then
      fig.identifier = ''
      register(label, {
        kind = 'figure', numbered = true, number = n, display = texts.display,
        ref_text = texts.ref, reference_text = texts.reference, dest = dest,
      })
    end
    S.sequences.figures[#S.sequences.figures + 1] = { n = n, text = texts.display, label = label, appendix = S.appendix or nil }
    return fig
  end,

  Table = function(tbl)
    local spec = C.numbering.tables
    local label = tbl.identifier ~= '' and tbl.identifier or nil
    if not spec.enabled then return nil end
    local long = tbl.caption.long
    if #long == 0 then
      if label then register(label, { kind = 'table', numbered = false, dest = label }) end
      return nil
    end
    S.count.tables = S.count.tables + 1
    local n = S.count.tables
    local texts = kind_texts('tables', n)
    local dest = label and make_dest(label) or nil
    tbl.caption.long = prefix_caption(long, label_inlines(texts.display, spec.separator, dest))
    if label then
      tbl.identifier = ''
      register(label, {
        kind = 'table', numbered = true, number = n, display = texts.display,
        ref_text = texts.ref, reference_text = texts.reference, dest = dest,
      })
    end
    S.sequences.tables[#S.sequences.tables + 1] = { n = n, text = texts.display, label = label, appendix = S.appendix or nil }
    return tbl
  end,

  Para = function(p)
    local has_math = false
    for _, el in ipairs(p.content) do
      if el.t == 'Math' then has_math = true break end
    end
    if not has_math then return nil end
    local blocks = pandoc.Blocks({})
    local current = pandoc.Inlines({})
    local split = false
    local function flush()
      local trimmed = trim_inlines(current)
      if #trimmed > 0 then blocks:insert(pandoc.Para(trimmed)) end
      current = pandoc.Inlines({})
    end
    for _, el in ipairs(p.content) do
      if el.t == 'Math' then
        local math_el, analysis = analyze_math(el)
        local numbered = analysis.numbered and C.numbering.equations.enabled
        if math_el ~= nil and numbered and el.mathtype == 'DisplayMath' then
          flush()
          blocks:extend(equation_block(math_el, analysis))
          split = true
        else
          if analysis.numbered and not C.numbering.equations.enabled then
            for _, label in ipairs(analysis.labels) do
              register(label, { kind = 'equation', numbered = false, dest = label })
            end
          end
          current:insert(math_el or el)
        end
      else
        current:insert(el)
      end
    end
    if split then
      flush()
      return blocks, false
    end
    return pandoc.Para(current), false
  end,

  Math = function(el)
    local math_el, analysis = analyze_math(el)
    if analysis.numbered then
      U.diag('L2W-E-MATH-005', 'error', 'a numbered equation environment outside a paragraph cannot be numbered',
        excerpt(el.text))
    end
    return math_el
  end,
}

-- ------------------------------------------------------------------- pass 3

local REF_TYPES = { ref = 'ref', eqref = 'eqref', ['ref+label'] = 'autoref', ['ref+Label'] = 'cref' }

local function resolve_label(ref_type, label)
  local kind = REF_TYPES[ref_type]
  if kind == nil then
    U.diag('L2W-E-REF-005', 'error', 'unsupported reference type "' .. tostring(ref_type) .. '" for "' .. tostring(label) .. '"')
    return nil
  end
  local entry = S.labels[label]
  local record = { label = label, type = kind, resolved = false, text = '??' }
  S.refs[#S.refs + 1] = record
  if entry == nil then
    U.diag('L2W-E-REF-001', 'error', 'reference to undefined label "' .. tostring(label) .. '"')
    return pandoc.Link(U.text_inlines('??'), '#' .. tostring(label))
  end
  entry.refs = entry.refs + 1
  if not entry.numbered then
    U.diag('L2W-E-REF-004', 'error', 'label "' .. label .. '" refers to a target without a number (' .. entry.kind
      .. ' numbering disabled or unnumbered target)')
    return pandoc.Link(U.text_inlines('??'), '#' .. entry.dest)
  end
  local text
  if kind == 'ref' then
    text = entry.ref_text
  elseif kind == 'eqref' then
    if entry.kind == 'equation' then
      text = entry.reference_text
    else
      text = '(' .. entry.ref_text .. ')'
      U.diag('L2W-W-REF-003', 'warning', '\\eqref{' .. label .. '} points to a ' .. entry.kind .. ', not an equation')
    end
  else
    text = entry.reference_text
    if ref_type == 'ref+Label' then
      -- \Cref starts a sentence: capitalise the first letter (UTF-8 aware); \cref keeps the profile text
      local first = text:match('^[%z\1-\127\194-\244][\128-\191]*')
      if first then text = pandoc.text.upper(first) .. text:sub(#first + 1) end
    end
  end
  record.resolved, record.text, record.dest = true, text, entry.dest
  return pandoc.Link(U.text_inlines(text), '#' .. entry.dest)
end

-- \cref{a,b,c} arrives as one link whose reference is the comma list: each label is
-- resolved on its own (its own hyperlink) and the pieces are joined like a list.
local function resolve_reference(link)
  local ref_type = link.attributes['reference-type']
  local label = link.attributes['reference'] or ''
  if not label:find(',', 1, true) then return resolve_label(ref_type, label) end
  local labels = {}
  for item in label:gmatch('[^,]+') do
    item = item:gsub('^%s+', ''):gsub('%s+$', '')
    if item ~= '' then labels[#labels + 1] = item end
  end
  local separator = C.numbering.list_separator or ', '
  local conjunction = C.numbering.list_conjunction or ' and '
  local out = pandoc.Inlines({})
  for i, item in ipairs(labels) do
    if i > 1 then out:extend(U.text_inlines(i == #labels and conjunction or separator)) end
    -- \Cref{a,b} capitalises only the first item
    local item_type = (i > 1 and ref_type == 'ref+Label') and 'ref+label' or ref_type
    local piece = resolve_label(item_type, item)
    if piece ~= nil then out:insert(piece) end
  end
  return out
end

local function cite_numbers(cite)
  local manual = C.bibliography.manual
  local parts = pandoc.Inlines({})
  local numbers = pandoc.Inlines({})
  local suffix
  for index, citation in ipairs(cite.citations) do
    local key = citation.id
    S.citations[key] = (S.citations[key] or 0) + 1
    if S.citations[key] == 1 then S.citation_order[#S.citation_order + 1] = key end
    local entry = S.bibliography.entries[key]
    if index > 1 then numbers:extend(U.text_inlines(manual.number_separator)) end
    if entry then
      numbers:insert(pandoc.Link({ pandoc.Str(tostring(entry.n)) }, '#' .. entry.dest))
    else
      U.diag('L2W-E-CITE-001', 'error', 'citation key "' .. key .. '" is not in the inline bibliography')
      numbers:insert(pandoc.Str('?'))
    end
    if index == #cite.citations and #citation.suffix > 0 then suffix = citation.suffix end
  end
  if suffix then
    numbers:extend(U.text_inlines(manual.number_separator))
    numbers:extend(suffix)
  end
  local before, after = manual.citation_format:match('^(.-){numbers}(.*)$')
  parts:extend(U.text_inlines(before or '['))
  parts:extend(numbers)
  parts:extend(U.text_inlines(after or ']'))
  return parts
end

local pass_references = {
  traverse = 'topdown',

  Link = function(link)
    if link.attributes['reference-type'] ~= nil then
      return resolve_reference(link), false
    end
    if link.target:sub(1, 1) == '#' then
      local entry = S.labels[link.target:sub(2)]
      if entry ~= nil and entry.dest ~= nil and entry.dest ~= link.target:sub(2) then
        link.target = '#' .. entry.dest
        return link
      end
    end
    return nil
  end,

  Cite = function(cite)
    if C.bibliography.mode == 'manual' then
      return cite_numbers(cite), false
    end
    for _, citation in ipairs(cite.citations) do
      local key = citation.id
      S.citations[key] = (S.citations[key] or 0) + 1
      if S.citations[key] == 1 then S.citation_order[#S.citation_order + 1] = key end
    end
    return nil, false
  end,

  RawInline = function(raw)
    if raw.format == 'latex' or raw.format == 'tex' then
      S.raw[#S.raw + 1] = { kind = 'inline', text = raw.text }
    end
    return nil
  end,

  RawBlock = function(raw)
    if raw.format == 'latex' or raw.format == 'tex' then
      S.raw[#S.raw + 1] = { kind = 'block', text = raw.text }
    end
    return nil
  end,
}

-- ------------------------------------------------------------------- pass 4

local function meta_summary(meta)
  local authors = meta.author
  local count, nonempty = 0, 0
  if authors ~= nil then
    if authors.t == 'MetaList' then
      count = #authors
      for _, item in ipairs(authors) do
        if stringify(item) ~= '' then nonempty = nonempty + 1 end
      end
    else
      count = 1
      if stringify(authors) ~= '' then nonempty = 1 end
    end
  end
  return {
    title = meta.title ~= nil and stringify(meta.title) ~= '',
    title_text = meta.title ~= nil and stringify(meta.title) or '',
    abstract = meta.abstract ~= nil and stringify(meta.abstract) ~= '',
    abstract_text = meta.abstract ~= nil and stringify(meta.abstract) or '',
    keywords_text = S.keywords,
    author_entries = count,
    author_nonempty = nonempty,
  }
end

local pass_report = {
  Pandoc = function(doc)
    local labels = {}
    for _, name in ipairs(S.label_order) do
      local entry = S.labels[name]
      labels[#labels + 1] = {
        label = name, kind = entry.kind, numbered = entry.numbered, number = entry.number,
        text = entry.display, ref_text = entry.ref_text, reference_text = entry.reference_text,
        dest = entry.dest, refs = entry.refs,
      }
    end
    local citations = {}
    for _, key in ipairs(S.citation_order) do citations[#citations + 1] = { id = key, count = S.citations[key] } end
    local report = {
      schema = 'latex2word.filter_report/1',
      pandoc_version = tostring(PANDOC_VERSION),
      labels = labels,
      sequences = S.sequences,
      unnumbered_headings = S.unnumbered,
      refs = S.refs,
      citations = citations,
      bibliography = { mode = C.bibliography.mode, manual_entries = S.bibliography.order },
      math = {
        display = S.math.display, inline = S.math.inline,
        normalized_alignment = S.math.aligned, literal_ampersands = S.math.literal,
      },
      raw_tex = S.raw,
      meta = meta_summary(doc.meta),
      diagnostics = U.diags,
    }
    U.write_json(C.report_path, report)
    return nil
  end,
}

return { pass_config, pass_frontmatter, pass_bibliography, pass_bibliography_place, pass_page_breaks,
  pass_scripts, pass_heading_labels, pass_numbering, pass_figure_notes, pass_references, pass_report }
