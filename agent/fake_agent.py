#!/usr/bin/env python3
"""Scripted fake agent. One script, two operations, five scenarios.

CLI mode (OpenShell, local docker):
    fake_agent.py implement < '{"task": "...", "feedback": "", "scenario": "happy"}'
    fake_agent.py test      < '{"diff": "..."}'
HTTP mode (Substrate):
    fake_agent.py serve      # POST /implement, POST /test, GET /readyz on :8080

The attempt counter lives in a file inside the workspace, not in the payload,
so a wiped workspace shows up as "attempt 1" forever.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios import plan  # noqa: E402

_HERE = Path(__file__).resolve().parent


def _default_target() -> Path:
    baked = Path("/opt/poc/target")
    return baked if baked.exists() else _HERE.parent / "target"


TARGET_DIR = Path(os.environ.get("TARGET_DIR", _default_target()))
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", "/workspace"))
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "60"))


def _log(msg: str) -> None:
    print(f"[fake_agent] {msg}", file=sys.stderr, flush=True)


def _copy_pristine(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(TARGET_DIR, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))


def _diff(rel: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    body = "".join(lines)
    return f"diff --git a/{rel} b/{rel}\n{body}" if body else ""


def _probe_egress() -> dict:
    try:
        p = subprocess.run(
            ["curl", "-sS", "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://example.com"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ok = p.returncode == 0 and p.stdout.strip().startswith(("2", "3"))
        return {"ok": ok, "exit_code": p.returncode, "http_code": p.stdout.strip(), "stderr": p.stderr.strip()[-500:]}
    except Exception as e:  # curl missing, timeout, ...
        return {"ok": False, "exit_code": None, "http_code": "", "stderr": repr(e)}


def implement(payload: dict) -> dict:
    scenario = payload.get("scenario") or os.environ.get("SCENARIO", "happy")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    counter = WORKSPACE / ".attempt"
    attempt = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(attempt))
    _log(f"implement scenario={scenario} attempt={attempt} feedback_len={len(payload.get('feedback') or '')}")

    files, notes, effects = plan(scenario, attempt)

    repo = WORKSPACE / "repo"
    _copy_pristine(repo)
    diffs = []
    for rel, new in files.items():
        old = (TARGET_DIR / rel).read_text()
        (repo / rel).write_text(new)
        diffs.append(_diff(rel, old, new))

    result: dict = {"diff": "".join(diffs), "notes": notes, "attempt": attempt, "scenario": scenario}
    if "sleep" in effects:
        seconds = float(payload.get("sleep_seconds", SLOW_SECONDS))
        _log(f"sleeping {seconds}s")
        time.sleep(seconds)
    if "egress" in effects:
        result["egress"] = _probe_egress()
        result["notes"] += f"; egress ok={result['egress']['ok']}"
    return result


def test(payload: dict) -> dict:
    diff = payload.get("diff", "")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _copy_pristine(repo)
        touched = [
            line[len("+++ b/"):].strip()
            for line in diff.splitlines()
            if line.startswith("+++ b/")
        ]
        tests_touched = any(p.startswith("tests/") for p in touched)
        if diff.strip():
            patch = Path(tmp) / "change.patch"
            patch.write_text(diff)
            ap = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=repo, capture_output=True, text=True)
            if ap.returncode != 0:
                return {"passed": False, "output": f"git apply failed:\n{ap.stderr}", "tests_touched": tests_touched}
        # The grader never trusts the agent's tests: restore them from the image.
        shutil.rmtree(repo / "tests")
        shutil.copytree(TARGET_DIR / "tests", repo / "tests")
        env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        pt = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=repo, capture_output=True, text=True, env=env, timeout=120,
        )
        output = (pt.stdout + pt.stderr)[-4000:]
        _log(f"test passed={pt.returncode == 0} tests_touched={tests_touched}")
        return {"passed": pt.returncode == 0, "output": output, "tests_touched": tests_touched}


OPS = {"implement": implement, "test": test}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/readyz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        op = self.path.strip("/")
        if op not in OPS:
            return self._send(404, {"error": f"unknown op {op!r}"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, OPS[op](payload))
        except Exception as e:
            _log(f"{op} failed: {e!r}")
            self._send(500, {"error": repr(e)})

    def log_message(self, fmt, *args):
        _log("http " + fmt % args)


def serve(port: int) -> None:
    _log(f"serving on :{port} target={TARGET_DIR} workspace={WORKSPACE}")
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "serve":
        serve(int(argv[2] if len(argv) > 2 else os.environ.get("PORT", 8080)))
        return 0
    if cmd not in OPS:
        print(json.dumps({"error": f"unknown op {cmd!r}"}))
        return 2
    try:
        raw = sys.stdin.read()
        result = OPS[cmd](json.loads(raw) if raw.strip() else {})
    except Exception as e:
        print(json.dumps({"error": repr(e)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
