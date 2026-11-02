"""Command-line interface: ``doctor``, ``config``, ``inspect``, ``figures`` and ``build``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from latex2word import __version__, build as build_module, figure_pipeline, inspection
from latex2word.config import ProjectConfig, check_project_files, load_config
from latex2word.diagnostics import render_json, render_result_line, render_text
from latex2word.errors import ConfigError, ExitCode, Latex2WordError
from latex2word.figure_pipeline import run_figures
from latex2word.inspection import inspect_project
from latex2word.logging_utils import configure_logging
from latex2word.toolchain import DEFAULT_PROBE_TIMEOUT_S, inspect_toolchain

_log = logging.getLogger(__name__)


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _common_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="count", default=argparse.SUPPRESS,
        help="more diagnostic output (repeat for debug logging)",
    )
    common.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="minimal output; only errors are logged",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="latex2word",
        description="LaTeX to Word conversion tooling: toolchain checks, project inspection, the vector "
        "figure pipeline and the semantic LaTeX to DOCX build (numbering, cross-references, bibliography).",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"latex2word {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    doctor = commands.add_parser(
        "doctor", parents=[common], help="inspect the external toolchain",
        description="Detect required and optional external tools without modifying anything. "
        "Exit 0: all required tools usable; 1: a required tool is missing or unusable; 2: error.",
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable JSON output")
    doctor.add_argument(
        "--timeout", type=_positive_float, default=DEFAULT_PROBE_TIMEOUT_S, metavar="SECONDS",
        help=f"timeout of each version probe (default: {DEFAULT_PROBE_TIMEOUT_S:g})",
    )
    doctor.set_defaults(handler=_cmd_doctor)

    config = commands.add_parser("config", help="validate or display a project configuration")
    config_commands = config.add_subparsers(dest="config_command", metavar="<action>", required=True)

    validate = config_commands.add_parser(
        "validate", parents=[common], help="validate a configuration file",
        description="Validate YAML syntax, the version-1 schema and path semantics. "
        "Files named by the configuration need not exist unless --check-files is given.",
    )
    validate.add_argument("config_file", type=Path, metavar="<config.yaml>")
    validate.add_argument("--json", action="store_true", help="machine-readable JSON output")
    validate.add_argument(
        "--check-files", action="store_true",
        help="also require the project root directory and main file to exist",
    )
    validate.set_defaults(handler=_cmd_config_validate)

    show = config_commands.add_parser(
        "show", parents=[common], help="print the normalised, resolved configuration",
    )
    show.add_argument("config_file", type=Path, metavar="<config.yaml>")
    show.add_argument("--json", action="store_true", help="output JSON instead of YAML")
    show.set_defaults(handler=_cmd_config_show)

    inspect = commands.add_parser(
        "inspect", parents=[common], help="inspect a LaTeX project's dependencies and figures (read-only)",
        description="Build the source dependency graph from the main TeX file and list reachable files, "
        "missing/outside-root dependencies, bibliography files and figures. Never writes anything. "
        "Exit 0: no fatal dependency errors; 1: project/dependency problems; 2: configuration or program error.",
    )
    inspect.add_argument("config_file", type=Path, metavar="<config.yaml>")
    inspect.add_argument("--json", action="store_true", help="machine-readable JSON output")
    inspect.set_defaults(handler=_cmd_inspect)

    figures = commands.add_parser(
        "figures", parents=[common], help="stage sources and render active figures to SVG",
        description="Inspect the project, stage reachable sources into the owned workspace, render active "
        "TikZ/circuitikz and PDF figures to SVG, validate them and write the figure manifest. "
        "No document conversion. Exit 0: all active figures rendered; 1: problems; 2: configuration or program error.",
    )
    figures.add_argument("config_file", type=Path, metavar="<config.yaml>")
    figures.add_argument("--json", action="store_true", help="machine-readable JSON output")
    figures.add_argument(
        "--previews", action="store_true",
        help="also write PNG previews (rsvg-convert) into the workspace; inspection artifacts only",
    )
    figures.add_argument(
        "--compare-pdf-backends", action="store_true",
        help="run every PDF to SVG backend for each PDF figure and record the comparison",
    )
    figures.set_defaults(handler=_cmd_figures)

    build = commands.add_parser(
        "build", parents=[common], help="convert the project to a preliminary semantic DOCX",
        description="Inspect, stage and render figures, rewrite the staged copy, convert with Pandoc and the latex2word "
        "filters (numbering, cross-references, math, bibliography), validate the DOCX structurally and replace the "
        "output atomically only on success. The project tree is never modified. "
        "Exit 0: built; 1: document problems (see the diagnostics); 2: configuration, profile or tool error.",
    )
    build.add_argument("config_file", type=Path, metavar="<config.yaml>")
    build.add_argument("--json", action="store_true", help="machine-readable JSON output")
    build.add_argument("--output", type=Path, metavar="<path>", help="output .docx path (default: from the configuration)")
    build.add_argument(
        "--keep-workspace", action="store_true",
        help="keep the workspace after a failed build even if the configuration says to discard it",
    )
    build.set_defaults(handler=_cmd_build)
    return parser


def _print_issues(error: ConfigError, config_file: Path) -> None:
    print(f"error: {config_file}: {error.message}", file=sys.stderr)
    if len(error.issues) > 1 or error.issues[0].path:
        for issue in error.issues:
            print(f"  - {issue}", file=sys.stderr)


def _print_config_error_json(error: ConfigError, config_file: Path) -> None:
    payload = {
        "valid": False,
        "config_file": str(config_file),
        "errors": [{"path": i.path, "message": i.message} for i in error.issues],
    }
    print(json.dumps(payload, indent=2))


def _report_config_error(error: ConfigError, args: argparse.Namespace) -> int:
    if args.json:
        _print_config_error_json(error, args.config_file)
    else:
        _print_issues(error, args.config_file)
    return ExitCode.ERROR


def _load(args: argparse.Namespace, *, check_files: bool = False) -> ProjectConfig:
    config = load_config(args.config_file)
    if check_files:
        check_project_files(config)
    return config


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = inspect_toolchain(timeout_s=args.timeout)
    if args.json:
        print(render_json(report))
    elif args.quiet:
        print(render_result_line(report))
    else:
        print(render_text(report, verbose=args.verbose > 0))
    return report.exit_code


def _cmd_config_validate(args: argparse.Namespace) -> int:
    try:
        config = _load(args, check_files=args.check_files)
    except ConfigError as error:
        return _report_config_error(error, args)
    if args.json:
        print(json.dumps({"valid": True, "config_file": config.config_file.as_posix(), "errors": []}, indent=2))
    elif not args.quiet:
        print(f"OK: {config.config_file} (schema_version {config.schema_version}, profile {config.profile})")
    return ExitCode.OK


def _cmd_config_show(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as error:
        return _report_config_error(error, args)
    data = config.to_dict()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), end="")
    return ExitCode.OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as error:
        return _report_config_error(error, args)
    report = inspect_project(config)
    if args.json:
        print(inspection.render_json(report))
    elif args.quiet:
        print(f"{report.summary()['status']}")
    else:
        print(inspection.render_text(report, verbose=args.verbose > 0))
    return report.exit_code


def _cmd_figures(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as error:
        return _report_config_error(error, args)
    run = run_figures(config, previews=args.previews, compare_pdf_backends=args.compare_pdf_backends)
    if args.json:
        print(json.dumps(run.to_dict(), indent=2))
    elif args.quiet:
        print(run.summary()["status"])
    else:
        print(figure_pipeline.render_text(run))
        if run.inspection.has_errors:
            for finding in run.inspection.findings:
                print(f"  {finding}")
    return ExitCode.OK if run.ok else ExitCode.PROBLEMS



def _cmd_build(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
        result = build_module.run_build(
            config, output=args.output, keep_workspace=args.keep_workspace,
            progress=None if args.quiet or args.json else (lambda message: print(f"  {message}", file=sys.stderr)),
        )
    except ConfigError as error:
        return _report_config_error(error, args)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif args.quiet:
        print("OK" if result.ok else "FAILED")
    else:
        print(build_module.render_text(result, verbose=args.verbose > 0))
    return ExitCode.OK if result.ok else ExitCode.PROBLEMS


def _use_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _exit_status(code: object) -> int:
    if code is None:
        return ExitCode.OK
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return ExitCode.ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    _use_utf8_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # --help, --version, usage errors
        return _exit_status(exit_request.code)

    args.verbose = getattr(args, "verbose", 0)
    args.quiet = getattr(args, "quiet", False)
    if args.quiet and args.verbose:
        print("error: --quiet and --verbose are mutually exclusive", file=sys.stderr)
        return ExitCode.ERROR
    configure_logging(verbosity=args.verbose, quiet=args.quiet)

    try:
        return int(args.handler(args))
    except Latex2WordError as error:
        print(f"error: {error}", file=sys.stderr)
        return int(error.exit_code)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - last-resort guard; no traceback unless -vv
        _log.debug("unexpected failure", exc_info=True)
        print(f"internal error: {type(error).__name__}: {error}", file=sys.stderr)
        return ExitCode.ERROR


if __name__ == "__main__":
    raise SystemExit(main())
