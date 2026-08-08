# feat: cross-stack service on existing code

The existing-code twin of greenfield-cross-stack.md: identical task-map shape,
backed by real files on disk, so greenfield vs. brownfield estimation
(source-bytes-driven vs. flat per-file allowance, docs/orchestrators_improvements.md
D4) can be compared on the same topology.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: scaffold
    description: create the app skeleton
    files: [app/main.py]
  - task_id: auth-api
    description: auth API routes
    slice: auth
    files: [app/auth.py]
    depends_on: [scaffold]
    implements: ["/api/auth"]
  - task_id: auth-ui
    description: auth admin page
    slice: auth
    files: [web/auth.tsx]
    depends_on: [scaffold]
    consumes: ["/api/auth"]
  - task_id: items-api
    description: items API routes
    slice: items
    files: [app/items.py]
    depends_on: [scaffold]
    implements: ["/api/items"]
  - task_id: items-ui
    description: items admin page
    slice: items
    files: [web/items.tsx]
    depends_on: [scaffold]
    consumes: ["/api/items"]
  - task_id: profile-api
    description: profile API routes
    slice: profile
    files: [app/profile.py]
    depends_on: [scaffold]
    implements: ["/api/profile"]
  - task_id: profile-ui
    description: profile admin page
    slice: profile
    files: [web/profile.tsx]
    depends_on: [scaffold]
    consumes: ["/api/profile"]
  - task_id: docs
    description: usage docs
    files: [docs/usage.md]
    depends_on: [scaffold]
  - task_id: verify
    description: end-to-end verification pass
    files: [tests/e2e.py]
    depends_on: [auth-api, auth-ui, items-api, items-ui, profile-api, profile-ui]
```
