# SageMaker Endpoint Autoscaling CDK Pattern

A reusable AWS CDK (Python) construct that orchestrates Application Auto Scaling deregistration and re-registration during SageMaker AI endpoint updates, solving the deployment deadlock where CloudFormation cannot update an endpoint while autoscaling is registered.

## Problem

CloudFormation cannot update a SageMaker AI endpoint while a scalable target is registered. Deregistering it manually either causes CloudFormation to lose track of the autoscaling resources or leaves the stack in `UPDATE_ROLLBACK_FAILED`.

## Solution

Three Lambda-backed custom resources deregister and re-register autoscaling with explicit dependency ordering:

```
Deregister ──▶ Endpoint Update ──▶ Register Scalable Target ──▶ Attach Scaling Policy
```

1. **Deregister** — removes the existing scalable target so CloudFormation can update the endpoint.
2. **Endpoint Update** — CloudFormation updates the endpoint configuration.
3. **Register Scalable Target** — waits for the endpoint to reach InService, then registers the scalable target.
4. **Attach Scaling Policy** — waits for the scalable target to be queryable, then applies the target-tracking scaling policy.

**Register Scalable Target** and **Attach Scaling Policy** use the CDK provider's asynchronous completion pattern. Each has an `on_event` handler that returns immediately and an `is_complete` handler that the provider's waiter re-invokes on an interval (every 30 seconds) until the readiness check passes. The wait is bounded by the provider's `total_timeout` (1 hour for Register Scalable Target, 15 minutes for Attach Scaling Policy), so it is not limited by any single Lambda invocation's timeout. The entire sequence runs inside the CloudFormation deployment.

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
uv run cdk bootstrap   # if not already bootstrapped
uv run cdk deploy InferenceStack
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

## Test an update

Change a value in `config/config.yaml` (for example, bump `max_capacity`) and redeploy:

```bash
uv run cdk deploy InferenceStack
```

The `ConfigHash` property on each custom resource forces re-invocation whenever a configuration value changes, triggering the full Deregister → Endpoint Update → Register Scalable Target → Attach Scaling Policy sequence.

## Clean up

```bash
uv run cdk destroy InferenceStack
```

This tears down the stack including the endpoint and deregisters the scalable target (handled by the **Register Scalable Target** resource's `on_event` Delete branch) and deletes the scaling policy (handled by the **Attach Scaling Policy** resource's `on_event` Delete branch), so no autoscaling resources are left behind.


## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
