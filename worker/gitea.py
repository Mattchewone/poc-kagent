"""Gitea on the host (poc-gitea): source of truth for the app and its gitops repo; PRs, merges, comments.

All git work happens in a cached clone under .cache/gitea-clone, never inside a sandbox, so the
sandboxes need no git credentials.
"""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from worker.shell import sh

ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for line in (ROOT / "scripts/versions.env").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


_V = _env()
PORT = os.environ.get("GITEA_PORT", _V.get("GITEA_PORT", "3300"))
USER = os.environ.get("GITEA_USER", _V.get("GITEA_USER", "poc"))
PASSWORD = os.environ.get("GITEA_PASSWORD", _V.get("GITEA_PASSWORD", ""))
REPO = os.environ.get("GITEA_REPO", _V.get("GITEA_REPO", "demo-todo"))
URL = os.environ.get("GITEA_URL", f"http://localhost:{PORT}")
CLONE = ROOT / ".cache/gitea-clone"
_lock = asyncio.Lock()  # one git operation at a time on the shared clone


def _token() -> str:
    tok = os.environ.get("GITEA_TOKEN")
    if tok:
        return tok
    f = ROOT / ".cache/gitea-token"
    if not f.exists():
        raise RuntimeError("no Gitea token: run scripts/gitea-up.sh")
    return f.read_text().strip()


def _remote() -> str:
    return f"http://{USER}:{PASSWORD}@{URL.split('://', 1)[1]}/{USER}/{REPO}.git"


async def _git(*args: str, stdin: str | None = None, cwd: Path = CLONE) -> str:
    _, out, _ = await sh("git", "-C", str(cwd), "-c", "credential.helper=", "-c", "user.name=poc-agent",
                         "-c", "user.email=agent@poc.local", *args, stdin=stdin, timeout=120)
    return out


async def _ensure_clone() -> None:
    if not (CLONE / ".git").exists():
        CLONE.parent.mkdir(parents=True, exist_ok=True)
        await sh("git", "-c", "credential.helper=", "clone", "-q", _remote(), str(CLONE), timeout=120)
    await _git("fetch", "-q", "--prune", "origin")


@dataclass
class Base:
    sha: str
    tar_b64: str


async def base(ref: str = "main") -> Base:
    """Commit of `ref` on Gitea plus a tar of its tree (no prefix), for the verify sandbox."""
    async with _lock:
        await _ensure_clone()
        sha = (await _git("rev-parse", f"origin/{ref}")).strip()
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(CLONE), "archive", "--format=tar", sha,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"git archive failed: {err.decode()}")
        return Base(sha, base64.b64encode(out).decode())


async def check_applies(sha: str, diff: str) -> str:
    """'' if the diff applies cleanly to `sha`, else git's complaint."""
    async with _lock:
        await _ensure_clone()
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            await _git("worktree", "add", "-q", "--detach", str(wt), sha)
            try:
                rc, _, err = await sh("git", "-C", str(wt), "apply", "--check", "--whitespace=nowarn", "-",
                                      stdin=diff, check=False)
                return "" if rc == 0 else err.strip()
            finally:
                await _git("worktree", "remove", "--force", str(wt))


async def archive(sha: str) -> bytes:
    """tar of a commit's tree (no prefix), e.g. for an image build."""
    async with _lock:
        await _ensure_clone()
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(CLONE), "archive", "--format=tar", sha,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"git archive {sha} failed: {err.decode()}")
        return out


def _api(path: str) -> str:
    return f"{URL}/api/v1/repos/{USER}/{REPO}{path}"


def _headers() -> dict:
    return {"Authorization": f"token {_token()}"}


async def open_pull_request(branch: str, base_sha: str, diff: str, title: str, body: str) -> dict:
    """Branch from base_sha, apply the diff, force-push, open a PR into main (or reuse the open one
    for this branch, updating its body). Returns number, url, head_sha."""
    async with _lock:
        await _ensure_clone()
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            await _git("worktree", "add", "-q", "-B", branch, str(wt), base_sha)
            try:
                await _git("apply", "--index", "--whitespace=nowarn", "-", stdin=diff, cwd=wt)
                await _git("commit", "-q", "-m", title, "-m", body, cwd=wt)
                head = (await _git("rev-parse", "HEAD", cwd=wt)).strip()
                await _git("push", "-q", "--force", "origin", f"{branch}:{branch}", cwd=wt)
            finally:
                await _git("worktree", "remove", "--force", str(wt))
                await _git("branch", "-D", branch)
    async with httpx.AsyncClient(timeout=30) as c:
        existing = await c.get(_api("/pulls"), params={"state": "open", "limit": 50}, headers=_headers())
        existing.raise_for_status()
        for pr in existing.json():
            if pr["head"]["ref"] == branch:
                r = await c.patch(_api(f"/pulls/{pr['number']}"), headers=_headers(), json={"title": title, "body": body})
                r.raise_for_status()
                return {"number": pr["number"], "url": pr["html_url"], "head_sha": head, "existing": True}
        r = await c.post(_api("/pulls"), headers=_headers(), json={"head": branch, "base": "main", "title": title, "body": body})
        r.raise_for_status()
        pr = r.json()
        return {"number": pr["number"], "url": pr["html_url"], "head_sha": head, "existing": False}


async def get_pr(number: int) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_api(f"/pulls/{number}"), headers=_headers())
        r.raise_for_status()
        return r.json()


async def pr_state(number: int) -> dict:
    pr = await get_pr(number)
    return {"state": pr["state"], "merged": bool(pr.get("merged")), "merge_sha": pr.get("merge_commit_sha") or "",
            "head_ref": pr["head"]["ref"], "mergeable": pr.get("mergeable")}


async def merge_pr(number: int) -> dict:
    """Merge commit into main, delete the branch. Idempotent: an already-merged PR is a success."""
    st = await pr_state(number)
    if not st["merged"]:
        if st["state"] == "closed":
            raise RuntimeError(f"PR #{number} is closed without being merged")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(_api(f"/pulls/{number}/merge"), headers=_headers(),
                             json={"Do": "merge", "delete_branch_after_merge": True})
            if r.status_code >= 400:
                raise RuntimeError(f"Gitea refused the merge (HTTP {r.status_code}): {r.text[:300]}"
                                   + (" - the branch probably conflicts with main; ask the agent to rebase"
                                      if r.status_code in (405, 409) else ""))
        st = await pr_state(number)
    return {"merge_sha": st["merge_sha"]}


async def close_pr(number: int) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(_api(f"/pulls/{number}"), headers=_headers(), json={"state": "closed"})
        if r.status_code not in (200, 201, 404):
            r.raise_for_status()


async def comment(number: int, body: str) -> None:
    """Comment on an issue or PR (same endpoint in Gitea)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_api(f"/issues/{number}/comments"), headers=_headers(), json={"body": body})
        r.raise_for_status()
