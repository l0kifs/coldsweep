"""Mechanical checks are exhaustive over their rule, so their coverage is real, not claimed."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from coldsweep.mechanical import CHUNK, MechanicalError, run_all, run_check
from coldsweep.models import MechanicalCheck, Profile, Scope, Shard


def emitter(payload: str) -> str:
    code = f"import sys; sys.stdout.write({payload!r})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_output_is_mapped_onto_the_configured_rule(repo: Path):
    check = MechanicalCheck(rule_id="missing-docstring",
                            command=emitter('[{"anchor": "src/a.py::f", "description": "no docstring"}]'))
    found = run_check(repo, check, ["src/a.py"])
    assert [f.rule_id for f in found] == ["missing-docstring"]


def test_a_check_may_not_relabel_itself(repo: Path):
    check = MechanicalCheck(rule_id="missing-docstring",
                            command=emitter('[{"rule_id": "something-else", "anchor": "src/a.py::f"}]'))
    assert run_check(repo, check, ["src/a.py"])[0].rule_id == "missing-docstring"


def test_both_output_shapes_are_accepted(repo: Path):
    wrapped = MechanicalCheck(rule_id="r", command=emitter('{"findings": [{"anchor": "src/a.py::f"}]}'))
    bare = MechanicalCheck(rule_id="r", command=emitter('[{"anchor": "src/a.py::f"}]'))
    assert len(run_check(repo, wrapped, ["src/a.py"])) == len(run_check(repo, bare, ["src/a.py"])) == 1


def test_the_file_list_is_substituted_and_quoted(repo: Path):
    check = MechanicalCheck(
        rule_id="r",
        command=f"{sys.executable} -c 'import sys, json; "
                'print(json.dumps([{"anchor": p} for p in sys.argv[1:]]))\' {files}',
    )
    found = run_check(repo, check, ["src/a.py", "src/b.py"])
    assert sorted(f.anchor for f in found) == ["src/a.py", "src/b.py"]


def test_a_nonzero_exit_with_output_is_fine(repo: Path):
    check = MechanicalCheck(
        rule_id="r",
        command=f"{sys.executable} -c 'print(\"[]\"); raise SystemExit(1)'")
    assert run_check(repo, check, ["src/a.py"]) == []


def test_a_nonzero_exit_with_no_output_is_an_error(repo: Path):
    check = MechanicalCheck(rule_id="r", command=f"{sys.executable} -c 'raise SystemExit(3)'")
    with pytest.raises(MechanicalError):
        run_check(repo, check, ["src/a.py"])


def test_non_json_output_is_an_error(repo: Path):
    with pytest.raises(MechanicalError):
        run_check(repo, MechanicalCheck(rule_id="r", command="echo not-json"), ["src/a.py"])


def test_malformed_findings_are_an_error(repo: Path):
    check = MechanicalCheck(rule_id="r", command=emitter('[{"description": "no anchor"}]'))
    with pytest.raises(MechanicalError):
        run_check(repo, check, ["src/a.py"])


def test_findings_are_attributed_back_to_their_shard(repo: Path):
    profile = Profile(
        scope=Scope(include=["src/**/*.py"]),
        mechanical=[MechanicalCheck(rule_id="r", command=emitter('[{"anchor": "src/b.py::f"}]'))],
    )
    shards = [Shard(id="s-one", files=["src/a.py"]), Shard(id="s-two", files=["src/b.py"])]
    found = run_all(repo, profile, shards, 1)
    assert [f.shard for f in found] == ["s-two"]
    assert found[0].source == "mechanical"


def test_no_checks_means_no_subprocesses(repo: Path):
    assert run_all(repo, Profile(), [], 1) == []


def test_no_files_means_no_subprocess(repo: Path):
    check = MechanicalCheck(rule_id="r", command="exit 7")
    assert run_check(repo, check, []) == []


def test_a_long_file_list_is_chunked_across_invocations(repo: Path):
    """One invocation per 200 files, so a large scope cannot overflow the argument list.

    The expected split is written out rather than read from the module: a test that derives it
    from ``CHUNK`` moves with any change to ``CHUNK`` and so can never detect one.
    """
    check = MechanicalCheck(
        rule_id="r",
        command=f"{shlex.quote(sys.executable)} -c 'import sys, json; "
                'print(json.dumps([{"anchor": "src/a.py", "description": str(len(sys.argv) - 1)}]))\' {files}',
    )
    assert CHUNK == 200, "the split below is written out, so it has to be checked against"
    found = run_check(repo, check, [f"src/f{i}.py" for i in range(205)])
    assert len(found) == 2, "two invocations, one finding each"
    assert sorted(int(f.description) for f in found) == [5, 200]


def test_an_id_from_a_check_is_dropped_rather_than_rejected(repo: Path):
    """Real linters emit ids of their own; identity here is derived, so theirs is discarded."""
    check = MechanicalCheck(rule_id="r", command=emitter(
        '[{"id": "LINT-42", "anchor": "src/a.py::f", "description": "d"}]'))
    found = run_check(repo, check, ["src/a.py"])
    assert len(found) == 1 and found[0].rule_id == "r"
    assert found[0].to_finding("s", 1).id.startswith("r-")


def test_a_finding_outside_every_shard_still_gets_a_home(repo: Path):
    profile = Profile(
        scope=Scope(include=["src/**/*.py"]),
        mechanical=[MechanicalCheck(rule_id="r", command=emitter('[{"anchor": "vendor/x.py::f"}]'))],
    )
    found = run_all(repo, profile, [Shard(id="s-one", files=["src/a.py"])], 1)
    assert [f.shard for f in found] == ["mechanical"]
