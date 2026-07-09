import json
from dataclasses import dataclass
from constructs import Construct
from aws_cdk import (
    Duration,
    CustomResource,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_sagemaker as sagemaker,
    custom_resources as cr,
)


@dataclass
class AutoscalingConfig:
    min_capacity: int
    max_capacity: int
    target_value: int
    scale_in_cooldown: int
    scale_out_cooldown: int


@dataclass
class SagemakerAutoscalingProps:
    endpoint: sagemaker.CfnEndpoint
    endpoint_config: sagemaker.CfnEndpointConfig
    autoscaling: AutoscalingConfig
    config_hash: str


DEREGISTER_CODE = """
import boto3
import botocore

client = boto3.client("application-autoscaling")

def handler(event, context):
    resource_id = event["ResourceProperties"]["ResourceId"]
    request_type = event["RequestType"]
    print(f"Request type: {request_type}, ResourceId: {resource_id}")

    if request_type in ("Create", "Update"):
        try:
            client.deregister_scalable_target(
                ServiceNamespace="sagemaker",
                ResourceId=resource_id,
                ScalableDimension="sagemaker:variant:DesiredInstanceCount",
            )
            print(f"Deregistered scalable target for {resource_id}")
        except client.exceptions.ObjectNotFoundException:
            print(f"No scalable target found for {resource_id} - skipping")

    return {"PhysicalResourceId": f"deregister-{resource_id}"}
"""

REGISTER_CODE = """
import time
import boto3

as_client = boto3.client("application-autoscaling")
sm_client = boto3.client("sagemaker")

def wait_for_in_service(endpoint_name):
    for _ in range(60):
        resp = sm_client.describe_endpoint(EndpointName=endpoint_name)
        status = resp["EndpointStatus"]
        print(f"Endpoint {endpoint_name} status: {status}")
        if status == "InService":
            return
        if status == "Failed":
            raise Exception("Endpoint in Failed state")
        time.sleep(10)
    raise Exception("Timed out waiting for endpoint InService")

def handler(event, context):
    props = event["ResourceProperties"]
    resource_id = props["ResourceId"]
    endpoint_name = resource_id.split("/")[1]
    request_type = event["RequestType"]
    print(f"Request type: {request_type}, ResourceId: {resource_id}")

    if request_type in ("Create", "Update"):
        wait_for_in_service(endpoint_name)
        as_client.register_scalable_target(
            ServiceNamespace="sagemaker",
            ResourceId=resource_id,
            ScalableDimension="sagemaker:variant:DesiredInstanceCount",
            MinCapacity=int(props["MinCapacity"]),
            MaxCapacity=int(props["MaxCapacity"]),
        )
        print(f"Registered scalable target for {resource_id}")

    if request_type == "Delete":
        try:
            as_client.deregister_scalable_target(
                ServiceNamespace="sagemaker",
                ResourceId=resource_id,
                ScalableDimension="sagemaker:variant:DesiredInstanceCount",
            )
            print(f"Deregistered scalable target for {resource_id}")
        except as_client.exceptions.ObjectNotFoundException:
            print("No scalable target found - skipping")

    return {"PhysicalResourceId": f"register-{resource_id}"}
"""

SCALING_POLICY_CODE = """
import json
import time
import boto3

client = boto3.client("application-autoscaling")

def wait_for_scalable_target(resource_id):
    for i in range(30):
        resp = client.describe_scalable_targets(
            ServiceNamespace="sagemaker",
            ResourceIds=[resource_id],
            ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        )
        if resp.get("ScalableTargets"):
            print(f"Scalable target found for {resource_id}")
            return
        print(f"Waiting for scalable target for {resource_id} - attempt {i + 1}")
        time.sleep(10)
    raise Exception("Timed out waiting for scalable target")

def handler(event, context):
    props = event["ResourceProperties"]
    policy_name = props["PolicyName"]
    resource_id = props["ResourceId"]
    request_type = event["RequestType"]
    print(f"Request type: {request_type}, PolicyName: {policy_name}")

    if request_type in ("Create", "Update"):
        wait_for_scalable_target(resource_id)
        client.put_scaling_policy(
            PolicyName=policy_name,
            ServiceNamespace="sagemaker",
            ResourceId=resource_id,
            ScalableDimension="sagemaker:variant:DesiredInstanceCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration=json.loads(props["ScalingPolicyConfig"]),
        )
        print(f"Put scaling policy for {resource_id}")

    if request_type == "Delete":
        try:
            client.delete_scaling_policy(
                PolicyName=policy_name,
                ServiceNamespace="sagemaker",
                ResourceId=resource_id,
                ScalableDimension="sagemaker:variant:DesiredInstanceCount",
            )
            print(f"Deleted scaling policy for {resource_id}")
        except client.exceptions.ObjectNotFoundException:
            print("No scaling policy found - skipping")

    return {"PhysicalResourceId": f"scaling-policy-{resource_id}"}
"""


class SagemakerAutoscalingConstruct(Construct):

    def __init__(self, scope: Construct, id: str, props: SagemakerAutoscalingProps):
        super().__init__(scope, id)

        endpoint_name = props.endpoint.endpoint_name
        resource_id = f"endpoint/{endpoint_name}/variant/AllTraffic"
        config = props.autoscaling

        stack = Stack.of(self)
        endpoint_arn = f"arn:{stack.partition}:sagemaker:{stack.region}:{stack.account}:endpoint/{endpoint_name}"

        autoscaling_policy = [
            iam.PolicyStatement(
                actions=[
                    "application-autoscaling:RegisterScalableTarget",
                    "application-autoscaling:DeregisterScalableTarget",
                    "application-autoscaling:PutScalingPolicy",
                    "application-autoscaling:DeleteScalingPolicy",
                    "application-autoscaling:DescribeScalableTargets",
                    "application-autoscaling:DescribeScalingPolicies",
                ],
                resources=["*"],  # Application Auto Scaling does not support resource-level permissions
            ),
            iam.PolicyStatement(
                actions=[
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DescribeEndpointConfig",
                    "sagemaker:UpdateEndpointWeightsAndCapacities",
                ],
                resources=[endpoint_arn],
            ),
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                    "cloudwatch:DescribeAlarms",
                ],
                resources=[f"arn:{stack.partition}:cloudwatch:{stack.region}:{stack.account}:alarm:TargetTracking-endpoint/{endpoint_name}/*"],
            ),
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:{stack.partition}:iam::{stack.account}:role/aws-service-role/sagemaker.application-autoscaling.amazonaws.com/*"],
                conditions={
                    "StringEquals": {
                        "iam:AWSServiceName": "sagemaker.application-autoscaling.amazonaws.com"
                    }
                },
            ),
        ]

        # Step 1: Deregister scalable target BEFORE endpoint update
        deregister_fn = _lambda.Function(
            self, "DeregisterFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(5),
            code=_lambda.Code.from_inline(DEREGISTER_CODE),
            initial_policy=autoscaling_policy,
        )

        deregister_provider = cr.Provider(
            self, "DeregisterProvider", on_event_handler=deregister_fn
        )

        deregister = CustomResource(
            self, "Deregister",
            service_token=deregister_provider.service_token,
            properties={"ResourceId": resource_id, "ConfigHash": props.config_hash},
        )

        props.endpoint.node.add_dependency(deregister)

        # Step 2: Re-register scalable target AFTER endpoint update
        register_fn = _lambda.Function(
            self, "RegisterFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(14),
            code=_lambda.Code.from_inline(REGISTER_CODE),
            initial_policy=autoscaling_policy,
        )

        register_provider = cr.Provider(
            self, "RegisterProvider", on_event_handler=register_fn
        )

        register = CustomResource(
            self, "Register",
            service_token=register_provider.service_token,
            properties={
                "ResourceId": resource_id,
                "MinCapacity": config.min_capacity,
                "MaxCapacity": config.max_capacity,
                "ConfigHash": props.config_hash,
            },
        )

        register.node.add_dependency(props.endpoint)

        # Step 3: Put scaling policy AFTER registration
        scaling_policy_config = {
            "TargetValue": config.target_value,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "SageMakerVariantInvocationsPerInstance",
            },
            "ScaleInCooldown": config.scale_in_cooldown,
            "ScaleOutCooldown": config.scale_out_cooldown,
        }

        scaling_policy_fn = _lambda.Function(
            self, "ScalingPolicyFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(14),
            code=_lambda.Code.from_inline(SCALING_POLICY_CODE),
            initial_policy=autoscaling_policy,
        )

        scaling_policy_provider = cr.Provider(
            self, "ScalingPolicyProvider", on_event_handler=scaling_policy_fn
        )

        scaling_policy = CustomResource(
            self, "ScalingPolicy",
            service_token=scaling_policy_provider.service_token,
            properties={
                "PolicyName": f"{endpoint_name}-target-tracking",
                "ResourceId": resource_id,
                "ScalingPolicyConfig": json.dumps(scaling_policy_config),
                "ConfigHash": props.config_hash,
            },
        )

        scaling_policy.node.add_dependency(register)
