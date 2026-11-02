"""Conversion profiles: load, schema-validate, resolve ``extends`` and check consistency.

A profile is *semantic policy* (numbering, cross-reference text, bibliography
policy, accepted-loss lists) plus declarative *layout* data (page geometry,
columns, typography, front-matter strategy, reference document). It never depends on the current working directory
and is never inferred from a document class: the configuration names it.

Lookup order for an identifier: every configured ``profile_dirs`` entry in order
(``<dir>/<id>.yaml``), then the profiles packaged with latex2word.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from latex2word.config import parse_yaml
from latex2word.errors import ConfigIssue, ProfileError
from latex2word.layout import (
    FrontmatterConfig,
    ReferenceDocxConfig,
    ResolvedLayout,
    resolve_frontmatter,
    resolve_layout,
    resolve_reference_docx,
)
from latex2word.logging_utils import log_event
from latex2word.metadata.adapters import available_adapters
from latex2word.numerals import FORCED_STYLE_FIELDS, template_fields, template_problems

_log = logging.getLogger(__name__)

SUPPORTED_PROFILE_SCHEMA_VERSIONS: tuple[int, ...] = (1,)
MAX_EXTENDS_DEPTH = 8
SECTION_LEVELS = ("level1", "level2", "level3", "level4", "level5", "level6")
COUNTER_KINDS = ("figures", "tables", "equations")

# Options the engine owns; profiles may not smuggle them in through pandoc.extra_args.
_RESERVED_PANDOC_OPTIONS = frozenset(
    {
        "--filter", "-F", "--lua-filter", "-L", "--defaults", "-d", "--output", "-o",
        "--from", "-f", "-r", "--read", "--to", "-t", "-w", "--write", "--bibliography",
        "--csl", "--citeproc", "-C", "--reference-doc", "--resource-path", "--metadata-file",
        "--extract-media", "--data-dir", "--include-in-header", "-H", "--include-before-body",
        "-B", "--include-after-body", "-A", "--template", "--print-default-data-file",
    }
)

_COMPLETE_PATHS: tuple[tuple[str, ...], ...] = (
    ("required_tools",),
    ("optional_tools",),
    ("numbering", "sections", "enabled"),
    ("numbering", "sections", "separator"),
    *(("numbering", "sections", "level1", key) for key in ("numeral", "format", "ref", "reference")),
    *(
        ("numbering", kind, key)
        for kind in COUNTER_KINDS
        for key in ("enabled", "numeral", "format", "separator", "ref", "reference", "reset")
    ),
    ("bibliography", "mode"),
    ("bibliography", "heading"),
    ("bibliography", "manual", "citation_format"),
    ("bibliography", "manual", "number_separator"),
    ("bibliography", "manual", "entry_label"),
    ("pandoc", "extra_args"),
    ("tex", "accepted_commands"),
    ("tex", "accepted_environments"),
)

# Layout data every resolved profile carries. A profile that does not extend ``generic`` (and so does not define
# them) inherits the built-in generic values: layout is optional in a profile file, complete in a resolved one.
_LAYOUT_KEYS = ("reference_docx", "layout", "frontmatter")


@dataclass(frozen=True)
class Profile:
    """A fully resolved profile (``extends`` chain merged)."""

    id: str
    display_name: str
    chain: tuple[str, ...]  # root ... self
    data: Mapping[str, Any]  # merged policy; treat as read-only
    csl: Path | None = None  # resolved bibliography.csl, if any
    layout: ResolvedLayout | None = None
    frontmatter: FrontmatterConfig | None = None
    reference_docx: ReferenceDocxConfig | None = None
    reference_docx_path: Path | None = None  # resolved absolute path when reference_docx.mode is external

    @property
    def numbering(self) -> Mapping[str, Any]:
        return self.data["numbering"]

    @property
    def bibliography(self) -> Mapping[str, Any]:
        return self.data["bibliography"]

    @property
    def required_tools(self) -> tuple[str, ...]:
        return tuple(self.data["required_tools"])

    @property
    def optional_tools(self) -> tuple[str, ...]:
        return tuple(self.data["optional_tools"])

    def content_hash(self) -> str:
        """SHA-256 of the canonical resolved profile (the identity recorded in the build manifest)."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "chain": list(self.chain),
            **copy.deepcopy(dict(self.data)),
        }


@cache
def load_profile_schema() -> dict[str, Any]:
    text = resources.files("latex2word").joinpath("schemas", "profile.schema.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return schema


def _issue_path(parts: Any) -> str:
    return ".".join(str(part) for part in parts)


@dataclass(frozen=True)
class _Source:
    profile_id: str
    origin: str  # human-readable (file path or "built-in")
    text: str
    directory: Path | None


def _find_source(profile_id: str, search_dirs: Sequence[Path]) -> _Source | None:
    for directory in search_dirs:
        candidate = Path(directory) / f"{profile_id}.yaml"
        if candidate.is_file():
            try:
                return _Source(profile_id, str(candidate), candidate.read_text(encoding="utf-8-sig"), candidate.parent)
            except (OSError, UnicodeDecodeError) as exc:
                raise ProfileError(f"cannot read profile {candidate}: {exc}") from exc
    resource = resources.files("latex2word").joinpath("profiles", f"{profile_id}.yaml")
    if resource.is_file():
        return _Source(profile_id, f"built-in:{profile_id}", resource.read_text(encoding="utf-8-sig"), None)
    return None


def available_profiles(search_dirs: Sequence[Path] = ()) -> list[str]:
    """Identifiers resolvable with ``search_dirs`` (custom directories first, then built-in)."""
    found: set[str] = set()
    for directory in search_dirs:
        if Path(directory).is_dir():
            found.update(p.stem for p in Path(directory).glob("*.yaml"))
    found.update(
        Path(str(entry.name)).stem
        for entry in resources.files("latex2word").joinpath("profiles").iterdir()
        if str(entry.name).endswith(".yaml")
    )
    return sorted(found)


def _parse_file(source: _Source) -> dict[str, Any]:
    data = parse_yaml(source.text, source.origin)
    if not isinstance(data, dict):
        raise ProfileError(f"{source.origin}: top level must be a mapping")
    version = data.get("profile_schema_version")
    if isinstance(version, bool) or version not in SUPPORTED_PROFILE_SCHEMA_VERSIONS:
        supported = ", ".join(str(v) for v in SUPPORTED_PROFILE_SCHEMA_VERSIONS)
        raise ProfileError(
            f"{source.origin}: unsupported profile_schema_version {version!r}; supported: {supported}",
            [ConfigIssue("profile_schema_version", f"unsupported value {version!r}")],
        )
    validator = Draft202012Validator(load_profile_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: (_issue_path(e.absolute_path), e.message))
    if errors:
        raise ProfileError(
            f"{source.origin}: {len(errors)} problem(s) found",
            [ConfigIssue(_issue_path(e.absolute_path), e.message) for e in errors],
        )
    if data["id"] != source.profile_id:
        raise ProfileError(
            f"{source.origin}: id {data['id']!r} does not match the file name {source.profile_id!r}",
            [ConfigIssue("id", f"expected {source.profile_id!r}")],
        )
    return data


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_META_KEYS = ("profile_schema_version", "id", "display_name", "extends")


def _resolve_chain(profile_id: str, search_dirs: Sequence[Path]) -> list[tuple[_Source, dict[str, Any]]]:
    """Files from the root profile down to ``profile_id``."""
    chain: list[tuple[_Source, dict[str, Any]]] = []
    seen: list[str] = []
    current: str | None = profile_id
    while current is not None:
        if current in seen:
            raise ProfileError(f"profile 'extends' cycle: {' -> '.join([*seen, current])}")
        if len(seen) >= MAX_EXTENDS_DEPTH:
            raise ProfileError(f"profile 'extends' chain deeper than {MAX_EXTENDS_DEPTH}: {' -> '.join(seen)}")
        seen.append(current)
        source = _find_source(current, search_dirs)
        if source is None:
            known = ", ".join(available_profiles(search_dirs)) or "(none)"
            if len(seen) == 1:
                raise ProfileError(f"unknown profile {current!r}; available: {known}")
            raise ProfileError(f"profile {seen[-2]!r} extends unknown profile {current!r}; available: {known}")
        data = _parse_file(source)
        chain.append((source, data))
        current = data.get("extends")
    chain.reverse()
    return chain


def _get(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _section_fields(level: int) -> list[str]:
    return ["n", *[f"n{i}" for i in range(1, level)], *FORCED_STYLE_FIELDS]


def _consistency_issues(data: Mapping[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for path in _COMPLETE_PATHS:
        if _get(data, path) is None:
            issues.append(ConfigIssue(".".join(path), "missing from the resolved profile (extend 'generic' or define it)"))
    if issues:
        return issues

    numbering = data["numbering"]
    sections = numbering["sections"]
    previous_defined = True
    for index, key in enumerate(SECTION_LEVELS, start=1):
        level = sections.get(key)
        if level is None:
            previous_defined = False
            continue
        if not previous_defined:
            issues.append(ConfigIssue(f"numbering.sections.{key}", "defined but a shallower level is not"))
        for field in ("numeral", "format", "ref", "reference"):
            if field not in level:
                issues.append(ConfigIssue(f"numbering.sections.{key}.{field}", "missing"))
        for field in ("format", "ref", "reference"):
            for problem in template_problems(level.get(field, ""), _section_fields(index)):
                issues.append(ConfigIssue(f"numbering.sections.{key}.{field}", problem))
    appendix = sections.get("appendix")
    if appendix is not None:
        for field in ("numeral", "format", "ref", "reference"):
            if field not in appendix:
                issues.append(ConfigIssue(f"numbering.sections.appendix.{field}", "missing"))
        for field in ("format", "ref", "reference"):
            for problem in template_problems(appendix.get(field, ""), _section_fields(1)):
                issues.append(ConfigIssue(f"numbering.sections.appendix.{field}", problem))
    for kind in COUNTER_KINDS:
        spec = numbering[kind]
        allowed = ["n", "n1", "n2", *FORCED_STYLE_FIELDS]
        for field in ("format", "ref", "reference"):
            template = spec[field]
            for problem in template_problems(template, allowed):
                issues.append(ConfigIssue(f"numbering.{kind}.{field}", problem))
            if not any(name in ("n", *FORCED_STYLE_FIELDS) for name in template_fields(template)):
                issues.append(ConfigIssue(f"numbering.{kind}.{field}", "must contain a counter field such as {n}"))
    manual = data["bibliography"]["manual"]
    for field, required in (("citation_format", "numbers"), ("entry_label", "n")):
        for problem in template_problems(manual[field], [required]):
            issues.append(ConfigIssue(f"bibliography.manual.{field}", problem))
        if required not in template_fields(manual[field]):
            issues.append(ConfigIssue(f"bibliography.manual.{field}", f"must contain the field {{{required}}}"))
    for index, argument in enumerate(data["pandoc"]["extra_args"]):
        name = argument.split("=", 1)[0]
        if name in _RESERVED_PANDOC_OPTIONS:
            issues.append(ConfigIssue(f"pandoc.extra_args.{index}", f"{name} is managed by latex2word and cannot be set here"))
    adapter = _get(data, ("frontmatter", "metadata_adapter"))
    if adapter is not None and adapter not in available_adapters():
        issues.append(
            ConfigIssue("frontmatter.metadata_adapter", f"unknown metadata adapter {adapter!r}; available: {', '.join(available_adapters())}")
        )
    return issues


def load_profile(profile_id: str, search_dirs: Sequence[Path] = ()) -> Profile:
    """Resolve ``profile_id`` (see the module docstring for the lookup order).

    Raises :class:`ProfileError` for an unknown, malformed, cyclic, incomplete or
    inconsistent profile. Independent of the current working directory.
    """
    dirs = tuple(Path(d) for d in search_dirs)
    chain = _resolve_chain(profile_id, dirs)
    merged: dict[str, Any] = {}
    csl: Path | None = None
    for source, data in chain:
        body = {k: v for k, v in data.items() if k not in _META_KEYS}
        merged = _merge(merged, body)
        csl_value = _get(data, ("bibliography", "csl"))
        if csl_value is not None:
            base = source.directory
            if base is None:
                base = Path(str(resources.files("latex2word").joinpath("profiles")))
            csl = (base / csl_value).resolve()
    if "generic" not in (source.profile_id for source, _ in chain):
        builtin = dict(_resolve_chain("generic", ())[-1][1])
        for key in _LAYOUT_KEYS:
            merged[key] = _merge(builtin[key], merged.get(key, {}))
    issues = _consistency_issues(merged)
    if issues:
        raise ProfileError(f"profile {profile_id!r}: {len(issues)} problem(s) found", issues)
    reference = resolve_reference_docx(merged)
    layout = resolve_layout(merged)
    frontmatter = resolve_frontmatter(merged)
    reference_path: Path | None = None
    if reference.mode == "external":
        if not reference.path:
            raise ProfileError(
                f"profile {profile_id!r}: reference_docx.mode is external but no path is given",
                [ConfigIssue("reference_docx.path", "required when mode is external")],
            )
        base = next((s.directory for s, d in reversed(chain) if "reference_docx" in d and s.directory is not None), None)
        if base is None:
            raise ProfileError(
                f"profile {profile_id!r}: an external reference document needs a profile file on disk (profile_dirs)",
                [ConfigIssue("reference_docx.path", "relative paths resolve against the profile file; built-in profiles have none")],
            )
        reference_path = (base / reference.path).resolve()
        if not reference_path.is_file():
            raise ProfileError(
                f"profile {profile_id!r}: reference_docx.path does not exist: {reference_path}",
                [ConfigIssue("reference_docx.path", f"file not found: {reference_path}")],
            )
    elif reference.path is not None:
        raise ProfileError(
            f"profile {profile_id!r}: reference_docx.path is only valid with mode external",
            [ConfigIssue("reference_docx.path", "remove it or set mode: external")],
        )
    if csl is not None and not csl.is_file():
        raise ProfileError(
            f"profile {profile_id!r}: bibliography.csl does not exist: {csl}",
            [ConfigIssue("bibliography.csl", f"file not found: {csl}")],
        )
    leaf = chain[-1][1]
    merged.get("bibliography", {}).pop("csl", None)
    profile = Profile(
        id=leaf["id"],
        display_name=leaf.get("display_name", leaf["id"]),
        chain=tuple(source.profile_id for source, _ in chain),
        data=merged,
        csl=csl,
        layout=layout,
        frontmatter=frontmatter,
        reference_docx=reference,
        reference_docx_path=reference_path,
    )
    log_event(_log, logging.INFO, "profile loaded", id=profile.id, chain=" > ".join(profile.chain))
    return profile
