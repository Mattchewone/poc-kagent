# demo-todo-gitops

Pushed to `poc/demo-todo-gitops` on poc-gitea by `scripts/delivery-up.sh`. Edit it in the mock-poc repo
(`deploy/gitops/`), not in Gitea.

| Path | Used by |
|---|---|
| `base/` | everything: Argo Rollout + NodePort Service for `kargo-registry:5000/demo-todo` |
| `stages/<stage>/` | Kargo's `promote` task renders it into the `stage/<stage>` branch; Argo CD app `demo-todo-<stage>` syncs that branch |
| `preview/` | per-PR Argo CD Applications created by Temporal (`demo-todo-pr-<n>`), which set the namespace, image and NodePort |
