"""Importing the package registers the custom Cloud Custodian primitives."""

from . import ebs_detach, ec2_ami


__all__ = ("ebs_detach", "ec2_ami")
