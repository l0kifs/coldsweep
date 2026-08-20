"""Identity is the load-bearing property: same finding in, same id out, every round."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coldsweep.models import Finding, RawFinding, derive_id, evidence_sha, normalize_snippet


def test_identity_is_stable_across_rephrasing():
    a = RawFinding(rule_id="r", anchor="src/x.py::f", description="No error handling here.")
    b = RawFinding(rule_id="r", anchor="src/x.py::f", description="This call is entirely unguarded.")
    assert a.to_finding("s1", 1).id == b.to_finding("s2", 4).id


def test_identity_separates_rules_anchors_and_evidence():
    base = dict(rule_id="r", anchor="src/x.py::f", evidence="open(p)")
    assert derive_id("r", "src/x.py::f", None) != derive_id("q", "src/x.py::f", None)
    assert derive_id("r", "src/x.py::f", None) != derive_id("r", "src/x.py::g", None)
    other = RawFinding.model_validate({**base, "evidence": "open(q)"})
    assert RawFinding.model_validate(base).to_finding("s", 1).id != other.to_finding("s", 1).id


def test_id_carries_the_rule_prefix():
    finding = RawFinding(rule_id="swallowed-exception", anchor="src/x.py::f").to_finding("s", 1)
    assert finding.id.startswith("swallowed-exception-")
    assert len(finding.id.split("swallowed-exception-")[1]) == 8


@pytest.mark.parametrize(("a", "b"), [
    ("x = f(1)   # trailing note", "x = f(1)"),
    ("x = 'v'", 'x = "v"'),
    ("if a:\n\n    b()\n", "if a:\n  b()"),
    ("a();  /* inline */ b()", "a(); b()"),
])
def test_normalization_erases_formatting_comments_and_quote_style(a, b):
    assert evidence_sha(a) == evidence_sha(b)


def test_normalization_keeps_genuinely_different_code_apart():
    assert evidence_sha("open(path)") != evidence_sha("open(other)")


def test_anchors_may_not_be_line_references():
    for bad in ("src/x.py::42", "src/x.py::L42", "src/x.py::10-20"):
        with pytest.raises(ValidationError):
            Finding(id="r-0", rule_id="r", anchor=bad)
    Finding(id="r-0", rule_id="r", anchor="src/x.py::Class::method_2")


def test_normalize_snippet_drops_blank_and_comment_only_lines():
    assert normalize_snippet("a()\n\n# note\n\nb()") == "a()\nb()"
