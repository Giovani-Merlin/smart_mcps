# feat: reporting slice

A single vertical slice whose two tasks' combined files alone exceed the
budget cap (asserted with a tightened token budget in the test — the
default budget is too generous for a fixture this small to pressure). Two
unrelated single-file tasks sit alongside it so slice members don't misfire
the hub-role threshold on such a small node count.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: scaffold
    description: create the app skeleton
    files: [app/main.py]
  - task_id: docs
    description: usage docs
    files: [docs/usage.md]
    depends_on: [scaffold]
  - task_id: config
    description: deployment config
    files: [deploy/config.yaml]
    depends_on: [scaffold]
  - task_id: reports-api
    description: reporting API routes
    slice: reports
    files: [app/reports.py]
    depends_on: [scaffold]
    implements: ["/api/reports"]
  - task_id: reports-ui
    description: reporting admin page
    slice: reports
    files: [web/reports.tsx]
    depends_on: [scaffold]
    consumes: ["/api/reports"]
```
