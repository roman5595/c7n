# c7n — FinOps stopped-EC2 / unattached-EBS cleanup

Cloud Custodian automation for AWS: back up and clean up long-idle
resources safely — always back up first, delete only once that backup is
confirmed usable, and never trust a tag alone (every destructive action is
re-checked against live AWS state at execution time).

Two functionally identical variants, differing only in how the custom code
gets into the running container. Pick one:

- **[c7n-updated-cloudtrail/](c7n-updated-cloudtrail/)** — plain upstream
  `c7n-org` image, code delivered via Kubernetes ConfigMap volumes. No
  registry or build step needed.
- **[c7n-updated-cloudtrail-baked/](c7n-updated-cloudtrail-baked/)** —
  same code, baked into a custom Docker image at build time instead. Needs
  a registry, but `docker build` becomes a real pre-deploy test gate.

Start with whichever variant's README you land on for the full picture —
each is self-contained (policies, tag contract, IAM, deploy steps). The
baked variant's README assumes you've read the ConfigMap variant's first
for what the automation itself does.

## What it does, briefly

- Stopped EC2 instances past a grace period → AMI backup → verify the AMI
  is real and usable → terminate.
- Unattached EBS volumes past a grace period → snapshot → delete, with a
  CloudTrail-backed check that the volume hasn't actually been reattached
  more recently than its tag claims.
- Separate janitor policies deregister/delete those backups themselves
  once *they've* aged past their own retention window.
- Durable audit trail: per-run output written to a centralized S3 bucket
  (one bucket, hub-account-only IAM, works the same whether there's 1
  target account or 500), plus CloudWatch metrics centralized to the same
  hub account — see each variant's `S3-AND-METRICS-FINDINGS.md`.

Both variants depend on an external tagging process writing/removing the
grace-period tags (`FinOpsStoppedDate`/`FinOpsUnattachedDate`) — see each
variant's "Tag contract" section for the exact contract this automation
assumes.

Planning a rollout across many accounts? [LIMITS.md](LIMITS.md) covers where
this generates AWS API load and which quotas are worth checking first.
