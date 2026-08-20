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


def test_moving_the_offending_code_to_another_file_does_not_count_as_a_fix(repo: Path):
    (repo / "src" / "a.py").write_text("def f():\n    g()\n")
    (repo / "src" / "b.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    findings = [_finding("except Exception:\n    pass")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "open"


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
    """Files are joined with a separator, so the end of one plus the start of the next is not
    a match that any single file actually contains."""
    (repo / "src" / "a.py").write_text("def f():\n    first_half()\n")
    (repo / "src" / "b.py").write_text("def g():\n    second_half()\n")
    findings = [_finding("first_half()\nsecond_half()")]
    verify_findings(repo, _profile(), findings, 2)
    assert findings[0].status == "verified", "the two halves never appear together in one file"


def test_evidence_spanning_two_files_is_not_a_match(repo: Path):
    """Concatenating the corpus would let the tail of one file and the head of the next form a
    snippet that exists in neither."""
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
