#!/usr/bin/env python3
import aws_cdk as cdk
from sagemaker_autoscaling.stacks.inference_stack import InferenceStack

app = cdk.App()

InferenceStack(app, "InferenceStack", env=cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or None,
))

app.synth()
