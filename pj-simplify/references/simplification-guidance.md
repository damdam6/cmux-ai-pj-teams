# Code simplification guidance

Adapted from `_shared/templates/pj/pj-agents/code-simplifier.md`. Read after implementation,
within the plan-selected changed-code scope.

## 2. Find changes that actually improve clarity

Read the changed code, its callers, relevant tests, and the repo's own style rules. Consider:

- Replace deep nesting with named steps or early returns where the control flow becomes clearer.
- Make conditionals, names, intermediate values, and error paths easier to follow; avoid nested
  ternaries and clever expressions that hide intent.
- Consolidate duplication introduced by the task when the shared concept is real. Unwind a
  single-use abstraction when it obscures a straightforward operation; keep useful boundaries.
- Remove newly obsolete code, unused imports, commented-out implementation, and temporary debug
  output only after confirming they are not intentional diagnostics or required behavior.
- Simplify asynchronous control flow only when concurrency, ordering, cancellation, and error
  propagation remain equivalent. A callback-to-await rewrite is not automatically equivalent.

Clarity and consistency are the goal, not fewer lines. Large mechanical changes may need no
cleanup. Keep an existing implementation when the proposed rewrite only changes taste.

