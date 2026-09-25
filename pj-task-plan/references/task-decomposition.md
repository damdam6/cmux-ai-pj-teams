# Task decomposition guidance

Adapted from `_shared/templates/pj/pj-agents/planner.md`. Read when turning the project
architecture into task boundaries and dependency order.

## 2. Decompose into independently reviewable outcomes

Start from requirements and contracts, then group the changes needed to deliver one verifiable
outcome. A task should be reviewable and mergeable once its declared prerequisites have landed.
Consider the happy path, invalid/empty inputs, failure handling, migration compatibility, and
integration proof as the scope warrants.

- Prefer meaningful feature or contract slices. A slice may span both frontend and backend;
  layer or repo names alone are not a reason to split it.
- Record real prerequisites: a contract, schema, or capability another task consumes. Separate
  these from shared-file overlap, which is a scheduling/conflict concern, not automatically a
  `deps` edge. Call out overlap when suggesting parallel or night execution.
- Order foundations and consumers by dependency, with incremental verification. Avoid a batch
  whose pieces cannot work until every task lands. Required error handling and security belong
  with the feature that needs them, not a speculative later polish phase.
- Make each task's repo subset explicit. Use the project's actual repo identifiers; explain a
  cross-repo interface dependency. Do not assume every task targets all repos.
- Identify concrete risks and mitigations, integration checkpoints, and project completion
  criteria. Estimate relative complexity only when it helps choose a boundary or sequence.

For every proposed task, capture:

| Field | Content |
|---|---|
| Title and outcome | What becomes true when this task is done |
| Architecture reference | Canonical path and the responsibility/contract section it implements |
| Scope and exclusions | Included behavior/modules and boundaries |
| Repos | Exact target repo set |
| Acceptance and verification | Observable completion criteria and relevant checks |
| Dependencies | Prerequisite tasks and the concrete artifact/capability needed |
| Integration and risk | Consumer connection, compatibility, overlap, and material uncertainty |

Module/file pointers help ground the split when known. Do not invent exact symbols before
investigation, prescribe an entire task's code design here, or mark it implementation-ready.
Every started task still goes through pj-plan for code architecture AND implementation planning.

