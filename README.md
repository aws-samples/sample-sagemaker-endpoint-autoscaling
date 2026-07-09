# SageMaker Endpoint Autoscaling CDK Pattern

A reusable AWS CDK (Python) construct that orchestrates Application Auto Scaling deregistration and re-registration during SageMaker AI endpoint updates, solving the deployment deadlock where CloudFormation cannot update an endpoint while autoscaling is registered.

## Problem

CloudFormation cannot update a SageMaker AI endpoint while a scalable target is registered. Deregistering it manually either causes CloudFormation to lose track of the autoscaling resources or leaves the stack in `UPDATE_ROLLBACK_FAILED`.

## Solution

Three Lambda-backed custom resources deregister and re-register autoscaling with explicit dependency ordering:

```
Deregister ──▶ Endpoint Update ──▶ Register ──▶ Scaling Policy
```

A `ConfigHash` property on each custom resource forces re-invocation whenever a configuration value changes.

## Project Structure

```
sagemaker_autoscaling/
├── constructs/
│   └── sagemaker_autoscaling_construct.py   # Core construct
└── stacks/
    └── inference_stack.py                   # Example stack
config/
└── config.yaml                              # Endpoint and autoscaling settings
```

## Setup

```bash
uv sync
```

## Configure

Edit `config/config.yaml` with your endpoint and autoscaling values.

## Deploy

```bash
cdk bootstrap   # if not already bootstrapped
cdk deploy InferenceStack
```

## Verify

```bash
aws application-autoscaling describe-scalable-targets \
  --service-namespace sagemaker \
  --resource-ids endpoint/<your-endpoint-name>/variant/AllTraffic

aws application-autoscaling describe-scaling-policies \
  --service-namespace sagemaker \
  --resource-id endpoint/<your-endpoint-name>/variant/AllTraffic
```

## Clean up

```bash
cdk destroy InferenceStack
```


## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
