"""propose_config_pr: turn a persistent config change into a reviewable Git change.

The agent never edits the cluster for persistent configuration. In dry-run it only
writes a proposal file locally; with GitHub enabled it opens a pull request that a
human must review and merge, after which Argo CD (manual sync) applies it.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import yaml


def build_resources_patch(workload: str, container: str, memory_limit: str, memory_request: str = "64Mi",
                          cpu_request: str = "100m") -> str:
    doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": workload},
           "spec": {"template": {"spec": {"containers": [{"name": container, "resources": {
               "requests": {"cpu": cpu_request, "memory": memory_request},
               "limits": {"memory": memory_limit}}}]}}}}
    return yaml.safe_dump(doc, sort_keys=False)


class GitHubPR:
    def __init__(self, repo: str, token: str, base: str = "main", transport: httpx.BaseTransport | None = None):
        self.repo, self.base = repo, base
        self.http = httpx.Client(base_url="https://api.github.com", transport=transport, timeout=20, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def open_pr(self, branch: str, path: str, content: str, title: str, body: str) -> str:
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
        return pr.json()["html_url"]


def propose_config_change(*, settings, policy, incident_id: str, namespace: str, workload: str,
                          params: dict, rationale: str, dry_run: bool,
                          transport: httpx.BaseTransport | None = None) -> dict:
    target = policy.gitops_target(namespace, workload)
    if target is None:
        raise ValueError(f"no GitOps target for {workload} in {namespace}")
    content = build_resources_patch(workload, params["container"], params["memory_limit"])
    title = f"aiops: raise {workload} memory limit to {params['memory_limit']} ({namespace})"
    body = (f"Proposed automatically by the AIOps agent for incident `{incident_id}`.\n\n{rationale}\n\n"
            "Review the incident report before merging. Argo CD sync stays manual.")
    proposal_dir = Path(settings.audit_dir) / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    out = proposal_dir / f"{incident_id}.patch.yaml"
    out.write_text(f"# target: {target['path']}\n# {title}\n{content}")
    result = {"mode": "proposal_file", "path": str(out), "repo_path": target["path"], "title": title}
    if dry_run or not settings.github_enabled:
        result["note"] = "dry-run" if dry_run else "GitHub integration disabled; proposal written locally"
        return result
    pr = GitHubPR(settings.github_repo, settings.github_token, settings.github_base_branch, transport)
    url = pr.open_pr(f"aiops/{incident_id}", target["path"], content, title, body)
    return {**result, "mode": "pull_request", "url": url}
