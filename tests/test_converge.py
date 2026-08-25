"""Termination is a computation over finding sets. No model is ever asked whether the work is done."""

from __future__ import annotations

from conftest import make_round

from coldsweep.converge import ConvergenceReport, evaluate, open_blocking, status_counts
from coldsweep.merge import merge_round
from coldsweep.models import Finding, Profile, RawFinding


def f(**kw) -> Finding:
    base = dict(id="r-1", rule_id="missing-error-handling", anchor="src/a.py::f",
                status="verified", first_seen_round=1, last_seen_round=1)
    return Finding.model_validate({**base, **kw})


def test_open_work_blocks_the_gate(profile):
    assert not evaluate([f(status="open")], profile, [1, 2, 3]).converged


def test_a_claimed_fix_blocks_until_it_is_confirmed(profile):
    report = evaluate([f(status="fixed")], profile, [1, 2, 3])
    assert not report.converged
    assert report.open_ids == ["r-1"]


def test_wontfix_and_adjudicated_disputes_do_not_block(profile):
    findings = [f(id="a", status="wontfix"), f(id="b", status="disputed", adjudicated=True)]
    assert evaluate(findings, profile, [1, 2, 3]).converged


def test_an_untriaged_dispute_blocks(profile):
    report = evaluate([f(status="disputed", adjudicated=False)], profile, [1, 2, 3])
    assert not report.converged and report.disputed_pending_ids == ["r-1"]


def test_the_unclassified_bucket_blocks_but_is_reported_separately(profile):
    findings = [f(status="open", rule_id="not-in-taxonomy")]
    report = evaluate(findings, profile, [1, 2, 3])
    assert not report.converged
    assert report.unclassified_ids == ["r-1"] and report.open_ids == []
    assert open_blocking(findings, profile) == []


def test_a_wontfixed_unclassified_finding_clears_the_bucket(profile):
    assert evaluate([f(status="wontfix", rule_id="not-in-taxonomy")], profile, [1, 2, 3]).converged


def test_k_quiet_rounds_are_required(profile):
    findings = [f(first_seen_round=2, last_seen_round=2)]
    assert not evaluate(findings, profile, [1, 2]).converged, "round 2 produced a finding"
    assert not evaluate(findings, profile, [1, 2, 3]).converged, "round 2 is still inside the window"
    assert evaluate(findings, profile, [1, 2, 3, 4]).converged


def test_fewer_completed_rounds_than_k_never_converges(profile):
    assert not evaluate([], profile, [1]).converged
    assert evaluate([], profile, [1, 2]).converged


def test_the_loop_terminates_once_the_scan_goes_quiet(profile):
    """The full M0 loop: a finding is found, fixed, and the round set converges by itself."""
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings: list[Finding] = []
    rounds: list[int] = []
    converged_at = None
    for n in range(1, 9):
        if evaluate(findings, profile, rounds).converged:
            converged_at = n - 1
            break
        scan = make_round(n, ("s1", ["src/g.py"], [raw] if n == 1 else []))
        findings, _ = merge_round(findings, scan, profile, n)
        rounds.append(n)
        for item in findings:            # stand-in for the fix + verify phases
            if item.status == "open":
                item.status = "verified"
    assert converged_at == 3
    assert [item.status for item in findings] == ["verified"]


def test_a_finding_that_keeps_coming_back_never_silently_converges(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings: list[Finding] = []
    rounds: list[int] = []
    for n in range(1, 9):
        scan = make_round(n, ("s1", ["src/g.py"], [raw]))
        findings, _ = merge_round(findings, scan, profile, n)
        rounds.append(n)
        for item in findings:
            if item.status == "open":
                item.status = "fixed"
        assert not evaluate(findings, profile, rounds).converged
    assert findings[0].status == "disputed", "the oscillation guard stops the re-fixing"


def test_status_counts_group_every_axis(profile):
    counts = status_counts([f(id="a", status="open"), f(id="b", status="open", source="mechanical")])
    assert counts["status"]["open"] == 2
    assert counts["source"]["mechanical"] == 1 and counts["source"]["agent"] == 1


def test_triage_is_flagged_once_scanning_can_do_nothing_more(profile):
    """Nothing open, rounds already quiet, only a dispute left -- another round buys nothing."""
    report = evaluate([f(status="disputed", adjudicated=False)], profile, [1, 2, 3])
    assert report.needs_triage is True and report.converged is False


def test_an_off_taxonomy_finding_also_needs_triage(profile):
    assert evaluate([f(status="open", rule_id="not-in-taxonomy")], profile, [1, 2, 3]).needs_triage


def test_open_work_is_not_a_triage_stop(profile):
    findings = [f(id="a", status="open"), f(id="b", status="disputed", adjudicated=False)]
    assert evaluate(findings, profile, [1, 2, 3]).needs_triage is False, "fixing can still progress"


def test_unquiet_rounds_are_not_a_triage_stop(profile):
    """A dispute plus an unfinished quiet window still needs the remaining rounds."""
    findings = [f(status="disputed", adjudicated=False, first_seen_round=3)]
    assert evaluate(findings, profile, [1, 2, 3]).needs_triage is False
    assert evaluate(findings, profile, [1, 2, 3, 4, 5]).needs_triage is True


def test_too_few_rounds_is_not_a_triage_stop(profile):
    assert evaluate([f(status="disputed", adjudicated=False)], profile, [1]).needs_triage is False


def test_a_converged_task_never_needs_triage(profile):
    assert evaluate([f(status="verified")], profile, [1, 2, 3]).needs_triage is False


def test_quiet_rounds_counts_back_from_the_most_recent_round(profile):
    """Rounds 3 and 4 produced nothing; round 2 did, which is where the run stops counting."""
    findings = [f(first_seen_round=2, last_seen_round=2)]
    assert evaluate(findings, profile, [1, 2, 3, 4]).quiet_rounds == 2
    assert evaluate(findings, profile, [1, 2]).quiet_rounds == 0
    assert evaluate([], profile, [1, 2, 3]).quiet_rounds == 3


def test_a_report_built_directly_defaults_to_a_shut_gate(profile):
    """The defaults matter: `evaluate` always sets these, so nothing else pins them."""
    report = ConvergenceReport(converged=False, k=2, max_rounds=8)
    assert report.needs_triage is False
    assert report.quiet_rounds == 0
    assert report.completed_rounds == [] and report.reasons == []


def test_status_counts_returns_exactly_the_three_axes(profile):
    counts = status_counts([f(status="open")])
    assert set(counts) == {"status", "rule", "source"}


# --- the two halves --------------------------------------------------------

def _split_profile() -> Profile:
    """A profile mixing a rule a subsystem decides with one an agent decides."""
    return Profile.model_validate({
        "version": 1, "name": "split",
        "scope": {"include": ["src/**/*.py"]},
        "convergence": {"k": 2, "max_rounds": 8},
        "rules": [
            {"id": "unimplemented-spec-item", "mode": "presence", "decided_by": "code",
             "description": "d"},
            {"id": "vacuous-implementation", "mode": "presence", "description": "d"},
        ],
    })


def _at(rule: str, anchor: str, first: int, last: int, status: str = "verified"):
    f = RawFinding(rule_id=rule, anchor=anchor, evidence=None, description="d").to_finding("s", first)
    f.last_seen_round, f.status = last, status
    return f


def test_the_decidable_half_can_converge_while_the_budgeted_half_plateaus():
    """The measured shape of a `features` run: the spec items settle, the agent rule does not.

    One verdict over both halves reports "not converged" and loses the fact that the half with a
    real answer already has one.
    """
    findings = [
        _at("unimplemented-spec-item", "SPEC.md::FR-1", 1, 1),
        _at("vacuous-implementation", "src/a.py::f", 3, 3, status="open"),
    ]
    report = evaluate(findings, _split_profile(), [1, 2, 3])
    assert not report.converged
    assert report.decidable.converged
    assert not report.budgeted.converged
    assert report.budgeted.open_ids


def test_a_half_measures_its_own_quiet_window():
    """New findings under the other half's rules must not disturb this half's window."""
    findings = [_at("vacuous-implementation", "src/a.py::f", 3, 3)]
    report = evaluate(findings, _split_profile(), [1, 2, 3])
    assert report.decidable.new_per_round == {"1": 0, "2": 0, "3": 0}
    assert report.budgeted.new_per_round["3"] == 1


def test_a_half_with_no_rules_is_not_converged():
    """An empty half has no answer. Reporting 0 would gate open on a taxonomy that cannot fail."""
    profile = _split_profile()
    profile.rules = [r for r in profile.rules if r.decided_by == "agent"]
    report = evaluate([], profile, [1, 2])
    assert report.decidable.empty and not report.decidable.converged
    assert report.budgeted.converged


def test_an_unadjudicated_dispute_shuts_its_own_half():
    findings = [_at("unimplemented-spec-item", "SPEC.md::FR-1", 1, 1, status="disputed")]
    report = evaluate(findings, _split_profile(), [1, 2, 3])
    assert not report.decidable.converged
    assert report.decidable.disputed_pending_ids
    assert report.budgeted.converged


def test_unclassified_findings_belong_to_neither_half():
    """An off-taxonomy rule id is gated globally; it must not silently shut a half."""
    findings = [_at("invented-rule", "src/a.py::f", 1, 1)]
    report = evaluate(findings, _split_profile(), [1, 2, 3])
    assert report.unclassified_ids
    assert report.decidable.converged and report.budgeted.converged
    assert not report.converged
