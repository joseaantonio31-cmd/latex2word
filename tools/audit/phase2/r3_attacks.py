import json, os, subprocess, sys, zipfile, shutil
from pathlib import Path
S = Path(sys.argv[1]); P = S / "r3"; SEC = S / "secret"
CANARY = "TOPSECRETCANARY42"; PNG = (SEC / "private.png").read_bytes()
pre = "\\documentclass{article}\n\\usepackage{graphicx}\n"
V = {
 "macro-input": (pre + f"\\newcommand{{\\p}}{{{SEC}/canary}}\n", "X \\input{\\p} Y", {}),
 "macro-graphic": (pre + f"\\newcommand{{\\p}}{{{SEC}/private.png}}\n", "X \\includegraphics{\\p} Y", {}),
 "let-input": (pre + "\\let\\inp\\input\n", f"X \\inp{{{SEC}/canary}} Y", {}),
 "let-graphic": (pre + "\\let\\ig\\includegraphics\n", f"X \\ig{{{SEC}/private.png}} Y", {}),
 "wrapper-macro": (pre + "\\newcommand{\\inp}[1]{\\input{#1}}\n", f"X \\inp{{{SEC}/canary}} Y", {}),
 "lstinputlisting": (pre, f"\\lstinputlisting{{{SEC}/canary.tex}}", {}),
 "inputminted": (pre, f"\\inputminted{{tex}}{{{SEC}/canary.tex}}", {}),
 "subfile": (pre, f"\\subfile{{{SEC}/canary.tex}}", {}),
 "includesvg": (pre, f"\\includesvg{{{SEC}/canary.tex}}", {}),
 "sty-macro": (pre + "\\usepackage{mymacros}\n", "X \\leak{} Y", {"mymacros.sty": f"\\newcommand\\leak{{\\input{{{SEC}/canary}}}}\n"}),
 "macro-usepackage": (pre + f"\\def\\x{{{SEC}/evil}}\\usepackage{{\\x}}\n", "X \\pk{} Y", {}),
 "literal-include": (pre, f"\\include{{{SEC}/canary}}", {}),
 "iffalse-nesting": (pre, f"Hello \\iffalse \\ifx \\fi \\input{{{SEC}/canary}} \\fi end.", {}),
 "filecontents": (pre, f"\\begin{{filecontents}}{{x.tex}}\n\\input{{{SEC}/canary}}\n\\end{{filecontents}}\nHi.", {}),
}
(SEC / "evil.sty").write_text("\\newcommand{\\pk}{TOPSECRETCANARY42}\n")
def leaked_docx(path):
    if not path.is_file(): return "absent"
    z = zipfile.ZipFile(path)
    text = any(CANARY in z.read(n).decode("latin-1") for n in z.namelist() if n.endswith(".xml"))
    img = any(z.read(n) == PNG for n in z.namelist() if n.startswith("word/media/"))
    return "LEAK" if (text or img) else "clean"
rows = []
for name, (preamble, body, extra) in V.items():
    d = P / name; d.mkdir(parents=True)
    (d / "main.tex").write_text(preamble + "\\begin{document}\n" + body + "\n\\end{document}\n")
    for k, v in extra.items(): (d / k).write_text(v)
    (d / "p.yaml").write_text(f"schema_version: 1\nproject: {{name: v, root: {d}, main: main.tex}}\nprofile: generic\nworkspace: {{root: {d}-ws}}\noutput: {{directory: {d}-out}}\n")
    env = dict(os.environ, LATEX2WORD_EXPERIMENTAL_CONFINED_READER="1")
    out = subprocess.run([sys.executable, "tools/audit/harness_build.py", str(d / "p.yaml")], capture_output=True, text=True, env=env).stdout
    res = json.loads(out); codes = sorted({x["code"] for x in res["diagnostics"] if x["severity"] == "error"})
    ast = Path(f"{d}-ws/build/confined_ast.json")
    ast_state = ("LEAK" if CANARY in ast.read_text() else "clean") if ast.is_file() else "absent"
    # (b) confined reader alone, no deny-list: flatten what the graph resolved, run the sandboxed reader
    from latex2word.source_graph import build_source_graph
    from latex2word.confinement import flatten, read_confined, FlattenError, check_ast_resources
    g = build_source_graph(d, d / "main.tex")
    try:
        flat = flatten(g); wd = Path(f"{d}-reader"); wd.mkdir()
        cr = read_confined(shutil.which("pandoc"), flat, g, wd, timeout_s=60)
        alone_text = "LEAK" if cr.ast is not None and CANARY in json.dumps(cr.ast) else "clean"
        res_codes = sorted({f.code for f in cr.findings + (check_ast_resources(cr.ast, d, d) if cr.ast else [])})
        alone = f"{alone_text} {','.join(res_codes) or '-'}"
    except FlattenError as exc:
        alone = f"flatten refused: {exc}"
    rows.append((name, res["status"], ",".join(c for c in codes if c != "L2W-E-BUILD-001") or "-", ast_state,
                 leaked_docx(Path(f"{d}-ws/build/preliminary.docx")), leaked_docx(Path(f"{d}-out/main.docx")), alone))
print("| attack | build | blocking errors | confined AST | preliminary.docx | final DOCX | confined reader alone (no deny-list) |")
print("|---|---|---|---|---|---|---|")
for r in rows: print("| " + " | ".join(r) + " |")
