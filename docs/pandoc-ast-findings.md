# Pandoc 3.12 behaviour that the M3 pipeline is built on

Every statement below was observed with Pandoc 3.12 (`pandoc-api-version` 1.23.1) and is pinned by a test in
`tests/test_pandoc_ast.py` (AST facts), `tests/test_math_normalization.py` (math) or `tests/test_build.py`
(end to end). If a Pandoc upgrade breaks one of those tests, the design decision next to it must be re-examined.

## LaTeX reader: what the AST looks like

| LaTeX | Pandoc AST | Consequence for latex2word |
|---|---|---|
| `\section{T}\label{x}` | `Header 1 ("x",[],[]) …` | the label *is* the identifier; no number is stored anywhere |
| `\section*{T}` | `Header 1 (id,["unnumbered"],[])` | "unnumbered" is read from the class, never from the title |
| `\begin{figure}…\caption{C}\label{x}` | `Figure ("x",…) (Caption Nothing [Plain C]) [Plain [Image …]]` | caption is the place for the number; a figure with an empty caption has no number |
| `\begin{table}…\caption{C}\label{x}` | `Table ("x",…) (Caption …) …` | same |
| `\begin{equation}…\label{x}…\end{equation}` | `Math DisplayMath "\begin{equation}\n…\label{x}\n\end{equation}"` | label and wrapper are **inside the math string**; the filter reads and strips them |
| `\[ … \]`, `$$ … $$` | `Math DisplayMath "…"` | unnumbered display |
| text, `equation`, text (no blank lines) | one `Para` with the `Math` as an inline between the text | the filter splits the paragraph; the DOCX writer splits it anyway |
| `\ref{x}` | `Link ("",[],[("reference-type","ref"),("reference","x")]) [Str "1"] ("#x","")` | Pandoc's own number (it numbers sections/floats itself) is **ignored**; the text is rebuilt from the profile |
| `\eqref{x}` | `reference-type eqref`, text `[x]` | |
| `\autoref`, `\cref` | `reference-type ref+label` | indistinguishable from each other |
| `\Cref` | `reference-type ref+Label` | |
| `\vref` | `reference-type ref` | converted by Pandoc |
| `\nameref`, `\pageref` | **nothing** – the command and its argument vanish | semantic loss; reported by the probe (`L2W-E-REF-006`) |
| `\cite`, `\citep`, `\citet`, `\autocite`, `\parencite`, `\textcite`, `\citeauthor`, `\citeyear`, `\footcite`, `\cites` | `Cite [Citation …] [RawInline latex "\cite{k}"]`; `\citet`/`\textcite`/`\citeauthor` → `AuthorInText`, `\citeyear` → `SuppressAuthor`, optional text → `citationSuffix` | Pandoc handles them; **without citeproc the Cite renders as nothing** (`See and .`) |
| `\nocite{k}` | nothing in the body | |
| `\includegraphics[width=0.5\linewidth]` / `\textwidth` | `Image` attribute `width="50%"` | |
| `\includegraphics[width=0.98\columnwidth]`, `.5\textwidth` | attribute kept as the raw string; the DOCX writer ignores it and uses the **intrinsic size** | the rewrite layer normalises widths (`0.98\linewidth`) |
| `\includegraphics{x.svg}` with `width=98%` | DOCX `wp:extent` = 98 % of the reference document's text width, height from the SVG aspect; identical for SVGs of any internal scale | physical size no longer depends on the SVG's coordinate system |
| unknown command `\foo{x}` | **dropped together with its argument**, no raw node, no warning | the probe (below) reports it |
| unknown environment | `Div ("",["name"],[]) content` | content kept; reported as info |
| `\begin{thebibliography}{99}\bibitem{a} … \bibitem{b} …` | **one** `Div ["thebibliography"]` with **one** `Para` that starts with `Str "99"` | keys lost, entries merged: rewritten before Pandoc runs |
| `\hypertarget{id}{text}` (block context) | `Div (id,…) [Para …]` | used to carry the bibitem key through the AST |
| `\bibliography{refs}` | metadata `bibliography: [refs.bib]` | removed from the staged copy when files are passed explicitly |
| `\usepackage{local}` with `local.sty` next to the main file | the package's `\newcommand`/`\newenvironment` are expanded | local `.sty` files must be staged; `.cls` files are **not** loaded |

## `+raw_tex` and the unknown-construct probe

`-f latex+raw_tex` turns every unknown command into a `RawInline`/`RawBlock` (so it can be diagnosed) but it also stops
`\input`/`\include` from being followed, and it stops `\ref`-family commands and local-package macros from being
converted. The real document is therefore converted *without* `raw_tex`, and `latex2word.texprobe` asks Pandoc a
narrower question: a probe document with one paragraph per distinct command/environment of the live, non-math document
body is converted with `+raw_tex`; names that come back raw are unknown. Macros defined by the document or by a
local `.sty` are trusted; those defined only in a `.cls` are not (Pandoc does not load classes).

## Math: where the literal `&` comes from

texmath (Pandoc's math converter) writes these constructs as OMML `<m:eqArr>` rows and emits **every alignment marker
as a literal `&` text run**:

* `aligned`, `alignedat`, `split`, `align` (with at least one `&`);
* an `array` whose column specification alternates exactly `r l` with an even number of columns (`{rl}`, `{rlrl}`, also with `|` rules; the odd `{rlr}` is already a matrix and is rewritten the same way for uniformity).

A legitimate `\&` becomes the same `<m:t>&</m:t>` run, so the two cannot be told apart in the DOCX. Clean OMML
matrices (`<m:m>`) come out of `array` with any other column pattern, `matrix`/`pmatrix`/`bmatrix`/`vmatrix`/`smallmatrix`,
`cases`, `gather`/`gathered`, `multline`, `eqnarray` (`rcl`), `flalign`, and `aligned` without any `&`.
`rcases` and unknown environments are not converted at all (Pandoc prints
`[WARNING] Could not convert TeX math …, rendering as TeX`, and the raw TeX lands in the document).

**Fix** (`filters/l2w_math.lua`, before the writer): every alignment environment, and every alternating-`rl` array, is
rewritten into an `array` that texmath turns into a matrix: cells are split at top-level `&`, rows at top-level `\\`
(nesting-aware: braces and nested environments), column alignment is `r l r l …`, and a trailing empty `c` column keeps
the spec non-alternating. Escaped `\&` is never touched. Anything that cannot be normalised safely is an error:
`L2W-E-MATH-001` (an `&` outside an array-like environment), `L2W-E-MATH-003` (`\intertext`, `\tag`, more than one
numbered row in one environment), `L2W-E-MATH-005` (a numbered environment outside a paragraph). The DOCX validator
fails if any `&` sits inside an `<m:eqArr>`, and compares the total number of `&` characters in the equations with the
number of `\&` the filter saw.

## DOCX writer

* A display equation is always a paragraph of its own; text before/after it becomes separate paragraphs. A number placed
  after the math in the same Pandoc paragraph therefore becomes the *next* paragraph. The filter emits the number as its
  own paragraph on purpose, with the custom styles `Equation` and `Equation Number` (style ids `Equation`,
  `EquationNumber`) as layout hooks for M4.
* `Span`/`Header` identifiers become `w:bookmarkStart` with Pandoc-generated names (`_<sha1>`); internal links are
  `w:hyperlink w:anchor` with the same names.
* Styles seen: `Heading1…`, `ImageCaption`, `TableCaption`, `Bibliography`, `CaptionedFigure`, `Abstract`, `Title`.
* `reference-section-title` metadata inserts an unnumbered `Heading1` before citeproc's bibliography.
* SVG images are embedded verbatim together with a PNG fallback; `SOURCE_DATE_EPOCH=0` makes the whole package
  reproducible.
