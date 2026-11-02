"""Pandoc compatibility classes with synthetic versions (no real Pandoc needed).

* a certified version (``CERTIFIED_VERSIONS``) is SUPPORTED;
* any other version at or above the minimum is TESTED_UNCERTIFIED: usable, reported with a warning;
* a version below the minimum, or a failed capability probe, is UNSUPPORTED: doctor reports the required tool FAIL.
"""

from __future__ import annotations

import pytest
from conftest import ALL_TOOLS, certified_pandoc_runner, fake_which, make_result

from latex2word.diagnostics import Severity, Status
from latex2word.pandoc_compat import (
    CERTIFIED_VERSIONS,
    MINIMUM_VERSION,
    PandocCompat,
    ProbeCheck,
    assess_pandoc,
    compat_findings,
    parse_pandoc_version,
)
from latex2word.toolchain import inspect_toolchain

CERTIFIED = ".".join(map(str, CERTIFIED_VERSIONS[0]))


def assess(version, probe_checks=None, monkeypatch=None):
    if probe_checks is not None:
        monkeypatch.setattr("latex2word.pandoc_compat.run_probe", lambda path, runner, timeout_s: probe_checks)
    return assess_pandoc(version, path="/fake/bin/pandoc", runner=None, timeout_s=1, probe=probe_checks is not None)


def test_the_certified_set_is_explicit():
    assert CERTIFIED_VERSIONS == ((3, 12),)  # changing it means completing the certification procedure
    assert MINIMUM_VERSION == (3, 0)


def test_version_parsing():
    assert parse_pandoc_version("pandoc 3.12\nFeatures: +server") == "3.12"
    assert parse_pandoc_version("pandoc.exe 3.13.0.1") == "3.13.0.1"
    assert parse_pandoc_version("no version here") is None


def test_a_certified_version_is_supported_with_an_info_finding():
    assessment = assess(CERTIFIED)
    assert assessment.compat is PandocCompat.SUPPORTED and assessment.reasons == ()
    (finding,) = compat_findings(assessment)
    assert finding.severity is Severity.INFO and finding.code == "L2W-I-PANDOC-003"


@pytest.mark.parametrize("version", ["3.13", "3.14.1", "3.11", "4.0"])
def test_other_versions_above_the_minimum_are_tested_uncertified_with_a_warning(version):
    assessment = assess(version)
    assert assessment.compat is PandocCompat.TESTED_UNCERTIFIED
    assert f"not in the certified set ({CERTIFIED})" in assessment.reasons[0]
    (finding,) = compat_findings(assessment)
    assert finding.severity is Severity.WARNING and finding.code == "L2W-W-PANDOC-003"


def test_an_unrecognised_version_is_uncertified_not_certified():
    assert assess(None).compat is PandocCompat.TESTED_UNCERTIFIED


@pytest.mark.parametrize("version", ["2.19.2", "1.0"])
def test_versions_below_the_minimum_are_unsupported_with_an_error(version):
    assessment = assess(version)
    assert assessment.compat is PandocCompat.UNSUPPORTED and "older than the minimum" in assessment.reasons[0]
    (finding,) = compat_findings(assessment)
    assert finding.severity is Severity.ERROR and finding.code == "L2W-E-PANDOC-003"


def test_a_failed_capability_probe_makes_even_a_certified_version_unsupported(monkeypatch):
    checks = (ProbeCheck("latex-reader", True, "ok"), ProbeCheck("lua-filters", False, "lua missing"))
    assessment = assess(CERTIFIED, checks, monkeypatch)
    assert assessment.compat is PandocCompat.UNSUPPORTED and assessment.reasons == ("lua-filters: lua missing",)


def test_a_passing_probe_keeps_the_version_classification(monkeypatch):
    checks = (ProbeCheck("latex-reader", True, "ok"),)
    assert assess(CERTIFIED, checks, monkeypatch).compat is PandocCompat.SUPPORTED
    assert assess("3.13", checks, monkeypatch).compat is PandocCompat.TESTED_UNCERTIFIED


def doctor(version):
    return inspect_toolchain(
        which=fake_which(ALL_TOOLS), runner=certified_pandoc_runner(version), interpreter="/fake/bin/python",
        probe=False,
    )


def test_doctor_is_ready_with_a_certified_pandoc():
    report = doctor(CERTIFIED)
    pandoc = next(d for d in report.diagnostics if d.subsystem == "pandoc")
    assert report.ready and pandoc.status is Status.PASS


def test_doctor_is_ready_but_warns_with_an_uncertified_pandoc():
    report = doctor("3.13")
    pandoc = next(d for d in report.diagnostics if d.subsystem == "pandoc")
    assert report.ready and pandoc.status is Status.PASS and "TESTED_UNCERTIFIED" in (pandoc.detail or "")
    assert [f.code for f in report.findings] == ["L2W-W-PANDOC-003"]


def test_doctor_is_not_ready_with_an_unsupported_pandoc():
    report = doctor("2.19")
    pandoc = next(d for d in report.diagnostics if d.subsystem == "pandoc")
    assert not report.ready and pandoc.status is Status.FAIL and pandoc.message.startswith("UNSUPPORTED")


def test_make_result_helper_shape():
    # guard for the shared fake: the doctor tests rely on it answering --version for pandoc
    assert certified_pandoc_runner("3.12")(("/fake/bin/pandoc", "--version"), timeout=1).stdout == "pandoc 3.12\n"
    assert make_result().returncode == 0
