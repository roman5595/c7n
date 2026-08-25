import copy
import types
import unittest
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch

from c7n.exceptions import PolicyExecutionError
from c7n.resources.ec2 import EC2
from jsonschema import Draft7Validator

from finops_c7n.ec2_ami import (
    ACTION_NAME,
    ANNOTATION,
    FILTER_NAME,
    AmiFromTag,
    CreateAmi,
)


INSTANCE_ID = "i-0123456789abcdef0"
AMI_ID = "ami-0123456789abcdef0"
MARKER = "MyAmiPointer"


def aws_tags(**values):
    return [{"Key": key, "Value": value} for key, value in values.items()]


def instance(**values):
    resource = {
        "InstanceId": INSTANCE_ID,
        "State": {"Name": "stopped"},
        "Tags": aws_tags(Name="demo", Environment="test"),
    }
    resource.update(values)
    return resource


def image(state="available", source=INSTANCE_ID, image_id=AMI_ID, tags=None):
    return {
        "ImageId": image_id,
        "Name": "custodian-final-%s" % INSTANCE_ID,
        "SourceInstanceId": source,
        "State": state,
        "Tags": aws_tags(**(tags or {})),
    }


class FakeClient:
    def __init__(self, images=None):
        self.images = copy.deepcopy(images or [])
        self.create_image_calls = []
        self.create_tags_calls = []
        self.describe_error = None

    def describe_images(self, **request):
        if self.describe_error:
            raise self.describe_error
        selected = self.images
        for item in request.get("Filters", []):
            name, values = item["Name"], item["Values"]
            field = {
                "image-id": "ImageId",
                "name": "Name",
                "source-instance-id": "SourceInstanceId",
            }[name]
            selected = [value for value in selected if value.get(field) in values]
        return {"Images": copy.deepcopy(selected)}

    def create_image(self, **request):
        self.create_image_calls.append(copy.deepcopy(request))
        self.images.append(
            {
                "ImageId": AMI_ID,
                "Name": request["Name"],
                "SourceInstanceId": request["InstanceId"],
                "State": "pending",
            }
        )
        return {"ImageId": AMI_ID}

    def create_tags(self, **request):
        self.create_tags_calls.append(copy.deepcopy(request))
        return {}


class FakeSession:
    def __init__(self, client):
        self.ec2 = client

    def client(self, service):
        if service != "ec2":
            raise AssertionError(service)
        return self.ec2


class FakeManager:
    def __init__(self):
        self.session_factory = object()
        self.config = types.SimpleNamespace(
            account_id="111111111111", region="eu-central-1"
        )
        # Real c7n resource managers wrap calls with backoff-on-throttling
        # retry logic; the fake just calls straight through since these
        # tests don't exercise transient AWS errors.
        self.retry = lambda func, *args, **kwargs: func(*args, **kwargs)


def worker_registry():
    from c7n.resources.ec2 import EC2 as WorkerEC2

    return (
        ACTION_NAME in WorkerEC2.action_registry,
        FILTER_NAME in WorkerEC2.filter_registry,
    )


class Ec2AmiExtensionTest(unittest.TestCase):
    def run_action(self, client, resource, **overrides):
        data = {
            "type": ACTION_NAME,
            "instance-tag": MARKER,
            "copy-tags": ["Environment"],
            "tags": {"Purpose": "final-backup"},
        }
        data.update(overrides)
        action = CreateAmi(data, FakeManager()).validate()
        with patch("finops_c7n.ec2_ami.local_session", return_value=FakeSession(client)):
            return action.process([resource])

    def run_filter(self, client, resources, **overrides):
        data = {
            "type": FILTER_NAME,
            "instance-tag": MARKER,
            "ami-available": True,
            "ami-source-matches-instance": True,
        }
        data.update(overrides)
        ami_filter = AmiFromTag(data, FakeManager())
        with patch("finops_c7n.ec2_ami.local_session", return_value=FakeSession(client)):
            return ami_filter.process(resources)

    def test_registry_and_schemas(self):
        self.assertIs(EC2.action_registry.get(ACTION_NAME), CreateAmi)
        self.assertIs(EC2.filter_registry.get(FILTER_NAME), AmiFromTag)
        Draft7Validator.check_schema(CreateAmi.schema)
        Draft7Validator.check_schema(AmiFromTag.schema)

    def test_registry_is_inherited_by_c7n_org_worker(self):
        with ProcessPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(worker_registry).result(), (True, True))

    def test_create_ami_and_write_configured_pointer_tag(self):
        resource = instance()
        client = FakeClient()

        self.run_action(client, resource)

        request = client.create_image_calls[0]
        self.assertEqual(request["Name"], "custodian-final-%s" % INSTANCE_ID)
        self.assertTrue(request["NoReboot"])
        self.assertEqual(
            [item["ResourceType"] for item in request["TagSpecifications"]],
            ["image", "snapshot"],
        )
        image_tags = {
            item["Key"]: item["Value"]
            for item in request["TagSpecifications"][0]["Tags"]
        }
        self.assertEqual(image_tags["Environment"], "test")
        self.assertEqual(image_tags["Purpose"], "final-backup")
        self.assertEqual(
            client.create_tags_calls,
            [{"Resources": [INSTANCE_ID], "Tags": [{"Key": MARKER, "Value": AMI_ID}]}],
        )

    def test_existing_marker_is_idempotent_noop(self):
        resource = instance(Tags=aws_tags(**{MARKER: AMI_ID}))
        client = FakeClient()

        self.run_action(client, resource)

        self.assertEqual(client.create_image_calls, [])
        self.assertEqual(client.create_tags_calls, [])

    def test_existing_pending_ami_is_adopted_after_interrupted_run(self):
        resource = instance()
        client = FakeClient([image(state="pending")])

        self.run_action(client, resource)

        self.assertEqual(client.create_image_calls, [])
        self.assertEqual(client.create_tags_calls[0]["Tags"][0]["Value"], AMI_ID)

    def test_non_active_recovery_image_blocks_new_create(self):
        resource = instance()
        client = FakeClient([image(state="failed")])

        with self.assertRaises(PolicyExecutionError):
            self.run_action(client, resource)
        self.assertEqual(client.create_image_calls, [])

    def test_one_failed_instance_does_not_block_later_instances(self):
        bad_id = "i-0badbadbadbadbad0"
        bad = instance(InstanceId=bad_id)
        good = instance()
        client = FakeClient(
            [
                {
                    "ImageId": "ami-0badbadbadbadbad0",
                    "Name": "custodian-final-%s" % bad_id,
                    "SourceInstanceId": bad_id,
                    "State": "failed",
                }
            ]
        )
        action = CreateAmi(
            {"type": ACTION_NAME, "instance-tag": MARKER}, FakeManager()
        ).validate()

        with patch(
            "finops_c7n.ec2_ami.local_session", return_value=FakeSession(client)
        ):
            with self.assertRaises(PolicyExecutionError):
                action.process([bad, good])

        self.assertEqual(len(client.create_image_calls), 1)
        self.assertEqual(client.create_image_calls[0]["InstanceId"], INSTANCE_ID)

    def test_name_prefix_produces_a_per_instance_name(self):
        resource = instance()
        client = FakeClient()

        self.run_action(client, resource, **{"name-prefix": "backup"})

        self.assertEqual(
            client.create_image_calls[0]["Name"], "backup-%s" % INSTANCE_ID
        )

    def test_name_prefix_rejects_template_placeholders(self):
        data = {
            "type": ACTION_NAME,
            "instance-tag": MARKER,
            "name-prefix": "backup-{InstanceId}",
        }
        errors = list(Draft7Validator(CreateAmi.schema).iter_errors(data))
        self.assertTrue(errors)

    def test_filter_schema_rejects_missing_or_disabled_required_checks(self):
        base = {"type": FILTER_NAME, "instance-tag": MARKER}
        # missing entirely
        self.assertTrue(list(Draft7Validator(AmiFromTag.schema).iter_errors(base)))
        # explicitly disabled - schema must reject, not just default to True
        disabled = dict(base, **{"ami-available": False, "ami-source-matches-instance": False})
        self.assertTrue(list(Draft7Validator(AmiFromTag.schema).iter_errors(disabled)))

    def test_filter_matches_only_available_ami_from_same_instance(self):
        resource = instance(Tags=aws_tags(**{MARKER: AMI_ID}))
        client = FakeClient([image()])

        self.assertEqual(self.run_filter(client, [resource]), [resource])
        self.assertEqual(resource[ANNOTATION]["ImageId"], AMI_ID)

    def test_filter_rejects_pending_wrong_source_and_bad_pointer(self):
        cases = (
            (image(state="pending"), AMI_ID),
            (image(source="i-0fedcba9876543210"), AMI_ID),
            (image(), "not-an-ami"),
        )
        for candidate, pointer in cases:
            with self.subTest(candidate=candidate, pointer=pointer):
                resource = instance(Tags=aws_tags(**{MARKER: pointer}))
                self.assertEqual(self.run_filter(FakeClient([candidate]), [resource]), [])

    def test_filter_can_bind_ami_to_user_selected_stop_cycle_tag(self):
        resource = instance(
            Tags=aws_tags(**{MARKER: AMI_ID, "FinOpsStoppedDate": "2026-08-01"})
        )
        matching = image(tags={"FinOpsStoppedDate": "2026-08-01"})
        stale = image(tags={"FinOpsStoppedDate": "2026-07-01"})

        self.assertEqual(
            self.run_filter(
                FakeClient([matching]),
                [resource],
                **{"match-tags": ["FinOpsStoppedDate"]},
            ),
            [resource],
        )
        self.assertEqual(
            self.run_filter(
                FakeClient([stale]),
                [resource],
                **{"match-tags": ["FinOpsStoppedDate"]},
            ),
            [],
        )

    def test_describe_error_fails_closed(self):
        resource = instance(Tags=aws_tags(**{MARKER: AMI_ID}))
        client = FakeClient()
        client.describe_error = RuntimeError("AccessDenied")

        with self.assertRaisesRegex(RuntimeError, "AccessDenied"):
            self.run_filter(client, [resource])


if __name__ == "__main__":
    unittest.main()
