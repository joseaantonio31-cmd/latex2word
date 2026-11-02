"""Stable Word style identifiers (``w:styleId``) used by the layout subsystem.

Styles are always addressed by id, never by their (localisable) display name.
``STYLE_NAMES`` gives the display name written into ``styles.xml`` when a style
has to be created. Pandoc derives ids from ``custom-style`` names by dropping
spaces, so ``"Equation Number"`` becomes :data:`EQUATION_NUMBER`.
"""

from __future__ import annotations

NORMAL = "Normal"
BODY_TEXT = "BodyText"
FIRST_PARAGRAPH = "FirstParagraph"
COMPACT = "Compact"
TITLE = "Title"
SUBTITLE = "Subtitle"
AUTHOR = "Author"
AUTHOR_AFFILIATION = "AuthorAffiliation"
AUTHOR_EMAIL = "AuthorEmail"
DATE = "Date"
ABSTRACT_TITLE = "AbstractTitle"
ABSTRACT = "Abstract"
ABSTRACT_LABEL = "AbstractLabel"  # character style
INDEX_TERMS = "IndexTerms"
KEYWORDS_LABEL = "KeywordsLabel"  # character style
HEADING = ("Heading1", "Heading2", "Heading3", "Heading4", "Heading5", "Heading6")
IMAGE_CAPTION = "ImageCaption"
TABLE_CAPTION = "TableCaption"
CAPTIONED_FIGURE = "CaptionedFigure"
CENTERED = "Centered"  # paragraphs of a center environment (custom-style "Centered")
COMPACT_UNCAPTIONED = "CompactUncaptioned"  # cells of tables without a caption (lists of abbreviations, ...)
CAPTION_NOTE = "CaptionNote"  # note block of a figure or table (source note macros / groups)
TOC_LEVELS = tuple(f"TOC{n}" for n in range(1, 10))  # Word built-in "toc N" styles of TOC field results
UNLISTED_SUFFIX = "Unlisted"  # Heading<n>Unlisted: looks like the heading, outline level body text (not in the TOC)
BIBLIOGRAPHY = "Bibliography"
EQUATION = "Equation"
EQUATION_NUMBER = "EquationNumber"
EQUATION_SPACER = "EquationSpacer"
TABLE = "Table"  # table style of data tables (Pandoc)
TABLE_EQUATION_LAYOUT = "EquationLayout"  # table style of the borderless equation layout table
TABLE_AUTHOR_BLOCK = "AuthorBlock"  # table style of the borderless author block table
TABLE_IMAGE_LAYOUT = "ImageLayout"  # table style of a borderless grid of images (e.g. logos side by side)

LAYOUT_TABLE_STYLES = frozenset({TABLE_EQUATION_LAYOUT, TABLE_AUTHOR_BLOCK, TABLE_IMAGE_LAYOUT})

STYLE_NAMES: dict[str, str] = {
    NORMAL: "Normal", BODY_TEXT: "Body Text", FIRST_PARAGRAPH: "First Paragraph", COMPACT: "Compact",
    TITLE: "Title", SUBTITLE: "Subtitle", AUTHOR: "Author", AUTHOR_AFFILIATION: "Author Affiliation",
    AUTHOR_EMAIL: "Author Email", DATE: "Date", ABSTRACT_TITLE: "Abstract Title", ABSTRACT: "Abstract",
    ABSTRACT_LABEL: "Abstract Label", INDEX_TERMS: "Index Terms", KEYWORDS_LABEL: "Keywords Label",
    "Heading1": "heading 1", "Heading2": "heading 2", "Heading3": "heading 3", "Heading4": "heading 4",
    "Heading5": "heading 5", "Heading6": "heading 6",
    IMAGE_CAPTION: "Image Caption", TABLE_CAPTION: "Table Caption", CAPTIONED_FIGURE: "Captioned Figure",
    BIBLIOGRAPHY: "Bibliography", EQUATION: "Equation", EQUATION_NUMBER: "Equation Number",
    EQUATION_SPACER: "Equation Spacer", TABLE: "Table", TABLE_EQUATION_LAYOUT: "Equation Layout",
    TABLE_AUTHOR_BLOCK: "Author Block", TABLE_IMAGE_LAYOUT: "Image Layout", CENTERED: "Centered", COMPACT_UNCAPTIONED: "Compact Uncaptioned", CAPTION_NOTE: "Caption Note",
    **{f"TOC{n}": f"toc {n}" for n in range(1, 10)},
}

# profile ``layout.typography`` role -> the Word style ids that carry it (first entry is the primary style)
ROLE_STYLES: dict[str, tuple[str, ...]] = {
    "default": (NORMAL,),
    "body": (BODY_TEXT, FIRST_PARAGRAPH),
    "title": (TITLE,),
    "subtitle": (SUBTITLE,),
    "author": (AUTHOR,),
    "affiliation": (AUTHOR_AFFILIATION,),
    "email": (AUTHOR_EMAIL,),
    "date": (DATE,),
    "abstract_title": (ABSTRACT_TITLE,),
    "abstract": (ABSTRACT,),
    "abstract_label": (ABSTRACT_LABEL,),
    "keywords": (INDEX_TERMS,),
    "keywords_label": (KEYWORDS_LABEL,),
    "heading1": ("Heading1",),
    "heading2": ("Heading2",),
    "heading3": ("Heading3",),
    "figure_caption": (IMAGE_CAPTION,),
    "table_caption": (TABLE_CAPTION,),
    "table_text": (COMPACT,),
    "bibliography": (BIBLIOGRAPHY,),
    "equation": (EQUATION, EQUATION_SPACER),
    "equation_number": (EQUATION_NUMBER,),
    "centered": (CENTERED,),
    "table_text_uncaptioned": (COMPACT_UNCAPTIONED,),
    "note": (CAPTION_NOTE,),
    **{f"toc{n}": (f"TOC{n}",) for n in range(1, 10)},
}

# styles the final document must define (the layout code refers to them)
REQUIRED_STYLES: tuple[str, ...] = (
    NORMAL, BODY_TEXT, FIRST_PARAGRAPH, COMPACT, TITLE, ABSTRACT, INDEX_TERMS, "Heading1", "Heading2", IMAGE_CAPTION,
    TABLE_CAPTION, BIBLIOGRAPHY, EQUATION, EQUATION_NUMBER, EQUATION_SPACER, TABLE, TABLE_EQUATION_LAYOUT,
)

CHARACTER_STYLES = frozenset({ABSTRACT_LABEL, KEYWORDS_LABEL})
