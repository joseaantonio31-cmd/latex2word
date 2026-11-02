# Supported LaTeX

latex2word converts what Pandoc's LaTeX reader understands, extended by staged rewrites and Lua filters. It is not a
TeX engine: macros are expanded only as far as Pandoc expands `\newcommand`, and local `.sty`/`.cls` files are
staged but not executed. Anything that would lose text is reported (`L2W-E-TEX-001` and related diagnostics).

| Area | Supported | Caveats |
|---|---|---|
| Structure | `\chapter`…`\subsubsection`, starred variants, `\appendix`, `\label` | starred headings stay out of the TOC unless `\addcontentsline` adds them |
| Pagination | `\pagenumbering{gobble,roman,arabic}`, `\clearpage`, `\newpage`, `\null` blank pages, `titlepage` | Word decides the final page breaks |
| Contents | `\tableofcontents`, `\listoftables`, `\listoffigures`, tocloft `\newlistof` lists, `\addcontentsline` | generated as Word fields; Word fills in page numbers on field update |
| Math | inline and display math, `equation`, `align`, `gather`, `\eqref` | `\tag`, `\intertext` and several numbered rows per environment are errors |
| Tables | `tabular`, `tabularx`, `longtable` (`\endhead`, `\endfoot`, `\endlastfoot`), `\multicolumn`, booktabs, `\newcolumntype`, `array` column decorations | `\multirow` follows Pandoc's own table model; column widths are recomputed for the page or column |
| Figures | `\includegraphics` (PDF, SVG, PNG/JPG), TikZ, circuitikz, pgfplots with external data files, `\resizebox` around figures | sub-figures, `scale`/`angle` options, EPS/TIFF are reported, not converted |
| Units | siunitx: `\SI`, `\qty`, `\si`, `\unit`, `\num`, `\SIrange`, `\qtyrange`, `\numrange`, lists, `\ang`, `\sisetup`, `\DeclareSIUnit` | unknown unit macros are left untouched and reported |
| References | `\ref`, `\eqref`, `\autoref`, `\cref`, `\Cref` (ranges and lists), hyperref links | `\nameref`, `\pageref` are errors; numbers are literal text |
| Citations | `\cite`, `\citep`, `\citet`, `\autocite`, `\parencite`, `\textcite`, `\citeauthor`, `\citeyear`, `\footcite`, `\cites`, `\nocite`; BibTeX/biblatex files; inline `thebibliography` | formatting follows the CSL style, not the biblatex style |
| Front matter | `\title`, `\author`, abstract, keywords (profile metadata adapters); title pages with `\tiny`…`\Huge` groups, literal `\vspace`, `\hrulefill` lines | `\vspace` with non-literal lengths is ignored with a warning |
| Notes | paragraph-level note groups or note macros attached to figures and tables (size and alignment declarations) | only inside floats or directly after a table |
| Text | emphasis, fonts, footnotes, lists, `center`, quotes, `\url`/`\href` | arbitrary programmable macros and conditionals are not evaluated |

The `inspect` command lists what a project uses; `build` writes `transformations.json` (every staged-copy edit) and
the diagnostics explain anything that was not converted.
