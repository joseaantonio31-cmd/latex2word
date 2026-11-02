| # | command | output | content markers | stderr |
|---|---|---|---|---|
| S1-self | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S2-input-rel-nosandbox | `pandoc -f latex -t docx -o out.docx t.tex` | docx=yes media=0 | CHAPTEROK  | none |
| S2-input-rel | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | [WARNING] Could not load include file chap.tex at t.tex line 4 column 13 Could not find data file data/data/do |
| S3-input-abs-nosandbox | `pandoc -f latex -t docx -o out.docx t.tex` | docx=yes media=0 | SANDBOXCANARY  | none |
| S3-input-abs | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-339a0585f904 |
| S4-local-sty | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S5-png | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S5-png-resource-path | `pandoc --sandbox --resource-path=. -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S5-png-on-cmdline | `pandoc --sandbox -f latex -t docx -o out.docx t.tex local.png` | docx=no media= |  | [WARNING] local.png is not UTF-8 encoded: falling back to latin1. Could not find data file data/data/docx/[Con |
| S6-svg | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S7-png-outside | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S8-bib | `pandoc --sandbox --citeproc --bibliography=refs.bib -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S9-bib-csl | `pandoc --sandbox --citeproc --bibliography=refs.bib --csl=style.csl -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S10-luafilter-io | `pandoc --sandbox --lua-filter=io.lua -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| S11-reference-doc | `pandoc --sandbox --reference-doc=ref.docx -f latex -t docx -o out.docx t.tex` | docx=no media= |  | Could not find data file data/data/docx/[Content_Types].xml  |
| A1-iffalse-nosandbox | `pandoc -f latex -t docx -o out.docx t.tex` | docx=yes media=0 | SANDBOXCANARY  | none |
| A1-iffalse | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-339a0585f904 |
| A2-filecontents-nosandbox | `pandoc -f latex -t docx -o out.docx t.tex` | docx=yes media=0 | SANDBOXCANARY  | none |
| A2-filecontents | `pandoc --sandbox -f latex -t docx -o out.docx t.tex` | docx=no media= |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-339a0585f904 |
