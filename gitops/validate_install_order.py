#!/usr/bin/env python3
"""Offline check: every namespace that a namespaced Role/RoleBinding in the rendered charts targets is either created by the
chart itself or listed as pre-created in docs/aiops-setup.md (4.2 step 0). Catches "namespaces X not found" before an install.
Usage: gitops/validate_install_order.py   (needs helm and network access to the pinned charts)"""
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHARTS = [
    ("argocd", "argo/argo-cd", "10.11.6", "argocd", "gitops/argocd/install/argocd-values.yaml", True),
    ("sealed-secrets", "oci://registry-1.docker.io/bitnamicharts/sealed-secrets", "2.22.0", "sealed-secrets",
     "gitops/argocd/install/sealed-secrets-values.yaml", True),
]
PRECREATED = set(re.findall(r"kubectl create namespace (\S+)", (REPO / "docs" / "aiops-setup.md").read_text().split("# 1. Sealed")[0]))


def render(name, chart, version, ns, values):
    out = subprocess.run(["helm", "template", name, chart, "--version", version, "-n", ns, "-f", str(REPO / values)],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def main():
    bad = []
    for name, chart, version, ns, values, creates_ns in CHARTS:
        docs = render(name, chart, version, ns, values)
        targets = {d["metadata"].get("namespace") for d in docs if d["kind"] not in ("CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding")}
        available = {ns} | PRECREATED   # helm --create-namespace creates the release namespace itself
        missing = {t for t in targets if t and t not in available}
        print(f"{name}: targets {sorted(t for t in targets if t)}; pre-created {sorted(PRECREATED)}; missing {sorted(missing) or 'none'}")
        bad += sorted(missing)
    if bad:
        print("FAIL: namespaces needed but never created before install:", bad)
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
