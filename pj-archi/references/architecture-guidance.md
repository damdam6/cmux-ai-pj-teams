# Project architecture guidance

Adapted from `_shared/templates/pj/pj-agents/architect.md`. Read during pj-archi investigation
and design; the project repo and the user supply the actual stack and constraints.

## 2. Understand the present system and the required change

- Trace the current responsibilities, dependencies, integration points, and data flow. Cite
  observed modules and symbols as evidence; distinguish existing behavior from a proposal.
- State functional outcomes and the non-functional constraints that affect this project:
  security/trust boundaries, latency, consistency, availability, migration, or operations when
  relevant. Unknown targets are questions, not invented numbers.
- Identify which existing patterns and facilities can be reused, and which current constraint
  actually requires a structural change. Inspect code before asking the user factual questions.

## 3. Design responsibilities, boundaries, and contracts

Describe each module's purpose, owned data/state, inputs and outputs, collaborators, and the
boundary it enforces. Show the main data/control flow and where validation, errors, retries,
authorization, and consistency are handled when those concerns apply.

Prefer cohesive responsibilities, explicit interfaces, limited coupling, testable boundaries,
and the simplest structure that meets the requirements. Follow the repo's established patterns.
Introduce a new abstraction only for a concrete responsibility or dependency problem.

For each consequential choice, compare the plausible alternatives, benefits, costs, and failure
modes, then record the selected option and rationale. Use the repo's ADR convention if it has
one; otherwise keep the decision in this architecture document. Check for oversized modules,
unclear ownership, implicit side effects, unnecessary distribution, and speculative scaling.

Do not transplant the source agent's example stack, database, pattern catalog, user-count scaling
ladder, or numeric performance targets. A pattern is a candidate to evaluate against this repo,
not a requirement. File-by-file changes and build steps are the task planner's later work.

