# Security

## Trust boundary

**latex2word currently assumes trusted LaTeX input. Do not use it to process untrusted or third-party LaTeX
projects.**

"Trusted" means that you would also be willing to compile the project with your own TeX installation. latex2word
reads the project and runs Pandoc and, for TikZ/pgfplots figures, TeX on it. The checks it applies are designed to
stop accidents and to report constructs it cannot convert. They are not a sandbox that holds against a document
written to read files from your machine.

What latex2word does guarantee for trusted input:

| Guarantee | Mechanism |
|---|---|
| The project tree is never modified | all rewrites happen in a staged copy inside a latex2word-owned workspace |
| The output never replaces an input file | the output target is compared canonically with every build input before conversion; non-DOCX targets are refused (`L2W-E-OUTPUT-001`..`005`) |
| Only the workspace is ever deleted | an ownership marker bound to the directory; links, the file system root, the home directory and the project are refused |
| No shell is involved | every external tool runs as an argument vector with a timeout |
| TeX shell escape is off | `latex -no-shell-escape` for figures |
| XML parsing does not resolve entities or fetch the network | hardened `lxml` parser; SVGs with entity declarations are refused |
| Literal paths outside the project are refused | `\input`, `\include`, `\includegraphics`, `\usepackage`, bibliography and data files that resolve outside the project root (also through symlinks) are errors |

## Why untrusted input is out of scope today

### Pandoc's LaTeX reader (audit F-02)

latex2word scans the project to decide what Pandoc may read; Pandoc's reader then interprets the same TeX by itself.
The two interpretations differ: Pandoc expands macros and aliases, and it ends conditionals and `filecontents`
differently from latex2word's scanner. Known constructs that made Pandoc read files from outside the project are now
errors, but scanner/reader disagreements remain:

* `\input{/path}` inside a `filecontents` body makes Pandoc 3.12 and 3.13 read an arbitrary file into the DOCX;
* `\iffalse … \ifx … \fi \input{/path} \fi` (conditional nesting) did the same with Pandoc 3.1.3; Pandoc 3.12 and
  3.13 do not follow it.

The structural fix is to give the reader only one flattened file of already validated content and to run it with
`pandoc --sandbox`. It exists behind `LATEX2WORD_EXPERIMENTAL_CONFINED_READER=1` (see
`reports/audit/remediation-phase2.md`). Local certification with Pandoc 3.12 and 3.13 found it blocks every known
vector and produces the same documents as the production path on the validation projects; it stays opt-in until it is made the default in a
separate, reviewed change.

### TeX for figures (audit F-07)

TikZ, circuitikz and pgfplots figures are compiled by `latex` and converted by `dvisvgm`
(`latex2word/tikz_render.py`, `render_tikz`). That is a full TeX run on project-derived code.

**Process boundary.** `render_tikz` writes a standalone document `<figure>.tex` into the figure's render directory and
runs `latex -interaction=nonstopmode -halt-on-error -file-line-error -no-shell-escape -output-directory=<render dir>
<figure>.tex` with the working directory set to the staged main directory. It inherits the user's environment plus
`SOURCE_DATE_EPOCH=0`/`FORCE_SOURCE_DATE=1`. `dvisvgm` then runs in the render directory on the DVI file.

**What the TeX process needs:**

* read: the generated `<figure>.tex`; the staged project files the drawing refers to (pgfplots data, `\input`
  inside the picture) below the staged main directory; the TeX distribution (formats, packages, fonts, `texmf.cnf`);
* write: `<figure>.dvi`, `.log`, `.aux` in the render directory;
* nothing else: no network, no other directory.

**Not enforced today.** With `-no-shell-escape` TeX cannot run programs, but it can still read files: TeX Live's
default `openin_any=a` lets `\input{/any/file}` or `\openin` inside a drawing read any readable file and typeset it
into the SVG, and a canary test confirmed the same outside read with MiKTeX. Setting
`openin_any=p` is not treated as the isolation mechanism: it is a TeX Live (kpathsea) setting that MiKTeX does not
share, it is configuration that the environment or `texmf.cnf` can override, its "paranoid" rule still allows reads
from the distribution trees, and it does nothing for `dvisvgm`. It could be a defence-in-depth layer, not the fix.

**Planned isolation (not implemented; needs local testing on each platform).** Run `latex` and `dvisvgm` in an
OS-level restricted process whose file-system view contains only the render directory (read-write), the staged
project (read-only) and the TeX distribution (read-only), with no network. Possible mechanisms: a container or
bubblewrap/landlock on Linux, `sandbox-exec` profiles on macOS, an AppContainer or low-integrity job object on
Windows. Each needs platform-specific tests with a real TeX installation, which the cloud audit environment does
not have.

## Reporting a problem

The project is a private preview without a public issue tracker. Report security problems privately to the
repository owner.
