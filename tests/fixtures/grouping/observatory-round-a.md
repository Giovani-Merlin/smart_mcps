# feat: observatory round A shape

Minimized reproduction of the real Observatory plan's group-DAG cycle
(docs/orchestrators_improvements.md, "Round A"): an SPA hub depending on a
backend hub, three two-task cross-stack vertical slices split across the two
hubs, and a verification task converging on all three slices. Unlike
greenfield-cross-stack.md (one hub, U4's merge guard alone resolves it),
here the cycle can originate earlier — at Louvain/lift/split, before merge
ever runs — so plan U5's SCC repair is what this fixture exercises.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: backend-hub
    description: shared backend platform
    files: [app/platform.py]
  - task_id: spa-hub
    description: shared SPA shell
    files: [web/shell.tsx]
    depends_on: [backend-hub]
  - task_id: auth-api
    description: auth API routes
    slice: auth
    files: [app/auth.py]
    depends_on: [backend-hub]
    implements: ["/api/auth"]
  - task_id: auth-ui
    description: auth admin page
    slice: auth
    files: [web/auth.tsx]
    depends_on: [spa-hub]
    consumes: ["/api/auth"]
  - task_id: items-api
    description: items API routes
    slice: items
    files: [app/items.py]
    depends_on: [backend-hub]
    implements: ["/api/items"]
  - task_id: items-ui
    description: items admin page
    slice: items
    files: [web/items.tsx]
    depends_on: [spa-hub]
    consumes: ["/api/items"]
  - task_id: profile-api
    description: profile API routes
    slice: profile
    files: [app/profile.py]
    depends_on: [backend-hub]
    implements: ["/api/profile"]
  - task_id: profile-ui
    description: profile admin page
    slice: profile
    files: [web/profile.tsx]
    depends_on: [spa-hub]
    consumes: ["/api/profile"]
  - task_id: verify
    description: end-to-end verification pass
    files: [tests/e2e.py]
    depends_on: [auth-api, auth-ui, items-api, items-ui, profile-api, profile-ui]
```
