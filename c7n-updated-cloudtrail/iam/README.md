# IAM

Two files, both need placeholders filled in before use:

- `workload-policy.json` — the permissions the automation actually needs
  (EC2/EBS describe+mutate, CloudTrail read-only, plus writing S3 audit
  output and CloudWatch metrics — see
  [../S3-AND-METRICS-FINDINGS.md](../S3-AND-METRICS-FINDINGS.md)). Attach
  this to the role below as a customer-managed policy.
- `trust-policy.json` — who can assume that role. Two statements:
  - IRSA: lets the `cloud-custodian-ec2-ami` Kubernetes ServiceAccount (see
    `../deploy/serviceaccount.yaml`) assume the role via OIDC federation.
  - Self-assume: `c7n-org` always calls `sts:AssumeRole` for every account
    listed in `accounts.yml`, **including the account it's already running
    in** — this statement is what makes that succeed. Required even for a
    single-account deployment, not just multi-account.

## Placeholders to fill in

| Placeholder | Value |
|---|---|
| `<ACCOUNT_ID>` | the AWS account ID this role lives in |
| `<EKS_OIDC_PROVIDER>` | this cluster's OIDC provider, without the `https://` prefix, e.g. `oidc.eks.<region>.amazonaws.com/id/<ID>` — get it via `aws eks describe-cluster --name <cluster> --query "cluster.identity.oidc.issuer"` |
| `<CLUSTER_NAME>` | matches whatever you name the role itself (see below) |
| `<S3_BUCKET_NAME>` (in `workload-policy.json`) | the audit-output bucket, must already exist — same value as `<S3_BUCKET_NAME>` in `../deploy/cronjob.yaml` |

## Suggested setup

```bash
aws iam create-role --role-name <CLUSTER_NAME>-cloud-custodian-ec2-ami-role \
  --assume-role-policy-document file://trust-policy.json

aws iam create-policy --policy-name <CLUSTER_NAME>-cloud-custodian-ec2-ami-permissions \
  --policy-document file://workload-policy.json

aws iam attach-role-policy --role-name <CLUSTER_NAME>-cloud-custodian-ec2-ami-role \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/<CLUSTER_NAME>-cloud-custodian-ec2-ami-permissions
```

Then point `../deploy/serviceaccount.yaml` and `../examples/accounts.yml` at
the resulting role ARN.
