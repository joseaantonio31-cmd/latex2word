"""The IEEE author-block adapter reads IEEEauthorblock macros and parbox column layouts."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_metadata_generic import codes, extract

from latex2word.diagnostics import Severity


def run(tmp_path: Path, argument: str):
    return extract(tmp_path, f"\\author{{{argument}}}", adapter="ieee-author-blocks")


def names(extraction) -> list[str]:
    return [a.name for a in extraction.metadata.authors]


def test_block_pairs_separated_by_and(tmp_path: Path):
    extraction, graph = run(
        tmp_path,
        "\\IEEEauthorblockN{Ada Example}\n\\IEEEauthorblockA{Example University\\\\Example City\\\\ada@example.org}\n"
        "\\and \\IEEEauthorblockN{Ben Sample}\n\\IEEEauthorblockA{Example University\\\\Example City\\\\ben@example.org}",
    )
    meta = extraction.metadata
    assert names(extraction) == ["Ada Example", "Ben Sample"]
    assert [a.email for a in meta.authors] == ["ada@example.org", "ben@example.org"]
    assert [a.lines for a in meta.affiliations] == [("Example University", "Example City")]
    assert [a.affiliation_ids for a in meta.authors] == [("a1",), ("a1",)]
    assert not any(f.severity is Severity.ERROR for f in extraction.findings)
    (edit,) = extraction.edits
    assert graph.files["main.tex"].text[edit.start : edit.end].startswith("\\author{\\IEEEauthorblockN")


def test_concatenated_blocks_and_missing_affiliation(tmp_path: Path):
    extraction, _ = run(
        tmp_path,
        "\\IEEEauthorblockN{Ada Example}\\IEEEauthorblockA{Lab One}"
        "\\IEEEauthorblockN{Ben Sample}"
        "\\IEEEauthorblockN{Cy Third}\\IEEEauthorblockA{Lab Two\\\\cy@example.org}",
    )
    authors = extraction.metadata.authors
    assert [(a.name, a.email, a.affiliation_ids) for a in authors] == [
        ("Ada Example", None, ("a1",)), ("Ben Sample", None, ()), ("Cy Third", "cy@example.org", ("a2",)),
    ]


def test_blind_version_alone_is_an_author_without_affiliation(tmp_path: Path):
    extraction, _ = run(tmp_path, "\\IEEEauthorblockN{Blind version}")
    (author,) = extraction.metadata.authors
    assert author.name == "Blind version" and author.affiliation_ids == () and extraction.metadata.affiliations == ()
    assert len(extraction.edits) == 1


def test_author_refmark_is_unsupported(tmp_path: Path):
    extraction, _ = run(tmp_path, "\\IEEEauthorblockN{Ada\\IEEEauthorrefmark{1}}\\IEEEauthorblockA{Lab}")
    assert "L2W-E-META-002" in codes(extraction) and extraction.edits == []


def test_block_a_without_preceding_block_n_is_an_error(tmp_path: Path):
    extraction, _ = run(tmp_path, "\\IEEEauthorblockA{Lab}")
    assert "L2W-E-META-002" in codes(extraction)


def cell(name: str, extra: str = "") -> str:
    return (
        f"\\parbox[t]{{0.3\\textwidth}}{{\\centering {{\\normalsize {name}}}\\\\\n"
        f"{{\\fontsize{{8.5}}{{9.5}}\\selectfont\\itshape Example University\\\\Example City{extra}}}}}"
    )


@pytest.mark.parametrize("count", [1, 2, 3, 4, 6])
def test_parbox_columns_with_makebox_and_hfill(tmp_path: Path, count: int):
    people = [f"Author Number{'x' * i}" for i in range(count)]
    cells = "%\n\\hfill ".join(cell(p, f"\\\\p{i}@example.org") for i, p in enumerate(people))
    extraction, _ = run(tmp_path, f"\\makebox[\\textwidth][c]{{{cells}}}")
    assert names(extraction) == people
    assert [a.email for a in extraction.metadata.authors] == [f"p{i}@example.org" for i in range(count)]
    assert len(extraction.metadata.affiliations) == 1
    assert extraction.metadata.affiliations[0].lines == ("Example University", "Example City")
    assert not any(f.severity is Severity.ERROR for f in extraction.findings), extraction.findings
    assert len(extraction.edits) == 1


def test_parbox_cells_without_makebox_and_unicode(tmp_path: Path):
    extraction, _ = run(tmp_path, cell("Zoë Örnek") + "\\hfill\n% comment\n" + cell("Nuño Ibáñez"))
    assert names(extraction) == ["Zoë Örnek", "Nuño Ibáñez"]


def test_cell_with_only_an_address_has_no_name(tmp_path: Path):
    extraction, _ = run(tmp_path, "\\parbox{3cm}{x@example.org\\\\Lab}")
    (author,) = extraction.metadata.authors
    assert (author.name, author.email, author.affiliation_ids) == ("", "x@example.org", ("a1",))


@pytest.mark.parametrize(
    "argument",
    [
        "\\IEEEauthorblockN{Ada} stray words",
        "\\IEEEauthorblockN{Ada} \\mystery{Lab}",
        "\\parbox{3cm}{Ada} \\footnote{x}",
        "\\parbox{3cm}{Ada\\thanks{x}}",
        "\\IEEEauthorblockN{Ada}\\IEEEauthorblockA{Lab $x$}",
        "\\parbox{3cm}{Ada",
    ],
)
def test_unknown_text_in_author_is_an_error(tmp_path: Path, argument: str):
    extraction, _ = run(tmp_path, argument)
    assert "L2W-E-META-002" in codes(extraction)
    assert extraction.edits == []


def test_layout_only_content_outside_blocks_is_fine(tmp_path: Path):
    extraction, _ = run(tmp_path, "{\\centering \\IEEEauthorblockN{Ada}\\hfill\\vspace{2pt}}")
    assert names(extraction) == ["Ada"]
    assert not any(f.severity is Severity.ERROR for f in extraction.findings)


def test_consumed_commands_include_the_ieee_macros(tmp_path: Path):
    extraction, _ = run(tmp_path, "\\IEEEauthorblockN{Ada}")
    assert {"IEEEauthorblockN", "IEEEauthorblockA", "parbox", "makebox", "hfill", "author", "and"} <= extraction.consumed_commands
    assert extraction.consumed_environments == {"IEEEkeywords"}


def test_generic_adapter_does_not_understand_ieee_blocks(tmp_path: Path):
    extraction, _ = extract(tmp_path, "\\author{\\IEEEauthorblockN{Ada}}")
    assert "L2W-E-META-002" in codes(extraction)
