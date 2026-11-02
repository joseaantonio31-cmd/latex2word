| # | command (reader stage) | exit | content markers in JSON AST | stderr |
|---|---|---|---|---|
| R1-self | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | none |
| R2-input-rel | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file chap.tex at t.tex line 4 column 13  |
| R3-input-abs | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-33 |
| R4-local-sty-nosandbox | `pandoc -f latex -t json t.tex` | rc=0 | STYOK  | none |
| R4-local-sty | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | none |
| R5-images | `pandoc --sandbox -f latex -t json t.tex` | rc=0 | "local.png" out.png"  | none |
| R6-citeproc-in-reader | `pandoc --sandbox --citeproc --bibliography=refs.bib -f latex -t json t.tex` | rc=0 | Probeperson  | none |
| R7-luafilter-io | `pandoc --sandbox --lua-filter=io.lua -f latex -t json t.tex` | rc=0 | LUAFETCH:77 LUAIO:SANDBOXCANARY  | none |
| R8-listing-minted-subfile | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-33 |
| A1-iffalse | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-33 |
| A2-filecontents | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-33 |
| A3-let-alias | `pandoc --sandbox -f latex -t json t.tex` | rc=0 |  | [WARNING] Could not load include file /tmp/claude-0/-home-user-latex2word/058838fe-e1e4-5d2a-8314-33 |
