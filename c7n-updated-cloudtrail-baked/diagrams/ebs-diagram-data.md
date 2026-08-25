# EBS Cleanup Pipeline

## 1 · Deletion pass

`policy: ebs-unattached-delete`

*[insert ebs-deletion-pass.png here]*

Five gates, all evaluated live on every scheduled run — two read the tag wiv writes, one is a raw AWS state read, and one (CloudTrail) exists specifically so the tag alone can never be trusted.

**1. Live state**

`State: available` — read directly from `DescribeVolumes` at run time. Catches a volume reattached since wiv's last daily check, even before the tag is removed.

*Source: AWS side*

**2. Tag present**

`tag:FinOpsUnattachedDate` must exist. No tag means wiv has no record of idle time for this volume, so nothing happens.

*Source: wiv tag*

**3. Tag age**

Tag value older than `14d` (non-prod) / `30d` (prod). wiv removes the tag on its next daily run after the volume is reattached — a tag that survived the whole window is wiv's own proof the volume sat idle the entire time.

*Source: wiv tag*

**4. Opt-out**

`tag:DoNotDelete` must be absent. The one manual override — pulls the volume out of every gate below, too.

*Source: human / wiv tag*

**5. CloudTrail detach check**

Custom filter `cloudtrail-detach-age` (`ebs_detach.py`). Looks up `DetachVolume` events for this exact volume in CloudTrail's 90-day Event History; a detach inside the last 14 days blocks the delete even if the tag claims otherwise. The one check a spoofed or stale tag can't get past on its own.

*Source: AWS side*

> ⚡ **Why "verified safe — no data loss":** a zero-delay `CreateSnapshot` → `DeleteVolume` was tested directly against real AWS, not assumed from the docs. The volume just sits in `deleting` limbo until the snapshot finishes, and a byte-for-byte checksum on the restored data came back identical. The single-pass snapshot-then-delete has no known data-loss window.

## 2 · Janitor pass — retention cleanup

`policy: ebs-unattached-snapshot-janitor`

*[insert ebs-janitor-pass.png here]*

A separate policy, same schedule: it only ever touches snapshots this automation tagged as its own, and only once they've aged past retention — 30 days non-prod, 90 days prod.

**1. Ownership**

`tag:FinOpsSource = ebs-unattached-cleanup`.

*Source: automation tag*

**2. Opt-out**

`tag:DoNotDelete` absent, same override as the deletion pass, so a snapshot flagged to keep survives past the retention window too.

*Source: human / wiv tag*

**3. Retention age**

Native `age` filter, reading the snapshot's real `CreationDate`, must be older than `30 days` (non-prod) / `90 days` (prod).

*Source: AWS side*
