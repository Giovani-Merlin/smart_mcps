# feat: gateway-fronted feature set

Hub B (gateway) depends on hub A (platform); four feature units depend on
gateway; an integration task converges on all four features. Minimized
reproduction of one contributing factor to the historical "Round A" group-DAG
cycle (docs/orchestrators_improvements.md, D3): a source and a downstream
convergence point sandwich a middle hub.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: platform
    description: shared platform primitives
    files: [app/platform.py]
  - task_id: gateway
    description: request gateway routing to every feature
    files: [app/gateway.py]
    depends_on: [platform]
  - task_id: billing
    description: billing feature
    files: [app/billing.py]
    depends_on: [gateway]
  - task_id: shipping
    description: shipping feature
    files: [app/shipping.py]
    depends_on: [gateway]
  - task_id: search
    description: search feature
    files: [app/search.py]
    depends_on: [gateway]
  - task_id: notifications
    description: notifications feature
    files: [app/notifications.py]
    depends_on: [gateway]
  - task_id: integration
    description: cross-feature integration test suite
    files: [tests/integration.py]
    depends_on: [billing, shipping, search, notifications]
```
