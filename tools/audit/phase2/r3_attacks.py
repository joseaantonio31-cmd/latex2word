"""F-02 attack vectors against the production path and the experimental confined reader (audit R3).

Usage (from the repository root, any OS):
    python tools/audit/phase2/r3_attacks.py <empty-scratch-dir> [--pandoc PATH]

Self-contained: creates its own canary files outside every attack project. For each vector it records
* control   -- does the UNCONFINED Pandoc reader read the canary? (proves the attack is live with this Pandoc;
               "n/a" for vectors that latex2word's scanner stops and Pandoc does not read by itself);
* the production build and the experimental build (harness: Pandoc gate and schema-order check bypassed,
  see tools/audit/harness_build.py): blocking errors, canary in the confined JSON AST / preliminary / final DOCX;
* the confined reader alone (no deny-list in front).

Paths inside TeX use forward slashes (Path.as_posix): a native Windows path such as C:\\Users\\... would be parsed
as TeX commands and make every attack fail for the wrong reason.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

CANARY = "TOPSECRETCANARY42"


def main(argv: list[str]) -> int:
    scratch = Path(argv[0]).resolve()
    pandoc = argv[argv.index("--pandoc") + 1] if "--pandoc" in argv else shutil.which("pandoc")
    if pandoc is None:
        print("pandoc not found", file=sys.stderr)
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    secret = scratch / "outside"
    secret.mkdir(exist_ok=True)
    (secret / "canary.tex").write_text(CANARY + "\n", encoding="utf-8")
    (secret / "evil.sty").write_text("\\newcommand{\\pk}{" + CANARY + "}\n", encoding="utf-8")
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    (secret / "private.png").write_bytes(png)
    sec = secret.as_posix()
    pre = "\\documentclass{article}\n\\usepackage{graphicx}\n"
    vectors = {
        "macro-input": (pre + f"\\newcommand{{\\p}}{{{sec}/canary}}\n", "X \\input{\\p} Y", {}),
        "macro-graphic": (pre + f"\\newcommand{{\\p}}{{{sec}/private.png}}\n", "X \\includegraphics{\\p} Y", {}),
        "let-input": (pre + "\\let\\inp\\input\n", f"X \\inp{{{sec}/canary}} Y", {}),
        "let-graphic": (pre + "\\let\\ig\\includegraphics\n", f"X \\ig{{{sec}/private.png}} Y", {}),
        "wrapper-macro": (pre + "\\newcommand{\\inp}[1]{\\input{#1}}\n", f"X \\inp{{{sec}/canary}} Y", {}),
        "lstinputlisting": (pre, f"\\lstinputlisting{{{sec}/canary.tex}}", {}),
        "inputminted": (pre, f"\\inputminted{{tex}}{{{sec}/canary.tex}}", {}),
        "subfile": (pre, f"\\subfile{{{sec}/canary.tex}}", {}),
        "includesvg": (pre, f"\\includesvg{{{sec}/canary.tex}}", {}),
        "sty-macro": (pre + "\\usepackage{mymacros}\n", "X \\leak{} Y",
                      {"mymacros.sty": f"\\newcommand\\leak{{\\input{{{sec}/canary}}}}\n"}),
        "macro-usepackage": (pre + f"\\def\\x{{{sec}/evil}}\\usepackage{{\\x}}\n", "X \\pk{} Y", {}),
        "literal-include": (pre, f"\\include{{{sec}/canary}}", {}),
        "iffalse-nesting": (pre, f"Hello \\iffalse \\ifx \\fi \\input{{{sec}/canary}} \\fi end.", {}),
        "filecontents": (pre, f"\\begin{{filecontents}}{{x.tex}}\n\\input{{{sec}/canary}}\n\\end{{filecontents}}\nHi.", {}),
    }

    def docx_state(path: Path) -> str:
        if not path.is_file():
            return "absent"
        with zipfile.ZipFile(path) as z:
            text = any(CANARY in z.read(n).decode("latin-1") for n in z.namelist() if n.endswith(".xml"))
            image = any(z.read(n) == png for n in z.namelist() if n.startswith("word/media/"))
        return "LEAK" if (text or image) else "clean"

    from latex2word.confinement import FlattenError, check_ast_resources, flatten, read_confined
    from latex2word.source_graph import build_source_graph

    repo = Path(__file__).resolve().parents[3]
    rows = []
    for name, (preamble, body, extra) in vectors.items():
        project = scratch / name
        if project.exists():
            shutil.rmtree(project)
        project.mkdir()
        (project / "main.tex").write_text(preamble + "\\begin{document}\n" + body + "\n\\end{document}\n", encoding="utf-8")
        for rel, text in extra.items():
            (project / rel).write_text(text, encoding="utf-8")
        control = subprocess.run([pandoc, "-f", "latex", "-t", "json", "main.tex"], cwd=project,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        live = CANARY in control.stdout or (name.endswith("graphic") and "private.png" in control.stdout)
        results = {}
        for mode in ("production", "experimental"):
            ws, out = scratch / f"{name}-{mode}-ws", scratch / f"{name}-{mode}-out"
            for d in (ws, out):
                shutil.rmtree(d, ignore_errors=True)
            config = scratch / f"{name}-{mode}.yaml"
            config.write_text(json.dumps({"schema_version": 1, "project": {"name": "v", "root": str(project), "main": "main.tex"},
                                          "profile": "generic", "workspace": {"root": str(ws)}, "output": {"directory": str(out)}}),
                              encoding="utf-8")  # JSON is valid YAML: no quoting issues with Windows paths
            env = dict(os.environ)
            env.pop("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", None)
            if mode == "experimental":
                env["LATEX2WORD_EXPERIMENTAL_CONFINED_READER"] = "1"
            run = subprocess.run([sys.executable, str(repo / "tools" / "audit" / "harness_build.py"), str(config)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=repo)
            try:
                res = json.loads(run.stdout)
            except ValueError:
                results[mode] = ("CRASH", run.stderr.strip()[-200:], "?", "?", "?")
                continue
            codes = sorted({x["code"] for x in res["diagnostics"] if x["severity"] == "error"} - {"L2W-E-BUILD-001"})
            ast = ws / "build" / "confined_ast.json"
            ast_state = ("LEAK" if CANARY in ast.read_text(encoding="utf-8") else "clean") if ast.is_file() else "absent"
            results[mode] = (res["status"], ",".join(codes) or "-", ast_state,
                             docx_state(ws / "build" / "preliminary.docx"), docx_state(out / "main.docx"))
        graph = build_source_graph(project, project / "main.tex")
        try:
            work = scratch / f"{name}-reader"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            confined = read_confined(pandoc, flatten(graph), graph, work, timeout_s=120)
            leaked = confined.ast is not None and CANARY in json.dumps(confined.ast)
            codes = sorted({f.code for f in confined.findings} | (
                {f.code for f in check_ast_resources(confined.ast, project, project)} if confined.ast else set()))
            alone = f"{'LEAK' if leaked else 'clean'} {','.join(codes) or '-'}"
        except FlattenError as exc:
            alone = f"flatten refused: {exc}"
        rows.append((name, "live" if live else "n/a", *results["production"], *results["experimental"], alone))

    header = ("attack", "control (unconfined reader)", "prod status", "prod errors", "prod AST", "prod preliminary",
              "prod final", "exp status", "exp errors", "exp AST", "exp preliminary", "exp final", "confined reader alone")
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in rows:
        print("| " + " | ".join(row) + " |")
    leaks = [r[0] for r in rows if "LEAK" in r[4:7] + r[9:12] or r[-1].startswith("LEAK")]
    print(f"\nexperimental-path leaks: {[r[0] for r in rows if 'LEAK' in r[9:12] or r[-1].startswith('LEAK')]}")
    print(f"production-path leaks: {[r[0] for r in rows if 'LEAK' in r[4:7]]}")
    return 1 if any("LEAK" in r[9:12] or r[-1].startswith("LEAK") for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
