# Agent image: base-image evaluation (local, aarch64)

Same application code and the same dependency set (64 Python packages) in each candidate.
Scanned with `trivy image --scanners vuln` (Trivy 0.76.1), **no `--ignore-unfixed`, no ignore files**.
Reproduce: `make aiops-image-check` (builds the final image and runs the scan + container check).

| Image | Python | OS findings (CRIT / HIGH / MED / LOW) | Python-package findings | Size | Result |
|---|---|---|---|---|---|
| `python:3.12-slim` (Debian 13), v0.1.0 (previously deployed) | 3.12 | 0 / **44** / 58 / 61 | 0 HIGH (6 lower) | 78 MB | works; 44 HIGH, none fixable |
| `python:3.12-alpine` | 3.12 | n/a | n/a | n/a | **fails to build**: `langgraph-checkpoint-sqlite` needs `sqlite-vec`, which has no musllinux aarch64 wheel (pip cannot resolve). Compiling from source was not pursued. |
| multi-stage, `distroless/python3-debian13:nonroot` | 3.13.5 | 0 / **26** / 88 / 53 | 0 | 57 MB | works; still Debian 13 util-linux/ncurses/libsystemd findings |
| multi-stage, `cgr.dev/chainguard/python` (Wolfi), **v0.2.0 (chosen)** | 3.15.1 | **0 / 0 / 0 / 0** | 0 | 63 MB | works |

## The 44 HIGH findings in the original image
8 distinct CVEs across 17 Debian packages, **none with a fixed version** (43 `affected`, 1 `fix_deferred`):
* util-linux family (bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux): CVE-2026-76642, -78408, -78409, -78410 (mount helper / nsenter / X-mount paths)
* ncurses (libncursesw6, libtinfo6, ncurses-base, ncurses-bin): CVE-2025-69720
* systemd libs (libsystemd0, libudev1): CVE-2026-16742; libacl1: CVE-2026-54369; perl-base: CVE-2026-9538
Runtime need: of the 17 packages only `libuuid1` is ever mapped into the Python process (for `uuid`); the other
16 are base-image tools the agent never loads, but Debian marks bsdutils, ncurses-*, perl-base and util-linux
as `Essential`, so they cannot be removed from a Debian image cleanly. Reachability from outside is low (the
pod is non-root with no capabilities), but "low" is not "none", which is why the base image was changed.

## Why Chainguard, and its costs
* Wolfi has no shell, no package manager and a much smaller package set in the runtime stage (30 OS packages vs 87).
* Costs: the free tier only publishes `latest` / `latest-dev`, so digest pins can age out of the registry and the
  base moves to a new Python (3.15.1 today). Mitigations: digests are pinned in the Dockerfile, dependencies are
  pinned in `requirements.lock`, the suite passes on 3.12, 3.13 and 3.15, and the distroless variant is kept
  here as a fallback (`Dockerfile.distroless`).
* Zero findings means "none known to Trivy's databases today", not "no vulnerabilities".

## Verification performed
* Full pytest suite on Python 3.12, 3.13 and 3.15 in fresh containers: 299 passed (+1 test that needs `kubectl`, fixed to skip when it is absent).
* `container_check.py` (34 checks) on every working image: non-root, `--read-only` root fs, `--cap-drop ALL`,
  `no-new-privileges`, tokens only through env, /healthz, 401 for missing/wrong/swapped tokens, valid webhook,
  report + audit hash chain, no token in logs, policy decisions, dangerous-action rejection, redaction, runbook retrieval.
