"""Metadata adapter registry.

An adapter turns the front-matter declarations of a LaTeX project into a
:class:`~latex2word.metadata.model.DocumentMetadata`. Adapters are chosen
explicitly by the profile (``frontmatter.metadata_adapter``), never by looking
at the document class. An adapter also reports which commands and environments
it consumed (so the unknown-construct probe does not flag them) and the staged
copy edits that remove or rewrite them (so Pandoc never sees a construct it
would silently drop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from latex2word.errors import ConfigError
from latex2word.metadata.model import DocumentMetadata
from latex2word.rewrite import Edit
from latex2word.source_graph import Finding, SourceGraph

if TYPE_CHECKING:
    from latex2word.layout import FrontmatterConfig


@dataclass
class MetadataExtraction:
    """What one adapter run produced."""

    adapter: str
    metadata: DocumentMetadata = field(default_factory=DocumentMetadata)
    findings: list[Finding] = field(default_factory=list)
    consumed_commands: frozenset[str] = frozenset()
    consumed_environments: frozenset[str] = frozenset()
    edits: list[Edit] = field(default_factory=list)  # staged-copy edits (metadata commands removed/rewritten)


@runtime_checkable
class MetadataAdapter(Protocol):
    name: str

    def extract(self, graph: SourceGraph, frontmatter: FrontmatterConfig) -> MetadataExtraction: ...


_REGISTRY: dict[str, MetadataAdapter] = {}


def register_adapter(adapter: MetadataAdapter) -> None:
    if adapter.name in _REGISTRY and _REGISTRY[adapter.name] is not adapter:
        raise ValueError(f"metadata adapter {adapter.name!r} is already registered")
    _REGISTRY[adapter.name] = adapter


def available_adapters() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def get_adapter(name: str) -> MetadataAdapter:
    _load_builtin()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ConfigError(f"unknown metadata adapter {name!r}; available: {known}") from None


_ADAPTER_SKIP = frozenset({"adapters", "model", "tex_text"})


def _load_builtin() -> None:
    """Import every adapter module of the package so each registers itself (no per-publisher import here).

    Adding a publisher adapter is therefore a new module in ``latex2word.metadata`` that calls
    :func:`register_adapter` - no generic module changes.
    """
    import importlib
    import pkgutil

    import latex2word.metadata as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.name not in _ADAPTER_SKIP and not module.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{module.name}")

