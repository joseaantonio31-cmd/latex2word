"""The generic adapter reads title/author/date/keywords and removes the author block from the staged copy."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from projectkit import make_project

from latex2word.diagnostics import Severity
from latex2word.metadata.adapters import available_adapters, get_adapter
from latex2word.profiles import load_profile
from latex2word.rewrite import Edit
from latex2word.source_graph import build_source_graph

FRONTMATTER = load_profile("ieee-conference").frontmatter


def frontmatter(environments=("IEEEkeywords",), commands=()):
    keywords = dataclasses.replace(FRONTMATTER.keywords, environments=tuple(environments), commands=tuple(commands))
    return dataclasses.replace(FRONTMATTER, keywords=keywords)


def extract(tmp_path: Path, files: dict[str, str] | str, adapter: str = "generic", **config):
    if isinstance(files, str):
        files = {"main.tex": "\\documentclass{article}\n" + files + "\n\\begin{document}\n\\maketitle\nBody.\n\\end{document}\n"}
    root = make_project(tmp_path, files)
    graph = build_source_graph(root, root / "main.tex")
    return get_adapter(adapter).extract(graph, frontmatter(**config)), graph


def codes(extraction) -> set[str]:
    return {f.code for f in extraction.findings}


def apply(graph, edit: Edit) -> str:
    text = graph.files[edit.file].text
    return text[: edit.start] + edit.replacement + text[edit.end :]


def test_registry_lists_both_adapters():
    assert {"generic", "ieee-author-blocks"} <= set(available_adapters())


def test_title_date_and_two_authors_sharing_an_affiliation(tmp_path: Path):
    source = (
        "\\title{A Study of Examples}\n\\date{May 2030}\n"
        "\\author{Ada Example \\\\ Example University \\\\ Example City \\\\ ada@example.org\n"
        "\\and Ben Sample \\\\ Example University \\\\ Example City \\\\ \\texttt{ben@example.org}\n"
        "\\and Cy Third \\\\ Other Institute}"
    )
    extraction, _ = extract(tmp_path, source)
    meta = extraction.metadata
    assert meta.title == "A Study of Examples" and meta.date == "May 2030"
    assert [a.name for a in meta.authors] == ["Ada Example", "Ben Sample", "Cy Third"]
    assert [a.email for a in meta.authors] == ["ada@example.org", "ben@example.org", None]
    assert [a.affiliation_ids for a in meta.authors] == [("a1",), ("a1",), ("a2",)]
    assert [a.lines for a in meta.affiliations] == [("Example University", "Example City"), ("Other Institute",)]
    assert [a.id for a in meta.affiliations] == ["a1", "a2"]
    assert not any(f.severity is Severity.ERROR for f in extraction.findings)


def test_missing_optional_parts(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\author{Ada Example \\and ada@example.org \\and Cy \\\\ Lab}")
    authors = extraction.metadata.authors
    assert [(a.name, a.email, a.affiliation_ids) for a in authors] == [
        ("Ada Example", None, ()), ("", "ada@example.org", ()), ("Cy", None, ("a1",)),
    ]


def test_email_wrappers_and_extra_addresses(tmp_path: Path):
    source = "\\author{Ada \\\\ \\href{mailto:ada@example.org}{ada@example.org} \\\\ \\email{second@example.org} \\\\ Lab}"
    meta = extract(tmp_path, source)[0].metadata
    assert meta.authors[0].email == "ada@example.org"
    assert meta.extra_emails == ("second@example.org",)
    assert meta.emails == ("ada@example.org", "second@example.org")
    assert meta.affiliations[0].lines == ("Lab",)


def test_unicode_names_are_preserved_exactly(tmp_path: Path):
    source = "\\author{Zoë Örnek \\\\ Universität Beispielstadt \\and Nuño Ibáñez \\\\ Jos\\'e \\& Co.}"
    meta = extract(tmp_path, source)[0].metadata
    assert [a.name for a in meta.authors] == ["Zoë Örnek", "Nuño Ibáñez"]
    assert meta.affiliations[0].lines == ("Universität Beispielstadt",)
    assert meta.affiliations[1].lines == ("José & Co.",)


def test_last_author_wins_and_every_declaration_is_removed(tmp_path: Path):
    extraction, graph = extract(tmp_path, "\\author{First Person}\n\\author{Second Person}")
    assert [a.name for a in extraction.metadata.authors] == ["Second Person"]
    assert len(extraction.edits) == 2
    edited = graph.files["main.tex"].text
    for edit in sorted(extraction.edits, key=lambda e: -e.start):
        edited = edited[: edit.start] + edit.replacement + edited[edit.end :]
    assert "\\author" not in edited and "\\documentclass{article}" in edited and "\\maketitle" in edited


def test_edit_removes_exactly_the_command_span(tmp_path: Path):
    extraction, graph = extract(tmp_path, "\\author[Short]{Ada \\\\ {Lab \\textit{One}}}\nAFTER")
    (edit,) = extraction.edits
    assert (edit.kind, edit.subject, edit.replacement, edit.file) == ("metadata-author", "author", "", "main.tex")
    removed = graph.files["main.tex"].text[edit.start : edit.end]
    assert removed == "\\author[Short]{Ada \\\\ {Lab \\textit{One}}}"
    assert apply(graph, edit).count("AFTER") == 1 and "Ada" not in apply(graph, edit)


def test_title_with_math_or_macros_is_left_to_pandoc_and_not_edited(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\title{Bounds on $x$}\n\\date{\\today}")
    assert extraction.metadata.title is None and extraction.metadata.date is None
    assert extraction.findings == [] and extraction.edits == []


def test_no_author_means_no_findings_or_edits(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\title{T}")
    assert extraction.metadata.authors == () and extraction.findings == [] and extraction.edits == []


def test_unbalanced_braces_are_an_error_and_source_is_untouched(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\author{Ada \\\\ {Lab")
    (finding,) = extraction.findings
    assert finding.code == "L2W-E-META-002" and finding.severity is Severity.ERROR
    assert finding.location.file == "main.tex" and finding.location.line == 2
    assert extraction.edits == [] and extraction.metadata.authors == ()


def test_unknown_content_in_author_is_an_error(tmp_path: Path):
    for argument in ("Ada\\thanks{Funded}", "Ada \\\\ \\mystery{Lab}", "Ada $x$", "Ada\\footnote{n}"):
        extraction, _ = extract(tmp_path / str(abs(hash(argument))), f"\\author{{{argument}}}")
        assert "L2W-E-META-002" in codes(extraction), argument
        assert extraction.edits == []


def test_author_with_only_layout_commands_is_an_error_when_it_had_content(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\author{\\normalsize}")
    assert codes(extraction) == {"L2W-E-META-003"} and extraction.edits == []


def test_empty_author_is_removed_without_error(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\author{ }")
    assert not any(f.severity is Severity.ERROR for f in extraction.findings)
    assert len(extraction.edits) == 1 and extraction.metadata.authors == ()


def test_author_in_an_included_file_is_found(tmp_path: Path):
    files = {
        "main.tex": "\\documentclass{article}\n\\input{front}\n\\begin{document}\nBody.\n\\end{document}\n",
        "front.tex": "\\author{Ada Example}\n",
    }
    extraction, _ = extract(tmp_path, files)
    assert extraction.metadata.authors[0].name == "Ada Example"
    assert extraction.edits[0].file == "front.tex"


def test_definitions_of_author_are_not_declarations(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\renewcommand{\\author}[1]{#1}\n\\let\\oldauthor\\author")
    assert extraction.findings == [] and extraction.edits == []


def test_keyword_environments_are_consumed_without_edit(tmp_path: Path):
    source = "\\begin{document}\\begin{IEEEkeywords}alpha, beta gamma; Zoë\\end{IEEEkeywords}"
    files = {"main.tex": f"\\documentclass{{article}}\n{source}\n\\end{{document}}\n"}
    extraction, _ = extract(tmp_path, files)
    assert extraction.metadata.keywords == ("alpha", "beta gamma", "Zoë")
    assert extraction.edits == [] and extraction.consumed_environments == {"IEEEkeywords"}


def test_keyword_command_is_rewritten_into_the_first_environment(tmp_path: Path):
    files = {"main.tex": "\\documentclass{article}\n\\begin{document}\nText \\keywords{one, two} more.\n\\end{document}\n"}
    extraction, graph = extract(tmp_path, files, environments=("kw", "other"), commands=("keywords",))
    (edit,) = extraction.edits
    assert edit.replacement == "\\begin{kw}one, two\\end{kw}" and edit.kind == "metadata-keywords"
    assert graph.files["main.tex"].text[edit.start : edit.end] == "\\keywords{one, two}"
    assert extraction.metadata.keywords == ("one", "two")
    assert "keywords" in extraction.consumed_commands and extraction.consumed_environments == {"kw", "other"}


def test_keyword_commands_without_an_environment_are_a_configuration_error(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\keywords{a}", environments=(), commands=("keywords",))
    assert "L2W-E-META-004" in codes(extraction) and extraction.edits == []


def test_math_keywords_are_left_to_pandoc(tmp_path: Path):
    files = {"main.tex": "\\documentclass{article}\n\\begin{document}\n\\begin{IEEEkeywords}$x$, y\\end{IEEEkeywords}\n\\end{document}\n"}
    extraction, _ = extract(tmp_path, files)
    assert extraction.metadata.keywords == () and "L2W-I-META-002" in codes(extraction)


def test_consumed_commands_cover_what_was_handled(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\title{T}")
    assert extraction.consumed_commands == {"title", "author", "date", "and"}
