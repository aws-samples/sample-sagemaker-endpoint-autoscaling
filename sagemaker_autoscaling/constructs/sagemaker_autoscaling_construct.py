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

# on_event starts the lifecycle action and returns immediately; the framework
# then polls is_complete on an interval until it reports IsComplete, bounded by
# the provider's total_timeout. This keeps the whole wait inside the
# CloudFormation deploy without a Lambda-bound polling loop.
REGISTER_ON_EVENT_CODE = """
import boto3

as_client = boto3.client("application-autoscaling")

def handler(event, context):
    props = event["ResourceProperties"]
    resource_id = props["ResourceId"]
    request_type = event["RequestType"]
    print(f"Request type: {request_type}, ResourceId: {resource_id}")

    # Delete is a single synchronous call - no readiness wait needed.
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

    # For Create/Update there is nothing to kick off here: the endpoint update
    # is driven by the CloudFormation Endpoint resource. Registration happens in
    # is_complete once the endpoint reaches InService.
    return {"PhysicalResourceId": f"register-{resource_id}"}
"""

REGISTER_IS_COMPLETE_CODE = """
import boto3

as_client = boto3.client("application-autoscaling")
sm_client = boto3.client("sagemaker")

def handler(event, context):
    props = event["ResourceProperties"]
    resource_id = props["ResourceId"]
    endpoint_name = resource_id.split("/")[1]
    request_type = event["RequestType"]

    # Deregister already ran synchronously in on_event.
    if request_type == "Delete":
        return {"IsComplete": True}

    resp = sm_client.describe_endpoint(EndpointName=endpoint_name)
    status = resp["EndpointStatus"]
    print(f"Endpoint {endpoint_name} status: {status}")

    if status == "Failed":
        raise Exception("Endpoint in Failed state")
    if status != "InService":
        return {"IsComplete": False}

    # register_scalable_target is idempotent, so it is safe for the framework to
    # retry is_complete after a transient failure.
    as_client.register_scalable_target(
        ServiceNamespace="sagemaker",
        ResourceId=resource_id,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        MinCapacity=int(props["MinCapacity"]),
        MaxCapacity=int(props["MaxCapacity"]),
    )
    print(f"Registered scalable target for {resource_id}")
    return {"IsComplete": True}
"""

ATTACH_SCALING_POLICY_ON_EVENT_CODE = """
import boto3

client = boto3.client("application-autoscaling")

def handler(event, context):
    props = event["ResourceProperties"]
    policy_name = props["PolicyName"]
    resource_id = props["ResourceId"]
    request_type = event["RequestType"]
    print(f"Request type: {request_type}, PolicyName: {policy_name}")

    # Delete is a single synchronous call - no readiness wait needed.
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

    # For Create/Update the policy is applied in is_complete once the scalable
    # target is queryable.
    return {"PhysicalResourceId": f"scaling-policy-{resource_id}"}
"""

ATTACH_SCALING_POLICY_IS_COMPLETE_CODE = """
import json
import boto3

client = boto3.client("application-autoscaling")

def handler(event, context):
    props = event["ResourceProperties"]
    policy_name = props["PolicyName"]
    resource_id = props["ResourceId"]
    request_type = event["RequestType"]

    # Delete already ran synchronously in on_event.
    if request_type == "Delete":
        return {"IsComplete": True}

    resp = client.describe_scalable_targets(
        ServiceNamespace="sagemaker",
        ResourceIds=[resource_id],
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
    )
    if not resp.get("ScalableTargets"):
        print(f"Waiting for scalable target for {resource_id}")
        return {"IsComplete": False}

    # put_scaling_policy is idempotent, so it is safe for the framework to retry
    # is_complete after a transient failure.
    client.put_scaling_policy(
        PolicyName=policy_name,
        ServiceNamespace="sagemaker",
        ResourceId=resource_id,
        ScalableDimension="sagemaker:variant:DesiredInstanceCount",
        PolicyType="TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration=json.loads(props["ScalingPolicyConfig"]),
    )
    print(f"Put scaling policy for {resource_id}")
    return {"IsComplete": True}
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
                resources=["*"],  # Scalable target ARN is not known until after registration
            ),
            iam.PolicyStatement(
                actions=[
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DescribeEndpointConfig",
                    "sagemaker:UpdateEndpointWeightsAndCapacities",
                ],
                resources=[
                    endpoint_arn,
                    f"arn:{stack.partition}:sagemaker:{stack.region}:{stack.account}:endpoint-config/*",
                ],
            ),
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                    "cloudwatch:DescribeAlarms",
                ],
                resources=["*"],  # RegisterScalableTarget permission validation requires unscoped CloudWatch access
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

        # Step 1 - Deregister: remove the scalable target BEFORE the Endpoint Update.
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

        # Step 3 - Register Scalable Target: register the scalable target AFTER
        # the Endpoint Update. Uses the async provider pattern (on_event +
        # is_complete): on_event returns immediately, then the provider's waiter
        # re-invokes is_complete on query_interval until the endpoint reaches
        # InService. The wait budget is total_timeout below, so it is not bound
        # by any single Lambda invocation's timeout.
        register_on_event_fn = _lambda.Function(
            self, "RegisterOnEventFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            code=_lambda.Code.from_inline(REGISTER_ON_EVENT_CODE),
            initial_policy=autoscaling_policy,
        )

        register_is_complete_fn = _lambda.Function(
            self, "RegisterIsCompleteFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            code=_lambda.Code.from_inline(REGISTER_IS_COMPLETE_CODE),
            initial_policy=autoscaling_policy,
        )

        register_provider = cr.Provider(
            self, "RegisterProvider",
            on_event_handler=register_on_event_fn,
            is_complete_handler=register_is_complete_fn,
            query_interval=Duration.seconds(30),
            total_timeout=Duration.hours(1),
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

        # Step 4 - Attach Scaling Policy: attach the target-tracking policy AFTER
        # the scalable target is registered. Uses the same async provider pattern:
        # on_event returns immediately, then the provider's waiter re-invokes
        # is_complete on query_interval until the scalable target is queryable.
        attach_scaling_policy_config = {
            "TargetValue": config.target_value,
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "SageMakerVariantInvocationsPerInstance",
            },
            "ScaleInCooldown": config.scale_in_cooldown,
            "ScaleOutCooldown": config.scale_out_cooldown,
        }

        attach_scaling_policy_on_event_fn = _lambda.Function(
            self, "AttachScalingPolicyOnEventFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            code=_lambda.Code.from_inline(ATTACH_SCALING_POLICY_ON_EVENT_CODE),
            initial_policy=autoscaling_policy,
        )

        attach_scaling_policy_is_complete_fn = _lambda.Function(
            self, "AttachScalingPolicyIsCompleteFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.minutes(2),
            code=_lambda.Code.from_inline(ATTACH_SCALING_POLICY_IS_COMPLETE_CODE),
            initial_policy=autoscaling_policy,
        )

        attach_scaling_policy_provider = cr.Provider(
            self, "AttachScalingPolicyProvider",
            on_event_handler=attach_scaling_policy_on_event_fn,
            is_complete_handler=attach_scaling_policy_is_complete_fn,
            query_interval=Duration.seconds(30),
            total_timeout=Duration.minutes(15),
        )

        attach_scaling_policy = CustomResource(
            self, "AttachScalingPolicy",
            service_token=attach_scaling_policy_provider.service_token,
            properties={
                "PolicyName": f"{endpoint_name}-target-tracking",
                "ResourceId": resource_id,
                "ScalingPolicyConfig": json.dumps(attach_scaling_policy_config),
                "ConfigHash": props.config_hash,
            },
        )

        attach_scaling_policy.node.add_dependency(register)
