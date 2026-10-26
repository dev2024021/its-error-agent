# its-error-agent

ITS 고객센터 장애(자연어) → 가설 수립 → 제한된 소스 탐색 → 근거 기반 원인 가설·수정 제안을
만드는 콘솔 프로그램. 최종 목표(개발자 각자의 구독형 CLI 중복 실행을 없애고, 이슈 발생 시
무인으로 분석·메일링하는 서비스)로 가는 중간 단계. 회사가 승인한 API는 **Vertex뿐**이라
`--analyze-code-agentic`(Vertex Function Calling 반복 탐색)이 주력 경로다.
`--analyze-code`(고정 1회 검색)는 비교용으로 남아있다. Claude Agent SDK 경로는 구독형
인증으로는 쓸 수 없고 별도 API 키 예산이 필요하다는 게 확인돼 2026-10-26에 코드를 삭제했다
(기록은 `HANDOFF.md` 참고) — 자세한 배경과 진행 상태는 `HANDOFF.md`를 먼저 확인할 것
(코드가 항상 최신 기준).

## 운영 규칙 (모든 작업에 적용)

- 한국어만 사용한다. 일본어 금지.
- 짧고 쉽게 설명하고, 현재 단계·작업 위치·변경 이유·검증 결과를 보고한다.
- 사용자는 Java/Spring 개발 경험이 있고 Python에는 익숙하지 않다 — 설명 시 감안한다.
- **설계를 먼저 설명하고 확인받은 범위만 구현한다. 다음 단계까지 임의로 진행하지 않는다.**
- 표준 라이브러리와 Git 기본 기능을 우선한다. 과도한 클래스 분리, 프레임워크, MCP,
  LangChain, 벡터 DB, 검색 서버는 도입하지 않는다.
- 실제 진입점은 `main.py` 하나로 유지한다. CLI 옵션으로 단계를 구분하며, 서버/상시
  프로세스를 만들지 않는다.
- 인증정보나 프로젝트 ID를 사용자에게 요구하지 않는다. 실제 Vertex API 호출 검증은
  사용자가 자신의 PyCharm/터미널에서 직접 실행한다 — 이 세션 안에서 직접 실행하지 않는다.

## 핵심 보안 불변식 (바꾸려면 먼저 사용자에게 확인)

- `source_tools.py`의 확장자 allowlist, `globals.properties`/`globals2.properties` 차단,
  민감정보 패턴 스캔을 느슨하게 만들지 않는다.
- `repository_sync.py`는 익명 HTTPS만 지원한다. 개인 Git 자격증명이나 전역 설정을 읽어오는
  코드를 추가하지 않는다.
- 원본 저장소(`repositories/`)에 Push, 커밋, 체크아웃 변경을 하지 않는다. 분석은 항상
  `analysis_sources/`의 필터링된 스냅샷에서만 수행한다.
- `code_analysis.py`의 `require_source_upload_approval()` — 소스를 AI로 전송하기 전 승인
  URL 일치 확인 — 을 우회하는 경로를 만들지 않는다.
- 원인을 확정된 사실처럼 표현하지 않는다. 모든 분석 결과는 "미확인 가설"이다
  (`assessment: hypotheses_only | insufficient_evidence`).

## 실행

```powershell
.\.venv\Scripts\python.exe .\main.py --sync-repository
.\.venv\Scripts\python.exe .\main.py --prepare-sources
.\.venv\Scripts\python.exe .\main.py --prepare-sources-from-dir <경로> --commit <SHA>  # .git 없는 로컬 디렉터리 버전
.\.venv\Scripts\python.exe .\main.py --analyze-code             # Vertex, 고정 1회 검색(구)
.\.venv\Scripts\python.exe .\main.py --analyze-code-agentic     # Vertex Function Calling 반복 탐색(주력)
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`--analyze-code-agentic`의 실행 상한은 환경변수로 조정한다: `AGENTIC_MAX_TURNS`(기본 25),
`AGENTIC_MAX_SECONDS`(기본 600), `AGENTIC_MAX_TOKENS`(기본 200,000),
`AGENTIC_MAX_COST_USD`(기본 0.5, 누적 비용 — 반복 탐색 중과 최종 보고서 호출 직전 양쪽에서
확인한다), `AGENTIC_MAX_COST_PER_CALL_USD`(기본 0.1 — 요청 한 건이 평소보다 비정상적으로
비싸면 누적 상한과 무관하게 즉시 중단).

`--analyze-code-agentic`는 `insufficient_evidence`로 끝났는데 모델이 스스로 적어둔
탐색 계획(`source_search_targets`)을 실제로 실행하지 않은 키워드가 있으면, 한 번 더
그 키워드를 확인시키고 재답변을 받는다(`function_calling_analysis._missed_keywords`) —
"계획만 세우고 실행 안 함" 패턴에 대한 구조적 안전망.

`--prepare-sources-from-dir`는 사내 Gitea 계정을 새로 만들지 않고, 과거 장애 발생 시점
커밋으로 직접 clone·checkout한 뒤 `.git`을 지운 로컬 폴더를 분석 영역으로 넣을 때 쓴다.
`prepare_sources()`와 동일한 보안 필터를 거치며, `repositories/`의 git clone 슬롯과는
무관하다(해당 폴더는 git 명령어를 전혀 쓰지 않는다).

PyCharm에서는 `Run → Edit Configurations → main → Script parameters`로 옵션을 교체한다.
환경변수 칸이 아니다.
