# EC2 Cleanup Pipeline

Every gate a stopped EC2 instance clears before Cloud Custodian backs it up as an AMI, terminates it — and, weeks later, before that AMI itself is deregistered by the janitor pass.

Scope: account <ACCOUNT_ID>, region <REGION>

**Legend:** teal dot = tag-based check · amber dot = AWS-side check · thick amber border = can't be spoofed by a tag · solid green = all gates pass · dashed red = any gate fails

## 1 · Backup & terminate

`ec2-stopped-create-ami` → `ec2-stopped-delete-with-ami`

Both policies run in the **same** daily invocation, every time — `ec2-stopped-create-ami` first, `ec2-stopped-delete-with-ami` second (file order, a readability choice). What actually guards against an unready AMI being consumed isn't the order: it's the live checks in gates 6 and 7 below, which read real AWS state at evaluation time and simply keep blocking, run after run, until they're genuinely satisfied.

Two policies, two runs: Run A backs the instance up (5 gates), then — once the AMI has finished baking — Run B re-checks everything plus the backup itself (7 gates) before terminating.

### Backup pass — ec2-stopped-create-ami

*[insert ec2-create-ami-pass.png here]*

**1. Live state**

`State.Name: stopped` — read directly from `DescribeInstances` at run time.

*Source: AWS side*

**2. Tag present**

`tag:FinOpsStoppedDate` must exist. No tag means wiv has no record of idle time, so nothing happens.

*Source: wiv tag*

**3. Tag age**

Tag value older than `14d` (non-prod) / `30d` (prod). wiv removes the tag on its next daily run after the instance is started — a tag that survived the whole window is wiv's own proof the instance stayed stopped.

*Source: wiv tag*

**4. Opt-out**

`tag:DoNotDelete` must be absent. The one manual override — pulls the instance out of every gate below, too.

*Source: human / wiv tag*

**5. No backup yet**

`tag:FinOpsAmiId` must be absent — keeps this policy from creating a second AMI for an instance that already has one.

*Source: automation tag*

### Terminate pass — ec2-stopped-delete-with-ami

*[insert ec2-delete-with-ami-pass.png here]*

**1. Live state**

`State.Name: stopped`, re-checked on this run too — catches an instance restarted since Run A, even before wiv's tag is removed.

*Source: AWS side*

**2. Tag present**

Same `tag:FinOpsStoppedDate` check as Run A, evaluated fresh.

*Source: wiv tag*

**3. Tag age**

Same `14d` / `30d` threshold as Run A.

*Source: wiv tag*

**4. Opt-out**

`tag:DoNotDelete` absent, re-checked in case it was added after Run A created the backup.

*Source: human / wiv tag*

**5. Backup exists**

`tag:FinOpsAmiId` must be present — the hand-off from Run A. No tag, no terminate.

*Source: automation tag*

**6. AMI verified**

Custom filter `verified-ami-backup`. A tag existing isn't proof the backup is trustworthy: this does a live check that the AMI is `available`, was built from this exact instance, and carries the same `FinOpsStoppedDate` as the instance's current tag — proving it backs up *this* stop-cycle, not a stale one. If the AMI is already `available` by the time this gate runs, the instance can be terminated that same run; if not, this gate simply blocks and the instance gets picked up on whichever later run finally sees it `available` — no separate wait step, no timing logic of its own.

*Source: AWS side*

**7. State age**

Native `state-age` filter, reading AWS's own `StateTransitionReason` — independent of wiv's tag entirely. Closes the gap where a stop→start→stop cycle happening entirely between two of wiv's daily checks could leave `FinOpsStoppedDate` unchanged even though the instance briefly ran again.

*Source: AWS side*

> 🔍 **Why file order (create first, delete second) isn't the safety mechanism:** both policies run in the same invocation every time; safety comes from gates 6 and 7 reading real AWS state at evaluation time, not from which policy happens to run first. A bug in either of those live checks would still fire on whichever run first sees a passing result, regardless of file order — order can only ever delay a misfire by one run, for one narrow bug shape, and that's a coincidence of timing, not real protection.

> ⚡ **Why "catches a spoofed tag (tested)":** proven directly in the POC, not just designed-in. A fixture instance had its `FinOpsStoppedDate` backdated 40 days, but had genuinely only been stopped for minutes — `state-age` alone blocked it (`matched:0`), even though every tag-based gate above it agreed to proceed. Terminate was then confirmed to still fire correctly once the instance had really been stopped long enough.

**Timeline:**

- **T+0** — wiv tags instance
- **T+14 / 30** — AMI created (Run A)
- **next run** — instance terminated (Run B)
- **+30d / +90d** — AMI deregistered by janitor

## 2 · Janitor pass — retention cleanup

`policy: ec2-stopped-ami-janitor`

*[insert ec2-ami-janitor-pass.png here]*

A separate policy: it only ever touches AMIs this automation tagged as its own, and only once they've aged past retention — 30 days non-prod, 90 days prod (mirroring the EBS snapshot janitor's retention).

**1. Ownership**

`tag:FinOpsSource = ec2-stopped-cleanup`.

*Source: automation tag*

**2. Opt-out**

`tag:DoNotDelete` absent, same override as the create/terminate pass, so an AMI flagged to keep survives past the retention window too.

*Source: human / wiv tag*

**3. Retention age**

Native `image-age` filter, reading the AMI's real `CreationDate`, must be older than `30 days` (non-prod) / `90 days` (prod).

*Source: AWS side*

## Not in this flow yet — flagged, not built

**Planned — IAM/SCP tag-write lock**

Today only this automation's own execution role is scoped (`workload-policy.json`). Nothing yet stops another IAM principal from writing or back-dating `FinOpsStoppedDate`, or from removing `DoNotDelete`. Per the source RFC, tag-writing should be locked to wiv's role alone. Until then, the tag-based gates above trust the tag's authenticity, not just its presence.

**Planned — Durable audit trail + alerting**

The only record of which gate blocked or passed a given instance is `kubectl logs` — and the completed Job, logs included, auto-deletes after `ttlSecondsAfterFinished: 86400` (1 day). Nothing pages anyone if a scheduled run fails outright. A persistent per-resource decision log and failure alerting are planned, not built.

---

Each pass also caps at `max-resources: 20` matched instances/AMIs per account/region — a circuit breaker that aborts the whole policy for that run if tripped, not a queue that spills into tomorrow.
