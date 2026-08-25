"""Auto-loaded by the Python interpreter itself at startup (see PEP/site docs
for the `sitecustomize` mechanism) - this is NOT imported explicitly anywhere.

Its only purpose is to register finops_c7n's custom create-ami action and
verified-ami-backup filter before ANY invocation of c7n-org/custodian, no
matter what command line was actually used to start it. This replaces the
old approach of requiring every caller to remember to run
`python -m finops_c7n.bootstrap` instead of the bare `c7n-org`/`custodian`
binaries - that requirement was a real, already-hit bug (Kubernetes
`command:` replaces the image ENTRYPOINT rather than layering on it, so a
bare `c7n-org` silently skipped registration and c7n reported the custom
filter as "Invalid filter type").

Deployed via a ConfigMap volume mount at the exact path this interpreter
resolves first for `sitecustomize.py` (verified empirically for the
`cloudcustodian/c7n-org:0.9.51.0` image: `/usr/lib/python3.12/sitecustomize.py`
wins over the venv's own site-packages - see deploy/cronjob.yaml). If a
future image version changes that resolution order, `scripts/test-locally.sh`
below is designed to catch it before deployment, since it exercises the bare
`c7n-org` command exactly as the CronJob does.
"""

import finops_c7n  # noqa: F401
