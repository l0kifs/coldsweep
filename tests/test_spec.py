"""Spec authoring, freeze and traceability.

The standing limit is pinned here too: the loop never validates the spec itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git_init, owned_rules

from coldsweep.converge import evaluate
from coldsweep.models import Profile, Scope, SpecConfig, SpecLock
from coldsweep.spec import (
    SpecError,
    anchor_for,
    blockers,
    drift_of,
    find_markers,
    freeze,
    item_sha,
    load_spec,
    parse_spec,
    run,
    spec_context,
)

SPEC = """# Session handling

## Lifetime

### FR-1 Session expiry

A session expires 30 minutes after the last request. An expired session returns 401.

### FR-2 Refresh window

A refresh within the window extends it; a refresh outside it is rejected.

### FR-3 Audit log

Every expiry is written to the audit log.
"""


def config(**kw) -> SpecConfig:
    kw.setdefault("unimplemented_rule_id", "unimplemented-spec-item")
    kw.setdefault("stale_reference_rule_id", "stale-spec-reference")
    return SpecConfig(**kw)


def test_items_are_split_at_their_own_headings():
    items = parse_spec(SPEC, config())
    assert [i.id for i in items] == ["FR-1", "FR-2", "FR-3"]
    assert items[0].title == "Session expiry"
    assert "401" in items[0].body and "refresh" not in items[0].body.lower()


def test_anchors_address_the_item_not_the_heading_text():
    assert next(i.anchor for i in parse_spec(SPEC, config())) == "SPEC.md::FR-1"


def test_retitling_an_item_keeps_its_identity():
    renamed = SPEC.replace("### FR-1 Session expiry", "### FR-1 Expiry of sessions")
    before = {i.id: i.sha for i in parse_spec(SPEC, config())}
    after = {i.id: i.sha for i in parse_spec(renamed, config())}
    assert before == after, "the id is written down, and the title is not part of the body"


def test_reflowing_a_paragraph_is_not_a_change():
    reflowed = SPEC.replace("A session expires 30 minutes after the last request.",
                            "A session expires 30 minutes\nafter the last request.")
    assert drift_of(SpecLock(spec="SPEC.md", items={i.id: i.sha for i in parse_spec(SPEC, config())}),
                    parse_spec(reflowed, config())).clean


def test_rewording_an_item_is_a_change():
    reworded = SPEC.replace("30 minutes", "15 minutes")
    drift = drift_of(SpecLock(spec="SPEC.md", items={i.id: i.sha for i in parse_spec(SPEC, config())}),
                     parse_spec(reworded, config()))
    assert drift.changed == ["FR-1"] and not drift.added and not drift.removed


def test_added_and_removed_items_are_both_drift():
    frozen = SpecLock(spec="SPEC.md", items={i.id: i.sha for i in parse_spec(SPEC, config())})
    trimmed = SPEC.split("### FR-3")[0] + "### FR-9 New thing\n\nSomething else.\n"
    drift = drift_of(frozen, parse_spec(trimmed, config()))
    assert drift.added == ["FR-9"] and drift.removed == ["FR-3"]
    assert not drift.clean and len(drift.reasons()) == 2


def test_duplicate_ids_are_refused():
    with pytest.raises(SpecError, match="duplicate spec item id"):
        parse_spec(SPEC + "\n### FR-1 Again\n\nBody.\n", config())


def test_a_pattern_without_an_id_group_is_refused():
    with pytest.raises(ValueError, match=r"\(\?P<id>"):
        SpecConfig(unimplemented_rule_id="x", item_pattern=r"^### (.+)$")


# --- traceability ----------------------------------------------------------

IMPL = '''class Session:
    def expire(self):
        # spec: FR-1
        raise NotImplementedError


def refresh():
    """Extend the window.

    spec: FR-2
    """
    return True


# spec: FR-404
def orphan():
    return None
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "session.py").write_text(IMPL)
    (tmp_path / "SPEC.md").write_text(SPEC)
    git_init(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def profile(**kw) -> Profile:
    cfg = config(**kw)
    return Profile(name="features", scope=Scope(include=["src/**/*.py", "SPEC.md"]),
                   fix_scope="task", spec=cfg,
                   rules=owned_rules(*(r for r in (cfg.unimplemented_rule_id,
                                                   cfg.stale_reference_rule_id) if r)))


def test_markers_anchor_to_the_enclosing_symbol(project: Path):
    markers = find_markers(project, profile())
    assert markers["FR-1"] == [("src/session.py", "src/session.py::Session::expire")]
    assert markers["FR-2"] == [("src/session.py", "src/session.py::refresh")]


def test_a_marker_outside_any_symbol_anchors_to_the_file(project: Path):
    assert find_markers(project, profile())["FR-404"] == [("src/session.py", "src/session.py")]


def test_anchor_for_never_returns_a_line_number():
    assert anchor_for("a.py", "def f():\n    pass\n", 2) == "a.py::f"
    assert anchor_for("a.md", "# heading\n", 1) == "a.md"


def test_an_unmarked_item_is_a_deterministic_finding(project: Path):
    findings, report = run(project, profile(), None)
    unimplemented = [f for f in findings if f.rule_id == "unimplemented-spec-item"]
    assert [f.anchor for f in unimplemented] == ["SPEC.md::FR-3"]
    assert report.implemented == 2 and report.unimplemented == 1
    assert unimplemented[0].evidence is None, "presence findings carry no evidence"


def test_a_marker_naming_a_deleted_item_is_a_finding(project: Path):
    findings, report = run(project, profile(), None)
    stale = [f for f in findings if f.rule_id == "stale-spec-reference"]
    assert [f.anchor for f in stale] == ["src/session.py"]
    assert stale[0].evidence == "spec: FR-404" and report.stale_markers == 1


def test_stale_markers_can_be_switched_off(project: Path):
    findings, _ = run(project, profile(stale_reference_rule_id=None), None)
    assert all(f.rule_id != "stale-spec-reference" for f in findings)


def test_a_marker_is_addressing_not_proof(project: Path):
    """FR-1's implementation raises NotImplementedError, and the deterministic half accepts it."""
    findings, _ = run(project, profile(), None)
    assert all(f.anchor != "SPEC.md::FR-1" for f in findings)
    context = spec_context(project, profile(), ["src/session.py"])
    assert "FR-1" in context and "a marker comment naming an item is not an implementation" in context


def test_the_scan_context_carries_only_the_items_the_shard_claims(project: Path):
    (project / "src" / "other.py").write_text("# spec: FR-3\ndef audit():\n    return 1\n")
    context = spec_context(project, profile(), ["src/other.py"])
    assert "FR-3" in context and "FR-1" not in context


def test_the_scan_context_is_empty_without_a_spec(project: Path):
    assert spec_context(project, Profile(scope=Scope(include=["src/**/*.py"])), ["src/session.py"]) == ""


# --- freeze and the gate ---------------------------------------------------

def test_an_unfrozen_spec_shuts_the_gate(project: Path):
    reasons = blockers(project, profile(), project / "spec.lock")
    assert reasons and "not frozen" in reasons[0]
    assert not evaluate([], profile(), [1, 2, 3], reasons).converged


def test_a_frozen_spec_that_has_not_drifted_does_not_block(project: Path):
    lock_path = project / "spec.lock"
    lock, _ = freeze(project, profile(), 1)
    lock_path.write_text(lock.model_dump_json())
    assert blockers(project, profile(), lock_path) == []
    assert evaluate([], profile(), [1, 2, 3]).converged


def test_editing_a_frozen_spec_shuts_the_gate_again(project: Path):
    lock_path = project / "spec.lock"
    lock, _ = freeze(project, profile(), 1)
    lock_path.write_text(lock.model_dump_json())
    (project / "SPEC.md").write_text(SPEC.replace("30 minutes", "15 minutes"))

    reasons = blockers(project, profile(), lock_path)
    assert reasons and "reworded" in reasons[0] and "re-freeze deliberately" in reasons[0]
    report = evaluate([], profile(), [1, 2, 3], reasons)
    assert not report.converged and report.needs_triage is False, "drift is not something triage clears"


def test_a_missing_spec_file_is_reported_not_ignored(project: Path):
    (project / "SPEC.md").unlink()
    assert "no spec at SPEC.md" in blockers(project, profile(), project / "spec.lock")[0]


def test_a_spec_with_no_matching_items_names_the_pattern(project: Path):
    (project / "SPEC.md").write_text("# Just prose\n\nNo items here.\n")
    with pytest.raises(SpecError, match="no items matching"):
        load_spec(project, config())


def test_profiles_without_a_spec_are_untouched(project: Path):
    plain = Profile(scope=Scope(include=["src/**/*.py"]))
    assert blockers(project, plain, project / "spec.lock") == []
    assert run(project, plain, None) == ([], run(project, plain, None)[1])


def test_the_loop_never_validates_the_spec_itself(project: Path):
    """A spec that omits a requirement converges cleanly. Documented, not fixed."""
    (project / "SPEC.md").write_text("### FR-1 Session expiry\n\nA session expires.\n")
    (project / "src" / "session.py").write_text("# spec: FR-1\ndef expire():\n    return None\n")
    lock_path = project / "spec.lock"
    lock, _ = freeze(project, profile(), 1)
    lock_path.write_text(lock.model_dump_json())

    findings, report = run(project, profile(), lock)
    assert findings == [] and report.unimplemented == 0
    assert evaluate([], profile(), [1, 2, 3], blockers(project, profile(), lock_path)).converged


def test_item_sha_ignores_formatting_only():
    assert item_sha("a  b\n\nc") == item_sha("a b c")
    assert item_sha("a b") != item_sha("a c")


def test_the_spec_document_is_never_handed_to_an_agent_as_a_shard(project: Path):
    """An agent given the spec reports its own items back, under anchors that close never."""
    from coldsweep.shard import build_shards
    with_spec = Profile(scope=Scope(include=["src/**/*.py", "SPEC.md"]), spec=config(),
                       rules=owned_rules("unimplemented-spec-item", "stale-spec-reference"))
    without = Profile(scope=Scope(include=["src/**/*.py", "SPEC.md"]))
    assert "SPEC.md" not in [f for s in build_shards(project, with_spec) for f in s.files]
    assert "SPEC.md" in [f for s in build_shards(project, without) for f in s.files]


def test_normalization_collapses_whitespace_without_joining_words():
    """Runs of whitespace become one space, not nothing: "a b" must not normalize to "ab"."""
    assert item_sha("a  b") == item_sha("a b")
    assert item_sha("a b") != item_sha("ab")


def test_an_item_pattern_without_a_title_group_still_parses():
    config_no_title = SpecConfig(unimplemented_rule_id="u",
                                 item_pattern=r"^###\s+(?P<id>[A-Za-z]+-\d+)")
    items = parse_spec(SPEC, config_no_title)
    assert [i.id for i in items] == ["FR-1", "FR-2", "FR-3"]
    assert items[0].title == "", "no title group means no title, not a placeholder"


def test_an_unparseable_source_anchors_to_the_file(project: Path):
    assert anchor_for("broken.py", "def (:\n", 1) == "broken.py"


def test_markers_are_not_searched_when_there_is_no_spec(project: Path):
    assert find_markers(project, Profile(scope=Scope(include=["src/**/*.py"]))) == {}


def test_an_unfrozen_run_reports_zero_frozen_items(project: Path):
    _, report = run(project, profile(), None)
    assert report.frozen == 0 and report.items == 3


def test_the_scan_context_separates_the_items_it_carries(project: Path):
    (project / "src" / "two.py").write_text("# spec: FR-2\n# spec: FR-3\ndef f():\n    return 1\n")
    context = spec_context(project, profile(), ["src/two.py"])
    assert "### FR-2 Refresh window" in context
    assert "### FR-3 Audit log" in context
    assert "\n\n### FR-3" in context, "items are separated, not run together"


def test_the_scan_context_is_empty_when_the_shard_claims_nothing(project: Path):
    (project / "src" / "plain.py").write_text("def f():\n    return 1\n")
    assert spec_context(project, profile(), ["src/plain.py"]) == ""


def test_a_missing_spec_fails_the_shard_rather_than_scanning_without_its_task_statement(
        project: Path):
    """Returning "" here scanned a features shard with no spec and reported nothing missing."""
    (project / "SPEC.md").unlink()
    with pytest.raises(SpecError):
        spec_context(project, profile(), ["src/session.py"])


def test_only_python_files_get_symbol_anchors():
    """A marker in prose anchors to the file; parsing it as Python would be a guess."""
    source = "def looks_like_code():\n    pass\n"
    assert anchor_for("notes.md", source, 1) == "notes.md"
    assert anchor_for("mod.py", source, 1) == "mod.py::looks_like_code"
