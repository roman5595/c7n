"""Policy-driven EC2 -> AMI support for Cloud Custodian."""

import re

from c7n.actions import BaseAction
from c7n.exceptions import PolicyExecutionError, PolicyValidationError
from c7n.filters import Filter
from c7n.resources.ec2 import EC2
from c7n.utils import chunks, local_session, type_schema


ACTION_NAME = "create-ami"
FILTER_NAME = "verified-ami-backup"
ANNOTATION = "c7n:AmiFromTag"
AMI_ID_RE = re.compile(r"^ami-[0-9a-f]{8,32}$")


def _tags(resource):
    return {
        tag["Key"]: tag.get("Value", "")
        for tag in resource.get("Tags", ())
        if tag.get("Key")
    }


def _render(template, resource):
    try:
        return template.format(
            InstanceId=resource["InstanceId"], Name=_tags(resource).get("Name", "")
        )
    except (KeyError, ValueError) as error:
        raise PolicyExecutionError("invalid AMI template: %s" % error) from error


@EC2.action_registry.register(ACTION_NAME)
class CreateAmi(BaseAction):
    """Create/recover one AMI and store its ID in a policy-selected EC2 tag."""

    valid_origin_states = ("stopped",)

    schema = type_schema(
        ACTION_NAME,
        required=["instance-tag"],
        **{
            "instance-tag": {"type": "string", "minLength": 1, "maxLength": 127},
            "name-prefix": {
                "type": "string",
                "default": "custodian-final",
                "minLength": 1,
                "maxLength": 100,
                "pattern": r"^[A-Za-z0-9()\[\].\-'@_]+$",
            },
            "description": {
                "type": "string",
                "default": "Cloud Custodian final AMI for {InstanceId}",
                "maxLength": 255,
            },
            "no-reboot": {"type": "boolean", "default": True},
            "copy-tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 127},
                "uniqueItems": True,
            },
            "tags": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        }
    )
    permissions = ("ec2:CreateImage", "ec2:CreateTags", "ec2:DescribeImages")

    def validate(self):
        if self.data["instance-tag"].lower().startswith("aws:"):
            raise PolicyValidationError("instance-tag cannot use the aws: prefix")
        return self

    def process(self, resources):
        resources = [
            resource for resource in resources
            if resource.get("State", {}).get("Name") in self.valid_origin_states
        ]
        client = local_session(self.manager.session_factory).client("ec2")
        errors = []
        for resource in resources:
            try:
                self._process_one(client, resource)
            except Exception as error:
                self.log.error(
                    "create-ami failed for %s: %s", resource.get("InstanceId"), error
                )
                errors.append(error)
        if errors:
            raise PolicyExecutionError(
                "create-ami failed for %d of %d instances"
                % (len(errors), len(resources))
            ) from errors[-1]

    def _process_one(self, client, resource):
        instance_id = resource["InstanceId"]
        marker = self.data["instance-tag"]
        if _tags(resource).get(marker):
            return

        name = "%s-%s" % (self.data.get("name-prefix", "custodian-final"), instance_id)
        # IncludeDisabled: recover an existing AMI even if it was disabled
        # after creation, so a re-run adopts it instead of failing on a
        # duplicate Name.
        existing = self.manager.retry(
            client.describe_images,
            Owners=["self"],
            IncludeDisabled=True,
            Filters=[
                {"Name": "name", "Values": [name]},
                {"Name": "source-instance-id", "Values": [instance_id]},
            ],
        ).get("Images", [])
        if len(existing) > 1:
            raise PolicyExecutionError(
                "%s has multiple AMIs named %s" % (instance_id, name)
            )
        if existing:
            if existing[0].get("State") not in ("pending", "available"):
                raise PolicyExecutionError(
                    "%s has a non-active AMI named %s; remove it before retrying"
                    % (instance_id, name)
                )
            image_id = existing[0]["ImageId"]
        else:
            request = {
                "InstanceId": instance_id,
                "Name": name,
                "Description": _render(
                    self.data.get(
                        "description", "Cloud Custodian final AMI for {InstanceId}"
                    ),
                    resource,
                ),
                "NoReboot": self.data.get("no-reboot", True),
            }
            image_tags = self._image_tags(resource, marker)
            if image_tags:
                request["TagSpecifications"] = [
                    {"ResourceType": kind, "Tags": image_tags}
                    for kind in ("image", "snapshot")
                ]
            # Not retried: CreateImage has no idempotency token, so retrying
            # after an ambiguous network error risks a duplicate AMI. The
            # existing-image lookup above is what makes a later re-run safe.
            image_id = client.create_image(**request)["ImageId"]

        self.manager.retry(
            client.create_tags,
            Resources=[instance_id], Tags=[{"Key": marker, "Value": image_id}]
        )
        self.log.info("create-ami: %s -> %s", instance_id, image_id)

    def _image_tags(self, resource, marker):
        instance_tags = _tags(resource)
        values = {
            key: instance_tags[key]
            for key in self.data.get("copy-tags", ())
            if key in instance_tags and key != marker
        }
        values.update(self.data.get("tags", {}))
        if len(values) > 50:
            raise PolicyExecutionError("create-ami resolved to more than 50 tags")
        return [{"Key": key, "Value": value} for key, value in sorted(values.items())]


@EC2.filter_registry.register(FILTER_NAME)
class AmiFromTag(Filter):
    """Match EC2s whose `instance-tag` AMI is available, self-sourced, and matches `match-tags`.

    `ami-available` and `ami-source-matches-instance` are not options - both
    checks always run regardless. They're required, fixed-`true` schema
    fields (not booleans you can flip) so every check this filter performs
    is visible directly in the policy YAML, not just in this docstring.
    """

    schema = type_schema(
        FILTER_NAME,
        required=["instance-tag", "ami-available", "ami-source-matches-instance"],
        **{
            "instance-tag": {"type": "string", "minLength": 1, "maxLength": 127},
            "ami-available": {
                "type": "boolean",
                "enum": [True],
                "description": "Always true: AMI must be State==available.",
            },
            "ami-source-matches-instance": {
                "type": "boolean",
                "enum": [True],
                "description": "Always true: AMI's SourceInstanceId must equal this InstanceId.",
            },
            "match-tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 127},
                "uniqueItems": True,
            },
        }
    )
    permissions = ("ec2:DescribeImages",)

    def process(self, resources, event=None):
        by_image = {}
        for resource in resources:
            image_id = _tags(resource).get(self.data["instance-tag"], "")
            if AMI_ID_RE.fullmatch(image_id):
                by_image.setdefault(image_id, []).append(resource)
        if not by_image:
            return []

        client = local_session(self.manager.session_factory).client("ec2")
        images = []
        for image_ids in chunks(list(by_image), 100):
            images.extend(
                self.manager.retry(
                    client.describe_images,
                    Owners=["self"],
                    # IncludeDisabled: a disabled AMI still reports
                    # State=="available" but AWS hides it from DescribeImages
                    # by default. Including it here means a disabled backup
                    # still shows up below and (correctly) blocks deletion,
                    # instead of silently vanishing from the results.
                    IncludeDisabled=True,
                    Filters=[{"Name": "image-id", "Values": image_ids}],
                ).get("Images", [])
            )

        matched = []
        for image in images:
            if image.get("State") != "available":
                continue
            for resource in by_image.get(image.get("ImageId"), ()):
                if image.get("SourceInstanceId") != resource.get("InstanceId"):
                    continue
                image_tags, instance_tags = _tags(image), _tags(resource)
                if all(
                    key in instance_tags and image_tags.get(key) == instance_tags[key]
                    for key in self.data.get("match-tags", ())
                ):
                    resource[ANNOTATION] = image
                    matched.append(resource)
        return matched
