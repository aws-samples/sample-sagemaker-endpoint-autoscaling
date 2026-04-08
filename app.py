#!/usr/bin/env python3

import aws_cdk as cdk

from sagemaker_autoscaling.sagemaker_autoscaling_stack import SagemakerAutoscalingStack


app = cdk.App()
SagemakerAutoscalingStack(app, "SagemakerAutoscalingStack")

app.synth()
