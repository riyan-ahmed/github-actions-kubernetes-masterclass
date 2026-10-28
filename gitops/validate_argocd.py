#!/usr/bin/env python3
"""Validate the Argo CD manifests against the REAL CRD schemas shipped in the pinned argo-cd chart (offline).

  aiops/.venv/bin/python gitops/validate_argocd.py        # needs helm (+ network on first use) and jsonschema

Renders argo/argo-cd 10.11.6 with gitops/argocd/install/argocd-values.yaml, extracts the Application and AppProject
CustomResourceDefinitions, and validates gitops/argocd/project.yaml and apps/*.yaml against them. A deliberately
broken manifest is used as a negative control so a silent validator cannot pass.
"""
import glob
import pathlib
import subprocess
import sys

import jsonschema
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
rendered = subprocess.run(["helm", "template", "argocd", "argo/argo-cd", "--version", "10.11.6", "-n", "argocd",
                           "-f", str(ROOT / "gitops/argocd/install/argocd-values.yaml")], check=True, capture_output=True, text=True).stdout
crds = {d["spec"]["names"]["kind"]: d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "CustomResourceDefinition"}


def schema(kind):
    return next(v for v in crds[kind]["spec"]["versions"] if v["name"] == "v1alpha1")["schema"]["openAPIV3Schema"]


ok = True
for path in [ROOT / "gitops/argocd/project.yaml", *map(pathlib.Path, sorted(glob.glob(str(ROOT / "gitops/argocd/apps/*.yaml"))))]:
    doc = yaml.safe_load(path.read_text())
    errs = list(jsonschema.Draft7Validator(schema(doc["kind"])).iter_errors(doc))
    print(("VALID   " if not errs else "INVALID ") + f"{path.relative_to(ROOT)} ({doc['kind']})")
    for e in errs[:5]:
        print("   ", list(e.path), e.message[:120])
    ok &= not errs
bad = yaml.safe_load((ROOT / "gitops/argocd/apps/skillpulse-stg.yaml").read_text())
bad["spec"]["syncPolicy"]["automated"], bad["spec"]["source"]["path"] = "yes", 5
control = bool(list(jsonschema.Draft7Validator(schema("Application")).iter_errors(bad)))
print("negative control (broken manifest) rejected:", control)
sys.exit(0 if ok and control else 1)
