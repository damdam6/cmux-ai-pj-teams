---
name: pj-rebase
description: Rebase FE and BE onto one user-supplied branch in a PJ combo workspace and report conflicts. Use for pj-rebase, FE/BE 같은 브랜치로 rebase, or 콤보 워크스페이스 리베이스.
---

# pj-rebase

입력: `/pj-rebase {branch}` 또는 Codex의 `$pj-rebase {branch}`.
브랜치가 없으면 질문한다. 있으면 현재 콤보 워크스페이스에서 바로 실행한다.
`SKILL_DIR`는 이 파일의 실제 부모 디렉터리이며, 입력은 안전하게 인용한 단일 인자로 전달한다.

```bash
python3 "$SKILL_DIR/scripts/rebase.py" -- "$TARGET_BRANCH"
```

스크립트가 FE → BE rebase와 충돌 확인을 수행한다. 실패해도 JSON 결과를 읽고
대상 브랜치, FE/BE 각각의 성공·충돌·미실행 사유와 충돌 파일을 짧게 답한다.
충돌 해결·continue·abort·push는 이 스킬에서 하지 않는다.
