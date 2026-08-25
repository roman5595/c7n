"""CloudTrail-verified detach age for Cloud Custodian's EBS resource.

Unlike EC2 (`state-age`, native, reads `StateTransitionReason`), AWS gives no
built-in way to check when an EBS volume actually became unattached -
`DescribeVolumes` carries no state-transition history at all. That means
`ebs-unattached-delete`'s `tag:FinOpsUnattachedDate` age check has no
AWS-ground-truth backstop the way `ec2-stopped-delete-with-ami` has
`state-age` alongside its tag check - a volume that was reattached and
detached again between two of wiv's daily tag-writer runs (or whose tag was
otherwise never refreshed) could pass the tag check without truly having sat
idle for the required period.

This filter closes that gap using CloudTrail's `LookupEvents` API, which
requires no Trail to be configured - every account/region has a free,
always-on 90-day "Event History" of management events. Our thresholds
(14-30 days) fit comfortably inside that window.
"""

from datetime import datetime, timedelta, timezone

from c7n.filters import Filter
from c7n.resources.ebs import EBS
from c7n.utils import local_session, type_schema


FILTER_NAME = "cloudtrail-detach-age"


@EBS.filter_registry.register(FILTER_NAME)
class NoRecentDetach(Filter):
    """Block volumes with a `DetachVolume` CloudTrail event within `days`.

    Semantics are deliberately a negative check, not a positive age lookup:
    if CloudTrail's Event History (90 days, always on, no Trail required)
    shows *no* `DetachVolume` event for this volume within the last `days`
    days, that already proves it wasn't detached more recently than the
    threshold - regardless of whether it was detached earlier than that or
    never attached at all - so the filter passes it through. If such an
    event IS found, the volume fails this filter (blocked from deletion):
    either the `FinOpsUnattachedDate` tag is stale/spoofed, or the volume
    was reattached and detached again more recently than the tag reflects.

    `LookupEvents` only supports one `LookupAttributes` entry at a time (a
    real, documented AWS API limitation - see
    https://docs.aws.amazon.com/awscloudtrail/latest/APIReference/API_LookupEvents.html),
    so this filters by `ResourceName` server-side and by `EventName`
    client-side, one call per candidate volume.
    """

    schema = type_schema(
        FILTER_NAME,
        required=["days"],
        days={"type": "number", "exclusiveMinimum": 0},
    )
    permissions = ("cloudtrail:LookupEvents",)

    def process(self, resources, event=None):
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.data["days"])
        client = local_session(self.manager.session_factory).client("cloudtrail")
        return [
            resource for resource in resources
            if not self._detached_since(client, resource["VolumeId"], cutoff)
        ]

    def _detached_since(self, client, volume_id, cutoff):
        paginator = client.get_paginator("lookup_events")

        def _fetch():
            return list(paginator.paginate(
                LookupAttributes=[
                    {"AttributeKey": "ResourceName", "AttributeValue": volume_id}
                ],
                StartTime=cutoff,
            ))

        for page in self.manager.retry(_fetch):
            for entry in page["Events"]:
                if entry["EventName"] == "DetachVolume":
                    return True
        return False
