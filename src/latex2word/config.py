"""Project configuration: load, schema-validate, normalise and resolve paths.

Path rules (all independent of the current working directory):

* ``project.root`` is resolved against the directory of the config file;
* ``project.main``, ``workspace.root`` and ``output.directory`` are resolved
  against the project root.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path, PurePath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from latex2word.errors import ConfigError, ConfigIssue
from latex2word.logging_utils import log_event

_log = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

DEFAULT_PROJECT_ROOT = "."
DEFAULT_TIMEOUT_S = 120
DEFAULT_WORKSPACE_ROOT = ".latex2word"
DEFAULT_KEEP_ON_FAILURE = True
DEFAULT_OUTPUT_DIRECTORY = "output"
DEFAULT_FAIL_ON = "error"


@dataclass(frozen=True)
class ProjectSection:
    name: str
    root: Path
    main: Path


@dataclass(frozen=True)
class ToolsSection:
    timeout_s: float


@dataclass(frozen=True)
class WorkspaceSection:
    root: Path
    keep_on_failure: bool


@dataclass(frozen=True)
class OutputSection:
    directory: Path
    filename: str | None = None  # default: "<main stem>.docx"


@dataclass(frozen=True)
class ValidationSection:
    fail_on: str



@dataclass(frozen=True)
class TexSection:
    """Commands/environments the user explicitly allows to be dropped (never the default)."""

    allow_commands: tuple[str, ...] = ()
    allow_environments: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagingSection:
    """Explicit extras staged in addition to the reachable source dependencies."""

    extra_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class FigurePatch:
    """A project-specific literal replacement applied to extracted TikZ source."""

    figure: str
    find: str
    replace: str


@dataclass(frozen=True)
class FiguresSection:
    """Optional figure rendering hooks (all empty by default)."""

    render_preamble: tuple[str, ...] = ()
    patches: tuple[FigurePatch, ...] = ()
    pdf_backends: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return not (self.render_preamble or self.patches or self.pdf_backends)

@dataclass(frozen=True)
class BibliographySection:
    """Configuration-level bibliography overrides (``None`` / empty: use the profile and the source)."""

    mode: str | None = None
    prefer: str | None = None
    files: tuple[str, ...] = ()  # project-relative POSIX paths
    csl: Path | None = None  # absolute

    @property
    def is_default(self) -> bool:
        return not (self.mode or self.prefer or self.files or self.csl)


@dataclass(frozen=True)
class ProjectConfig:
    """Normalised configuration with every path resolved to an absolute path."""

    config_file: Path
    schema_version: int
    project: ProjectSection
    profile: str
    tools: ToolsSection
    workspace: WorkspaceSection
    output: OutputSection
    validation: ValidationSection
    staging: StagingSection = StagingSection()
    figures: FiguresSection = FiguresSection()
    profile_dirs: tuple[Path, ...] = ()
    bibliography: BibliographySection = BibliographySection()
    tex: TexSection = TexSection()

    def to_dict(self) -> dict[str, Any]:
        """Deterministic plain-data form (POSIX-style paths, no run-specific values)."""
        data: dict[str, Any] = {
            "config_file": self.config_file.as_posix(),
            "schema_version": self.schema_version,
            "project": {
                "name": self.project.name,
                "root": self.project.root.as_posix(),
                "main": self.project.main.as_posix(),
            },
            "profile": self.profile,
            "tools": {"timeout_s": self.tools.timeout_s},
            "workspace": {
                "root": self.workspace.root.as_posix(),
                "keep_on_failure": self.workspace.keep_on_failure,
            },
            "output": {"directory": self.output.directory.as_posix()},
            "validation": {"fail_on": self.validation.fail_on},
        }
        if self.output.filename:
            data["output"]["filename"] = self.output.filename
        if self.profile_dirs:
            data["profile_dirs"] = [d.as_posix() for d in self.profile_dirs]
        if not self.bibliography.is_default:
            bib = self.bibliography
            data["bibliography"] = {
                key: value
                for key, value in (
                    ("mode", bib.mode),
                    ("prefer", bib.prefer),
                    ("files", list(bib.files) or None),
                    ("csl", bib.csl.as_posix() if bib.csl else None),
                )
                if value is not None
            }
        if self.tex.allow_commands or self.tex.allow_environments:
            data["tex"] = {
                "allow_commands": list(self.tex.allow_commands),
                "allow_environments": list(self.tex.allow_environments),
            }
        if self.staging.extra_files:
            data["staging"] = {"extra_files": list(self.staging.extra_files)}
        if not self.figures.is_default:
            data["figures"] = {
                "render_preamble": list(self.figures.render_preamble),
                "patches": [{"figure": p.figure, "find": p.find, "replace": p.replace} for p in self.figures.patches],
                "pdf_backends": list(self.figures.pdf_backends),
            }
        return data


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate mapping keys instead of keeping the last."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
            except TypeError:
                continue  # unhashable key; the base class reports it
        return super().construct_mapping(node, deep=deep)


@cache
def load_schema() -> dict[str, Any]:
    """The packaged JSON Schema (version 1) for project configuration."""
    text = resources.files("latex2word").joinpath("schemas", "project.schema.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return schema


def _issue_path(parts: Any) -> str:
    return ".".join(str(part) for part in parts)


def _schema_issues(data: dict[str, Any]) -> list[ConfigIssue]:
    validator = Draft202012Validator(load_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: (_issue_path(e.absolute_path), e.message))
    return [ConfigIssue(_issue_path(e.absolute_path), e.message) for e in errors]


def _version_issue(data: dict[str, Any]) -> ConfigIssue | None:
    if "schema_version" not in data:
        return None
    value = data["schema_version"]
    if isinstance(value, int) and not isinstance(value, bool) and value in SUPPORTED_SCHEMA_VERSIONS:
        return None
    supported = ", ".join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)
    return ConfigIssue("schema_version", f"unsupported schema_version {value!r}; supported: {supported}")


def parse_yaml(text: str, source: str = "<config>") -> Any:
    """Parse YAML with a safe loader; no arbitrary object construction."""
    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)  # SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc


def _resolve_relative(base: Path, value: str) -> Path:
    return (base / value).resolve()


def _semantic_issues(
    project_root: Path, main: str, workspace_root: Path, output_dir: Path
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    main_path = PurePath(main)
    if main_path.anchor or main_path.is_absolute():
        issues.append(ConfigIssue("project.main", "must be a relative path inside the project root"))
    elif not _resolve_relative(project_root, main).is_relative_to(project_root):
        issues.append(ConfigIssue("project.main", "must stay inside the project root"))
    for key, directory in (("workspace.root", workspace_root), ("output.directory", output_dir)):
        if directory == project_root or project_root.is_relative_to(directory):
            issues.append(ConfigIssue(key, "must not be the project root or one of its parent directories"))
    return issues


def _extra_file_issues(project_root: Path, extra_files: list[str]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for index, value in enumerate(extra_files):
        key = f"staging.extra_files.{index}"
        pure = PurePath(value)
        if pure.anchor or pure.is_absolute():
            issues.append(ConfigIssue(key, "must be a relative path inside the project root"))
        elif not _resolve_relative(project_root, value).is_relative_to(project_root):
            issues.append(ConfigIssue(key, "must stay inside the project root"))
    return issues


def _build_figures(data: dict[str, Any]) -> FiguresSection:
    return FiguresSection(
        render_preamble=tuple(data.get("render_preamble", ())),
        patches=tuple(FigurePatch(p["figure"], p["find"], p["replace"]) for p in data.get("patches", ())),
        pdf_backends=tuple(data.get("pdf_backends", ())),
    )


def _bibliography_issues(project_root: Path, files: list[str]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for index, value in enumerate(files):
        key = f"bibliography.files.{index}"
        pure = PurePath(value)
        if pure.anchor or pure.is_absolute():
            issues.append(ConfigIssue(key, "must be a relative path inside the project root"))
        elif not _resolve_relative(project_root, value).is_relative_to(project_root):
            issues.append(ConfigIssue(key, "must stay inside the project root"))
    return issues


def build_config(data: dict[str, Any], config_file: Path) -> ProjectConfig:
    """Normalise schema-valid ``data`` and resolve its paths relative to ``config_file``."""
    config_dir = config_file.resolve().parent
    project = data["project"]
    project_root = _resolve_relative(config_dir, project.get("root", DEFAULT_PROJECT_ROOT))
    workspace = data.get("workspace", {})
    workspace_root = _resolve_relative(project_root, workspace.get("root", DEFAULT_WORKSPACE_ROOT))
    output_dir = _resolve_relative(project_root, data.get("output", {}).get("directory", DEFAULT_OUTPUT_DIRECTORY))

    extra_files = list(data.get("staging", {}).get("extra_files", []))
    issues = _semantic_issues(project_root, project["main"], workspace_root, output_dir)
    issues += _extra_file_issues(project_root, extra_files)
    bibliography = data.get("bibliography", {})
    bib_files = list(bibliography.get("files", []))
    issues += _bibliography_issues(project_root, bib_files)
    if issues:
        raise ConfigError(f"{config_file.name}: invalid configuration", issues)

    return ProjectConfig(
        config_file=config_file.resolve(),
        schema_version=data["schema_version"],
        project=ProjectSection(project["name"], project_root, _resolve_relative(project_root, project["main"])),
        profile=data["profile"],
        tools=ToolsSection(data.get("tools", {}).get("timeout_s", DEFAULT_TIMEOUT_S)),
        workspace=WorkspaceSection(workspace_root, workspace.get("keep_on_failure", DEFAULT_KEEP_ON_FAILURE)),
        output=OutputSection(output_dir, data.get("output", {}).get("filename")),
        validation=ValidationSection(data.get("validation", {}).get("fail_on", DEFAULT_FAIL_ON)),
        staging=StagingSection(tuple(PurePath(v).as_posix() for v in extra_files)),
        figures=_build_figures(data.get("figures", {})),
        profile_dirs=tuple(_resolve_relative(config_dir, d) for d in data.get("profile_dirs", ())),
        tex=TexSection(
            tuple(data.get("tex", {}).get("allow_commands", ())), tuple(data.get("tex", {}).get("allow_environments", ()))
        ),
        bibliography=BibliographySection(
            mode=bibliography.get("mode"),
            prefer=bibliography.get("prefer"),
            files=tuple(PurePath(v).as_posix() for v in bib_files),
            csl=_resolve_relative(config_dir, bibliography["csl"]) if "csl" in bibliography else None,
        ),
    )


def load_config(path: str | Path) -> ProjectConfig:
    """Load and validate a configuration file.

    Raises :class:`ConfigError` (with per-field ``issues``) when the file cannot
    be read, is not valid YAML, violates the schema, or is semantically invalid.
    Filesystem existence of the project is *not* checked; see
    :func:`check_project_files`.
    """
    config_file = Path(path)
    try:
        text = config_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {config_file.name}: {exc}") from exc

    data = parse_yaml(text, config_file.name)
    if not isinstance(data, dict):
        raise ConfigError(f"{config_file.name}: top level must be a mapping")

    version_issue = _version_issue(data)
    issues = [version_issue] if version_issue else _schema_issues(data)
    if issues:
        raise ConfigError(f"{config_file.name}: {len(issues)} problem(s) found", issues)

    config = build_config(data, config_file)
    log_event(_log, logging.INFO, "configuration loaded", file=config_file.name, profile=config.profile)
    return config


def check_project_files(config: ProjectConfig) -> None:
    """Raise :class:`ConfigError` unless the project root and main file exist."""
    issues: list[ConfigIssue] = []
    if not config.project.root.is_dir():
        issues.append(ConfigIssue("project.root", "directory does not exist"))
    elif not config.project.main.is_file():
        issues.append(ConfigIssue("project.main", "file does not exist"))
    if issues:
        raise ConfigError(f"{config.config_file.name}: project files not found", issues)
