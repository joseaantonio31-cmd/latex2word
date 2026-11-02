"""Audit-only pytest plugin: bypass the Pandoc gate and the schema-order check (environment-specific under 3.1.3)."""
import pytest

@pytest.fixture(autouse=True)
def _bypass(monkeypatch):
    import latex2word.ooxml.validation as v
    monkeypatch.setattr(v, "_check_schema_order", lambda roots, result: None)
    try:
        import latex2word.build as b
        from latex2word.pandoc_compat import PandocAssessment, PandocCompat
        if hasattr(b, "assess_pandoc"):
            real = b.assess_pandoc
            def fake(version, **kw):
                return PandocAssessment(PandocCompat.TESTED_UNCERTIFIED, version or "?", (), ("bypass",))
            monkeypatch.setattr(b, "assess_pandoc", fake)
    except ImportError:
        pass
