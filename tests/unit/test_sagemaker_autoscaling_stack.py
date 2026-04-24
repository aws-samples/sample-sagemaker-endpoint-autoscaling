import json
import yaml
from unittest.mock import patch
import aws_cdk as core
import aws_cdk.assertions as assertions
from aws_cdk import aws_sagemaker as sagemaker
from sagemaker_autoscaling.constructs.sagemaker_autoscaling_construct import (
    AutoscalingConfig,
    SagemakerAutoscalingConstruct,
    SagemakerAutoscalingProps,
)
from sagemaker_autoscaling.stacks.inference_stack import InferenceStack

TEST_CONFIG = {
    "endpoint": {
        "endpoint_name": "test-endpoint",
        "image": "123456789.dkr.ecr.us-east-1.amazonaws.com/test:latest",
        "model_data_url": "s3://test-bucket/model.tar.gz",
        "instance_type": "ml.m5.large",
        "initial_instance_count": 1,
    },
    "autoscaling": {
        "min_capacity": 1,
        "max_capacity": 5,
        "target_value": 100,
        "scale_in_cooldown": 300,
        "scale_out_cooldown": 60,
    },
}


def test_construct_creates_three_custom_resources():
    app = core.App()
    stack = core.Stack(app, "TestStack")

    endpoint_config = sagemaker.CfnEndpointConfig(
        stack, "EndpointConfig",
        production_variants=[
            sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                variant_name="AllTraffic",
                model_name="test-model",
                instance_type="ml.m5.large",
                initial_instance_count=1,
            )
        ],
    )

    endpoint = sagemaker.CfnEndpoint(
        stack, "Endpoint",
        endpoint_name="test-endpoint",
        endpoint_config_name=endpoint_config.attr_endpoint_config_name,
    )

    SagemakerAutoscalingConstruct(
        stack, "Autoscaling",
        props=SagemakerAutoscalingProps(
            endpoint=endpoint,
            endpoint_config=endpoint_config,
            autoscaling=AutoscalingConfig(
                min_capacity=1,
                max_capacity=5,
                target_value=100,
                scale_in_cooldown=300,
                scale_out_cooldown=60,
            ),
            config_hash=json.dumps({"test": "hash"}),
        ),
    )

    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::CloudFormation::CustomResource", 3)


def test_stack_creates_endpoint_and_autoscaling():
    with patch("builtins.open", create=True) as mock_open:
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = lambda s, *a: None
        with patch.object(yaml, "safe_load", return_value=TEST_CONFIG):
            app = core.App()
            stack = InferenceStack(app, "TestStack")

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties("AWS::SageMaker::Endpoint", {
        "EndpointName": "test-endpoint",
    })
    template.resource_count_is("AWS::CloudFormation::CustomResource", 3)
