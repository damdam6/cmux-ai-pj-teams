# Task code architecture and implementation planning

Adapted from `_shared/templates/pj/pj-agents/code-architect.md` and the actionable-step
guidance in `planner.md`. One task planner owns both responsibilities; their documents are
separate so each decision has one canonical home.

## Code architecture and an executable plan

Read the project's architecture and the task's filed definition, then investigate the code as it
exists in this task's worktree. Earlier task merges may have changed the implementation since the
board planned the task graph. Apply the code-architect's pattern analysis: organization/naming,
existing architectural and testing patterns, ownership boundaries, callers, and the dependency
graph before proposing new abstractions.

Design the simplest implementation that fits those patterns. For every important changed or new
component, identify the repo-relative file/symbol, its responsibility, key interface, dependencies,
and role in the data/control flow. Cover contract adaptation, state ownership, error paths, and
cross-repo integration when relevant. Distinguish facts observed in code from proposed changes;
do not fill unknown locations with invented symbols.

Turn that design into ordered, concrete work steps: what changes where, why it is needed, what
must exist first, and how the step will be checked. Interfaces/types, core logic, integration,
UI, tests, and docs are possible dependencies, not a mandatory layer order for every repo.
Include required edge cases and failure handling with their feature. Prefer extending existing
facilities to speculative rewrites, and make intermediate states verifiable where possible.

Write the code blueprint in a separate `code-archi.md` (or the repo's designated task design
document), following `code-archi-schema.md`. Write the work sequence in the task's existing plan
document, following `plan-schema.md`, with an exact link to that blueprint. Keep project-wide
context as references and explain task-specific design decisions in code-archi. The worker must
receive both a code blueprint and an implementation sequence;
"worker designs the modules later" is an unfinished plan.

Before handoff, check that the actual file locations are known, responsibilities and interfaces
are settled, dependencies and risky transitions have a concrete approach, and acceptance criteria
have a verification method. A small change can satisfy this in a few lines; task size changes the
detail, not ownership of the design.
