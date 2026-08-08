# feat: three converging branches of uneven length

Three branches (`alpha` two tasks, `beta` three tasks, `gamma` one task) all fork
from a shared `root` and converge on a single `leaf`. Plan U4's granularity
regression fixture: at `independent`, `leaf`'s branch stays split from the other
two (every cross-branch pair fails `chain_compatible` — the branches are
genuinely parallel, not totally ordered). At `balanced`, `chain_compatible` is
relaxed and one cross-branch merge is admissible (it does not regress the
simulated makespan), but a second candidate is rejected by the makespan check
alone — this is the shape that isolates `makespan_regression` as a real, live
rejection reason rather than only a synthetic unit-test one. At `monolithic`,
the makespan check is also dropped and everything collapses into one group.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: root
    description: shared root
    files: [app/root.py]
  - task_id: alpha1
    description: alpha branch, step 1
    files: [app/alpha1.py]
    depends_on: [root]
  - task_id: alpha2
    description: alpha branch, step 2
    files: [app/alpha2.py]
    depends_on: [alpha1]
  - task_id: beta1
    description: beta branch, step 1
    files: [app/beta1.py]
    depends_on: [root]
  - task_id: beta2
    description: beta branch, step 2
    files: [app/beta2.py]
    depends_on: [beta1]
  - task_id: beta3
    description: beta branch, step 3
    files: [app/beta3.py]
    depends_on: [beta2]
  - task_id: gamma1
    description: gamma branch, single step
    files: [app/gamma1.py]
    depends_on: [root]
  - task_id: leaf
    description: converges all three branches
    files: [app/leaf.py]
    depends_on: [alpha2, beta3, gamma1]
```
