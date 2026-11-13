"""WorkItemWorkflow: one request, from intake to production. Temporal owns the whole lifecycle.

    intake (chat / Gitea issue / Jira later)
      -> run_agent         Temporal invokes dev-agent over A2A (kagent session per work item)
           agent calls submit_revision -> Update `submit` -> verify in a fresh OpenShell sandbox
      -> publish_pr        branch agent/<work_id> + PR on Gitea (force-pushed on each new revision)
      -> build_pr_image    image of the PR head, pushed to the Kargo registry
      -> deploy_pr_env     Argo CD Application on kargo-nonprod, namespace demo-todo-pr-<n>
      -> in review         waits for: feedback (chat / `/agent` PR comment) -> back to run_agent,
                           merge (chat tool, gated) or a merge/close in Gitea (webhook, poll fallback)
      -> release_image     semver image of the merge commit; Kargo Warehouse refreshed
      -> wait_for_stage    dev (auto-promoted) -> staging -> prod (each promoted in Kargo or via chat)

Handlers: Update `submit` (agent), Signal `feedback`, Update `merge`, Signal `pr_event` (webhook),
Update `promote`, Signal `abandon`, Query `status`. Activities live in worker/work_activities.py
and are called by name, so this module stays importable inside the workflow sandbox.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from worker.change import AppEnv, Revision, StartAppInput, VerifyInput, VerifyResult

WORK_QUEUE = "mock-poc"
STAGES = ("dev", "staging", "prod")
MAX_REVISIONS = 12
MAX_FAILED_TURNS = 3  # agent turns in a row without a passing revision -> ask a human
PR_POLL = timedelta(minutes=5)  # fallback in case a Gitea webhook is missed
REVIEW_IDLE = timedelta(days=7)  # PR untouched this long -> close it, tear the preview down

# phases (what the dashboard, the chat and the pages show)
QUEUED = "queued"
CODING = "agent working"
NEEDS_INPUT = "needs input"
PUBLISHING = "opening pr"
PREVIEWING = "deploying preview"
IN_REVIEW = "in review"
MERGING = "merging"
RELEASING = "building release"
ROLLOUT = "rolling out"  # + state["stage"]
AWAITING = "awaiting promotion"  # + state["stage"]
DONE = "done"
CLOSED = "closed"

# phases in which a human is expected to do something (wait_for_work returns on these)
HUMAN_PHASES = (NEEDS_INPUT, IN_REVIEW, AWAITING, DONE, CLOSED)


# ---- payloads -------------------------------------------------------------

@dataclass
class WorkRequest:
    work_id: str
    title: str
    description: str
    kind: str = "feature"  # feature | bug (the agent may correct it on submit)
    source: str = "chat"  # chat | gitea-issue | jira
    source_ref: str = ""  # issue number / key, for back-links
    requested_by: str = ""
    runtime: str = "openshell"  # where verification runs: openshell | local


@dataclass
class Feedback:
    text: str
    author: str = ""
    source: str = "chat"  # chat | pr-comment | issue-comment


@dataclass
class PrEvent:
    action: str  # merged | closed
    merge_sha: str = ""
    by: str = ""


@dataclass
class AgentTurnInput:
    work_id: str
    prompt: str
    turn: int


@dataclass
class AgentTurnResult:
    session_id: str
    state: str
    text: str


@dataclass
class PublishInput:
    work_id: str
    title: str
    kind: str
    description: str
    source: str
    source_ref: str
    base_sha: str
    diff: str
    summary: str
    verify: dict
    revision: int


@dataclass
class PrInfo:
    number: int
    url: str
    head_sha: str
    branch: str


@dataclass
class ImageInput:
    sha: str
    repo: str  # demo-todo-pr | demo-todo
    tag: str


@dataclass
class PrEnvInput:
    work_id: str
    pr_number: int
    image_tag: str


@dataclass
class PrEnv:
    app: str
    namespace: str
    url: str


@dataclass
class ReleaseInput:
    work_id: str
    merge_sha: str
    kind: str


@dataclass
class StageInput:
    stage: str
    version: str


@dataclass
class CommentInput:
    pr_number: int
    body: str


_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)
_OPTS = {
    "start_app_env": dict(start_to_close_timeout=timedelta(minutes=5), retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2), maximum_attempts=4)),
    "verify_change": dict(start_to_close_timeout=timedelta(minutes=6), retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2), maximum_attempts=3)),
    "stop_app_env": dict(start_to_close_timeout=timedelta(minutes=2), retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2), maximum_attempts=10)),
    # An agent turn can take a while; heartbeats show it is alive. A retry re-invokes the same
    # kagent session with a "you were interrupted" prompt (see work_activities.run_agent).
    "run_agent": dict(start_to_close_timeout=timedelta(minutes=45), heartbeat_timeout=timedelta(minutes=2),
                      retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=3)),
    "publish_pr": dict(start_to_close_timeout=timedelta(minutes=2), retry_policy=_RETRY),
    "build_image": dict(start_to_close_timeout=timedelta(minutes=10), heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=3)),
    "deploy_pr_env": dict(start_to_close_timeout=timedelta(minutes=8), heartbeat_timeout=timedelta(minutes=1),
                          retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=3)),
    "teardown_pr_env": dict(start_to_close_timeout=timedelta(minutes=3), retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=5), maximum_attempts=10)),
    "comment_pr": dict(start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY),
    "pr_state": dict(start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY),
    "merge_pr": dict(start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=2)),
    "close_pr": dict(start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY),
    "release_image": dict(start_to_close_timeout=timedelta(minutes=10), heartbeat_timeout=timedelta(minutes=2),
                          retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=3)),
    "promote_stage": dict(start_to_close_timeout=timedelta(minutes=1), retry_policy=_RETRY),
}
# Waiting on a Kargo stage: one long activity polling with heartbeats (keeps workflow history
# small). dev is auto-promoted, so it should finish in minutes; staging/prod wait for a human.
_STAGE_OPTS = {
    "dev": timedelta(minutes=30),
    "staging": timedelta(days=14),
    "prod": timedelta(days=14),
}


async def act(name: str, arg, result_type=None, **override):
    opts = {**_OPTS.get(name, {}), **override}
    return await workflow.execute_activity(name, arg, result_type=result_type, **opts)


def agent_prompt(req: WorkRequest) -> str:
    return "\n".join([
        f"New work item `{req.work_id}` ({req.kind}, from {req.source}"
        + (f" {req.source_ref}" if req.source_ref else "") + ").",
        f"Title: {req.title}",
        "Request:",
        req.description,
        "",
        f"Implement it with tests, then call submit_revision with work_id=\"{req.work_id}\".",
        "If the request is too ambiguous to implement, do not guess: end your turn with one short question.",
    ])


def feedback_prompt(req: WorkRequest, pr: dict | None, items: list[Feedback]) -> str:
    lines = [f"Feedback on work item `{req.work_id}` ({req.title})"
             + (f", PR #{pr['number']}" if pr else "") + ":"]
    for f in items:
        who = f" ({f.author}, {f.source})" if f.author else f" ({f.source})"
        lines.append(f"- {f.text}{who}")
    lines += ["", "Revise the change and submit_revision again with the full diff against origin/main."]
    return "\n".join(lines)


def retry_prompt(req: WorkRequest, last: dict) -> str:
    v = last.get("verify", {})
    detail = v.get("error") or (v.get("output") or "")[-1500:]
    return "\n".join([
        f"Work item `{req.work_id}`: revision {last.get('n')} did not pass verification.",
        detail,
        "",
        "Fix it and call submit_revision again. If you are stuck, end your turn with a short explanation.",
    ])


# ---- workflow -------------------------------------------------------------

@workflow.defn
class WorkItemWorkflow:
    def __init__(self) -> None:
        self.req: WorkRequest | None = None
        self.state: dict = {"phase": QUEUED, "revisions": [], "turns": [], "feedback": [], "stages": {},
                            "log": []}
        self.inbox: list[Feedback] = []  # feedback not yet given to the agent
        self.verify_lock = asyncio.Lock()
        self.merge_requested: str | None = None  # who asked
        self.merge_outcome: dict | None = None
        self.pr_closed: PrEvent | None = None
        self.abandon_reason: str | None = None

    # -- helpers --
    def _log(self, msg: str) -> None:
        self.state["log"].append({"at": workflow.now().isoformat(timespec="seconds"), "msg": msg})
        self.state["log"] = self.state["log"][-60:]

    def _phase(self, phase: str, stage: str | None = None) -> None:
        self.state["phase"] = phase
        if stage is None:
            self.state.pop("stage", None)
        else:
            self.state["stage"] = stage
        self._log(phase + (f": {stage}" if stage else ""))

    # -- handlers --
    @workflow.update
    async def submit(self, rev: Revision) -> dict:
        """From the agent (submit_revision): verify this diff now and answer with the result."""
        async with self.verify_lock:
            n = len(self.state["revisions"]) + 1
            entry = {"n": n, "rev_id": rev.rev_id, "summary": rev.summary, "diff": rev.diff,
                     "turn": len(self.state["turns"]), "submitted_at": workflow.now().isoformat()}
            self.state["revisions"].append(entry)
            self._log(f"revision {n} submitted")
            env: AppEnv | None = None
            try:
                env = await act("start_app_env", StartAppInput(self.req.runtime, self.req.work_id, n), AppEnv)
                entry["env"] = env.name
                res: VerifyResult = await act("verify_change", VerifyInput(env, rev.diff), VerifyResult)
            except ActivityError as e:
                cause = e.cause.message if e.cause else str(e)
                res = VerifyResult(passed=False, base_sha="", error=f"verification could not run: {cause}")
            finally:
                if env:
                    await act("stop_app_env", env)
            entry.update(base_sha=res.base_sha, passed=res.passed, verify={
                "agent_tests": res.agent_tests, "base_tests": res.base_tests, "tests_touched": res.tests_touched,
                "weakened": res.weakened, "touched": res.touched, "error": res.error, "output": res.output,
            })
            self._log(f"revision {n} {'passed' if res.passed else 'failed'} verification")
            return revision_summary(entry, self.state)

    @submit.validator
    def _v_submit(self, rev: Revision) -> None:
        if self.state["phase"] != CODING:
            raise ValueError(f"work item is '{self.state['phase']}', not waiting for code from the agent")
        if len(self.state["revisions"]) >= MAX_REVISIONS:
            raise ValueError(f"revision limit ({MAX_REVISIONS}) reached")
        if not rev.diff.strip():
            raise ValueError("empty diff")

    @workflow.signal(name="feedback")
    def add_feedback(self, fb: Feedback) -> None:
        self.inbox.append(fb)
        self.state["feedback"].append({"text": fb.text, "author": fb.author, "source": fb.source,
                                       "at": workflow.now().isoformat(timespec="seconds")})
        self._log(f"feedback from {fb.author or fb.source}")

    @workflow.update
    async def merge(self, by: str = "") -> dict:
        self.merge_outcome = None
        self.merge_requested = by or "chat"
        await workflow.wait_condition(lambda: self.merge_outcome is not None)
        if "error" in self.merge_outcome:
            raise ApplicationError(self.merge_outcome["error"], non_retryable=True)
        return self.merge_outcome

    @merge.validator
    def _v_merge(self, by: str = "") -> None:
        if self.state["phase"] != IN_REVIEW or self.inbox or self.merge_requested:
            raise ValueError(f"cannot merge now: work item is '{self.state['phase']}'"
                             + (" with feedback still to address" if self.inbox else ""))

    @workflow.signal
    def pr_event(self, ev: PrEvent) -> None:
        if self.pr_closed is None:
            self.pr_closed = ev
            self._log(f"pr {ev.action}" + (f" by {ev.by}" if ev.by else ""))

    @workflow.update
    async def promote(self, stage: str) -> dict:
        """Promote this work item's release into `stage` (a Kargo Promotion)."""
        out = await act("promote_stage", StageInput(stage, self.state["release"]["version"]), dict)
        self.state["stages"].setdefault(stage, {})["promotion"] = out.get("promotion")
        self._log(f"promotion to {stage} requested")
        return out

    @promote.validator
    def _v_promote(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; stages are {', '.join(STAGES)}")
        if self.state["phase"] not in (AWAITING, ROLLOUT) or self.state.get("stage") != stage:
            raise ValueError(f"{stage} is not waiting for a promotion (work item is '{self.state['phase']}'"
                             + (f", stage {self.state['stage']}" if self.state.get("stage") else "") + ")")

    @workflow.signal
    def abandon(self, reason: str = "") -> None:
        if self.abandon_reason is None:
            self.abandon_reason = reason or "abandoned"
            self._log(f"abandon requested: {self.abandon_reason}")

    @workflow.query
    def status(self) -> dict:
        return self.state

    # -- main loop --
    @workflow.run
    async def run(self, req: WorkRequest) -> str:
        self.req = req
        self.state.update(work_id=req.work_id, title=req.title, kind=req.kind, description=req.description,
                          source=req.source, source_ref=req.source_ref, requested_by=req.requested_by)
        pr: dict | None = None
        env: PrEnv | None = None
        try:
            prompt = agent_prompt(req)
            failed_turns = 0
            # ---------------- build + review loop ----------------
            while True:
                if self.abandon_reason:
                    return await self._close(pr, env, f"abandoned: {self.abandon_reason}")
                passed = await self._agent_turn(prompt)
                if passed is None:  # nothing submitted: the agent asked something or gave up
                    failed_turns = 0
                    fb = await self._wait_for_input()
                    if fb is None:
                        return await self._close(pr, env, f"abandoned: {self.abandon_reason}")
                    prompt = feedback_prompt(req, pr, fb)
                    continue
                if not passed:
                    failed_turns += 1
                    if failed_turns >= MAX_FAILED_TURNS:
                        failed_turns = 0
                        fb = await self._wait_for_input()
                        if fb is None:
                            return await self._close(pr, env, f"abandoned: {self.abandon_reason}")
                        prompt = feedback_prompt(req, pr, fb)
                    else:
                        prompt = retry_prompt(req, self.state["revisions"][-1])
                    continue
                failed_turns = 0

                # verified -> PR + preview
                last = self.state["revisions"][-1]
                self._phase(PUBLISHING)
                info: PrInfo = await act("publish_pr", PublishInput(
                    req.work_id, req.title, req.kind, req.description, req.source,
                    req.source_ref, last["base_sha"], last["diff"], last["summary"], last["verify"], last["n"]), PrInfo)
                pr = {"number": info.number, "url": info.url, "branch": info.branch, "head_sha": info.head_sha,
                      "revision": last["n"]}
                self.state["pr"] = pr
                self._phase(PREVIEWING)
                tag = f"pr{info.number}-{info.head_sha[:8]}"
                try:
                    await act("build_image", ImageInput(info.head_sha, "demo-todo-pr", tag), str)
                    env = await act("deploy_pr_env", PrEnvInput(req.work_id, info.number, tag), PrEnv)
                    self.state["preview"] = {"url": env.url, "namespace": env.namespace, "app": env.app,
                                             "image": tag, "revision": last["n"]}
                    await act("comment_pr", CommentInput(info.number, (
                        f"Preview of revision {last['n']} (`{tag}`) is live on kargo-nonprod: {env.url}\n\n"
                        "Comment `/agent <what to change>` to send feedback to the agent, or merge when happy. "
                        "Merging builds a release and Kargo rolls it out: dev → staging → prod.")))
                except ActivityError as e:
                    cause = e.cause.message if e.cause else str(e)
                    self.state["preview"] = {"error": cause, "revision": last["n"]}
                    self._log(f"preview failed: {cause}")
                    # The Argo CD app may exist half-deployed: remember it so it still gets torn down.
                    name = f"demo-todo-pr-{info.number}"
                    env = env or PrEnv(name, name, "")

                # ---------------- review ----------------
                outcome = await self._review(pr)
                if outcome == "feedback":
                    prompt = feedback_prompt(req, pr, self._take_feedback())
                    continue
                if outcome == "closed":
                    return await self._close(pr, env, "pr closed without merging", close_pr=False)
                if outcome == "abandoned":
                    return await self._close(pr, env, f"abandoned: {self.abandon_reason}")
                break  # merged

            # ---------------- delivery ----------------
            merge_sha = self.pr_closed.merge_sha if self.pr_closed else ""
            await self._teardown(env)
            env = None
            if not merge_sha:
                st = await act("pr_state", pr["number"], dict)
                merge_sha = st.get("merge_sha", "")
            self.state["merge_sha"] = merge_sha
            self._phase(RELEASING)
            rel = await act("release_image", ReleaseInput(req.work_id, merge_sha, req.kind), dict)
            self.state["release"] = rel
            await act("comment_pr", CommentInput(pr["number"], (
                f"Released as `{rel['image']}`. Kargo picks it up and rolls it out to dev automatically; "
                "staging and prod need a promotion (Kargo UI, or ask the chat agent).")))
            for stage in STAGES:
                self._phase(ROLLOUT if stage == "dev" else AWAITING, stage)
                res = await act("wait_for_stage", StageInput(stage, rel["version"]), dict,
                                schedule_to_close_timeout=_STAGE_OPTS[stage], start_to_close_timeout=_STAGE_OPTS[stage],
                                heartbeat_timeout=timedelta(minutes=2),
                                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10)))
                self.state["stages"].setdefault(stage, {}).update(res)
                if not res.get("verified"):
                    self.state["result"] = f"{rel['version']} failed verification in {stage}: {res.get('message', '')}"
                    self._phase(DONE)
                    return self.state["result"]
                self._log(f"{rel['version']} verified in {stage}")
            self.state["result"] = f"{rel['version']} live in prod"
            self._phase(DONE)
            return self.state["result"]
        finally:
            await self._teardown(env)  # cancelled / failed mid-review: never leak a preview namespace
            await workflow.wait_condition(workflow.all_handlers_finished)

    # -- steps --
    async def _agent_turn(self, prompt: str) -> bool | None:
        """One agent turn. True: latest revision of this turn passed; False: submitted but failing;
        None: nothing submitted (the agent asked a question or stopped)."""
        turn = len(self.state["turns"]) + 1
        before = len(self.state["revisions"])
        self.state["turns"].append({"n": turn, "prompt": prompt, "started_at": workflow.now().isoformat()})
        self._phase(CODING)
        try:
            res: AgentTurnResult = await act("run_agent", AgentTurnInput(self.req.work_id, prompt, turn), AgentTurnResult)
            self.state["session_id"] = res.session_id
            self.state["turns"][-1].update(state=res.state, reply=res.text[-4000:])
        except ActivityError as e:
            cause = e.cause.message if e.cause else str(e)
            self.state["turns"][-1].update(state="error", reply=f"agent turn failed: {cause}")
        await workflow.wait_condition(lambda: not self.verify_lock.locked())  # a submit still verifying
        mine = self.state["revisions"][before:]
        if not mine:
            return None
        return bool(mine[-1].get("passed"))

    async def _wait_for_input(self) -> list[Feedback] | None:
        self._phase(NEEDS_INPUT)
        try:
            await workflow.wait_condition(lambda: bool(self.inbox) or self.abandon_reason is not None,
                                          timeout=REVIEW_IDLE)
        except asyncio.TimeoutError:
            self.abandon_reason = f"no input for {REVIEW_IDLE}"
        if self.abandon_reason:
            return None
        return self._take_feedback()

    def _take_feedback(self) -> list[Feedback]:
        items, self.inbox = self.inbox, []
        return items

    async def _review(self, pr: dict) -> str:
        """Wait in review. Returns feedback | merged | closed | abandoned."""
        self._phase(IN_REVIEW)
        idle_since = workflow.now()
        while True:
            try:
                await workflow.wait_condition(
                    lambda: bool(self.inbox) or self.merge_requested is not None or self.pr_closed is not None
                    or self.abandon_reason is not None, timeout=PR_POLL)
            except asyncio.TimeoutError:
                st = await act("pr_state", pr["number"], dict)
                if st.get("state") == "closed" and self.pr_closed is None:
                    self.pr_event(PrEvent("merged" if st.get("merged") else "closed", st.get("merge_sha", "")))
                elif workflow.now() - idle_since > REVIEW_IDLE:
                    self.abandon_reason = f"pr idle for {REVIEW_IDLE}"
                continue
            if self.pr_closed:
                return "merged" if self.pr_closed.action == "merged" else "closed"
            if self.abandon_reason:
                return "abandoned"
            if self.merge_requested:
                self._phase(MERGING)
                try:
                    out = await act("merge_pr", pr["number"], dict)
                except ActivityError as e:
                    cause = e.cause.message if e.cause else str(e)
                    self.merge_outcome = {"error": f"could not merge PR #{pr['number']}: {cause}"}
                    self.merge_requested = None
                    self._phase(IN_REVIEW)
                    continue
                self.pr_event(PrEvent("merged", out.get("merge_sha", ""), self.merge_requested))
                self.merge_outcome = {"pr": pr["number"], "merge_sha": out.get("merge_sha", ""), "merged_by": self.merge_requested}
                return "merged"
            if self.inbox:
                return "feedback"

    async def _teardown(self, env: PrEnv | None) -> None:
        """Delete the PR environment once (safe to call again from `finally`)."""
        preview = self.state.setdefault("preview", {})
        if env and not preview.get("torn_down"):
            await act("teardown_pr_env", env)
            preview["torn_down"] = True

    async def _close(self, pr: dict | None, env: PrEnv | None, result: str, close_pr: bool = True) -> str:
        await self._teardown(env)
        if pr and close_pr:
            await act("close_pr", pr["number"])
        self.state["result"] = result
        self._phase(CLOSED)
        return result


def revision_summary(entry: dict, state: dict) -> dict:
    v = entry.get("verify", {})
    out = {
        "work_id": state.get("work_id"),
        "revision": entry.get("n"),
        "passed": entry.get("passed"),
        "verify": {k: v.get(k) for k in ("agent_tests", "base_tests", "tests_touched", "weakened", "touched", "error")},
    }
    if not entry.get("passed"):
        out["verify"]["output_tail"] = (v.get("output") or "")[-2500:]
        out["next"] = "fix the problem and call submit_revision again"
    else:
        out["next"] = ("verified: the pipeline opens the PR and deploys a preview. "
                       "End your turn with a short summary for the reviewer.")
    return {k: val for k, val in out.items() if val not in (None, "", [], {})}


def summarize(state: dict, verbose: bool = False) -> dict:
    """What chat tools return: no diffs, no prompts, trimmed output."""
    revs = state.get("revisions", [])
    last = revs[-1] if revs else {}
    turns = state.get("turns", [])
    out = {
        "work_id": state.get("work_id"),
        "title": state.get("title"),
        "phase": state.get("phase"),
        "stage": state.get("stage"),
        "revision": last.get("n"),
        "passed": last.get("passed"),
        "pr": state.get("pr", {}).get("url"),
        "preview_url": (state.get("preview") or {}).get("url") if not (state.get("preview") or {}).get("torn_down") else None,
        "preview_error": (state.get("preview") or {}).get("error"),
        "release": (state.get("release") or {}).get("version"),
        "stages": {s: ("verified" if v.get("verified") else v.get("message") or "pending")
                   for s, v in state.get("stages", {}).items()} or None,
        "result": state.get("result"),
    }
    if state.get("phase") in (NEEDS_INPUT, IN_REVIEW) and turns:
        out["agent_says"] = (turns[-1].get("reply") or "")[-1500:]
    if verbose and last:
        out["verify"] = {k: last.get("verify", {}).get(k) for k in ("agent_tests", "base_tests", "touched", "error")}
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}
