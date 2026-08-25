"""Whether a fix landed is decided by the repository, not by what the fix agent reported."""

from __future__ import annotations

from pathlib import Path

import yaml
from conftest import FIXTURES

from coldsweep.models import Profile, RawFinding
from coldsweep.verify import evidence_present, verify_findings


def _profile() -> Profile:
    data = yaml.safe_load((FIXTURES / "profile_mixed.yaml").read_text())
    return Profile.model_validate(data)


def _finding(evidence: str | None, rule: str = "swallowed-exception"):
    f = RawFinding(rule_id=rule, anchor="src/a.py::f", evidence=evidence,
                   description="d").to_finding("s", 1)
    f.status = "fixed"
    return f


def test_evidence_still_present_means_not_fixed(repo: Path):
    (repo / "src" / "a.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    findings = [_finding("except Exception:\n    pass")]
    stats = verify_findings(repo, _profile(), findings, 2)
    assert stats["reopened"] == 1 and findings[0].status == "open"


def test_evidence_gone_means_fixed(repo: Path):
    (repo / "src" / "a.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        raise\n")
    findings = [_finding("except Exception:\n    pass")]
    stats = verify_findings(repo, _profile(), findings, 2)
    assert stats["verified"] == 1 and findings[0].status == "verified"


def test_reformatting_the_offending_code_does_not_count_as_a_fix(repo: Path):
    (repo / "src" / "a.py").write_text(
        "def f():\n    try:\n        g()\n    except Exception:   # nothing to do\n\n        pass\n")
    findings = [_finding("except Exception:\n    pass")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "open", "comments and blank lines are normalized away"


def test_an_identical_snippet_in_another_file_is_not_this_finding(repo: Path):
    """The predicate is the anchor's file. A common idiom surviving elsewhere would otherwise
    reopen every finding under its rule; code that was moved is re-derived by the next scan at
    its new anchor instead."""
    (repo / "src" / "a.py").write_text("def f():\n    g()\n")
    (repo / "src" / "b.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    findings = [_finding("except Exception:\n    pass")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "verified"


def test_an_anchor_outside_the_scope_is_deferred_not_verified(repo: Path):
    """Absence from the corpus is not proof when the file was never in the corpus."""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_f():\n    f(1)\n")
    f = RawFinding(rule_id="swallowed-exception", anchor="tests/test_a.py::test_f",
                   evidence="def test_f():\n    f(1)", description="d").to_finding("s", 1)
    f.status = "fixed"
    stats = verify_findings(repo, _profile(), [f], 2)
    assert stats == {"verified": 0, "reopened": 0, "deferred": 1}
    assert f.status == "fixed" and "outside the profile scope" in f.history[-1].detail


def test_an_anchor_whose_file_is_gone_is_deferred_not_verified(repo: Path):
    findings = [_finding("except Exception:\n    pass")]
    (repo / "src" / "a.py").unlink()
    stats = verify_findings(repo, _profile(), findings, 2)
    assert stats["deferred"] == 1 and findings[0].status == "fixed"


def test_presence_findings_are_deferred_not_guessed_at(repo: Path):
    findings = [_finding(None, rule="undocumented-public-symbol")]
    stats = verify_findings(repo, _profile(), findings, 2)
    assert stats == {"verified": 0, "reopened": 0, "deferred": 1}
    assert findings[0].status == "fixed", "resolved by a later round failing to re-derive it"


def test_repeated_failed_verification_trips_the_oscillation_guard(repo: Path):
    (repo / "src" / "a.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    findings = [_finding("except Exception:\n    pass")]
    for _ in range(3):
        findings[0].status = "fixed"
        verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "disputed" and findings[0].adjudicated is False


def test_evidence_present_needs_evidence():
    assert evidence_present("anything", None) is False
    assert evidence_present("a()\nb()", "b()") is True


def test_verification_with_nothing_fixed_reports_zeroes(repo: Path):
    findings = [_finding("except Exception:\n    pass")]
    findings[0].status = "open"
    assert verify_findings(repo, _profile(), findings, 2) == {
        "verified": 0, "reopened": 0, "deferred": 0}


def test_an_absence_finding_with_no_evidence_is_deferred_not_assumed_fixed(repo: Path):
    """No evidence means no deterministic predicate, whatever the rule's mode says."""
    findings = [_finding(None, rule="swallowed-exception")]
    stats = verify_findings(repo, _profile(), findings, 2)
    assert stats["deferred"] == 1 and findings[0].status == "fixed"


def test_evidence_is_not_matched_across_a_file_boundary(repo: Path):
    """A snippet whose halves live in two different files is in neither of them."""
    (repo / "src" / "a.py").write_text("def f():\n    first_half()\n")
    (repo / "src" / "b.py").write_text("def g():\n    second_half()\n")
    findings = [_finding("first_half()\nsecond_half()")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "verified", "the two halves never appear together in one file"


def test_evidence_spanning_two_files_is_not_a_match(repo: Path):
    """Same at file boundaries: the anchor's file is read on its own, never concatenated."""
    (repo / "src" / "a.py").write_text("first_half()\n")
    (repo / "src" / "b.py").write_text("second_half()\n")
    findings = [_finding("first_half()\nsecond_half()")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "verified", "neither file contains the snippet"


def test_evidence_within_one_file_is_still_a_match(repo: Path):
    (repo / "src" / "a.py").write_text("first_half()\nsecond_half()\n")
    findings = [_finding("first_half()\nsecond_half()")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "open"


def _scoped_profile(include: list[str]) -> Profile:
    data = yaml.safe_load((FIXTURES / "profile_mixed.yaml").read_text())
    data["scope"]["include"] = include
    return Profile.model_validate(data)


def _foreign(file: str, anchor: str, evidence: str):
    f = RawFinding(rule_id="swallowed-exception", anchor=anchor, evidence=evidence,
                   description="d").to_finding("s", 1)
    f.status = "fixed"
    return f


def test_a_file_with_no_syntax_support_defers_instead_of_reopening(repo: Path):
    """Outside a resolvable language the snippet can only be searched file-wide.

    A second copy of the same idiom then reopens a finding whose own copy was fixed. Deferring
    costs one more round; reopening throws away a correct fix and eventually disputes it.
    """
    (repo / "src" / "app.rb").write_text(
        "def a\n  begin\n    g\n  rescue\n  end\nend\n\n"
        "def b\n  begin\n    g\n  rescue\n  end\nend\n")
    findings = [_foreign("src/app.rb", "src/app.rb::a", "rescue\nend")]
    stats = verify_findings(repo, _scoped_profile(["src/**/*.rb"]), findings, 2)
    assert stats["deferred"] == 1 and stats["reopened"] == 0
    assert findings[0].status == "fixed"
    assert "no syntax support" in findings[0].history[-1].detail


def test_a_resolvable_language_still_verifies_per_symbol(repo: Path):
    """C# resolves, so a fix in one method is not undone by the same idiom in another."""
    (repo / "src" / "R.cs").write_text(
        "class R {\n  void A() { try { g(); } catch (Exception) { throw; } }\n"
        "  void B() { try { g(); } catch (Exception) { }\n }\n}\n")
    findings = [_foreign("src/R.cs", "src/R.cs::R::A", "catch (Exception) { }")]
    stats = verify_findings(repo, _scoped_profile(["src/**/*.cs"]), findings, 2)
    assert stats["verified"] == 1 and findings[0].status == "verified"


def test_a_deleted_symbol_in_a_resolvable_language_is_not_a_free_pass(repo: Path):
    """The grammar resolves, so `None` here means the symbol is gone, not that we cannot tell."""
    (repo / "src" / "R.cs").write_text("class R {\n  void B() { try { g(); } catch (Exception) { } }\n}\n")
    findings = [_foreign("src/R.cs", "src/R.cs::R::A", "catch (Exception) { }")]
    stats = verify_findings(repo, _scoped_profile(["src/**/*.cs"]), findings, 2)
    assert stats["reopened"] == 1 and findings[0].status == "open"
