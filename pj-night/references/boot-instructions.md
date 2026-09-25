# The night instruction block

This is the text pj-night appends to the `/pj-task-start` invocation. pj-task-start forwards
"any reference docs and switches from the request" to pj-open-ws, which drops the user's own
words into the planner prompt verbatim ("Also: …"). Under `한 세션` that prompt boots the one
session that plans, implements, reviews and reports — so this block is the whole night contract,
delivered in one place, to the one session that has to keep it.

Copy it as-is. `{ATTEMPTS}` is the number `night-queue.py plan --attempts` recorded (default 3).
Nothing else in it is a variable.

---

```
한 세션. 밤 자동 실행(pj-night) 중이고, 이 태스크는 아무도 지켜보지 않습니다.

- 묻지 말고 쭉 진행하세요. 계획에서든 구현에서든 모호한 곳은 계획 의도 안에서 스스로
  판단하고, 판단한 것을 보고에 남기세요. 사람에게 묻는 것은 답이 오지 않습니다.
- 프로젝트 archi의 책임·모듈·인터페이스를 바꿔야 해도 스스로 결정하고 근거를 기록하세요.
  사용자나 보드의 답을 기다리지 마세요. 개별 code-archi와 구현 plan은 별도 md로 작성하고,
  바뀐 설계와 그에 따른 작업 단계·검증·인계를 각 문서에 반영하세요.
- pj-plan에서 Simplifier 실행 여부와 범위를 정하세요. 선택했다면 구현 후 pj-simplify를
  실행하고 그 변경까지 포함해 리뷰받으세요. 단순화 자체가 리뷰를 대체하지 않습니다.
- 리뷰에서 blocking finding 이 남으면 스스로 고치고 재리뷰를 요청하세요. 라운드는 최대
  {ATTEMPTS}회입니다.
- 선택된 리뷰어 모두의 실제 회신을 받고 처리해야 리뷰 통과입니다. 실행·전달 실패나
  생략은 통과가 아닙니다. 실패하면 허용된 라운드 안에서 재시도하고, 계속 실패하면
  멈추세요. night에서는 review.complete와 done.report도 이 조건을 검사합니다.
- {ATTEMPTS}회 안에 해결되지 않거나, 스스로 진행할 수 없는 상태가 되면 더 돌리지 말고
  지금 상태 그대로 멈추세요. 무엇이 어디까지 됐고 무엇이 막혔는지 보고만 남기고,
  /pj-done 은 실행하지 마세요.
- 커밋은 저장소에 저장된 규칙을 재사용하세요. 규칙을 보완해도 형식 확인이 필요한
  상태라면 임의로 정하지 말고, 커밋 전에 중단하고 미결정 사항을 보고하세요.
- 리뷰가 끝나고 blocking 이 남지 않았으면 /pj-done 을 직접 실행하세요. 밤 실행에서는 이
  호출이 사용자의 지시입니다 — 사용자는 /pj-night 을 실행하면서 "리뷰까지 통과한 것은
  머지해도 좋다"고 이미 말했습니다. 보드가 이어서 pj-wrap에서 선택한 merge 또는 squash로
  통합합니다. 명시적인 선택이 없으면 저장된 기본값을 쓰며, 미설정 기본값은 merge입니다.
```

---

## Why the /pj-done line has to say that

pj-done's own rule is that nothing invokes it automatically, because a session that just wrote
code is the worst judge of whether the work is done — the human gate is a person reading the
diff. The night run does not delete that gate; it moves it earlier in time. Running `/pj-night`
**is** the person saying "리뷰까지 통과한 것은 머지해라", for a queue they chose, on a branch that
is not `main` and is never pushed. A session reading pj-done's rule while its own boot prompt
carries this instruction needs that stated, or it will correctly refuse.

The night skips the user's live diff review, while the selected AI reviewers still review the
code. The morning report must distinguish those two facts. The user's later PR review remains
outside this local integration workflow.

## Why `한 세션` (solo)

In split topology the worker's boot prompt is a fixed template in `pj-cmux.py`; only the lines
keyed to `SWITCHES` vary, and there is no night switch. The night contract would then have to
survive a hop — planner writes it into 작업 인계, worker reads it back from disk — which is one
more place for it to be dropped at 3am.

Solo puts it in the one session that does everything. The cost is real and named here so it is
not discovered in the morning: **solo skips the plan review.** The code review still runs
(pj-review keeps the separate reviewer session under solo); what the night gives up is the
planner grading the implementation against the plan's intent.
