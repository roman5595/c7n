# c7n-updated-cloudtrail

Cloud Custodian automation that backs up and cleans up long-idle AWS
resources: stopped EC2 instances (AMI backup, then terminate) and
unattached EBS volumes (snapshot, then delete). Tag-driven — see
[Tag contract](#tag-contract) below — with a live AWS-state re-check before
every destructive action, never trusting a tag alone.

Delivery mechanism: plain upstream `cloudcustodian/c7n-org` image, no
custom Docker build. `finops_c7n/` (the custom filter/action code) and
`sitecustomize.py` are mounted in via ConfigMap volumes at runtime;
`sitecustomize.py` is Python's own auto-load mechanism (the interpreter
executes it at startup regardless of what command runs), so the bare
`c7n-org`/`custodian` binaries work directly — no ENTRYPOINT wrapper to
remember to use. See [../c7n-updated-cloudtrail-baked/](../c7n-updated-cloudtrail-baked/)
for the same code baked into a custom image instead, if you'd rather trade
the ConfigMap mounts for a build/push step in exchange for a build-time
test gate.

## Policies

Three custodian policies, all in [examples/policies.yml](examples/policies.yml):

- `ec2-stopped-create-ami` — stopped + `FinOpsStoppedDate` tag older than
  the grace threshold + no `DoNotDelete` + no `FinOpsAmiId` yet → creates
  an AMI, writes `FinOpsAmiId` back onto the instance.
- `ec2-stopped-delete-with-ami` — same gates, same threshold (one shared
  grace period, not two escalating ones — the split across two policies is
  purely to let `create-ami` happen in one run and `terminate` in a later
  one, once the AMI has finished baking) + `FinOpsAmiId` present, and the
  `verified-ami-backup` filter confirms that AMI is `available`, sourced
  from this exact instance, and tagged with the same `FinOpsStoppedDate`
  stop-cycle, plus native `state-age` independently confirms (from AWS's
  own `StateTransitionReason`, not a tag) the instance's last real
  transition is also past the grace threshold → `terminate`.
- `ebs-unattached-delete` — unattached (`State: available`) +
  `FinOpsUnattachedDate` older than the same grace threshold + no
  `DoNotDelete` + `cloudtrail-detach-age` (see below) → **one policy, one
  pass**: native `snapshot` (with `copy-tags: [FinOpsUnattachedDate]`) then
  native `delete`. No two-phase split needed here — an EBS snapshot is
  independent of its source volume the moment `CreateSnapshot` is
  accepted, unlike an EC2 AMI which needs time to bake before its source
  can safely go away.

Plus two janitor policies (`ec2-stopped-ami-janitor`,
`ebs-unattached-snapshot-janitor`) that deregister/delete the backups
themselves once they've aged past their own retention window — scoped only
to backups this automation created (matched by the `FinOpsSource` tag it
writes), never a blanket sweep.

## Tag contract

This automation only acts on what a tag says **plus** what live AWS state
confirms — it never trusts a tag alone. `FinOpsStoppedDate`/
`FinOpsUnattachedDate` are expected to be written and removed by something
outside this repo (an external daily tagging job, referred to below as
"the tagger"): tag the resource the day it becomes idle, remove the tag
the moment it's used again. A date tag that survived the whole grace
window is therefore proof the resource was idle the whole time — this
automation never tracks duration itself.

| Tag | Set by | Applied to | Meaning |
|---|---|---|---|
| `FinOpsStoppedDate` | tagger (daily) | stopped EC2 | start of the EC2 grace period; removed by the tagger on start |
| `FinOpsUnattachedDate` | tagger (daily) | unattached EBS | start of the EBS grace period; removed by the tagger on re-attach |
| `DoNotDelete` | human / tagger | any resource | absolute exclusion — pulls the resource out of every gate |
| `FinOpsSource` | this automation | snapshots/AMIs it creates | lets the janitor policies recognize their own artifacts |
| `FinOpsAmiId` | this automation | the instance (EC2 phase 1) | two-phase AMI→terminate hand-off across runs |

**Hard prerequisite:** if the tagger doesn't reliably remove the date tag
when a resource comes back into use, the safety model breaks — live AWS
state alone (`DescribeVolumes`/`DescribeInstances`) can't tell you *how
long* something has been idle, only its current state.

**Restrict who can write these tags (IAM/SCP), separately from this
repo.** Nothing here prevents another principal from forging or
back-dating `FinOpsStoppedDate`/`FinOpsUnattachedDate`, or removing
`DoNotDelete`. The live-state re-check below limits the blast radius of a
forged tag (a resource actually in use gets filtered out regardless of
what the tag claims), but it isn't a substitute for locking down who can
write the tag in the first place.

## What's new here vs. a plain tag-driven `ec2-stopped-*` setup

EC2's `ec2-stopped-delete-with-ami` has two independent signals before it
terminates an instance: the `tag:FinOpsStoppedDate` age check, and the
native `state-age` filter, which reads AWS's own `StateTransitionReason` —
real ground truth that can't be spoofed by (re)writing a tag. EBS's
`ebs-unattached-delete` only ever had the first kind: `DescribeVolumes`
keeps no state-transition history at all, so there was no `state-age`
equivalent for "how long has this volume actually been idle" — a volume
reattached and detached again between two of the tagger's daily runs could
pass the tag check without ever really having sat idle for the full
window.

`finops_c7n/ebs_detach.py` (one class: `NoRecentDetach`, registered as
filter `cloudtrail-detach-age` on `aws.ebs`) fills that gap using
`cloudtrail:LookupEvents` — a free, always-on, no-Trail-required 90-day
"Event History" every account/region already has. Applied to
`ebs-unattached-delete` in [examples/policies.yml](examples/policies.yml):

```yaml
- type: cloudtrail-detach-age
  days: 14
```

Logic is a negative check: look up `DetachVolume` events for this exact
volume (`ResourceName` lookup attribute) within the last `days`. Found one
→ block (the tag is stale/spoofed, or the volume was reattached-and-detached
more recently than it claims). Found none → pass (detached earlier than the
window, or never attached at all — both are safe).

Why this shape and not something heavier:
- `LookupEvents` supports only **one** `LookupAttributes` entry at a time
  (a documented API constraint) — so this filters by `ResourceName`
  server-side and by `EventName == DetachVolume` client-side.
- Event History covers 90 days; the grace threshold here is 14–30. No need
  for `c7n-trailcreator`'s Athena/S3-Select approach (built for
  retroactive lookups *beyond* 90 days) or a `mode: cloudtrail` Lambda
  (real-time, but needs its own Lambda IAM role + EventBridge rule, and
  has **no retroactive coverage** for volumes already unattached before
  it's deployed — the opposite of what's needed for an already-running
  fleet).
- Rate limit is 2 req/s **per account, per region** (per the [API
  reference](https://docs.aws.amazon.com/awscloudtrail/latest/APIReference/API_LookupEvents.html))
  — isolated per target account, so it doesn't get tighter as more
  accounts are added. `self.manager.retry(...)` (c7n's own retry helper)
  already retries on `ThrottlingException` by default, so no extra retry
  logic was needed.

New IAM permission: `cloudtrail:LookupEvents` (read-only, `Resource: "*"`
— this action has no resource-level scoping). See [iam/](iam/).

## Files

```text
c7n-updated-cloudtrail/
├── finops_c7n/              # the custom filter/action code
│   ├── __init__.py          # imports both, registers with c7n's plugin registries
│   ├── ec2_ami.py           # create-ami action, verified-ami-backup filter
│   └── ebs_detach.py        # cloudtrail-detach-age filter
├── tests/                   # 22 unit tests
├── examples/
│   ├── accounts.yml         # fill in your account/role/regions
│   └── policies.yml         # the 5 policies described above
├── iam/                     # workload permission policy + IRSA trust policy
├── deploy/                  # k8s manifests - see below
├── scripts/test-locally.sh  # run before touching the cluster
├── sitecustomize.py         # the auto-load mechanism, mounted as a ConfigMap
└── S3-AND-METRICS-FINDINGS.md  # audit output + metrics, validated details
```

## How to deploy

1. Set up IAM — see [iam/README.md](iam/README.md).
2. Fill in [examples/accounts.yml](examples/accounts.yml) (account ID,
   role ARN, regions) and review [examples/policies.yml](examples/policies.yml)
   (grace period thresholds, prod vs non-prod values are called out inline).
2b. Fill in `<S3_BUCKET_NAME>`/`<S3_PREFIX>`/`<REGION>` in
   [deploy/cronjob.yaml](deploy/cronjob.yaml)'s command block — the bucket
   must already exist, in this hub account. See
   [S3-AND-METRICS-FINDINGS.md](S3-AND-METRICS-FINDINGS.md) for what this
   buys you and exactly which IAM it needs (already in
   `iam/workload-policy.json`, just needs the same bucket name filled in).
3. Test locally, before touching the cluster:
   ```bash
   ./scripts/test-locally.sh
   ```
   Runs the unit tests, then a bare `c7n-org validate` against the plain
   upstream image with the same ConfigMap-equivalent mounts the real
   deployment uses — a pass here means the real deployment will behave the
   same way. **Fill in step 2 first** — the placeholder `<ACCOUNT_ID>`
   fails `c7n-org validate`'s `^[0-9]{12}$` schema check on its own.
4. Generate the ConfigMaps (already generated once into `deploy/` from the
   placeholder examples — regenerate after you fill them in):
   ```bash
   kubectl create configmap cloud-custodian-ec2-ami-policies \
     --from-file=accounts.yml=examples/accounts.yml \
     --from-file=policies.yml=examples/policies.yml \
     -n monitoring --dry-run=client -o yaml > deploy/configmap-policies.yaml

   kubectl create configmap cloud-custodian-ec2-ami-code \
     --from-file=finops_c7n/__init__.py \
     --from-file=finops_c7n/ec2_ami.py \
     --from-file=finops_c7n/ebs_detach.py \
     -n monitoring --dry-run=client -o yaml > deploy/configmap-code.yaml

   kubectl create configmap cloud-custodian-ec2-ami-sitecustomize \
     --from-file=sitecustomize.py \
     -n monitoring --dry-run=client -o yaml > deploy/configmap-sitecustomize.yaml
   ```
5. `kubectl apply -f deploy/serviceaccount.yaml`, then the three
   `configmap-*.yaml` files, then `deploy/cronjob.yaml`.
6. Leave `suspend: true` (the default) until you've triggered at least one
   manual run and reviewed its logs:
   ```bash
   kubectl create job --from=cronjob/cloud-custodian-ec2-ami -n monitoring manual-test-1
   kubectl logs -n monitoring -l job-name=manual-test-1
   ```
   The log now only prints a short CSV summary and the S3 path — the full
   per-resource JSON is in the bucket, not in `kubectl logs`.

## Known gaps, not addressed by this repo

- **No IAM/SCP restriction on who can write the FinOps date tags** — see
  [Tag contract](#tag-contract) above.
- **No alerting** on a failed or skipped run. `ResourceLimitExceeded` (the
  `max-resources` circuit breaker tripping) is emitted as a CloudWatch
  metric but nothing currently alarms on it — see
  [S3-AND-METRICS-FINDINGS.md](S3-AND-METRICS-FINDINGS.md).
- **Single account only**, as shipped — `examples/accounts.yml` lists one
  account. `c7n-org` fans out to multiple accounts/regions natively (just
  more entries in `accounts.yml`), but cross-account IAM (a role in each
  target account trusting this one) isn't set up here.
