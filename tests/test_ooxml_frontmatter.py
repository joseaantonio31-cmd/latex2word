from __future__ import annotations

import io
import zipfile

import pytest
from lxml import etree

from latex2word.layout import AuthorBlockLayout, FrontmatterConfig, LabelledBlock
from latex2word.metadata.model import Affiliation, Author, DocumentMetadata
from latex2word.ooxml.frontmatter import (
    apply_core_properties,
    apply_frontmatter,
    rendered_front_text,
)
from latex2word.ooxml.namespaces import NS, qn, text_of, w, xpath
from latex2word.ooxml.package import OpcPackage, parse_xml
from latex2word.profiles import load_profile

LAYOUT = load_profile("ieee-conference").layout
W_NS, M_NS = NS["w"], NS["m"]


def config(
    author_layout: str = "stacked", max_columns: int = 4, *, affiliation: bool = True, email: bool = True,
    abstract: LabelledBlock | None = None, keywords: LabelledBlock | None = None, show_date: bool = False,
) -> FrontmatterConfig:
    return FrontmatterConfig(
        metadata_adapter="generic",
        author_block=AuthorBlockLayout(author_layout, max_columns, affiliation, email),
        abstract=abstract or LabelledBlock("run-in", "Summary", ": "),
        keywords=keywords or LabelledBlock("run-in", "Tags", " - "),
        show_date=show_date,
    )


def para(style: str, text: str = "") -> str:
    run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{run}</w:p>'


def build(*children: str) -> etree._Element:
    xml = f'<w:document xmlns:w="{W_NS}" xmlns:m="{M_NS}"><w:body>{"".join(children)}<w:sectPr/></w:body></w:document>'
    return parse_xml(xml.encode())


ABSTRACT = (
    '<w:p><w:pPr><w:pStyle w:val="Abstract"/></w:pPr><w:r><w:t>Energy is </w:t></w:r>'
    '<m:oMath><m:r><m:t>E=mc</m:t></m:r></m:oMath><w:r><w:t> here.</w:t></w:r></w:p>'
)


def people(count: int, *, email: bool = True, affiliation: bool = True) -> DocumentMetadata:
    names = ["Ada Example", "Zoë Örnek", "Nuño Ibáñez", "Bo Sample", "Cy Fictional", "Di Placeholder"]
    authors = tuple(
        Author(names[i], affiliation_ids=(f"a{i}",) if affiliation else (), email=f"p{i}@example.org" if email else None)
        for i in range(count)
    )
    affiliations = tuple(
        Affiliation(f"a{i}", organization=f"Example University {i}", department="Dept. of Examples",
                    city="Sampleville", country="Exampleland")
        for i in range(count)
    )
    return DocumentMetadata(title="A Title", authors=authors, affiliations=affiliations)


def body_children(document: etree._Element) -> list[etree._Element]:
    return list(document.find(w("body")))


def styles_of(document: etree._Element) -> list[str]:
    out = []
    for child in body_children(document):
        if child.tag == w("p"):
            node = child.find(f"{w('pPr')}/{w('pStyle')}")
            out.append(node.get(w("val")) if node is not None else "")
        else:
            out.append(etree.QName(child).localname)
    return out


def base_document() -> etree._Element:
    return build(para("Title", "A Title"), para("Author"), para("Date", "2026"), para("AbstractTitle", "Abstract"),
                 ABSTRACT, para("IndexTerms", "alpha, beta"), para("Heading1", "I. Intro"))


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6])
def test_stacked_authors(count: int) -> None:
    document, metadata = base_document(), people(count)
    outcome, findings = apply_frontmatter(document, metadata, LAYOUT, config("stacked"))
    assert findings == []
    assert outcome.author_layout == "stacked" and outcome.author_count_rendered == count
    assert outcome.author_names == tuple(a.name for a in metadata.authors)
    assert not document.findall(f".//{w('tbl')}")
    styles = styles_of(document)
    assert styles[:1 + 3 * count] == ["Title"] + ["Author", "AuthorAffiliation", "AuthorEmail"] * count
    texts = rendered_front_text(document)
    assert texts["layout"] == "stacked" and len(texts["authors"]) == count
    for author, text in zip(metadata.authors, texts["authors"]):
        assert author.name in text and author.email in text and "Example University" in text
    assert outcome.removed_author_paragraphs == 0


@pytest.mark.parametrize(
    ("count", "rows", "cols"), [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 1, 4), (5, 2, 3), (6, 2, 3)]
)
def test_columns_authors(count: int, rows: int, cols: int) -> None:
    document, metadata = base_document(), people(count)
    outcome, _ = apply_frontmatter(document, metadata, LAYOUT, config("columns", 4))
    assert (outcome.author_rows, outcome.author_columns, outcome.author_layout) == (rows, cols, "columns")
    tables = document.findall(f".//{w('tbl')}")
    assert len(tables) == 1
    table = tables[0]
    assert table.find(f"{w('tblPr')}/{w('tblStyle')}").get(w("val")) == "AuthorBlock"
    assert table.find(f"{w('tblPr')}/{w('tblLayout')}").get(w("type")) == "fixed"
    assert table.find(f"{w('tblPr')}/{w('jc')}").get(w("val")) == "center"
    width = int(table.find(f"{w('tblPr')}/{w('tblW')}").get(w("w")))
    assert width == LAYOUT.frontmatter_section.column_width.twips
    grid = [int(c.get(w("w"))) for c in table.findall(f"{w('tblGrid')}/{w('gridCol')}")]
    assert len(grid) == cols and sum(grid) == width
    assert len(table.findall(w("tr"))) == rows
    for row in table.findall(w("tr")):
        assert row.find(f"{w('trPr')}/{w('cantSplit')}") is not None
        assert len(row.findall(w("tc"))) == cols
        for cell, expected in zip(row.findall(w("tc")), grid):
            assert int(cell.find(f"{w('tcPr')}/{w('tcW')}").get(w("w"))) == expected
            assert cell.find(f"{w('tcPr')}/{w('vAlign')}").get(w("val")) == "top"
    following = table.getnext()
    assert following is not None and following.tag == w("p")
    texts = rendered_front_text(document)
    assert texts["layout"] == "columns" and len(texts["authors"]) == count
    whole = "\n".join(texts["authors"])
    for author in metadata.authors:
        assert whole.count(author.name) == 1 and whole.count(author.email) == 1
    padding = rows * cols - count
    empty = [c for c in table.iter(w("tc")) if not text_of(c).strip()]
    assert len(empty) == padding
    for cell in empty:
        assert len(cell.findall(w("p"))) == 1


def test_table_after_title_and_followed_by_paragraph_even_when_next_is_table() -> None:
    document = build(para("Title", "T"), "<w:tbl/>")
    apply_frontmatter(document, people(2), LAYOUT, config("columns"))
    kinds = styles_of(document)
    assert kinds[:4] == ["Title", "tbl", "", "tbl"]
    for table in document.findall(f"{w('body')}/{w('tbl')}"):
        nxt = table.getnext()
        assert nxt is not None and nxt.tag != w("tbl")


def test_unicode_and_missing_optionals() -> None:
    document = base_document()
    metadata = DocumentMetadata(authors=(Author("Zoë Örnek"), Author("Nuño Ibáñez", email="nuno@example.org")))
    outcome, _ = apply_frontmatter(document, metadata, LAYOUT, config("stacked"))
    assert outcome.author_names == ("Zoë Örnek", "Nuño Ibáñez")
    assert styles_of(document)[:4] == ["Title", "Author", "Author", "AuthorEmail"]
    assert [text_of(p) for p in xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Author']")] == ["Zoë Örnek", "Nuño Ibáñez"]
    assert xpath(document, "//w:t[@xml:space='preserve']")


def test_toggles_hide_affiliation_and_email() -> None:
    document = base_document()
    apply_frontmatter(document, people(2), LAYOUT, config("stacked", affiliation=False, email=False))
    assert "AuthorAffiliation" not in styles_of(document) and "AuthorEmail" not in styles_of(document)


def test_affiliation_lines_use_breaks() -> None:
    document = base_document()
    metadata = DocumentMetadata(
        authors=(Author("Ada Example", affiliation_ids=("a", "b")),),
        affiliations=(Affiliation("a", lines=("Line One", "Line Two")), Affiliation("b", organization="Example University")),
    )
    apply_frontmatter(document, metadata, LAYOUT, config("stacked"))
    affiliation = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='AuthorAffiliation']")[0]
    assert len(affiliation.findall(f".//{w('br')}")) == 2
    assert text_of(affiliation) == "Line One\nLine Two\nExample University"


def test_no_authors_inserts_nothing() -> None:
    document = base_document()
    outcome, findings = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config("columns"))
    assert outcome.author_layout == "none" and outcome.author_count_rendered == 0
    assert "tbl" not in styles_of(document)
    assert styles_of(document) == ["Title", "Abstract", "IndexTerms", "Heading1", "sectPr"]
    assert findings == []
    assert rendered_front_text(document) == {"layout": "none", "authors": []}


def test_missing_title_warns_and_inserts_at_start() -> None:
    document = build(ABSTRACT)
    outcome, findings = apply_frontmatter(document, people(1), LAYOUT, config("stacked"))
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-040"]
    assert not outcome.title_present
    assert styles_of(document)[0] == "Author"


def test_title_recorded() -> None:
    outcome, _ = apply_frontmatter(base_document(), people(1), LAYOUT, config())
    assert outcome.title_present and outcome.title_text == "A Title"


def test_author_paragraphs_removed_and_unknown_name_warned() -> None:
    document = build(para("Title", "T"), para("Author", "Ada Example"), para("Author", "Stranger Danger"),
                     para("Author"), para("Heading1", "I. Intro"), para("Author", "kept later"))
    outcome, findings = apply_frontmatter(document, people(1), LAYOUT, config("stacked", affiliation=False, email=False))
    assert outcome.removed_author_paragraphs == 2
    assert [f.code for f in findings] == ["L2W-W-LAYOUT-041"]
    assert "Stranger Danger" in findings[0].message
    texts = [text_of(p) for p in xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Author']")]
    assert texts == ["Ada Example", "kept later"]


def test_known_author_paragraph_does_not_warn() -> None:
    document = build(para("Title", "T"), para("Author", "Ada Example"), para("Heading1", "I."))
    _, findings = apply_frontmatter(document, people(1), LAYOUT, config())
    assert findings == []


@pytest.mark.parametrize("show", [True, False])
def test_date(show: bool) -> None:
    document = base_document()
    outcome, _ = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config(show_date=show))
    assert ("Date" in styles_of(document)) is show
    assert outcome.date_hidden is (not show)


def test_abstract_run_in_preserves_math_and_uses_config_label() -> None:
    document = base_document()
    math_before = document.findall(f".//{{{M_NS}}}oMath")
    runs_before = [r for r in xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Abstract']/w:r")]
    outcome, _ = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config())
    assert "AbstractTitle" not in styles_of(document)
    abstract = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Abstract']")[0]
    assert abstract[0].tag == w("pPr")
    label = abstract[1]
    assert label.find(f"{w('rPr')}/{w('rStyle')}").get(w("val")) == "AbstractLabel"
    assert label.find(w("t")).text == "Summary: " and label.find(w("t")).get(qn("xml:space")) == "preserve"
    math_after = document.findall(f".//{{{M_NS}}}oMath")
    assert len(math_after) == len(math_before) == 1 and math_after[0] is math_before[0]
    assert list(abstract)[2:4] and [r for r in abstract.findall(w("r"))][1:] == runs_before
    assert outcome.abstract_present and outcome.abstract_layout == "run-in"
    assert "Abstract" not in text_of(document)


def test_abstract_run_in_keeps_title_bookmarks() -> None:
    title = ('<w:p><w:pPr><w:pStyle w:val="AbstractTitle"/></w:pPr><w:bookmarkStart w:id="7" w:name="abs"/>'
             '<w:r><w:t>Abstract</w:t></w:r><w:bookmarkEnd w:id="7"/></w:p>')
    document = build(para("Title", "T"), title, ABSTRACT)
    apply_frontmatter(document, DocumentMetadata(), LAYOUT, config())
    abstract = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Abstract']")[0]
    assert [etree.QName(c).localname for c in abstract[:4]] == ["pPr", "bookmarkStart", "bookmarkEnd", "r"]


def test_abstract_heading_layout() -> None:
    document = base_document()
    block = LabelledBlock("heading", "Summary", ": ")
    outcome, _ = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config(abstract=block))
    title = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='AbstractTitle']")[0]
    assert text_of(title) == "Summary" and len(title.findall(w("r"))) == 1
    assert title[0].tag == w("pPr")
    abstract = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='Abstract']")[0]
    assert text_of(abstract) == "Energy is  here."
    assert len(abstract.findall(f".//{{{M_NS}}}oMath")) == 1
    assert outcome.abstract_layout == "heading"


def test_abstract_heading_created_when_missing() -> None:
    document = build(para("Title", "T"), ABSTRACT)
    apply_frontmatter(document, DocumentMetadata(), LAYOUT, config(abstract=LabelledBlock("heading", "Summary", "")))
    assert styles_of(document) == ["Title", "AbstractTitle", "Abstract", "sectPr"]


def test_keywords_run_in_and_heading() -> None:
    document = base_document()
    outcome, _ = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config())
    keywords = xpath(document, "//w:p[w:pPr/w:pStyle/@w:val='IndexTerms']")[0]
    label = keywords[1]
    assert label.find(f"{w('rPr')}/{w('rStyle')}").get(w("val")) == "KeywordsLabel"
    assert label.find(w("t")).text == "Tags - "
    assert outcome.keywords_present and outcome.keywords_layout == "run-in"

    document = base_document()
    apply_frontmatter(document, DocumentMetadata(), LAYOUT, config(keywords=LabelledBlock("heading", "Tags", "")))
    styles = styles_of(document)
    index = styles.index("IndexTerms")
    assert styles[index - 1] == "AbstractTitle"
    assert text_of(body_children(document)[index - 1]) == "Tags"


def test_missing_abstract_and_keywords() -> None:
    document = build(para("Title", "T"), para("Heading1", "I."))
    outcome, findings = apply_frontmatter(document, DocumentMetadata(), LAYOUT, config())
    assert not outcome.abstract_present and not outcome.keywords_present and findings == []
    assert styles_of(document) == ["Title", "Heading1", "sectPr"]


def test_body_bookmarks_stay_in_place() -> None:
    document = build(para("Title", "T"), '<w:bookmarkStart w:id="1" w:name="sec"/>', para("Heading1", "I."),
                     '<w:bookmarkEnd w:id="1"/>')
    apply_frontmatter(document, people(2), LAYOUT, config("columns"))
    kinds = styles_of(document)
    assert kinds.index("bookmarkStart") < kinds.index("Heading1") < kinds.index("bookmarkEnd")


def test_outcome_to_dict() -> None:
    outcome, _ = apply_frontmatter(base_document(), people(2), LAYOUT, config("columns"))
    data = outcome.to_dict()
    assert data["author_names"] == ["Ada Example", "Zoë Örnek"]
    assert data["last_frontmatter_element_hint"] is None
    assert data["author_rows"] == 1 and data["author_columns"] == 2


def core_package(creator: str = "", title: str = "") -> OpcPackage:
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
        f"<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>"
        '<dcterms:created>2020-01-01T00:00:00Z</dcterms:created></cp:coreProperties>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docProps/core.xml", core)
    return OpcPackage.load(buffer.getvalue())


def test_core_properties() -> None:
    package = core_package()
    apply_core_properties(package, people(2))
    root = package.xml("docProps/core.xml")
    assert root.find(qn("dc:creator")).text == "Ada Example; Zoë Örnek"
    assert root.find(qn("dc:title")).text == "A Title"
    assert root.find("{http://purl.org/dc/terms/}created").text == "2020-01-01T00:00:00Z"


def test_core_properties_keep_existing_title_and_missing_part() -> None:
    package = core_package(title="Existing")
    apply_core_properties(package, people(1))
    assert package.xml("docProps/core.xml").find(qn("dc:title")).text == "Existing"
    empty = OpcPackage({})
    apply_core_properties(empty, people(1))
    assert not empty.has("docProps/core.xml")


def test_generic_profile_single_column_width() -> None:
    layout = load_profile("generic").layout
    document = base_document()
    apply_frontmatter(document, people(3), layout, config("columns"))
    table = document.find(f".//{w('tbl')}")
    assert int(table.find(f"{w('tblPr')}/{w('tblW')}").get(w("w"))) == layout.page.text_width.twips
