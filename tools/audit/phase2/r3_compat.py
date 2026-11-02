import json, os, shutil, subprocess, sys, zipfile, hashlib
from pathlib import Path
S = Path(sys.argv[1]); out = S / "r3cmp"; shutil.rmtree(out, ignore_errors=True); out.mkdir()
FX = Path("tests/fixtures")
cases = {
  "clean_article": (FX / "audit/clean_article", "generic", None),
  "minimal_project(\\input)": (FX / "minimal_project", "generic", None),
  "local_class(.sty/.cls)": (FX / "semantic/local_class", "generic", None),
  "generic_article(bib+svg)": (FX / "semantic/generic_article", "generic", None),
  "citations": (FX / "semantic/citations", "generic", None),
  "multifile_project": (FX / "audit/multifile_project", "generic", None),
  "report_structure": (FX / "semantic/report_structure", "report-es-test", "profiles"),
}
print("| fixture | legacy reader build | default (confined) build | final document.xml identical | diagnostics identical |")
print("|---|---|---|---|---|")
for name, (src, profile, pdir) in cases.items():
    res = {}
    for mode in ("legacy", "default"):
        d = out / f"{src.name}-{mode}"; shutil.copytree(src, d)
        extra = f"profile_dirs: [{d / pdir}]\n" if pdir else ""
        (out / f"{src.name}-{mode}.yaml").write_text(f"schema_version: 1\nproject: {{name: x, root: {d}, main: main.tex}}\nprofile: {profile}\n{extra}workspace: {{root: {d}-ws}}\noutput: {{directory: {d}-out}}\n")
        env = {k: v for k, v in os.environ.items() if k not in ("LATEX2WORD_EXPERIMENTAL_CONFINED_READER", "LATEX2WORD_UNSAFE_LEGACY_READER")}
        if mode == "legacy": env["LATEX2WORD_UNSAFE_LEGACY_READER"] = "1"
        r = json.loads(subprocess.run([sys.executable, "tools/audit/harness_build.py", str(out / f"{src.name}-{mode}.yaml")], capture_output=True, text=True, env=env).stdout)
        diags = sorted((x["code"], x["message"].replace(str(d), "<P>")) for x in r["diagnostics"] if x["code"] != "L2W-W-CONFINE-005")  # the legacy-reader warning
        docx = Path(r["output"]) if r["output"] else Path(f"{d}-ws/build/preliminary.docx")
        xml = zipfile.ZipFile(docx).read("word/document.xml") if docx.is_file() else b""
        res[mode] = (r["status"], diags, hashlib.sha256(xml).hexdigest() if xml else None, docx.name)
    p, e = res["legacy"], res["default"]
    diff = sorted(set(e[1]) ^ set(p[1]))
    print(f"| {name} | {p[0]} ({p[3]}) | {e[0]} ({e[3]}) | {'yes' if p[2] and p[2] == e[2] else 'NO'} | {'yes' if not diff else 'NO: ' + '; '.join(c + ' ' + m[:70] for c, m in diff[:3])} |")
