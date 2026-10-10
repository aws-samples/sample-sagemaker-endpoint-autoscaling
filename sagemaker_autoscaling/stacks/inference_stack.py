import json
import yaml
from pathlib import Path
from constructs import Construct
from aws_cdk import Stack, aws_iam as iam, aws_sagemaker as sagemaker
from ..constructs.sagemaker_autoscaling_construct import (
    AutoscalingConfig,
    SagemakerAutoscalingConstruct,
    SagemakerAutoscalingProps,
)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class InferenceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)

        ep = config["endpoint"]
        asc = config["autoscaling"]

        for field in ("endpoint_name", "image", "model_data_url", "instance_type", "initial_instance_count"):
            if ep.get(field) is None:
                raise ValueError(f"endpoint.{field} must be set in config/config.yaml")

        for field in ("min_capacity", "max_capacity", "target_value", "scale_in_cooldown", "scale_out_cooldown"):
            if asc.get(field) is None:
                raise ValueError(f"autoscaling.{field} must be set in config/config.yaml")

        # Parse S3 bucket and ECR repo from config for least-privilege scoping
        model_data_url = ep["model_data_url"]
        s3_bucket = model_data_url.split("/")[2]
        s3_key_prefix = "/".join(model_data_url.split("/")[3:])

        image_uri = ep["image"]  # e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/repo:tag
        ecr_account = image_uri.split(".")[0]
        ecr_region = image_uri.split(".")[3]
        ecr_repo = image_uri.split("/")[1].split(":")[0]

        execution_role = iam.Role(
            self, "SagemakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            inline_policies={
                "SagemakerEndpointExecution": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "s3:GetObject",
                            ],
                            resources=[f"arn:{self.partition}:s3:::{s3_bucket}/{s3_key_prefix}"],
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "ecr:GetAuthorizationToken",
                            ],
                            resources=["*"],  # GetAuthorizationToken does not support resource-level permissions
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "ecr:BatchCheckLayerAvailability",
                                "ecr:GetDownloadUrlForLayer",
                                "ecr:BatchGetImage",
                            ],
                            resources=[f"arn:{self.partition}:ecr:{ecr_region}:{ecr_account}:repository/{ecr_repo}"],
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            resources=[f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:/aws/sagemaker/Endpoints/{ep['endpoint_name']}*"],
                        ),
                    ]
                ),
            },
        )

        container_def = {
            "image": ep["image"],
            "modelDataUrl": ep["model_data_url"],
        }
        if ep.get("environment"):
            container_def["environment"] = ep["environment"]

        model = sagemaker.CfnModel(
            self, "Model",
            execution_role_arn=execution_role.role_arn,
            primary_container=container_def,
        )

        endpoint_config = sagemaker.CfnEndpointConfig(
            self, "EndpointConfig",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model.attr_model_name,
                    instance_type=ep["instance_type"],
                    initial_instance_count=ep["initial_instance_count"],
                )
            ],
        )
        endpoint_config.add_dependency(model)

        endpoint = sagemaker.CfnEndpoint(
            self, "Endpoint",
            endpoint_name=ep["endpoint_name"],
            endpoint_config_name=endpoint_config.attr_endpoint_config_name,
        )
        endpoint.add_dependency(endpoint_config)

        # Both endpoint and autoscaling values go into config_hash so that
        # a change to either triggers the full deregister → register chain.
        config_hash = json.dumps({
            "endpoint": {
                "image": ep["image"],
                "modelDataUrl": ep["model_data_url"],
                "instanceType": ep["instance_type"],
                "initialInstanceCount": ep["initial_instance_count"],
                "environment": ep.get("environment"),
            },
            "autoscaling": {
                "minCapacity": asc["min_capacity"],
                "maxCapacity": asc["max_capacity"],
                "targetValue": asc["target_value"],
                "scaleInCooldown": asc["scale_in_cooldown"],
                "scaleOutCooldown": asc["scale_out_cooldown"],
            },
        })

        SagemakerAutoscalingConstruct(
            self, "Autoscaling",
            props=SagemakerAutoscalingProps(
                endpoint=endpoint,
                autoscaling=AutoscalingConfig(
                    min_capacity=asc["min_capacity"],
                    max_capacity=asc["max_capacity"],
                    target_value=asc["target_value"],
                    scale_in_cooldown=asc["scale_in_cooldown"],
                    scale_out_cooldown=asc["scale_out_cooldown"],
                ),
                config_hash=config_hash,
            ),
        )
