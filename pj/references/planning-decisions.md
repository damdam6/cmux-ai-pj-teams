# Design decisions across the pj stages

This policy applies to pj-archi, pj-task-plan, the task's pj-plan, and design questions discovered
during implementation or simplification. A stage's location does not change who decides.

## Normal execution

Read the code and canonical documents for factual answers first. For an unresolved choice that
changes scope, module responsibility, an interface, behavior, or code architecture, use the shared decision policy
with the user. Ask one question at a time and give a recommended answer and its trade-off. Honor
decisions the user already made; do not turn recording or moving between skills into a new
approval gate. Routine implementation details already determined by the plan/conventions need
no new question.

Changing a PROJECT architecture boundary follows this same rule: the task planner discusses it
with the user. There is no escalation or approval request to the project board. The board is the
home of project design and task bookkeeping, not a higher design authority.

In a split task, pj-work/pj-simplify use the existing `decision.request` → planner → `decision.reply`
route. The planner performs the shared decision policy and updates the plan before replying. In solo, the same
session performs the planner's decision work directly. Solo by itself does not waive questions.

## Explicit unattended execution

An invocation or boot instruction explicitly selecting pj-night, 묻지 말고, 쭉 진행, 알아서,
or 확인 없이 authorizes autonomous design choices. Choose a coherent option using the task intent,
available evidence, and repo constraints, record assumptions and consequences, and continue.
This includes changes to previously documented project boundaries; do not wait for the user or
the board to decide them. Do not infer unattended mode from the time of day, a closed pane, or
solo topology alone.

Autonomy cannot supply missing credentials, an unavailable repo, or an inaccessible external
fact. In pj-night, record such a blocker and the current state, then stop the affected task
without asking an unanswered question or claiming completion. Respect the night repair limit.

## Apply the decision where the next stage reads it

Update the affected scope, code architecture, implementation steps, acceptance criteria, and
handoff as appropriate. Add the rationale and whether the choice was user-set or autonomous.
Keep the task's code architecture and implementation plan as separate Markdown documents. For a
deviation from project architecture, cite the baseline path/section and describe the replacement
contract in code-archi, then update the plan's execution/verification and handoff links so an old
reference does not override the new decision.
Keep canonical design documents consistent when they are writable within this work's scope;
otherwise state the remaining documentation update explicitly. Do not edit another worktree or
invent a board round trip as a prerequisite to proceeding with an already settled decision.

The source agent templates' generic instructions and example stacks do not override this policy,
the repo's own rules, or the user's instructions.
