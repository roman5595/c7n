# c7n-updated-cloudtrail-baked

Same automation as [../c7n-updated-cloudtrail/](../c7n-updated-cloudtrail/)
— identical `finops_c7n` code and policies (stopped-EC2 AMI backup +
terminate, unattached-EBS snapshot + delete, both with a CloudTrail-backed
detach-age check, both janitor policies for backup retention) — delivered
a different way: **baked into a custom Docker image at build time**,
instead of mounted in via ConfigMap volumes at runtime.

Read [../c7n-updated-cloudtrail/README.md](../c7n-updated-cloudtrail/README.md)
first for what the automation actually does and the tag contract it
depends on. This README only covers what's different about *delivery*.

## Which variant to use

| | ConfigMap (`../c7n-updated-cloudtrail/`) | Baked (this one) |
|---|---|---|
| Image | Plain upstream, unmodified | Custom, built from `Dockerfile` |
| Code delivery | Mounted via ConfigMap volumes | `COPY`'d into the image at build time |
| Registry needed | No | Yes — somewhere this cluster can pull from |
| Pre-deploy test gate | Manual (`scripts/test-locally.sh`) | Automatic (`docker build` fails outright if tests fail) |
| Update a policy | Edit YAML, regenerate + reapply ConfigMap | Same, no rebuild needed (`accounts.yml`/`policies.yml` stay ConfigMap-mounted either way) |
| Update `finops_c7n` code | Edit + regenerate + reapply ConfigMap | Edit, rebuild, push, update `image:` |

Both land on the same `sitecustomize.py` mechanism (Python auto-executes
it at startup, so the bare `c7n-org`/`custodian` binaries work directly —
no custom `ENTRYPOINT` wrapper to remember to use in either variant).

Pick the ConfigMap variant if you don't want to run a registry for this,
or if the policy/code content changes often and you'd rather skip the
build/push cycle. Pick this one if you want `docker build` itself to be a
hard gate that a broken change can't get past, and you already have
somewhere to push images.

## What's actually different from `../c7n-updated-cloudtrail/`

- [Dockerfile](Dockerfile): 3-stage build (`plugin` → `test` → `runtime`).
  `COPY`s `finops_c7n/` and `sitecustomize.py` into the image instead of
  mounting them. `sitecustomize.py` lands at the same path Python resolves
  first on this image (`/usr/lib/python3.12/sitecustomize.py`) — same
  mechanism as the ConfigMap variant, just `COPY`'d at build time.
- The `test` stage runs the full unit test suite, then `c7n-org validate`
  via the **bare** binary (no wrapper) — if either fails, `docker build`
  fails, so a broken build can never reach `docker push`.
- **No custom `ENTRYPOINT`/`CMD`** — deliberately left unset, inheriting
  the upstream image's own. `docker run <image> validate -c ... -u ...`
  works with zero entrypoint override.
- `deploy/`: only one ConfigMap now (policies/accounts only) — no code or
  sitecustomize ConfigMaps, both are baked into the image.
  `cronjob.yaml`'s `command:` drops the `PYTHONPATH` plumbing entirely.
- No `scripts/test-locally.sh` — `docker build` *is* the test now.

## Build and test locally

```bash
docker build -t c7n-org-finops-baked:test .
```

The build's `test` stage runs the full unit suite plus `c7n-org validate`
against the bare binary — a failing test fails the build. **This will
fail out of the box** against the example `examples/accounts.yml`:
`c7n-org validate` checks `account_id` against a strict `^[0-9]{12}$`
schema, and the placeholder `<ACCOUNT_ID>` doesn't match it. Fill in a
real account ID first (step 2 below) and it passes. After a successful
build, you can double-check manually:

```bash
docker run --rm -v "$(pwd)/examples:/work:ro" c7n-org-finops-baked:test \
  validate -c /work/accounts.yml -u /work/policies.yml
# => Configuration valid / Validation complete - all policies are valid!

docker run --rm --entrypoint /src/.venv/bin/python c7n-org-finops-baked:test -c "
from c7n.resources.ec2 import EC2
from c7n.resources.ebs import EBS
print('verified-ami-backup' in EC2.filter_registry,
      'create-ami' in EC2.action_registry,
      'cloudtrail-detach-age' in EBS.filter_registry)
"
# => True True True
```

## How to deploy

1. Set up IAM — see [iam/README.md](iam/README.md) (identical to the
   ConfigMap variant — same permissions regardless of delivery mechanism,
   now includes S3 + CloudWatch for the hub role — see
   [S3-AND-METRICS-FINDINGS.md](S3-AND-METRICS-FINDINGS.md)).
2. Fill in [examples/accounts.yml](examples/accounts.yml) and review
   [examples/policies.yml](examples/policies.yml).
2b. Fill in `<S3_BUCKET_NAME>`/`<S3_PREFIX>`/`<REGION>` in
   [deploy/cronjob.yaml](deploy/cronjob.yaml)'s command block — the bucket
   must already exist, in this hub account.
3. Build and push to a registry you control:
   ```bash
   docker build -t <YOUR_REGISTRY>/c7n-org-finops:<TAG> .
   docker push <YOUR_REGISTRY>/c7n-org-finops:<TAG>
   ```
4. Update `image:` in [deploy/cronjob.yaml](deploy/cronjob.yaml) to that
   pushed reference.
5. Generate the one ConfigMap:
   ```bash
   kubectl create configmap cloud-custodian-ec2-ami-policies \
     --from-file=accounts.yml=examples/accounts.yml \
     --from-file=policies.yml=examples/policies.yml \
     -n monitoring --dry-run=client -o yaml > deploy/configmap-policies.yaml
   ```
6. `kubectl apply -f deploy/serviceaccount.yaml`, then
   `configmap-policies.yaml`, then `deploy/cronjob.yaml`.
7. Leave `suspend: true` (the default) until you've triggered at least one
   manual run and reviewed its logs:
   ```bash
   kubectl create job --from=cronjob/cloud-custodian-ec2-ami -n monitoring manual-test-1
   kubectl logs -n monitoring -l job-name=manual-test-1
   ```
   `kubectl logs` now only shows `c7n-org run`'s own `-v` progress output —
   the full per-resource JSON is in the S3 bucket, not in `kubectl logs`.

## Known gaps, not addressed by this repo

Same as [../c7n-updated-cloudtrail/README.md](../c7n-updated-cloudtrail/README.md#known-gaps-not-addressed-by-this-repo)
— tag-write IAM/SCP restriction, alerting, and multi-account rollout are
all independent of delivery mechanism. Durable audit trail (S3) and
metrics are now wired in — see
[S3-AND-METRICS-FINDINGS.md](S3-AND-METRICS-FINDINGS.md).
