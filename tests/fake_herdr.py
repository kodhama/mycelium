"""A test-only stand-in for herdr 0.9.3's read commands, for tests/test_pane.py.

It answers only the reads `mycelium.pane` makes, in the reply shapes probed
live from herdr 0.9.3 on 2026-11-09, from a JSON state file that
FAKE_HERDR_STATE names:

    {"panes": [<pane record>...], "agents": [<agent record>...],
     "process_info": [<process_info record>...]}

    python fake_herdr.py pane list
    python fake_herdr.py pane get <pane_id>
    python fake_herdr.py agent get <pane_id>
    python fake_herdr.py pane process-info --pane <pane_id>

A missing record is answered as herdr 0.9.3 answers it: a JSON error on
stderr, nothing on stdout, and exit 1 (`pane_not_found` or
`agent_not_found`). A pane listed with no process info answers
`process-info` with `pane_not_found`, as a herdr pane carrying
`restore_error` does. Any other command (every write, such as typing or
renaming) is refused as herdr refuses an unknown one, so a write would surface
as an error in the tests.

FAKE_HERDR_FAIL makes every call fail: `exit` (exit 3, a line on stderr),
`garbage` (non-JSON, exit 0), `error` (a JSON error herdr would not mean as
not-found, exit 1) or `sleep` (outlast any test timeout). In the state file,
`read_delay` makes each `pane process-info` answer only after that many
seconds, `delayed` lists `[call, seconds]` pairs answered only after that
long, `slow` lists calls that hang, and `broken` lists calls that fail. FAKE_HERDR_LOG, when
set, names a file that gets one JSON line per call's arguments.
"""

import json
import os
import sys
import time
from pathlib import Path


def reply(name: str, result: dict) -> int:
    print(json.dumps({"id": f"cli:{name}", "result": result}))
    return 0


def not_found(name: str, code: str, message: str) -> int:
    error = {"error": {"code": code, "message": message}, "id": f"cli:{name}"}
    print(json.dumps(error), file=sys.stderr)
    return 1


def by_pane(records: list[dict], pane_id: str) -> dict | None:
    return next((record for record in records if record.get("pane_id") == pane_id), None)


def answer(argv: list[str], state: dict) -> int:
    panes = state.get("panes", [])
    if argv[:2] == ["pane", "process-info"]:  # a pane read answers, but only after this long
        time.sleep(state.get("read_delay", 0))
    for call, seconds in state.get("delayed", []):
        if argv == call:
            time.sleep(seconds)
    if argv in state.get("slow", []):  # this one call hangs, as a stalled herdr would
        time.sleep(30)
        return 0
    if argv in state.get("broken", []):  # this one call fails as a herdr fault would
        error = {"error": {"code": "internal", "message": "read failed"}, "id": "cli:x"}
        print(json.dumps(error), file=sys.stderr)
        return 1
    match argv:
        case ["pane", "list"]:
            return reply("pane:list", {"panes": panes, "type": "pane_list"})
        case ["pane", "get", pane_id]:
            pane = by_pane(panes, pane_id)
            if pane is None:
                return not_found("pane:get", "pane_not_found", f"pane {pane_id} not found")
            return reply("pane:get", {"pane": pane, "type": "pane_info"})
        case ["agent", "get", pane_id]:
            agent = by_pane(state.get("agents", []), pane_id)
            if agent is None:
                message = f"agent target {pane_id} not found"
                return not_found("agent:get", "agent_not_found", message)
            return reply("agent:get", {"agent": agent, "type": "agent_info"})
        case ["pane", "process-info", "--pane", pane_id]:
            info = by_pane(state.get("process_info", []), pane_id)
            if info is None:
                return not_found("pane:process_info", "pane_not_found", "pane not found")
            return reply("pane:process_info", {"process_info": info, "type": "pane_process_info"})
    print(f"unknown command: {' '.join(argv)}", file=sys.stderr)
    print("run 'herdr --help' for usage", file=sys.stderr)
    return 2


def fail(mode: str) -> int:
    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "garbage":
        print("herdr: this is not json")
        return 0
    if mode == "error":
        message = "the herdr server is not running"
        print(
            json.dumps({"error": {"code": "server_unavailable", "message": message}}),
            file=sys.stderr,
        )
        return 1
    print("herdr: something went wrong", file=sys.stderr)
    return 3


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_HERDR_LOG")
    if log:
        with open(log, "a") as file:
            file.write(json.dumps(argv) + "\n")
    mode = os.environ.get("FAKE_HERDR_FAIL")
    if mode:
        return fail(mode)
    state = json.loads(Path(os.environ["FAKE_HERDR_STATE"]).read_text())
    return answer(argv, state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
