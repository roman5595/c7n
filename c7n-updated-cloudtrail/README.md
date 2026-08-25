# c7n-updated-cloudtrail — c7n-updated + CloudTrail-verified EBS detach age

This is [../c7n-updated/](../c7n-updated/) (ConfigMap + `sitecustomize.py`
delivery, no custom Docker image) **plus one addition**: a CloudTrail-backed
ground-truth check for `ebs-unattached-delete`, closing the one gap that
variant still had. **This is the currently deployed build** — its CronJob
(`cloud-custodian-ec2-ami-v3`, `suspend: true`) and three ConfigMaps are
applied in the `monitoring` namespace, side by side with `c7n-updated`'s
(`-v2`, also suspended) so both can still be compared if needed.

## What's new here vs. `c7n-updated`

EC2's `ec2-stopped-delete-with-ami` has two independent signals before it
terminates an instance: the `tag:FinOpsStoppedDate` age check, and the
native `state-age` filter, which reads AWS's own `StateTransitionReason` -
real ground truth that can't be spoofed by (re)writing a tag. EBS's
`ebs-unattached-delete` only ever had the first kind: `DescribeVolumes`
keeps no state-transition history at all, so there was no `state-age`
equivalent for "how long has this volume actually been idle" - a volume
reattached and detached again between two of wiv's daily tag-writer runs
could pass the tag check without ever really having sat idle for 14 days.

`finops_c7n/ebs_detach.py` (new file, one class: `NoRecentDetach`,
registered as filter `cloudtrail-detach-age` on `aws.ebs`) fills that gap
using `cloudtrail:LookupEvents` - a free, always-on, no-Trail-required
90-day "Event History" every account/region already has. Added to
`ebs-unattached-delete` in `examples/policies.yml`:

```yaml
- type: cloudtrail-detach-age
  days: 14
```

Logic is a negative check: look up `DetachVolume` events for this exact
volume (`ResourceName` lookup attribute) within the last `days`. Found one →
block (the tag is stale/spoofed, or the volume was reattached-and-detached
more recently than it claims). Found none → pass (detached earlier than the
window, or never attached at all - both are safe).

Why this shape and not something heavier:
- `LookupEvents` supports only **one** `LookupAttributes` entry at a time
  (a real, documented API constraint) - so this filters by `ResourceName`
  server-side and by `EventName == DetachVolume` client-side.
- Event History covers 90 days; our threshold is 14-30. No need for
  `c7n-trailcreator`'s Athena/S3-Select approach (that tool exists
  specifically for retroactive lookups *beyond* 90 days, which we don't
  need) or a `mode: cloudtrail` Lambda (real-time, but needs its own Lambda
  IAM role + EventBridge rule, and has **no retroactive coverage** for
  volumes already unattached before it's deployed - the opposite of what we
  need for an already-running fleet).
- Rate limit is 2 req/s **per account, per region** (confirmed via the
  [official API reference](https://docs.aws.amazon.com/awscloudtrail/latest/APIReference/API_LookupEvents.html))
  - isolated per target account, so it doesn't get tighter as more accounts
  are added later. `self.manager.retry(...)` (same helper already used
  elsewhere in `finops_c7n`) already retries on `ThrottlingException` by
  default (verified in `c7n/query.py`'s `QueryResourceManager.retry`), so
  no extra retry logic was needed for the rare within-account burst case.

New IAM permission: `cloudtrail:LookupEvents` (read-only, `Resource: "*"` -
this action has no resource-level scoping), added to
`../c7n-custom/iam/workload-policy.json` as its own statement,
`VerifyEbsDetachViaCloudTrail`.

## Real-world verification (not just unit tests)

Tested directly against the POC account (<ACCOUNT_ID>): created a volume,
attached it to a throwaway instance, detached it for real (producing a
genuine `DetachVolume` CloudTrail event), then tagged
`FinOpsUnattachedDate` 40 days in the past - i.e. a tag that *lies* about
how long it's been idle. A control volume, never attached at all, got the
same backdated tag. One `ebs-unattached-delete` run: the spoofed-tag volume
was correctly left `available` (blocked), the control volume was correctly
snapshotted and deleted (`matched:1`, not 2). See `POC-TEST.md` in
`c7n-custom/` for the full run log.

## Files (only what differs from `../c7n-updated/`)

```text
c7n-updated-cloudtrail/
├── finops_c7n/
│   ├── __init__.py         # now imports both ec2_ami and ebs_detach
│   ├── ec2_ami.py          # unchanged
│   └── ebs_detach.py        # new - NoRecentDetach / cloudtrail-detach-age
├── tests/
│   ├── test_ec2_ami.py      # unchanged
│   └── test_ebs_detach.py   # new - 8 tests
├── examples/policies.yml    # ebs-unattached-delete gains the new filter
└── deploy/
    ├── cronjob.yaml          # named -v3, its own ConfigMap names (see below)
    ├── configmap-code.yaml   # now also carries ebs_detach.py
    ├── configmap-policies.yaml
    └── configmap-sitecustomize.yaml
```

Everything else (`sitecustomize.py`, `scripts/test-locally.sh`,
`serviceaccount.yaml`, `accounts.yml`) is unchanged from `../c7n-updated/`.

## How to test (before touching the cluster)

```bash
./scripts/test-locally.sh
```

Same as `c7n-updated` - unit tests (now 22, was 14) then a bare `c7n-org
validate` against the plain upstream image with the ConfigMap-equivalent
mounts.

## How to (re)generate the ConfigMaps

```bash
kubectl create configmap cloud-custodian-ec2-ami-v3-policies \
  --from-file=accounts.yml=examples/accounts.yml \
  --from-file=policies.yml=examples/policies.yml \
  -n monitoring --dry-run=client -o yaml > deploy/configmap-policies.yaml

kubectl create configmap cloud-custodian-ec2-ami-v3-code \
  --from-file=finops_c7n/__init__.py \
  --from-file=finops_c7n/ec2_ami.py \
  --from-file=finops_c7n/ebs_detach.py \
  -n monitoring --dry-run=client -o yaml > deploy/configmap-code.yaml

kubectl create configmap cloud-custodian-ec2-ami-v3-sitecustomize \
  --from-file=sitecustomize.py \
  -n monitoring --dry-run=client -o yaml > deploy/configmap-sitecustomize.yaml
```

## Status

- Deployed to `monitoring` (CronJob `cloud-custodian-ec2-ami-v3`,
  `suspend: true`), IAM permission live (policy version `v5`).
- Verified end to end against real AWS - see above and `POC-TEST.md`.
- `c7n-updated`'s `-v2` CronJob is still present, also suspended, for
  side-by-side comparison - not yet decided whether to remove it.
