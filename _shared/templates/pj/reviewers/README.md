# 리뷰어 정의

리뷰어 하나 = 이 아래 폴더 하나. **추가는 폴더를 만드는 것이고, pj-cmux.py 는 열거하지 않고
발견한다** — 종류를 py 상수에 모으면 그 목록이 곧 "리뷰어란 무엇인가"의 두 번째 정의가 되고,
폴더와 상수가 어긋나는 순간 어느 쪽이 진짜인지 알 방법이 없다.

폴더 이름이 곧 **정의 id** 다. 이벤트의 `role` 필드에 그대로 들어가고, 태스크당 그 id 의 세션은
하나다(reuse). id 를 바꾸면 그 id 로 기록된 과거 이벤트와의 연결이 끊어지므로 바꾸지 않는다.

## 리뷰 옵션

리뷰어 ID는 검토 관점이며, 실행 프로필과 별개입니다. 태스크에 기록된 review 프로필의
`reviewOption`(없으면 runtime)이 전체 리뷰어의 옵션입니다. `code`는 기록된 프로필 그대로,
다른 리뷰어는 그 옵션에 대한 기본값을 사용합니다. 옵션 프로필의 `reviewers` 매핑으로
리뷰어별 프로필을 지정할 수 있습니다. 기본 옵션은 `claude`, `codex`, `grok`이며 별도 계정도
설정된 프로필 ID를 옵션으로 사용할 수 있습니다. 모델과 effort는 표준 CLI argv로 설정합니다.
`launcher parse --role review --reviewer <id> --hint <option>`으로 각 리뷰어를 해석합니다.
자세한 계정·프로필 예시는 [portable-setup.md](../../../../pj/references/portable-setup.md)에 있습니다.

## Grok 추가 리뷰

`grok`은 기존 `code`와 함께 실행하는 선택 리뷰어다. 계획 시 `reviewers: ["grok"]`으로
선택하며 solo/night에서도 사용할 수 있다. 기본 런처는 두 runtime 모두 사용자 설정의 `grok` 프로필이며 결과는
`reviewer-grok` 스트림에 기록된다.

모델·effort를 지정하려면 `launchers.local.json`에 해당 CLI의 지원 옵션을 넣은 argv 프로필을
준비하고 그 ID를 최초 리뷰 요청에 전달한다. 예: `grok-cross-check`. 자연어 effort를 개인
모델 별칭으로 자동 변환하지 않는다. 요청한 설정과 일치하는 프로필이 없으면 설정을 먼저
확인하며 기본값으로 조용히 대체하지 않는다. 세션 생성 후 프로필은 고정되며 재리뷰는
그 세션을 재사용한다. 기존 코드 리뷰어의 모델만 바꾸는 요청은 `code` 역할의 프로필 변경이다.

## 파일

- `reviewer.json` — 필수. 아래 스키마.
- `lens.md` — 선택. 이 리뷰어가 무엇을 보는지. `spawn: true` 인 리뷰어의 프롬프트에 끼워진다.

## reviewer.json

| 키 | 뜻 |
|---|---|
| `title` | 한 줄 이름. 사람에게 보이는 문구 |
| `when` | worker 가 이 리뷰어를 켤지 판단하는 기준. `default: true` 면 무시된다 |
| `default` | `true` = 매 라운드 무조건 요청. `false` = worker 가 `when` 을 보고 결정 |
| `launchers` | `{claude, codex, grok, ...}` 옵션별 프로필. 정의에 없는 커스텀 옵션은 해당 옵션 프로필을 사용. `spawn: false`면 `null` |
| `default_launcher` | 선택. 추가 Grok 교차 검증처럼 모든 옵션에서 유지할 기본 프로필. 옵션 프로필의 `reviewers` 명시값이 우선 |
| `spawn` | `true` = 자기 세션을 띄운다. `false` = 이미 있는 세션을 깨운다 |
| `to` | 요청이 배달되는 역할 |
| `stream` | 붙음 기록과 findings 가 쌓이는 스트림 이름 (`exchanges/{slug}/{stream}.jsonl`) |
| `reply_type` | 회신 이벤트 타입. `EVENT_TYPES` 에 있어야 한다 |
| `finding_field` | finding 의 위치 필드 이름 |
| `prompt` | `spawn: true` 일 때 렌더할 셸 템플릿. 없으면 `null` |
| `solo` | solo 태스크에서도 쓰는지. `false` 면 solo 에서 skip 으로 기록된다 |

`stream` 을 정의마다 따로 두는 이유: 리뷰어가 여러 개면 한 파일에 섞였을 때 "이 회신이 누구
것인가"를 payload 를 파싱해야 알 수 있다. 스트림이 곧 답이면 그 질문이 사라진다. `code` 만
`reviewer` 를 쓰는 것은 기존 기록이 거기 있기 때문이다 — 새 정의는 자기 이름을 쓴다.
