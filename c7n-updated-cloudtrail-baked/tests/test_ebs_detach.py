import types
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from c7n.resources.ebs import EBS
from jsonschema import Draft7Validator

from finops_c7n.ebs_detach import FILTER_NAME, NoRecentDetach


VOLUME_ID = "vol-0123456789abcdef0"


def volume(**values):
    resource = {"VolumeId": VOLUME_ID, "State": "available"}
    resource.update(values)
    return resource


def event(name, minutes_ago):
    return {
        "EventName": name,
        "EventTime": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    }


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeClient:
    def __init__(self, events=()):
        # One page, matching how a real volume's event history (already
        # narrowed by ResourceName + StartTime) is small enough in practice
        # that pagination is rarely exercised.
        self.paginator = FakePaginator([{"Events": list(events)}])

    def get_paginator(self, name):
        assert name == "lookup_events"
        return self.paginator


class FakeSession:
    def __init__(self, client):
        self.cloudtrail = client

    def client(self, service):
        if service != "cloudtrail":
            raise AssertionError(service)
        return self.cloudtrail


class FakeManager:
    def __init__(self):
        self.session_factory = object()
        self.config = types.SimpleNamespace(
            account_id="111111111111", region="eu-central-1"
        )
        self.retry = lambda func, *args, **kwargs: func(*args, **kwargs)


def worker_registry():
    from c7n.resources.ebs import EBS as WorkerEBS

    return FILTER_NAME in WorkerEBS.filter_registry


class EbsDetachExtensionTest(unittest.TestCase):
    def run_filter(self, client, resources, **overrides):
        data = {"type": FILTER_NAME, "days": 14}
        data.update(overrides)
        detach_filter = NoRecentDetach(data, FakeManager())
        with patch("finops_c7n.ebs_detach.local_session", return_value=FakeSession(client)):
            return detach_filter.process(resources)

    def test_registry_and_schema(self):
        self.assertIs(EBS.filter_registry.get(FILTER_NAME), NoRecentDetach)
        Draft7Validator.check_schema(NoRecentDetach.schema)

    def test_registry_is_inherited_by_c7n_org_worker(self):
        with ProcessPoolExecutor(max_workers=1) as executor:
            self.assertTrue(executor.submit(worker_registry).result())

    def test_no_events_at_all_passes(self):
        resource = volume()
        self.assertEqual(self.run_filter(FakeClient(), [resource]), [resource])

    def test_recent_detach_blocks(self):
        resource = volume()
        client = FakeClient([event("DetachVolume", minutes_ago=60)])
        self.assertEqual(self.run_filter(client, [resource]), [])

    def test_other_event_types_do_not_block(self):
        resource = volume()
        client = FakeClient([
            event("CreateSnapshot", minutes_ago=60),
            event("DeleteVolume", minutes_ago=30),
        ])
        self.assertEqual(self.run_filter(client, [resource]), [resource])

    def test_detach_among_other_events_still_blocks(self):
        resource = volume()
        client = FakeClient([
            event("CreateSnapshot", minutes_ago=90),
            event("DetachVolume", minutes_ago=60),
        ])
        self.assertEqual(self.run_filter(client, [resource]), [])

    def test_each_volume_evaluated_independently(self):
        blocked = volume(VolumeId="vol-blocked")
        clean = volume(VolumeId="vol-clean")

        # A single fake client/paginator can't distinguish per-volume calls,
        # so exercise this with two separate filter runs instead - what
        # matters is that process() looks up each resource's own VolumeId
        # (asserted via the paginate() call args below), not a shared result.
        client = FakeClient([event("DetachVolume", minutes_ago=10)])
        self.run_filter(client, [blocked])
        self.assertEqual(
            client.paginator.calls[0]["LookupAttributes"],
            [{"AttributeKey": "ResourceName", "AttributeValue": "vol-blocked"}],
        )

        client2 = FakeClient()
        self.run_filter(client2, [clean])
        self.assertEqual(
            client2.paginator.calls[0]["LookupAttributes"],
            [{"AttributeKey": "ResourceName", "AttributeValue": "vol-clean"}],
        )

    def test_start_time_reflects_configured_days(self):
        client = FakeClient()
        before = datetime.now(timezone.utc) - timedelta(days=14)
        self.run_filter(client, [volume()], days=14)
        after = datetime.now(timezone.utc) - timedelta(days=14)

        start_time = client.paginator.calls[0]["StartTime"]
        self.assertTrue(before <= start_time <= after)


if __name__ == "__main__":
    unittest.main()
