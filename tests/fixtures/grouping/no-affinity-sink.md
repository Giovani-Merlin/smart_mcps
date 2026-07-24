# feat: cross-cutting audit pass

A task (audit) depending on every feature but sharing files with none of
them, sandwiched between a shared scaffold every feature depends on.
Minimized reproduction of the other contributing factor to the historical
"Round A" group-DAG cycle (docs/orchestrators_improvements.md, D3):
`depends_on` carries no affinity, so a sink with no shared-file gravity gets
no vote in where it lands until it is forced to merge with whichever branch
happened to cluster first.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: scaffold
    description: shared scaffold
    files: [app/main.py]
  - task_id: billing-api
    description: billing API routes
    files: [app/billing.py, app/billing_models.py]
    depends_on: [scaffold]
  - task_id: billing-worker
    description: billing background worker
    files: [app/billing_worker.py, app/billing_models.py]
    depends_on: [scaffold]
  - task_id: shipping-api
    description: shipping API routes
    files: [app/shipping.py, app/shipping_models.py]
    depends_on: [scaffold]
  - task_id: shipping-worker
    description: shipping background worker
    files: [app/shipping_worker.py, app/shipping_models.py]
    depends_on: [scaffold]
  - task_id: audit
    description: cross-cutting audit of every feature, touching no shared files
    files: [app/audit.py]
    depends_on: [billing-api, billing-worker, shipping-api, shipping-worker]
```
