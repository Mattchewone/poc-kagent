"""Milestone 4: fake agent scenarios, no cluster. Runs the CLI exactly as the runtimes do."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "agent" / "fake_agent.py"


def call(op: str, payload: dict, workspace: Path, **env) -> dict:
    p = subprocess.run(
        [sys.executable, str(AGENT), op],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"AGENT_WORKSPACE": str(workspace), "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", **env},
        timeout=60,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout)


@pytest.fixture
def ws(tmp_path):
    return tmp_path / "workspace"


def test_happy_buggy_then_fixed(ws):
    a1 = call("implement", {"scenario": "happy", "task": "fix add"}, ws)
    assert a1["attempt"] == 1 and "abs(a - b)" in a1["diff"]
    t1 = call("test", {"diff": a1["diff"]}, ws)
    assert t1["passed"] is False and t1["tests_touched"] is False

    a2 = call("implement", {"scenario": "happy", "feedback": t1["output"]}, ws)
    assert a2["attempt"] == 2 and "a + b" in a2["diff"]
    t2 = call("test", {"diff": a2["diff"]}, ws)
    assert t2["passed"] is True, t2["output"]


def test_never_stays_buggy(ws):
    for n in (1, 2, 3):
        a = call("implement", {"scenario": "never"}, ws)
        assert a["attempt"] == n
        assert call("test", {"diff": a["diff"]}, ws)["passed"] is False


def test_cheat_is_caught(ws):
    a = call("implement", {"scenario": "cheat"}, ws)
    t = call("test", {"diff": a["diff"]}, ws)
    assert t["tests_touched"] is True
    assert t["passed"] is False  # tests restored from the image, bug still there


def test_egress_reports_probe(ws):
    a = call("implement", {"scenario": "egress"}, ws)
    assert set(a["egress"]) >= {"ok", "exit_code", "http_code"}
    assert call("test", {"diff": a["diff"]}, ws)["passed"] is True


def test_slow_sleeps_then_fixes(ws):
    a = call("implement", {"scenario": "slow", "sleep_seconds": 0.1}, ws)
    assert call("test", {"diff": a["diff"]}, ws)["passed"] is True


def test_wiped_workspace_resets_counter(tmp_path):
    assert call("implement", {"scenario": "happy"}, tmp_path / "w1")["attempt"] == 1
    assert call("implement", {"scenario": "happy"}, tmp_path / "w2")["attempt"] == 1
