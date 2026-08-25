# c7n-updated-cloudtrail-baked — same code, baked into a custom image

This is [../c7n-updated-cloudtrail/](../c7n-updated-cloudtrail/) (identical
`finops_c7n` code, including `ebs_detach.py`/`cloudtrail-detach-age`, identical
policies) delivered a different way: **baked into a custom Docker image at
build time**, instead of mounted in via ConfigMap volumes at runtime.

## Four variants now exist - what each one is

| | `c7n-custom` | `c7n-updated` | `c7n-updated-cloudtrail` | `c7n-updated-cloudtrail-baked` (this one) |
|---|---|---|---|---|
| Image | Custom, `Dockerfile`, personal Docker Hub | Plain upstream | Plain upstream | Custom, `Dockerfile` |
| Code delivery | Baked at build time | ConfigMap volume | ConfigMap volume | Baked at build time |
| Load mechanism | `bootstrap.py` ENTRYPOINT wrapper | `sitecustomize.py` | `sitecustomize.py` | `sitecustomize.py` |
| Bare `c7n-org`/`custodian` works? | No - must use the wrapper | Yes | Yes | Yes |
| Build-time test gate | Yes | No (`scripts/test-locally.sh`, manual) | No (same) | Yes |
| EBS CloudTrail check | No | No | Yes | Yes |

This variant is the "best of both": the structural immunity to the
ENTRYPOINT-bypass bug class that made `c7n-updated` worth building in the
first place (`sitecustomize.py`, not a custom ENTRYPOINT), *plus* the
build-time test gate that got traded away when `c7n-updated` dropped the
custom image. The one thing it brings back from `c7n-custom`: needing an
image registry and a build/push step again.

## What's actually different from `c7n-updated-cloudtrail`

- New `Dockerfile` (3-stage: `plugin` → `test` → `runtime`, same shape as
  `../c7n-custom/Dockerfile`) that `COPY`s `finops_c7n/` and
  `sitecustomize.py` into the image instead of mounting them.
- `sitecustomize.py` lands at the exact same path found to win in this
  image's `sys.path` order (`/usr/lib/python3.12/sitecustomize.py`) - same
  file, same reasoning, just `COPY`'d at build time instead of mounted.
- The `test` stage runs the same 22 unit tests, then `c7n-org validate` via
  the **bare** binary (no wrapper) - if either fails, `docker build` fails,
  so a bad build can never reach `docker push`.
- **No custom `ENTRYPOINT`/`CMD` at all** - deliberately left unset,
  inheriting the upstream image's own. `sitecustomize.py` makes a wrapper
  unnecessary; confirmed locally that `docker run <image> validate -c ... -u
  ...` with zero entrypoint override works.
- `deploy/`: only one ConfigMap now (`cloud-custodian-ec2-ami-v4-policies` -
  policies/accounts only). No code or sitecustomize ConfigMaps - both are
  baked into the image. `cronjob.yaml`'s `command:` drops the
  `PYTHONPATH`/bootstrap plumbing entirely - it's just the plain
  `c7n-org run`/`report` calls, same as what a bare install would look like.
- `scripts/test-locally.sh` is gone - `docker build` *is* the test now.

## Local build/test - already done once

```bash
docker build -t c7n-org-finops-baked:test .
```

Ran for real: all 22 unit tests passed, `c7n-org validate` (bare binary)
passed, both *inside* the build (`test` stage). Confirmed after the build,
with zero ENTRYPOINT override:

```bash
docker run --rm -v "$(pwd)/examples:/work:ro" c7n-org-finops-baked:test \
  validate -c /work/accounts.yml -u /work/policies.yml
# => Configuration valid / Validation complete - all policies are valid!
```

and directly confirmed the registrations exist at runtime:

```bash
docker run --rm --entrypoint /src/.venv/bin/python c7n-org-finops-baked:test -c "
from c7n.resources.ec2 import EC2
from c7n.resources.ebs import EBS
print('verified-ami-backup' in EC2.filter_registry,
      'create-ami' in EC2.action_registry,
      'cloudtrail-detach-age' in EBS.filter_registry)
"
# => True True True
```

See `POC-TEST.md` (in `../c7n-custom/`) for the dated write-up.

## Status - deployed and verified for real

Pushed to `docker.io/roman5595/c7n-org-ami:0.9.51.0-finops.2` (public repo,
same personal Docker Hub account `c7n-custom` already used). Applied to
`monitoring` as CronJob `cloud-custodian-ec2-ami-v4` (`suspend: true`,
alongside `-v2`/`-v3`). A manual run pulled the image from Docker Hub for
real and completed cleanly - all 5 policies ran across both `eu-west-1`
and `eu-central-1` with zero registration errors (no fixtures were live at
the time, so everything reported `matched:0`, but the point of this run was
proving the baked image + real pull + real cluster works end to end, which
the earlier variants' fixture-based tests already covered for the
filter/action logic itself). See `POC-TEST.md` for the dated write-up.
