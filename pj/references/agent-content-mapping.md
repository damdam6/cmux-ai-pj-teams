# Non-review agent content integrated into pj skills

Source material: `_shared/templates/pj/pj-agents/`. The skills below incorporate the useful
reasoning directly; execution does not require loading those agent files or spawning a new role.

| Source | Destination | Incorporated elements |
|---|---|---|
| `architect.md` | `pj-archi` | Current system analysis, requirements/constraints, module responsibilities, interfaces/data flow, alternatives and trade-offs, relevant security/performance/operational boundaries, architectural red flags |
| `planner.md` | `pj-task-plan` | Requirement coverage, affected modules, verifiable task boundaries, dependencies, incremental delivery, integration order, risks and completion criteria |
| `code-architect.md` | `pj-plan` | Existing pattern/dependency analysis, simplest fitting design, file/symbol responsibilities, interfaces and data flow, concrete implementation blueprint and dependency-aware build sequence |
| `planner.md` | `pj-plan` | Actionable steps, exact locations, edge/error cases, incremental verification, decision rationale and plan completeness checks |
| `code-simplifier.md` | `pj-simplify` | Clarity and repo consistency, behavior preservation, scoped cleanup of nesting/duplication/dead code, simpler names/control flow, before/after verification |

The source examples' framework choices, Stripe walkthrough, scaling ladder, arbitrary line-count
and coverage thresholds, fixed model/tool frontmatter, and generic defense boilerplate are not
copied into the workflow. Project constraints and runtime launchers retain their existing owners.

Board flow: pj-archi → pj-task-plan → pj-task-regi (filing) → pj-task-start.
Task flow: pj-plan (separate code-archi and implementation-plan Markdown documents) → pj-work → optional pj-simplify
→ pj-review. Normal decisions use the shared decision policy with the user; pj-night decisions are autonomous and
recorded. See [planning-decisions.md](planning-decisions.md).
