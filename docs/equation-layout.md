# Equation layout: tradeoff analysis

Pandoc emits a numbered display equation as two sibling paragraphs: `Equation`
(holding a native `m:oMathPara`) and `EquationNumber` (holding `(n)`). Publisher
layouts want the equation centred on the column and the number flush with the
column edge. Three ways to get there were considered.

| Criterion | A. Tabs (`<tab> math <tab> (n)`) | B. Three-cell table `[side \| equation \| side]` | C. Word native `#` equation array |
|---|---|---|---|
| Editability in Word | Good: one paragraph, plain text editing | Good: a normal table; the equation cell edits like any equation | Best in principle (one object), but the number is generated text inside the math zone |
| Rendering stability in Word | Fair: math becomes *inline* math (compact style); centring depends on the number width not pushing past the centre tab | Good: fixed-layout table with a fixed grid; display math is kept | Fair: depends on Word's equation-array engine; numbering is not a Word field |
| Cross-reference / bookmark preservation | Good: bookmarks stay in the paragraph | Good: the original paragraphs are *moved*, so bookmarks and hyperlink anchors are untouched | Poor: the number is part of the math (`#(1)`), the Pandoc number paragraph and its anchors would have to be rebuilt |
| Wide / multi-line equations | Poor: inline math cannot hold multi-line display math; a wide equation collides with the number | Good: an equation wraps inside its cell; multi-line `m:oMathPara` stays display math | Good: the number sits on the last line natively |
| Structural cleanliness | One paragraph, but math is rewritten (`oMathPara` unwrapped) | Extra table and guard paragraphs between adjacent tables; OMML is byte-identical | OMML must be rewritten; non-standard markup |
| LibreOffice / other consumers | Good: tabs are universally supported; inline math renders | Good: tables are universally supported | Poor: the `#` construct is a Word-ism and is shown as literal text elsewhere |

## Decision

* `layout.equations.mode: table` (default): option B.
* `layout.equations.mode: tabs`: option A is supported as an alternative for
  profiles that forbid layout tables. A multi-line equation silently falling back
  to B would hide the difference, so the build reports `L2W-W-LAYOUT-032` and uses
  the table for that equation.
* Option C is not implemented: it forces rewriting the OMML and the number,
  loses the cross-reference anchors, and degrades outside Word.

## Constraint

Both outer cells have the same width (`number_width`) so the equation is centred
independently of the number's width:

    equation width <= column width - 2 * number_width

An equation wider than this wraps inside its cell instead of colliding with the
number. When the column leaves less than 1.0 in for the middle cell, the side
widths are reduced symmetrically (floor 0.25 in) and `L2W-W-LAYOUT-031` is
reported. The width of an equation cannot be measured without Word's math layout
engine, so the constraint is not verified automatically.

## Verification status

`MICROSOFT_WORD_REVIEW_REQUIRED`: the automated build has no Word available. The
tests assert the XML structure (schema order, widths, preserved OMML and
bookmarks) only; the visual result (centring, vertical alignment, no collision
with long equations, borderless table style) must be reviewed in Microsoft Word.
