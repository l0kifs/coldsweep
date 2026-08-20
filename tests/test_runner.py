"""The agent boundary: envelope handling, schema enforcement, retries, and phase isolation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from coldsweep.models import AgentConfig, FixResult, Models, Profile, RawFinding, Rule, Scope, Shard
from coldsweep.runner import AgentError, Runner, build_argv, extract_json, render, unwrap_envelope

STUB = str(Path(__file__).parent / "stub_agent.py")


def stub_profile(**agent_kw) -> Profile:
    return Profile(
        name="stub",
        scope=Scope(include=["src/**/*.py"]),
        models=Models(scan="model-a", fix="model-b", scan_alt="model-c"),
        agent=AgentConfig(command=[sys.executable, STUB], append_flags=False, retries=1,
                          timeout_s=60, **agent_kw),
        rules=[
            Rule(id="swallowed-exception", mode="absence", description="An except block that discards the error."),
            Rule(id="undocumented-public-symbol", mode="presence", description="A public symbol is undocumented."),
        ],
    )


def test_envelope_is_unwrapped():
    assert unwrap_envelope('{"type":"result","result":"{\\"findings\\": []}"}') == '{"findings": []}'


def test_bare_output_passes_through():
    assert unwrap_envelope('{"findings": []}') == '{"findings": []}'


def test_an_errored_envelope_is_an_error():
    with pytest.raises(AgentError):
        unwrap_envelope('{"type":"result","is_error":true,"result":"rate limited"}')


@pytest.mark.parametrize("text", [
    '{"findings": []}',
    'Here you go:\n```json\n{"findings": []}\n```\nHope that helps.',
    'prose {not json} then {"findings": []}',
])
def test_json_is_recovered_from_chatty_responses(text):
    assert extract_json(text) == {"findings": []}


def test_braces_inside_strings_do_not_confuse_extraction():
    assert extract_json('{"description": "use {files} here"}') == {"description": "use {files} here"}


def test_no_json_at_all_is_an_error():
    with pytest.raises(AgentError):
        extract_json("nothing useful here")


def test_scan_alternates_models_so_blind_spots_do_not_align():
    runner = Runner(Path("."), stub_profile())
    assert [runner.scan_phase(n).model for n in (1, 2, 3, 4)] == ["model-a", "model-c", "model-a", "model-c"]


def test_without_scan_alt_every_round_uses_the_same_model():
    profile = stub_profile()
    profile.models.scan_alt = None
    runner = Runner(Path("."), profile)
    assert {runner.scan_phase(n).model for n in (1, 2, 3)} == {"model-a"}


def test_scan_gets_read_only_tools_and_fix_gets_write_access():
    runner = Runner(Path("."), stub_profile())
    assert "Edit" not in runner.scan_phase(1).tools
    assert "Edit" in runner.fix_phase().tools


def test_argv_carries_model_and_tool_restrictions():
    profile = stub_profile()
    profile.agent.append_flags = True
    argv = build_argv(profile.agent, Runner(Path("."), profile).scan_phase(1))
    assert argv[argv.index("--model") + 1] == "model-a"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--permission-mode") + 1] == profile.agent.permission_mode


def test_scan_prompt_carries_only_the_shard_and_the_taxonomy():
    runner = Runner(Path("."), stub_profile())
    prompt = render("scan.md", files="- `src/a.py`", rules=runner._rules_block())
    assert "src/a.py" in prompt and "swallowed-exception" in prompt
    for leak in ("findings.jsonl", "previous round", "first_seen_round", "status"):
        assert leak not in prompt, f"scan prompt must not leak {leak}"


def test_scan_returns_findings_from_the_shard(repo: Path):
    (repo / "src" / "a.py").write_text(
        'def load():\n    """Doc."""\n    try:\n        g()\n    except OSError:\n        pass\n')
    results = asyncio.run(Runner(repo, stub_profile()).scan([Shard(id="s1", files=["src/a.py"])], 1))
    assert len(results) == 1 and results[0].ok
    assert [f.rule_id for f in results[0].findings] == ["swallowed-exception"]
    assert results[0].model == "model-a"


def test_a_schema_violation_is_retried_then_fails_the_shard(repo: Path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "badschema")
    results = asyncio.run(Runner(repo, stub_profile()).scan([Shard(id="s1", files=["src/a.py"])], 1))
    assert results[0].ok is False and "attempt" in results[0].error
    assert results[0].error.startswith("scan agent"), "the failure names the phase that failed"


def test_a_transient_failure_is_absorbed_by_the_retry(repo: Path, monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_MODE", "flaky")
    monkeypatch.setenv("STUB_STATE", str(tmp_path / "state"))
    results = asyncio.run(Runner(repo, stub_profile()).scan([Shard(id="s1", files=["src/a.py"])], 1))
    assert results[0].ok and results[0].attempts == 2


def test_a_failed_shard_fails_the_round_rather_than_reducing_coverage(repo: Path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "garbage")
    from coldsweep.models import ScanRound
    results = asyncio.run(Runner(repo, stub_profile()).scan(
        [Shard(id="s1", files=["src/a.py"]), Shard(id="s2", files=["src/b.py"])], 1))
    assert ScanRound(round=1, shards=results).ok is False
    assert len(ScanRound(round=1, shards=results).failed_shards) == 2


def test_the_adjudicator_falls_back_to_different_when_the_agent_fails(repo: Path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "garbage")
    from coldsweep.models import RawFinding
    a = RawFinding(rule_id="r", anchor="src/a.py::f", description="x").to_finding("s", 1)
    b = RawFinding(rule_id="r", anchor="src/a.py::g", description="y").to_finding("s", 1)
    assert Runner(repo, stub_profile()).adjudicator()(a, b) is False


def test_rules_a_subsystem_already_decides_are_kept_out_of_the_scan_prompt():
    from coldsweep.models import Rule
    profile = stub_profile()
    profile.rules.append(Rule(id="untested-behaviour", mode="presence",
                              description="Decided by the mutation subsystem.", decided_by="code"))
    block = Runner(Path("."), profile)._rules_block()
    assert "untested-behaviour" not in block, "agents handle only the tail"
    assert "swallowed-exception" in block
    assert "untested-behaviour" in profile.rule_ids, "still taxonomy, so its findings are classified"


def test_a_profile_with_no_agent_rules_spawns_no_scan_agents(repo: Path, monkeypatch):
    from coldsweep.models import Rule
    monkeypatch.setenv("STUB_MODE", "garbage")
    profile = stub_profile()
    profile.rules = [Rule(id="only-code", mode="presence", description="d", decided_by="code")]
    results = asyncio.run(Runner(repo, profile).scan([Shard(id="s1", files=["src/a.py"])], 1))
    assert results[0].ok and results[0].findings == [] and results[0].attempts == 0


@pytest.mark.parametrize(("text", "expected"), [
    (r'{"d": "he said \"stop\""}', {"d": 'he said "stop"'}),
    (r'{"d": "a backslash \\"}', {"d": "a backslash \\"}),
    (r'{"d": "brace } inside"}', {"d": "brace } inside"}),
    (r'{"d": "\\"} trailing', {"d": "\\"}),
])
def test_extraction_tracks_strings_and_escapes(text, expected):
    """The scanner counts braces, so a brace or an escaped quote inside a string must not end it."""
    assert extract_json(text) == expected


def test_extraction_takes_the_first_complete_object():
    assert extract_json('{"a": 1} {"b": 2}') == {"a": 1}


def test_a_rules_block_says_so_when_a_profile_has_no_agent_rules():
    profile = stub_profile()
    profile.rules = []
    assert "no rules" in Runner(Path("."), profile)._rules_block()


def test_every_rule_gets_its_own_line():
    block = Runner(Path("."), stub_profile())._rules_block()
    assert len(block.splitlines()) == 2
    assert all(line.startswith("- `") for line in block.splitlines())


def test_fix_returns_one_result_per_group(repo: Path):
    findings = [RawFinding(rule_id="swallowed-exception", anchor=f"src/{n}.py::f",
                           description="d").to_finding("s", 1) for n in ("a", "b")]
    groups = {f.file: [f] for f in findings}
    outcomes = asyncio.run(Runner(repo, stub_profile()).fix(groups))
    assert sorted(outcomes) == ["src/a.py", "src/b.py"]
    assert all(isinstance(r, FixResult) for r in outcomes.values())
    assert {o.id for r in outcomes.values() for o in r.results} == {f.id for f in findings}


def test_a_failing_fix_agent_is_returned_not_raised(repo: Path, monkeypatch):
    """One unfixable file must not lose the results of the others."""
    monkeypatch.setenv("STUB_MODE", "garbage")
    finding = RawFinding(rule_id="swallowed-exception", anchor="src/a.py::f",
                         description="d").to_finding("s", 1)
    outcomes = asyncio.run(Runner(repo, stub_profile()).fix({"src/a.py": [finding]}))
    assert isinstance(outcomes["src/a.py"], AgentError)


def test_a_fix_prompt_names_the_mode_of_every_rule(repo: Path):
    known = RawFinding(rule_id="swallowed-exception", anchor="src/a.py::f", description="d",
                       evidence="except:\n    pass").to_finding("s", 1)
    unknown = RawFinding(rule_id="not-in-profile", anchor="src/a.py::g",
                         description="d").to_finding("s", 1)
    prompt = render("fix.md", files="- `src/a.py`", rules="", findings="")
    assert "{{findings}}" not in prompt
    runner = Runner(repo, stub_profile())
    assert runner.profile.mode_of(known.rule_id) == "absence"
    assert runner.profile.mode_of(unknown.rule_id) is None


def test_adjudication_returns_a_verdict(repo: Path):
    a = RawFinding(rule_id="r", anchor="src/a.py::f", description="one").to_finding("s", 1)
    b = RawFinding(rule_id="r", anchor="src/a.py::g", description="two").to_finding("s", 1)
    result = asyncio.run(Runner(repo, stub_profile()).adjudicate_pair(a, b))
    assert result.verdict == "different" and result.same is False
    assert result.reason


def test_an_adjudication_prompt_fills_both_sides_even_without_evidence():
    a = RawFinding(rule_id="r", anchor="src/a.py::f", description="one").to_finding("s", 1)
    prompt = render("adjudicate.md", a_rule=a.rule_id, a_anchor=a.anchor,
                    a_description=a.description, a_evidence=a.evidence or "(none)",
                    b_rule="r", b_anchor="src/a.py::g", b_description="two", b_evidence="(none)")
    assert "{{" not in prompt and prompt.count("(none)") == 2


def test_the_attempt_count_is_the_retry_budget_plus_the_first_try(repo: Path, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "garbage")
    profile = stub_profile()
    profile.agent.retries = 3
    results = asyncio.run(Runner(repo, profile).scan([Shard(id="s1", files=["src/a.py"])], 1))
    assert results[0].ok is False and results[0].attempts == 4
    assert "4 attempt(s)" in results[0].error


@pytest.mark.parametrize(("text", "expected"), [
    # A brace inside a string must not close the object...
    ('{"d": "{ unbalanced"}', {"d": "{ unbalanced"}),
    ('{"d": "closing } brace"}', {"d": "closing } brace"}),
    # ...and an escaped quote must not end the string that hides it.
    (r'{"d": "a \" } b"}', {"d": 'a " } b'}),
    (r'{"d": "trailing backslash \\", "e": 1}', {"d": "trailing backslash \\", "e": 1}),
])
def test_brace_counting_respects_string_boundaries(text, expected):
    """These are the only inputs where mis-tracking string state changes the answer: a brace
    that is inside a string, reached past an escape."""
    assert extract_json(text) == expected


def test_an_empty_response_is_an_error():
    for text in ("", "   \n  "):
        with pytest.raises(AgentError, match="empty agent response"):
            extract_json(text)


def test_an_envelope_without_a_result_field_passes_through():
    assert unwrap_envelope('{"type": "result"}') == '{"type": "result"}'


def test_a_non_object_payload_passes_through():
    assert unwrap_envelope("[1, 2]") == "[1, 2]"
    assert unwrap_envelope("plain text") == "plain text"


def test_a_structured_result_is_re_encoded():
    assert json.loads(unwrap_envelope('{"result": {"findings": []}}')) == {"findings": []}


def test_prose_butted_straight_against_the_closing_brace_is_still_trimmed():
    """The slice must end at the brace, not one past it: agents do write `}Hope that helps.`"""
    assert extract_json('{"findings": []}Hope that helps.') == {"findings": []}
    assert extract_json('{"a": 1}x') == {"a": 1}


def test_an_empty_string_is_a_string_like_any_other():
    """`""` closes on its second quote; treating that quote as escaped swallows the rest."""
    assert extract_json('{"": 1}') == {"": 1}
    assert extract_json('{"a": ""}') == {"a": ""}


def test_an_errored_envelope_reports_what_the_agent_said():
    with pytest.raises(AgentError, match="rate limited"):
        unwrap_envelope('{"is_error": true, "result": "rate limited"}')
