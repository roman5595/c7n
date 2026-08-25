# S3 output and CloudWatch metrics — validated findings

Empirically tested (not just source-read) against a real hub + spoke AWS
account pair. Both features are wired into [deploy/cronjob.yaml](deploy/cronjob.yaml)
using the settings validated here.

## S3 — centralized audit trail, one bucket for any number of accounts

Original concern: does a multi-account rollout need one S3 bucket **per
target account** (500 accounts → 500 buckets)? No.

- `c7n`'s `S3Output` (`c7n/resources/aws.py`) always writes using the
  **hub/base identity** (`session_factory(assume=False)`), never the
  per-account assumed role — regardless of which spoke account's resources
  are being scanned. Confirmed empirically: a spoke role given **zero** S3
  permissions still wrote successfully into the hub bucket.
- Only permission needed, only on the hub execution role:
  `s3:PutObject` on the bucket (see [iam/workload-policy.json](iam/workload-policy.json),
  `WriteAuditOutputToS3`). No spoke-side S3 IAM at all.
- Output path auto-partitions per account/region/policy/run, so multiple
  accounts writing to the same bucket never collide:
  `<bucket>/<prefix>/<account-name>/<region>/<policy-name>/<YYYY>/<MM>/<DD>/<HH>/{resources,metadata,custodian-run.log}.json.gz`
- Output is gzip-compressed. This is hardcoded in `BlobOutput.__exit__`
  (`c7n/output.py`) — there is no config flag to disable it. Readable with
  `zcat`, Athena, or S3 Select.
- **Region gotcha**: without an explicit `?region=` on the `-s` URL, the S3
  client's region follows whichever spoke region is *currently being
  scanned*, not the bucket's actual region. Always pin it:
  `-s "s3://<bucket>/<prefix>?region=<bucket-region>"`.
- Once S3 output is on, `kubectl logs` stops being the audit trail — the
  full per-resource JSON dump moves to S3. The job's log now only prints a
  short human-readable CSV summary plus the S3 path, not the raw match
  data (see the command block in `deploy/cronjob.yaml`).

## CloudWatch metrics — two routing modes, pick based on IAM appetite

- Plain `--metrics` (no URI): each account's metrics land in **that
  account's own** CloudWatch, namespace `CloudMaid`. Requires
  `cloudwatch:PutMetricData` on **every spoke role**.
- `--metrics-uri "aws://master?region=<region>"`: all accounts' metrics
  land in the **hub account's** CloudWatch instead — same centralization
  model as S3, hub-only IAM (`WriteCentralizedMetrics` in
  `iam/workload-policy.json`). This is the mode wired into
  `deploy/cronjob.yaml`, to match the S3 hub-centralization choice above.
- **Dimension shape differs between the two modes** — `_format_metric` in
  `c7n/resources/aws.py` drops the `Scope` dimension in master mode and
  adds `Region`+`Account` instead. A dashboard/alarm built against one
  mode's shape won't match the other mode's data if you ever switch.
- Confirmed available metric names (auto-emitted per policy run, from
  `c7n/policy.py` / `c7n/ctx.py`): `ResourceCount`, `ResourceTime`,
  `ActionTime`, `ApiCalls`, `PolicyException`, `ResourceLimitExceeded`.
  The last one is worth alarming on directly — it fires when the
  `max-resources` circuit breaker trips (see `policies.yml`), which does
  **not** fail the job's exit code, so it's otherwise a silent gap.

## Prometheus

No native support — confirmed via `grep -rli "prometheus\|statsd"` across
the full c7n/c7n_org source tree, zero matches. Two real options if
wanted, neither included here:
1. Run a `cloudwatch_exporter` scraping the `CloudMaid` namespace — zero
   c7n code changes, reuses the CloudWatch centralization above.
2. Register a custom `metrics_outputs` backend (`statsd`/`prometheus`) via
   the same `sitecustomize.py` injection this repo already uses for
   `finops_c7n` — bigger lift, but consistent with the existing extension
   pattern.

## What you still need to fill in

- `<S3_BUCKET_NAME>` / `<S3_PREFIX>` in `deploy/cronjob.yaml` — the bucket
  must already exist in the hub account; this repo doesn't create it.
- The bucket's own region in the `?region=` query params (both `-s` and
  `--metrics-uri` in `deploy/cronjob.yaml`).
