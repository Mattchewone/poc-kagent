# Demo: from a chat request to prod

How to drive a change to demo-todo from request to production, and what to watch while it happens.
One Temporal workflow (`WorkItemWorkflow`) owns each request until its release image is built. Kargo
takes it from there:

1. **Input.** Chat with **delivery-agent**, or open a Gitea issue with `/agent` in it.
2. **The agent works.** Temporal invokes **dev-agent** (Claude Code on a Substrate actor) over A2A.
   Each revision is verified in a fresh OpenShell sandbox, and a passing revision becomes a PR.
3. **You review the PR environment.** A preview namespace on kargo-nonprod runs the PR's image.
   Feedback in chat or a `/agent` PR comment goes back to the agent, and the PR and the preview update.
4. **You merge.** Temporal builds a release image and the work item is done. Kargo + Argo CD roll it
   out to dev automatically, and you promote it to staging and then prod.

Small diagrams of each part (intake, agent turn, verify, preview, release, credentials) are in
[architecture.md](architecture.md). Architecture and reasoning: [architecture.md](architecture.md),
[ADR 0016](adr/0016-temporal-owns-the-work-item.md), [ADR 0017](adr/0017-kargo-delivery-and-pr-environments.md).

## 1. Before you start

```bash
../kargo/scripts/up.sh   # the kargo stack (6 kind clusters), if not already up
make start            # poc cluster back from `make stop` (~2 min); first time: make up
make dev              # temporal + worker + pipeline server + claude auth proxy
make delivery         # once: gitops repo, Kargo project demo-todo, first release 0.1.0, stage edges
make kagent-key       # once per key change; prints which auth mode it chose
make demo-ui          # opens every UI below
```

`make delivery` is idempotent; run it again after `make stop`/`start` if the edge containers are gone.
After changing [clusters/poc/kagent/](../clusters/poc/kagent/), run `make kagent-apply`.

| What | URL | Login |
|---|---|---|
| kagent UI (chat: **delivery-agent**) | http://localhost:8082 | none |
| Work items: a readable view of each Temporal workflow | http://localhost:8765 | none |
| Temporal | http://localhost:8233 | none |
| Gitea | http://localhost:3300/poc/demo-todo | `poc` / `poc-password-local` |
| Kargo | https://localhost:31444/project/demo-todo | password `admin` |
| Argo CD | https://localhost:31443 | `admin` / `admin` |
| dev / staging / prod | http://localhost:31081 / 31082 / 31083 | none |
| PR preview | http://localhost:311NN (NN = PR number) | none |


## 2. Set up your screen

| Window | What it shows |
|---|---|
| **kagent UI** → delivery-agent | the chat, tool calls, the Approve prompt on merge and promote |
| **Terminal: `make watch`** | actors waking/suspending, verify sandboxes, Temporal work items, Gitea PRs, timeline |
| **Work item page** http://localhost:8765/work/&lt;id&gt; | step bar, each agent turn (prompt + reply), revisions with tests and diffs, PR, preview, release (stage status live from Kargo) |
| **Temporal UI** | the `WorkItemWorkflow` history: every activity, update and signal |
| **Kargo UI** | Freight moving through dev → staging → prod |

Optional: the Argo CD UI (preview apps `demo-todo-pr-N` appear and disappear), and
`k9s --context kind-kargo-nonprod` to watch the preview and stage pods.

### Two agents

- **delivery-agent** is the one you chat with. It doesn't write code. It starts work items, follows
  them (`wait_for_work`), relays feedback, and merges or promotes behind an **Approve** click.
- **dev-agent** is the coder. Only Temporal talks to it, one kagent session per work item, so you'll
  see its sessions listed in kagent but you never need to open them. Its turns, prompts and replies are
  on the work item page.

Both run as Substrate actors (gVisor sandboxes inside the `kagent-default` pod, not pods of their
own). `make watch` reads atelet's log to show them: **● awake** while working, **◌ suspended**
(snapshot saved, CPU freed) between turns.

## 3. Walkthrough: a feature, end to end

**1. Ask.** Open http://localhost:31081 (dev) to see the current app. Start a new chat with
**delivery-agent**:

> Add a "Clear completed" button in the footer that removes all done todos. Hide it when nothing is done.

- delivery-agent calls `start_work` and gives you the work item page, then calls `wait_for_work`.
- **Temporal:** a `WorkItemWorkflow` named after the request (e.g. `add-a-clear-completed-bu-1a2b3c`),
  phase **agent working**, with a `run_agent` activity in flight.
- **`make watch`:** two actors wake: the chat, and dev-agent's session for this work item.

**2. The agent works.** dev-agent clones the repo, writes the code and tests, runs `npm test`, and
calls `submit_revision`.

- **Temporal:** a `submit` update. Its handler runs `start_app_env`, `verify_change` and
  `stop_app_env`: a fresh OpenShell sandbox (`p-<hash>-1` in `make watch`) runs the agent's tests and
  then the original tests restored over them.
- If it fails, the agent sees the output in the tool reply and fixes it within the same turn. If a
  whole turn ends without a passing revision, Temporal re-prompts it with the failure, up to 3 turns
  in a row, and then asks you (phase **needs input**).
- When it passes, the turn ends and the workflow runs `publish_pr` (branch `agent/<id>`, PR on Gitea),
  `build_image` (`demo-todo-pr:pr<N>-<sha>`) and `deploy_pr_env`.

**3. Review the PR environment.** Phase **in review**. delivery-agent replies with the preview URL and
the PR. The PR also has a comment with the preview link.

- Open the preview (http://localhost:311NN). It runs on kargo-nonprod in namespace
  `demo-todo-pr-N`, deployed by Argo CD from the gitops repo's `preview/` overlay with the PR's image.
- Ask for a change, either in chat (*"Show how many will be cleared, like 'Clear completed (2)'"*) or
  as a PR comment starting with `/agent`. The webhook turns the comment into a `feedback` signal.
- Temporal starts another agent turn in the **same** kagent session (the actor still has its checkout).
  Revision 2 is verified, force-pushed to the same PR, and rolled out to the **same** preview
  namespace. Same URL, new image: refresh and check.

**4. Merge and roll out.** In chat:

> Looks good, merge it.

- delivery-agent calls `merge_pull_request`. That tool sits on a gated server, so kagent shows
  **Approve / Reject**. Nothing happens until you click.
- After you approve, the workflow:
  1. merges the PR in Gitea;
  2. deletes the preview (Argo CD app, namespace, edge);
  3. runs `release_image`. A feature bumps the minor version (0.1.0 → 0.2.0) and a bug bumps the patch.
     The image is pushed to kargo-registry and the Warehouse refreshed. Phase **released**: the
     workflow is finished.
- Kargo takes over: it creates Freight, auto-promotes it to dev, Argo Rollouts runs a short canary,
  and the smoke test checks `/version.txt` and `/api/stats`. The work item page's release card and
  `release_status` show it live.
- Merging in Gitea's UI instead works the same: the webhook signals the workflow.
- Check http://localhost:31081: the button is there.

**5. Promote.** In chat, *"Promote 0.2.0 to staging"* (Approve), then *"and to prod"* (Approve). The
tool creates a Kargo Promotion directly; Temporal isn't involved. Or drag the Freight in the Kargo UI.
Staging only takes Freight verified in dev, and prod only Freight verified in staging.

## 4. Input from a Gitea issue

Open an issue in http://localhost:3300/poc/demo-todo/issues with `/agent` in the body, for example:

> **Title:** Todos should show when they were created
> **Body:** /agent Show "added 5 min ago" next to each todo.

The webhook starts a work item `issue-<n>-…` and comments the work item link on the issue. The PR says
`Closes #<n>`, so merging closes the issue. Follow and merge it from Gitea, or ask delivery-agent:
*"What's the status of issue-3-…?"*. A later `/agent …` comment on the issue becomes feedback.

Jira would plug in the same way: a webhook handler calling the same intake function.

## 5. Things worth trying

| Try | What should happen |
|---|---|
| Two requests at once | two workflows, two dev-agent sessions, two PRs, two preview namespaces (`demo-todo-pr-N`) |
| A vague request (*"make it nicer"*) | the agent asks a question instead of guessing; phase **needs input**; answer in chat (`request_changes`) |
| *"Abandon it"* | `abandon_work`: PR closed, preview deleted, workflow completes `abandoned` |
| Close the PR in Gitea | webhook → workflow completes `pr closed without merging`, preview deleted |
| Restart the worker mid-turn (`kill $(cat .cache/worker.pid); make dev`) | Temporal retries `run_agent`; the agent is told it was interrupted and carries on in the same session |
| Ask to merge while feedback is pending, or promote prod before staging | the workflow's validator / Kargo refuses; the chat relays the reason |
| *"Delete the failing test and submit"* | the verifier still runs the original tests and flags weakened test files |

## 6. Where to look when something is off

| Symptom | Look at |
|---|---|
| Chat says `Invalid API key` | `make kagent-key` output; `.cache/claude-proxy.log` |
| Work item stuck in **agent working** | Temporal: the `run_agent` activity (heartbeat details name the kagent session); `.cache/worker.log`; the session in the kagent UI |
| Verification failing | the work item page (full test output per revision) |
| **preview failed** on the work item | Argo CD app `demo-todo-pr-N`; `kubectl --context kind-kargo-nonprod -n demo-todo-pr-N get pods`; `docker ps \| grep demo-todo-edge` |
| PR comment / Gitea merge not picked up | Gitea → repo Settings → Webhooks → recent deliveries; `.cache/pipeline.log`. The 5-minute poll catches merges anyway |
| Release not reaching dev | Kargo UI → demo-todo → dev (promotion and verification); `kubectl --context kind-kargo-hub -n demo-todo get freight,promotions` |
| Stage URL doesn't load | `make delivery` (recreates edges); `kubectl --context kind-kargo-nonprod -n demo-todo-dev get pods` |

## 7. Resetting between demos

- **Open work items:** *"abandon &lt;id&gt;"* in chat, or `.venv/bin/python scripts/mcp_call.py abandon_work work_id=<id>`.
- **Old chats and dev-agent sessions:** delete them in the kagent UI. Their actors go with them.
- **Gitea:** revert merge commits in the UI to put the app back. The next release then ships the revert.
- **Kargo:** old Freight is harmless. Promote an older Freight to roll a stage back.
- **Temporal history:** `make dev-stop`, delete `.cache/temporal.db`, `make dev`.

`make stop` pauses the poc cluster; the kargo stack has its own `down.sh`.
