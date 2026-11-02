"""Publisher-neutral front-matter metadata.

Nothing in this model is specific to a publisher: an IEEE author block, an
Elsevier author-affiliation list and a plain ``\\author{A \\and B}`` all
populate the same structures. Every optional field may be ``None``/empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Affiliation:
    id: str
    organization: str | None = None
    department: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    lines: tuple[str, ...] = ()  # verbatim display lines when the source gives free-form text

    def display_lines(self) -> tuple[str, ...]:
        """The lines to print: the verbatim ones, else the structured fields in reading order."""
        if self.lines:
            return self.lines
        place = ", ".join(part for part in (self.city, self.region) if part)
        return tuple(part for part in (self.department, self.organization, place, self.country) if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "organization": self.organization, "department": self.department, "city": self.city,
            "region": self.region, "country": self.country, "lines": list(self.lines),
        }


@dataclass(frozen=True)
class Author:
    name: str  # display name, Unicode exactly as written
    affiliation_ids: tuple[str, ...] = ()
    email: str | None = None
    orcid: str | None = None
    corresponding: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "affiliation_ids": list(self.affiliation_ids), "email": self.email,
            "orcid": self.orcid, "corresponding": self.corresponding,
        }


@dataclass(frozen=True)
class DocumentMetadata:
    title: str | None = None
    subtitle: str | None = None
    authors: tuple[Author, ...] = ()
    affiliations: tuple[Affiliation, ...] = ()
    abstract: str | None = None
    keywords: tuple[str, ...] = ()
    date: str | None = None
    acknowledgments: str | None = None
    extra_emails: tuple[str, ...] = field(default=())

    @property
    def emails(self) -> tuple[str, ...]:
        found = [a.email for a in self.authors if a.email]
        found.extend(e for e in self.extra_emails if e not in found)
        return tuple(found)

    @property
    def corresponding_author(self) -> Author | None:
        return next((a for a in self.authors if a.corresponding), None)

    def affiliation(self, affiliation_id: str) -> Affiliation | None:
        return next((a for a in self.affiliations if a.id == affiliation_id), None)

    def author_affiliations(self, author: Author) -> tuple[Affiliation, ...]:
        return tuple(a for a in (self.affiliation(i) for i in author.affiliation_ids) if a is not None)

    def with_document_text(
        self, *, title: str | None = None, abstract: str | None = None, keywords: tuple[str, ...] | None = None
    ) -> DocumentMetadata:
        """Fill the fields that only the Pandoc stage can see (rendered title/abstract/keyword text)."""
        return replace(
            self,
            title=title if title is not None else self.title,
            abstract=abstract if abstract is not None else self.abstract,
            keywords=keywords if keywords is not None else self.keywords,
        )

    def counts(self) -> dict[str, int]:
        return {
            "title": int(bool(self.title)), "authors": len(self.authors), "affiliations": len(self.affiliations),
            "emails": len(self.emails), "abstract": int(bool(self.abstract)), "keywords": len(self.keywords),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "subtitle": self.subtitle, "date": self.date,
            "authors": [a.to_dict() for a in self.authors], "affiliations": [a.to_dict() for a in self.affiliations],
            "abstract": self.abstract, "keywords": list(self.keywords), "acknowledgments": self.acknowledgments,
            "counts": self.counts(),
        }
