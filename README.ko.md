# Madeleine

**Claude Code를 위한 연상 기억.** 나눈 대화를 저장하고, 카드로 정리하고, 서로 이어 그래프로 만든다. 질문을 칠 때마다 훅이 그 질문이 건드리는 과거 대화를 찾아 Claude 앞에 놓는다. 임베딩도, 벡터 DB도, 서버도 없다. 마크다운 파일과 git, 파이썬 표준 라이브러리만 쓴다.

[English](README.md)

이름은 프루스트에서 왔다. 《잃어버린 시간을 찾아서》에서 화자는 홍차에 적신 마들렌을 먹는 순간, 떠올리려 애쓰지 않은 어린 시절을 통째로 떠올린다. 이 시스템에서도 기억을 검색하지 않는다. 지금 나누는 대화가 과거 기억을 불러온다.

## 실제 모습

지어낸 대화 6개로 만든 시연용 vault에서 뽑은 실제 출력이다. 이렇게 묻는다.

> the basil is going black again, should I change how often I water it?

Claude가 답하기 전에 훅이 다음 내용을 문맥에 넣는다.

```
Recalled memories (injected automatically: recent conversations not yet ingested, then past conversations this prompt touches). Each line is a memory that came to mind. Keep them in the background; open the path or [[link]] when you need detail. If you Read a summary, run recall_ppr.py learn:
- 2026-03-20 [[2026-03-20_drip_timer_broke_basil_rot__summary|The drip timer broke and the basil started rotting]] — timer battery · overwatering · root rot · "the soil never dries out"
- 2026-03-02 [[2026-03-02_balcony_vegetable_garden_plan__summary|Planning a vegetable garden on the balcony]] — tomato planters · south-facing balcony · drip irrigation · "too much shade by 3pm" · seed swap
- 2026-06-01 [[2026-06-01_essay_why_i_garden__summary|Drafting an essay on why I garden]] — patience · "nothing happens for weeks" · first essay draft
(3줄 더)
```

세 번째 카드("왜 정원을 가꾸는지 쓴 글")는 질문과 겹치는 단어가 하나도 없다. 베란다 텃밭 카드와 이어진 링크를 타고 올라왔다.

## 다른 기억 시스템과 다른 점

- **검색이 아니라 연상이다.** 대화 카드, 카드에 나온 사람·장소·프로젝트, 카드 사이 링크로 그래프를 만들고 그 위에서 개인화 페이지랭크(PPR, 활성 확산)를 돌린다. 방식은 [HippoRAG](https://arxiv.org/abs/2405.14831)를 따랐다.
- **Claude가 파일을 직접 읽는다.** 아무것도 임베딩하지 않는다. 카드는 YAML 머리말이고, Claude가 열어 읽고 링크를 따라간다. 모든 기억이 직접 읽고 고칠 수 있는 파일이다.
- **실제로 쓰인 기억이 강해진다.** Claude가 떠오른 카드를 실제로 읽으면 그 카드로 이어진 연결이 굵어진다(헵 학습, 반감기 90일). 떠오르기만 하고 읽히지 않은 카드는 아무것도 얻지 않는다.
- **마음층을 따로 둔다.** Claude가 나와 일하는 방식을 정하는 파일은 두 가지다. 하나는 Claude가 반드시 지킬 행동 규칙이고, 다른 하나는 내가 무언가를 결정한 순간을 말 그대로 인용해 모은 `episodes.md`다. 내가 어떻게 생각하는지는 규칙으로 적지 않는다. Claude가 사건을 읽고 그때그때 판단한다.

## 작동 방식

```mermaid
flowchart LR
  S[Claude Code / Codex 세션] -->|세션 훅| Q[00_Inbox/_queue<br/>원문]
  Q -->|ingest라고 말함| C[20_Wiki/summaries<br/>카드 + 링크]
  Q -->|ingest라고 말함| L[20_Wiki/recall.md<br/>원장 줄]
  C --> G((그래프))
  L --> G
  G -->|질문 훅: PPR| S
  C -->|distill이라고 말함| M[99_Config/claude_memory<br/>규칙 + 사건]
  M -->|세션 시작 때 로드| S
```

1. **캡처.** 세션 훅이 모든 세션을 마크다운 원문으로 `00_Inbox/_queue/`에 저장한다. 도구 호출과 시스템 문구는 빼고, 내 말과 Claude의 답만 남긴다.
2. **정리.** `ingest`라고 말하면 Claude가 원문을 읽고 세 가지를 쓴다.
   - 카드: 주장, 정정, 남은 질문, 등장한 개체, 결정적 순간
   - `20_Wiki/recall.md`의 원장 한 줄: 나중에 이 대화를 다시 불러올 단어들
   - 관련된 과거 카드로 가는 링크
3. **연상.** 질문마다 `recall_ppr.py hook`이 원장 단어, 개체 별칭, 모든 원문의 본문에서 출발점을 잡고 PPR을 돌려 상위 원장 줄 10개를 넣는다. 아직 정리하지 않은 원문은 내 발화 몇 줄과 함께 맨 위에 올라온다.
4. **학습.** Claude가 떠오른 카드를 읽고 나면 `recall_ppr.py learn`을 돌린다. 그 카드로 이어진 연결이 굵어진다.
5. **검토.** 가끔 `distill`이라고 말한다. Claude가 최근 카드에서 사건 후보와 행동 규칙 후보를 뽑아 보여 주고, 나는 하나씩 넣거나 버린다. 넣기로 한 것은 `mind-manager` 에이전트가 원문을 인용해 기록한다.

## 실측

만든 사람의 vault로 쟀다. 한 사람이 2025년 1월부터 2026년 10월까지 나눈 대화이고, 장비는 Intel Core i9-10910에 메모리 32GB다.

- 대화 1,191개, 디스크의 원문 26MB
- 그래프: 노드 2,673개(대화 1,191개와 개체 1,482개), 엣지 17,319개
- 질문 1회당 훅 시간: 10번 재서 0.9~1.6초
- 의존성: 파이썬 표준 라이브러리. 임베딩·벡터 DB 비용 0원

## 시작하기

1. GitHub에서 **Use this template**를 눌러 **비공개** 레포를 만든다. 포크하지 않는다. 공개 레포를 포크하면 그 포크도 공개되고, 내 대화가 그 안에 쌓인다.
2. 클론하고 Claude Code로 연다.
   ```bash
   git clone git@github.com:<계정>/<내-vault>.git
   cd <내-vault>
   claude
   ```
   폴더 신뢰 확인 창에서 허용해야 `.claude/settings.json`의 훅이 돈다.
3. 평소처럼 작업한다. 세션이 끝날 때 캡처되고, 놓친 세션은 다음 시작 때 캡처된다.
4. `ingest`라고 말해 쌓인 원문을 카드로 만든다. 카드가 하나라도 생기면 연상이 돌기 시작한다.

필요한 것: Claude Code 또는 Codex CLI, Python 3, git. 선택: 암호화 보관함용 [age](https://github.com/FiloSottile/age), 둘러보기용 [Obsidian](https://obsidian.md).

## 명령

vault 안에서 Claude에게 이렇게 말한다. 각 명령은 `CLAUDE.md`에 정의돼 있다.

| 말 | 일어나는 일 |
| --- | --- |
| `ingest`, `process next` | 가장 오래된 원문을 카드, 원장 줄, 링크로 만든다 |
| `process <파일>` | 원문 하나를 같은 방식으로 처리한다 |
| `distill` | 최근 카드에서 남길 사건과 행동 규칙을 검토한다 |
| `capture <규칙>` | 승인한 행동 규칙을 마음층에 쓴다 |
| `create entity <이름>` | 템플릿으로 개체 페이지를 만든다 |
| `lint the wiki` | `lint.py`의 무결성 검사를 돌린다 |
| `import <export.json>` | claude.ai 데이터 내보내기 파일을 큐로 가져온다 |

## 폴더 구조

```
CLAUDE.md                  Claude가 따르는 운영 지침
00_Inbox/_queue/           정리를 기다리는 원문
00_Inbox/_processed/       정리를 마친 원문 (수정하지 않음)
20_Wiki/summaries/         대화당 카드 하나 (링크 추가 말고는 수정하지 않음)
20_Wiki/recall.md          원장: 카드마다 불러올 단어 한 줄
20_Wiki/recall_cold.md     180일 동안 쓰이지 않은 원장 줄
20_Wiki/entities/          사람, 회사, 프로젝트
20_Wiki/threads/           오래 이어지는 주제의 허브 노트
20_Wiki/log.md             작업마다 한 줄
99_Config/claude_memory/   마음층: MEMORY.md 색인, episodes.md, 규칙 노트
99_Config/prompts/         카드 작성 지침
99_Scripts/                훅과 도구
99_Style/voice.md          내 편집에서 쌓이는 문체 기록
90_Vaulted/                민감한 기억용 암호화 보관함 (선택)
30_Drafts/, 40_Published/  글쓰기
```

## 개인정보

- 모든 내용은 이 폴더 안에 마크다운으로 남는다. 레포를 직접 푸시하지 않는 한 내 컴퓨터 밖으로 나가지 않는다.
- 훅은 커밋도 푸시도 하지 않는다. 여러 기기에서 쓰려면 비공개 원격 저장소에 직접 푸시한다.
- `vault_lock.py`는 민감한 대화를 `age`로 암호화한 보관함에 넣는다. 잠긴 동안에는 연상, grep, 정리가 그 대화를 보지 못한다. `unlock`과 `lock`은 별도 터미널에서 돌리고, 비밀번호는 대화창에 치지 않는다.
- `link_memory.sh`는 `~/.claude/projects/<이 폴더>/memory`에 심볼릭 링크 하나를 만든다. Claude Code가 세션을 시작할 때 이 링크로 `MEMORY.md`를 읽는다.

## Codex CLI

vault에서 `codex`를 실행한다. 설정 파일 세 개가 같은 동작을 연결한다.
- `.codex/config.toml`: Codex가 `CLAUDE.md`를 읽게 한다.
- `.codex/hooks.json`: `MEMORY.md`를 불러오고, 세션을 캡처하고, 연상을 돌린다. 각 훅은 처음 실행할 때 Codex가 신뢰 여부를 묻는다.
- `.codex/agents/mind-manager.toml`: 같은 에이전트를 Codex에서도 쓰게 한다.

## 설계 원칙

- **있는 그대로 기록한다.** 카드에는 누가 무슨 말을 했고 얼마나 확신했는지가 남는다. 내 말은 그대로 두고, AI가 만든 문장을 내 말로 기록하지 않는다.
- **절차는 최소로 둔다.** 태그 분류 체계도, 개념 노트도, 상태 필드도 없다. 깊이는 절차가 아니라 읽기에서 나온다.
- **생각하는 방식은 규칙으로 적지 않는다.** 생각하는 방식에 이름을 붙여 규칙으로 적으면 Claude는 그 규칙을 연기한다. 말 그대로 인용한 사건을 남기면 Claude가 상황에 맞게 읽어 낸다.

## 라이선스

MIT
