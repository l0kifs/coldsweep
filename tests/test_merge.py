"""Merge decides what is the same work item. A false merge deletes work; a duplicate costs a re-check."""

from __future__ import annotations

from conftest import load_round, make_round

from coldsweep.converge import is_unclassified
from coldsweep.merge import ADJUDICATE_FLOOR, AUTO_MERGE, merge_round, similarity
from coldsweep.models import RawFinding


def ingest(existing, scan, profile, round_no, adjudicator=None):
    return merge_round(existing, scan, profile, round_no, adjudicator)


def test_differently_phrased_duplicates_merge(profile):
    first = make_round(1, ("s1", ["src/loader.py"], [
        {"rule_id": "undocumented-public-symbol", "anchor": "src/loader.py::Loader::load",
         "description": "Loader.load is completely undocumented."},
    ]))
    findings, _ = ingest([], first, profile, 1)
    assert len(findings) == 1

    findings, record = ingest(findings, load_round("round_duplicates.json"), profile, 2)
    assert record.new == 1, "only the writer.py finding is new"
    assert record.exact == 1, "the reworded loader.py finding is the same identity"
    loader = next(f for f in findings if f.rule_id == "undocumented-public-symbol")
    assert loader.first_seen_round == 1 and loader.last_seen_round == 2


def test_distinct_findings_at_the_same_anchor_stay_distinct(profile):
    findings, record = ingest([], load_round("round_distinct.json"), profile, 1)
    assert record.new == 3
    assert len({f.id for f in findings}) == 3
    assert len({f.anchor for f in findings}) == 1, "all three sit at one anchor"


def test_reingesting_the_same_round_creates_nothing(profile):
    scan = load_round("round_distinct.json")
    findings, _ = ingest([], scan, profile, 1)
    findings, record = ingest(findings, scan, profile, 2)
    assert record.new == 0 and record.exact == 3
    assert len(findings) == 3


def test_off_taxonomy_rules_are_quarantined_not_renamed(profile):
    scan = make_round(1, ("s1", ["src/a.py"], [
        {"rule_id": "vibes-are-off", "anchor": "src/a.py::f", "description": "Feels wrong."},
    ]))
    findings, record = ingest([], scan, profile, 1)
    assert record.unclassified == 1
    assert is_unclassified(findings[0], profile) is True
    assert findings[0].rule_id == "vibes-are-off", "the reported rule id is preserved verbatim"


def test_near_identical_anchor_and_wording_auto_merges(profile):
    first = make_round(1, ("s1", ["src/g.py"], [
        {"rule_id": "missing-error-handling", "anchor": "src/g.py::Gateway::send",
         "evidence": "r = httpx.post(url, json=body)",
         "description": "The HTTP call has no timeout and no error handling."},
    ]))
    findings, _ = ingest([], first, profile, 1)
    second = make_round(2, ("s1", ["src/g.py"], [
        {"rule_id": "missing-error-handling", "anchor": "src/g.py::Gateway::send",
         "evidence": "r = httpx.post(url, json=body, headers=h)",
         "description": "The HTTP call has no timeout and no error handling"},
    ]))
    _, record = ingest(findings, second, profile, 2)
    assert record.fuzzy == 1 and record.new == 0


def test_uncertain_band_becomes_a_duplicate_when_no_adjudicator_is_wired(profile):
    findings, pair = _uncertain_band_pair(profile)
    assert ADJUDICATE_FLOOR <= pair < AUTO_MERGE
    merged, record = ingest(findings[0], findings[1], profile, 2)
    assert record.new == 1 and record.fuzzy == 0 and record.adjudicated == 0
    assert len(merged) == 2, "unresolved ambiguity resolves toward a duplicate, never toward a merge"


def test_uncertain_band_merges_only_when_adjudication_says_same(profile):
    findings, _ = _uncertain_band_pair(profile)
    merged, record = ingest(findings[0], findings[1], profile, 2, adjudicator=lambda a, b: True)
    assert record.adjudicated == 1 and record.new == 0 and len(merged) == 1

    merged, record = ingest(findings[0], findings[1], profile, 2, adjudicator=lambda a, b: False)
    assert record.adjudicated == 0 and record.new == 1 and len(merged) == 2


def _uncertain_band_pair(profile):
    first = make_round(1, ("s1", ["src/g.py"], [
        {"rule_id": "missing-error-handling", "anchor": "src/g.py::Gateway::send",
         "evidence": "r = httpx.post(url)",
         "description": "The outbound call is unguarded against transport failure."},
    ]))
    existing, _ = ingest([], first, profile, 1)
    second = make_round(2, ("s1", ["src/g.py"], [
        {"rule_id": "missing-error-handling", "anchor": "src/g.py::Gateway::dispatch",
         "evidence": "r = httpx.post(url, timeout=None)",
         "description": "The outbound call is unguarded against transport failure."},
    ]))
    candidate = second.shards[0].findings[0].to_finding("s1", 2)
    return (existing, second), similarity(candidate, existing[0])


def test_two_incoming_findings_cannot_collapse_into_one_existing(profile):
    first = make_round(1, ("s1", ["src/g.py"], [
        {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
         "evidence": "except OSError:\n    pass", "description": "Error discarded."},
    ]))
    existing, _ = ingest([], first, profile, 1)
    second = make_round(2, ("s1", ["src/g.py"], [
        {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
         "evidence": "except OSError:\n    pass\n", "description": "Error discarded"},
        {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
         "evidence": "except IOError:\n    pass", "description": "Error discarded."},
    ]))
    merged, record = ingest(existing, second, profile, 2, adjudicator=lambda a, b: True)
    assert len(merged) == 2, "the second incoming finding cannot claim an already-matched existing one"
    assert record.new + record.fuzzy + record.adjudicated + record.exact == 2


def test_similarity_never_reaches_across_rules_or_files(profile):
    first = make_round(1, ("s1", ["src/g.py"], [
        {"rule_id": "missing-error-handling", "anchor": "src/g.py::send",
         "evidence": "x = 1", "description": "Unguarded call."},
    ]))
    existing, _ = ingest([], first, profile, 1)
    second = make_round(2,
        ("s1", ["src/g.py"], [{"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
                               "evidence": "x = 2", "description": "Unguarded call."}]),
        ("s2", ["src/h.py"], [{"rule_id": "missing-error-handling", "anchor": "src/h.py::send",
                               "evidence": "x = 3", "description": "Unguarded call."}]),
    )
    merged, record = ingest(existing, second, profile, 2, adjudicator=lambda a, b: True)
    assert record.new == 2 and len(merged) == 3


def test_re_derivation_reopens_a_fixed_finding(profile):
    scan = make_round(1, ("s1", ["src/g.py"], [
        {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
         "evidence": "except:\n    pass", "description": "Error discarded."},
    ]))
    findings, _ = ingest([], scan, profile, 1)
    findings[0].status = "fixed"
    findings, record = ingest(findings, make_round(2, *[(s.shard, s.files,
        [f.model_dump() for f in s.findings]) for s in scan.shards]), profile, 2)
    assert findings[0].status == "open" and record.reopened == 1


def test_oscillation_guard_disputes_after_three_reopens(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    for round_no in (2, 3, 4):
        findings[0].status = "fixed"
        findings, _ = ingest(findings, make_round(round_no, ("s1", ["src/g.py"], [raw])), profile, round_no)
    assert findings[0].reopen_count == 3
    assert findings[0].status == "disputed" and findings[0].adjudicated is False


def test_findings_no_longer_re_derived_close_after_k_rounds(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    findings, record = ingest(findings, make_round(2, ("s1", ["src/g.py"], [])), profile, 2)
    assert findings[0].status == "open" and record.stale_closed == 0, "one quiet round is not enough"
    findings, record = ingest(findings, make_round(3, ("s1", ["src/g.py"], [])), profile, 3)
    assert findings[0].status == "lapsed" and record.stale_closed == 1


def test_a_failed_round_closes_nothing(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    broken = make_round(3, ("s1", ["src/g.py"], []), ok=False)
    findings, record = ingest(findings, broken, profile, 3)
    assert findings[0].status == "open" and record.stale_closed == 0


def test_every_decision_is_recorded_in_history(profile):
    scan = load_round("round_distinct.json")
    findings, _ = ingest([], scan, profile, 1)
    findings, _ = ingest(findings, scan, profile, 2)
    for f in findings:
        actions = [e.action for e in f.history]
        assert actions[0] == "created"
        assert "seen" in actions
        assert all(e.round in (1, 2) for e in f.history)


def test_mechanical_source_survives_the_merge(profile):
    scan = make_round(1, ("s1", ["src/a.py"], [
        {"rule_id": "swallowed-exception", "anchor": "src/a.py::f",
         "evidence": "except:\n    pass", "description": "Discarded."},
    ]), source="mechanical")
    findings, _ = ingest([], scan, profile, 1)
    assert findings[0].source == "mechanical"


def test_similarity_is_symmetric_and_bounded(profile):
    a = RawFinding(rule_id="r", anchor="src/x.py::f", description="one").to_finding("s", 1)
    b = RawFinding(rule_id="r", anchor="src/x.py::g", description="two").to_finding("s", 1)
    assert similarity(a, b) == similarity(b, a)
    assert 0.0 <= similarity(a, b) <= 1.0
    assert similarity(a, a) == 1.0


def test_a_verified_finding_reopens_when_it_comes_back(profile):
    """`fixed` is not the only status a re-derivation must undo -- a fix that was confirmed and
    then regressed has to reopen too."""
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    findings[0].status = "verified"
    findings, record = ingest(findings, make_round(2, ("s1", ["src/g.py"], [raw])), profile, 2)
    assert findings[0].status == "open" and record.reopened == 1


def test_merging_never_mutates_the_findings_it_was_given(profile):
    """The caller keeps its list; merge returns a new one. A shallow copy would share history."""
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    existing, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    before = [f.model_dump() for f in existing]
    merged, _ = ingest(existing, make_round(2, ("s1", ["src/g.py"], [raw])), profile, 2)
    assert [f.model_dump() for f in existing] == before
    assert merged[0].last_seen_round == 2 and existing[0].last_seen_round == 1


def test_the_record_counts_every_raw_finding_it_was_handed(profile):
    scan = load_round("round_distinct.json")
    _, record = ingest([], scan, profile, 1)
    assert record.ingested == sum(len(s.findings) for s in scan.shards) == 3


def test_an_exact_match_is_recorded_with_a_score_of_one(profile):
    scan = load_round("round_distinct.json")
    findings, _ = ingest([], scan, profile, 1)
    findings, record = ingest(findings, scan, profile, 2)
    assert all(d.score == 1.0 for d in record.decisions if d.method == "exact")
    seen = [e for e in findings[0].history if e.action == "seen"]
    assert seen and seen[0].score == 1.0


def test_a_wontfixed_off_taxonomy_finding_is_out_of_the_bucket_count(profile):
    raw = {"rule_id": "vibes-are-off", "anchor": "src/a.py::f", "description": "Feels wrong."}
    findings, record = ingest([], make_round(1, ("s1", ["src/a.py"], [raw])), profile, 1)
    assert record.unclassified == 1

    findings[0].status = "wontfix"
    _, record = ingest(findings, make_round(2, ("s1", ["src/a.py"], [raw])), profile, 2)
    assert record.unclassified == 0, "a retired finding is not still in the bucket"


def test_the_audit_trail_says_when_a_rule_was_off_taxonomy(profile):
    off = {"rule_id": "vibes-are-off", "anchor": "src/a.py::f", "description": "Feels wrong."}
    on = {"rule_id": "swallowed-exception", "anchor": "src/a.py::g", "description": "Discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/a.py"], [off, on])), profile, 1)
    created = {f.rule_id: f.history[0].detail for f in findings}
    assert created["vibes-are-off"] == "off-taxonomy rule_id"
    assert created["swallowed-exception"] == ""


def test_a_fixed_finding_that_stops_being_re_derived_also_closes(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    findings[0].status = "fixed"
    findings, _ = ingest(findings, make_round(2, ("s1", ["src/g.py"], [])), profile, 2)
    assert findings[0].status == "fixed", "one quiet round is not enough"
    findings, record = ingest(findings, make_round(3, ("s1", ["src/g.py"], [])), profile, 3)
    assert findings[0].status == "lapsed" and record.stale_closed == 1


def test_an_already_closed_finding_is_not_closed_again(profile):
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    findings[0].status = "lapsed"
    _, record = ingest(findings, make_round(4, ("s1", ["src/g.py"], [])), profile, 4)
    assert record.stale_closed == 0


def test_evidence_text_is_backfilled_onto_a_record_that_lacks_it(profile):
    """A record carrying a hash but no snippet -- hand-edited, or written by an older run --
    gets its text back rather than staying unreadable forever."""
    raw = {"rule_id": "swallowed-exception", "anchor": "src/g.py::send",
           "evidence": "except:\n    pass", "description": "Error discarded."}
    findings, _ = ingest([], make_round(1, ("s1", ["src/g.py"], [raw])), profile, 1)
    findings[0].evidence = None

    findings, record = ingest(findings, make_round(2, ("s1", ["src/g.py"], [raw])), profile, 2)
    assert record.exact == 1
    assert findings[0].evidence == "except:\npass"
