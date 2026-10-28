# AIOps agent image: multi-stage, Chainguard Python (Wolfi), non-root, no shell in the runtime image.
# Build context: aiops/
#   docker build --platform linux/arm64 -t skillpulse-aiops-agent:0.2.0 -f aiops/Dockerfile aiops/
#
# Base images are pinned by digest for reproducibility. To update: pull the new images, run
# aiops/docker/container_check.py and a Trivy scan, then change both digests deliberately.
#   latest-dev -> cgr.dev/chainguard/python:latest-dev@sha256:d0c4d53a...   (builder, has pip)
#   latest     -> cgr.dev/chainguard/python:latest@sha256:1fa5b8de...       (runtime, no pip/shell)
# Dependencies come from requirements.lock (exact versions resolved and tested together).
# The Debian-slim, distroless and Alpine alternatives and why they lost: aiops/docker/README.md

FROM cgr.dev/chainguard/python@sha256:d0c4d53aa4ef455b60a9918fd680bf80b82151e2e56c524d8931ffb8115865bd AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
USER root
COPY requirements.lock /tmp/requirements.lock
RUN pip install --target /deps -r /tmp/requirements.lock
RUN mkdir -p /data/audit /out/aiops
COPY __init__.py /out/aiops/__init__.py
COPY agent /out/aiops/agent
COPY runbooks /out/aiops/runbooks
COPY policies /out/aiops/policies

FROM cgr.dev/chainguard/python@sha256:1fa5b8de4d71f79354c71b5f8fd3cb591f51e41dc9e9d620eda2526d1e57a221
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/deps:/app
COPY --from=builder /deps /deps
COPY --from=builder /out /app
COPY --from=builder --chown=65532:65532 /data /data
WORKDIR /app
USER 65532:65532
EXPOSE 8085
ENTRYPOINT ["python", "-m", "aiops.agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8085"]
