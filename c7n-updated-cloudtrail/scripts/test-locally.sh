#!/bin/bash
# This variant has no Docker build step (plain upstream image, code
# delivered via ConfigMap - see ../c7n-updated-cloudtrail-baked/ for the
# custom-image alternative, which gets an equivalent gate for free at
# `docker build` time). Run this by hand, or wire it into CI, before ever
# regenerating/applying the cloud-custodian-ec2-ami-code or
# cloud-custodian-ec2-ami-sitecustomize ConfigMaps in deploy/.
#
# Uses the exact same plain upstream image the CronJob uses (no custom
# build), and mounts finops_c7n/ + sitecustomize.py at the exact same paths
# deploy/cronjob.yaml mounts them at, so a pass here means the real
# deployment will behave the same way.
set -euo pipefail

IMAGE="docker.io/cloudcustodian/c7n-org:0.9.51.0"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== unit tests ==="
docker run --rm \
  -v "$ROOT/finops_c7n:/extra-python/finops_c7n:ro" \
  -v "$ROOT/tests:/tests:ro" \
  -e PYTHONPATH=/extra-python \
  --entrypoint /src/.venv/bin/python \
  "$IMAGE" \
  -m unittest discover -s /tests -v

echo "=== policy validation, via the BARE c7n-org command (not a wrapper) ==="
echo "=== this is the real proof: sitecustomize.py must auto-register  ==="
echo "=== our filter/action with zero explicit import anywhere         ==="
docker run --rm \
  -v "$ROOT/finops_c7n:/extra-python/finops_c7n:ro" \
  -v "$ROOT/examples:/work:ro" \
  -v "$ROOT/sitecustomize.py:/usr/lib/python3.12/sitecustomize.py:ro" \
  -e PYTHONPATH=/extra-python \
  --entrypoint c7n-org \
  "$IMAGE" \
  validate -c /work/accounts.yml -u /work/policies.yml

echo "=== all good ==="
