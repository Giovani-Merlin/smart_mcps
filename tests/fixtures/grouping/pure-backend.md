# feat: backend-only billing and shipping slices

Control case (docs/orchestrators_improvements.md): a same-stack plan where
affinity is real (both tasks of each slice touch a shared backend file) and
no cross-stack route tags are involved. Shows how slices behave when the
stack-boundary problem that afflicts cross-stack slices is absent.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: scaffold
    description: shared backend scaffold
    files: [app/main.py]
  - task_id: billing-api
    description: billing API routes
    slice: billing
    files: [app/billing.py, app/billing_models.py]
    depends_on: [scaffold]
  - task_id: billing-worker
    description: billing background worker
    slice: billing
    files: [app/billing_worker.py, app/billing_models.py]
    depends_on: [scaffold]
  - task_id: shipping-api
    description: shipping API routes
    slice: shipping
    files: [app/shipping.py, app/shipping_models.py]
    depends_on: [scaffold]
  - task_id: shipping-worker
    description: shipping background worker
    slice: shipping
    files: [app/shipping_worker.py, app/shipping_models.py]
    depends_on: [scaffold]
```
