# AWS API limits at scale

Four places this automation generates AWS API load. At hundreds of accounts
none of them should be a problem, but the two hub-side ones are worth
knowing about before a large rollout.

| What | Quota (AWS default) | Scoped to | Concern at ~700 accounts |
|---|---|---|---|
| **EC2 API** (describe + create/delete) | Token bucket per action, e.g. 50 burst / 10 per sec for unfiltered `DescribeInstances`/`DescribeVolumes`; 100 / 5 for `CreateTags`, `CreateSnapshot`, `DeleteVolume` | **each target account, per region** — separate budget per pair | None. Isolated per account, so account count never compounds it. Each run makes only a handful of describes per policy, and `max-resources` caps mutations at 20 per policy per account per region. |
| **S3 writes** (audit output) | ≥3,500 `PUT` per sec **per prefix**, unlimited prefixes per bucket | bucket/prefix — *not* the calling identity | None. Output auto-partitions to `<prefix>/<account>/<region>/<policy>/<date>/`, spreading writes across thousands of prefixes. |
| **CloudWatch** `PutMetricData` | 500 per sec (adjustable) | **hub account**, per region, when using `--metrics-uri aws://master` | Low, but this is a real concentration point — every account's metrics land on one account's quota. Check headroom before enabling at full scale. |
| **STS** `AssumeRole` | 600 per sec — **one bucket shared by every STS action** (`AssumeRole`, `GetCallerIdentity`, …), unlike EC2 above where each action has its own. Adjustable via support. | **hub account**, per region — cross-account assumes only consume the *caller's* quota, never the target's | Low. Exactly **1 `AssumeRole` per (account, region)** — so 700 accounts × 5 regions = 3,500 calls per run, all against the hub's budget. |

## Why it paces itself

`c7n-org` never fires every account at AWS simultaneously. It submits all
(account, region) combinations to a worker pool capped at `C7N_ORG_PARALLEL`
(default: the node's CPU count × 4), so real concurrency stays in the dozens
regardless of account count. More accounts means a longer run, not a
proportionally higher request rate.

## Optional: spread the STS load

The 600/s STS quota is per account **per region**, so each regional endpoint
carries its own separate budget. By default every `AssumeRole` call goes to
the STS endpoint of the region the hub pod runs in, which means all accounts
share that single region's 600/s. Setting:

```yaml
env:
  - name: C7N_USE_STS_REGIONAL
    value: "yes"
```

sends each call to the STS endpoint of the *target* region instead, so the
load spreads across one 600/s budget per region scanned — 5 regions gives
5 × 600/s rather than a single 600/s. Free, no code changes, and AWS
recommends regional STS endpoints generally (lower latency, no dependency on
a single-region global endpoint).

One prerequisite if you enable it: each target account needs STS active in
each region you scan. That is the default everywhere — automatic and
non-disableable for opt-in regions once the region is enabled, and on by
default for standard regions unless someone deliberately turned it off in
IAM → Account settings.

## Memory, not an API limit

`resources.limits.memory: 1Gi` is only enough for a couple of concurrent
(account, region) combinations — beyond that the pod gets `OOMKilled`, since
each combination runs in its own process. Raise it alongside the account
count for any real multi-account rollout.
