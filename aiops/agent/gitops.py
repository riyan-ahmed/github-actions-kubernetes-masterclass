"""propose_config_pr: turn a persistent config change into a reviewable Git change.

The agent never edits the cluster for persistent configuration. In dry-run it only
writes a proposal file locally; with GitHub enabled it opens a pull request that a
human must review and merge, after which Argo CD applies it (manual sync for stg/prd, automatic for oom-demo).
"""
from __future__ import annotations

import base64
import posixpath
from pathlib import Path

import httpx
import yaml


def build_resources_patch(workload: str, container: str, memory_limit: str, memory_request: str = "64Mi",
                          cpu_request: str = "100m", include_requests: bool = True) -> str:
    resources: dict = {"limits": {"memory": memory_limit}}
    if include_requests:
        resources = {"requests": {"cpu": cpu_request, "memory": memory_request}, **resources}
    doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": workload},
           "spec": {"template": {"spec": {"containers": [{"name": container, "resources": resources}]}}}}
    return yaml.safe_dump(doc, sort_keys=False)


def check_pr_path(path: str, prefixes: list[str]) -> str:
    """The pull request may only touch one normalised, relative file under an allowed prefix."""
    norm = posixpath.normpath(path)
    if (norm != path or path.startswith("/") or ".." in path.split("/") or not path.endswith(".yaml")
            or not any(path.startswith(pre) for pre in prefixes)):
        raise ValueError(f"path {path!r} is outside the allowed pull-request scope {prefixes}")
    return path


class GitHubPR:
    def __init__(self, repo: str, token: str, base: str = "main", transport: httpx.BaseTransport | None = None):
        self.repo, self.base = repo, base
        self.http = httpx.Client(base_url="https://api.github.com", transport=transport, timeout=20, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def get_pr(self, number: int) -> dict:
        r = self.http.get(f"/repos/{self.repo}/pulls/{int(number)}")
        r.raise_for_status()
        j = r.json()
        return {"state": j.get("state"), "merged": bool(j.get("merged_at")), "merged_at": j.get("merged_at"),
                "merge_commit_sha": j.get("merge_commit_sha"), "closed_at": j.get("closed_at"),
                "merged_by": (j.get("merged_by") or {}).get("login")}

    def open_pr(self, branch: str, path: str, content: str, title: str, body: str) -> str:
        return self.open_pr_detail(branch, path, content, title, body)["url"]

    def open_pr_detail(self, branch: str, path: str, content: str, title: str, body: str) -> dict:
        r = self.http.get(f"/repos/{self.repo}/git/ref/heads/{self.base}")
        r.raise_for_status()
        sha = r.json()["object"]["sha"]
        self.http.post(f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}).raise_for_status()
        existing = self.http.get(f"/repos/{self.repo}/contents/{path}", params={"ref": branch})
        payload = {"message": title, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
        self.http.put(f"/repos/{self.repo}/contents/{path}", json=payload).raise_for_status()
        pr = self.http.post(f"/repos/{self.repo}/pulls",
                            json={"title": title, "head": branch, "base": self.base, "body": body})
        pr.raise_for_status()
        j = pr.json()
        return {"url": j["html_url"], "number": j["number"], "created_at": j.get("created_at"), "branch": branch,
                "base": self.base}


def propose_config_change(*, settings, policy, incident_id: str, namespace: str, workload: str,
                          params: dict, rationale: str, dry_run: bool,
                          transport: httpx.BaseTransport | None = None) -> dict:
    target = policy.gitops_target(namespace, workload)
    if target is None:
        raise ValueError(f"no GitOps target for {workload} in {namespace}")
    check_pr_path(target["path"], policy.g.get("pr_path_prefixes", ["k8s/gitops/"]))
    content = build_resources_patch(workload, params["container"], params["memory_limit"],
                                    include_requests=target.get("patch_requests", True))
    title = f"aiops: raise {workload} memory limit to {params['memory_limit']} ({namespace})"
    app = target.get("argocd_app")
    sync_note = (f"After you merge, Argo CD Application `{app}` syncs automatically (pruning disabled)."
                 if app else "Argo CD sync stays manual.")
    body = (f"Proposed automatically by the AIOps agent for incident `{incident_id}`.\n\n{rationale}\n\n"
            f"**Scope:** this PR changes only `{target['path']}` (`limits.memory` of container "
            f"`{params['container']}`). **Merging it is the human approval.** {sync_note}\n\n"
            "Review the incident report before merging.")
    proposal_dir = Path(settings.audit_dir) / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    out = proposal_dir / f"{incident_id}.patch.yaml"
    out.write_text(f"# target: {target['path']}\n# {title}\n{content}")
    result = {"mode": "proposal_file", "path": str(out), "repo_path": target["path"], "title": title}
    # `dry_run` here is the PR dry-run (AIOPS_PR_DRY_RUN), supplied by actions.py; it is NOT the Kubernetes dry-run.
    if dry_run or not settings.github_enabled:
        result["note"] = "PR dry-run" if dry_run else "GitHub integration disabled; proposal written locally"
        return result
    pr = GitHubPR(settings.github_repo, settings.github_token, settings.github_base_branch, transport)
    detail = pr.open_pr_detail(f"aiops/{incident_id}", target["path"], content, title, body)
    return {**result, "mode": "pull_request", **detail, "memory_limit": params["memory_limit"],
            "container": params["container"], "argocd_app": app}
