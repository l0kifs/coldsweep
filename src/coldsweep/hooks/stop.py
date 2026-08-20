"""Stop hook: refuses to let an interactive session finish while the gate is shut.

Deliberately contains no logic of its own. It shells out to `coldsweep converged` and translates
that exit code into the hook protocol -- exit 0 to allow the stop, exit 2 to send the session
back to work. Any judgement here would be a second, divergent copy of the termination rule.

The task is named explicitly, as everywhere else in coldsweep -- a hook that guessed which task
it was gating would be the same stale-state trap the tool exists to remove.

Install into .claude/settings.json:

    {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "coldsweep-stop-hook --task refactor-auth"}]}]}}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ALLOW_STOP = 0
BLOCK_STOP = 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    task = os.environ.get("COLDSWEEP_TASK")
    for flag in ("--task", "-t"):
        if flag in args:
            index = args.index(flag)
            task = args[index + 1] if index + 1 < len(args) else None
    if not task:
        print("coldsweep-stop-hook: no task named; pass --task <name> or set COLDSWEEP_TASK.", file=sys.stderr)
        return BLOCK_STOP

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        # The loop-breaker below lives in this payload. Unable to read it, the hook cannot rule
        # out that it was set, and a wrongly blocked stop is unrecoverable while a wrongly
        # allowed one costs one manual re-check. Say so and stand down.
        print("coldsweep-stop-hook: hook payload was not a JSON object; allowing the stop rather "
              "than risking a gate that cannot be reopened.", file=sys.stderr)
        return ALLOW_STOP

    # Without this the gate jams shut permanently: the session can never satisfy a hook that
    # keeps firing on the stop it just blocked.
    if payload.get("stop_hook_active"):
        return ALLOW_STOP

    cwd = payload.get("cwd")
    result = subprocess.run([sys.executable, "-m", "coldsweep", "converged", "--task", task],
                            cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode == ALLOW_STOP:
        return ALLOW_STOP

    print(f"coldsweep task {task!r} has not converged. Run `coldsweep status --task {task}` for the open "
          f"findings, then `coldsweep run --task {task}`.", file=sys.stderr)
    return BLOCK_STOP


if __name__ == "__main__":
    sys.exit(main())
