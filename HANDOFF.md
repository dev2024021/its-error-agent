# ITS 장애 코드 분석 AI Agent 인수인계

작성일: 2026-10-13. 이 문서는 Claude가 기존 Python 프로젝트의 작업을 이어가기 위한 기록이다. 코드가 최신 기준이며, 먼저 파일을 확인할 것.

## 작업 방식과 사용자 요구

- 한국어만 사용한다. 일본어 금지.
- 짧고 쉽게 설명하고, 현재 단계·작업 위치·변경 이유·검증 결과를 보고한다.
- 사용자는 Java/Spring 개발 경험이 있고 Python에는 익숙하지 않다.
- 설계를 먼저 설명하고 확인받은 범위만 구현한다. 다음 단계까지 임의로 진행하지 않는다.
- 표준 라이브러리와 Git 기본 기능 우선. 과도한 클래스 분리, 프레임워크, MCP, LangChain, 벡터 DB, 검색 서버는 도입하지 않는다.
- 실제 진입점은 main.py 하나. PyCharm 실행 구성의 환경변수를 사용한다.
- 인증정보나 프로젝트 ID를 전달해 달라고 요구하지 않는다. 실제 Vertex 호출은 사용자가 자신의 PyCharm에서 실행할 수 있다.

## 환경과 목표

- 프로젝트: `its-error-agent` (이 레포 루트)
- Python 3.13, PyCharm, `.venv`.
- requirements.txt: google-genai 2.29.0, pydantic 2.13.5.
- 웹서버가 아닌 콘솔 프로그램이다.
- 최종 목표: ITS 고객센터 자연어 장애 → 가설·탐색 후보 → 제한된 소스 탐색 → 근거 기반 원인 가설·수정 제안 → 이후 개발자 메일.
- 현재 입력: `data/its_error1.txt`. 파일명을 불필요하게 바꾸지 않는다.
- 환경변수: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, 선택 VERTEX_MODEL. 현재 코드 기본 모델은 gemini-3.1-flash-lite이며 사용 가능 여부는 실행 환경에서 확인한다.
- PyCharm의 main 실행 구성 환경변수는 다른 실행 구성이나 에이전트 셸에 자동 전달되지 않는다.

## 파일별 책임

| 파일 | 역할 |
|---|---|
| main.py | CLI 진입점, 단계별 실행과 출력·오류 처리 |
| incident_analysis.py | 초기 장애 해석, Pydantic 응답 모델·프롬프트, 고정 샘플 |
| repository_config.json | 서비스·저장소 URL·경로·브랜치·전송 승인 URL |
| repository_sync.py | 개인 인증 설정을 배제한 익명 HTTPS Clone/Fetch |
| source_tools.py | 커밋 소스 검사·분석 영역 추출, 공통 검색·행 읽기·목록 도구 |
| code_analysis.py | 초기 후보로 제한된 코드 수집, 두 번째 AI 분석, 인용 검증 |
| SOURCE_TOOLS.md | 4·5단계 실행과 제한 설명 |
| tests/ | Git 접근, 소스 도구, 코드 분석의 자동 테스트 |

## 진행 상태

### 1·2단계 완료

텍스트 읽기, Vertex 연결, response_schema를 이용한 구조화 출력과 Pydantic 검증을 사용자가 실제 호출로 확인했다. 수동 JSON 파싱으로 대체하지 않는다.

초기 응답에는 hypotheses(id, description, incident_basis, technical_basis, assumptions, source_search_targets)와 additional_information이 있다. 고객센터 입력 인용과 일반 기술 추론·미확인 전제를 분리한다. 검색 후보는 실제 파일이나 클래스의 존재를 뜻하지 않는다.

보안 헤더 이름도 Java/XML 설정 검색에 유효할 수 있으므로 Python에서 일괄 제외하지 않는다. 소스 탐색 목적과 실제 응답 헤더 등 실행 환경 확인 목적을 구분한다.

과거 스키마 변경 후 HTTP 400 `Request contains an invalid argument`가 반복됐다. 생성용 스키마의 제약을 줄이고 로컬 Pydantic 검증은 유지한 뒤 호출이 성공했다. 서버의 구체적 원인은 확정하지 못했으므로 특정 속성이 원인이라고 단정하지 않는다. incident_analysis.py의 Text, Keyword, MODEL_CONFIG, simplify_generation_schema를 참고한다.

### 3단계 완료

- 공개 테스트 저장소: https://github.com/dev2024021/sampleRepo.git
- 로컬 저장소: `repositories/sampleRepo`. Python 프로젝트 자체의 형상관리 저장소와 다르다.
- 개인 Credential Manager·Git 전역/시스템 설정·상속된 Git 인증 환경을 배제하고 익명 HTTPS를 사용한다. URL 내 인증정보도 금지한다.
- 최초 Clone은 --no-checkout이다. 작업 트리에 소스가 보이지 않아도 정상이다. 이후 Fetch하고 Git 객체에서 선택 커밋의 소스를 추출한다.
- Push, PR, 원본 코드 수정 기능은 없다. Fetch가 작업 파일을 갱신하지 않는다.
- 사용자 확인 커밋: `29ac64b074e2c092a1a33265f7da441d5c4d9402`, main.
- 샘플 Java 프로젝트 원본은 이 레포 바로 상위 폴더의 `devcon_sampleRepo/`(별도 경로)에 있다. 다른 세션이 관리했으므로 이 프로젝트 작업 중 임의 변경하지 않는다.

### 4단계 완료

- 선택 커밋의 허용 소스만 검사해 `analysis_sources/sampleRepo/<SHA>-<식별자>/`에 추출한다. current.json과 파일 해시 manifest로 관리한다.
- search_code(keyword, path), read_file(path, start_line, end_line), list_files(path)를 공통 도구로 구현했다.
- 키워드 검색은 대소문자 무시 일반 문자열 검색이다. AI 없이 독립 실행 가능하다.
- 사용자는 목록·검색·행 읽기를 실행해 확인했다. index.html 24행의 iframe을 찾은 이력이 있다.
- 샘플 준비 결과는 허용 파일 9개, 총 7,634바이트였다. 최신 결과는 재실행해 확인한다.

### 5단계 구현 완료, 실제 코드 포함 AI 분석 검증 미완료

`--analyze-code` 흐름:

1. incident_analysis.analyze_incident로 초기 가설·검색 후보 생성(첫 AI 호출).
2. collect_code가 후보로 공통 도구를 실행하고 주변 코드를 수집.
3. 코드가 있으면 analyze_code로 코드 기반 분석(두 번째 AI 호출).
4. Pydantic과 validate_evidence로 전달한 파일·행·원문 인용을 대조.

판정은 hypotheses_only 또는 insufficient_evidence만 허용한다. 인용 일치는 원인 추론의 정확성을 보장하지 않는다. 모든 원인은 미확인 가설이다.

수집 한도: 후보 12개, 후보별 일치 2개, 범위 8개, 주변 ±5행, 문맥 24KiB, 입력 16KiB, 요청 데이터 48KiB. 자동 Fetch는 하지 않는다.

사용자의 최근 실제 실행 결과:

```text
검색어 3개, 코드 범위 0개, 0행 수집
searched_keywords: X-Frame-Options, Content-Security-Policy, frame-ancestors
assessment: insufficient_evidence
findings: []
관련 코드 없음: 코드 기반 AI 호출 생략
```

첫 호출·응답 검증·검색 결과 없음 처리는 동작했다. 소스가 없는 것이 아니라 선택한 후보가 소스와 일치하지 않았다. iframe은 이전 수동 검색에서 발견됐지만 이번 모델 후보에는 없었다. 첫 AI의 가설·근거 전체는 현재 5단계 콘솔에 표시되지 않는다. 따라서 검색어만 보고 해석이 적절했다고 단정하면 안 된다.

고정 sample_analysis를 이용한 로컬 수집에서는 후보 6개로 범위 2개, 22행을 얻었다. 이는 실제 모델의 후보 생성이나 두 번째 실제 API 호출의 성공 증명이 아니다.

## 다음 작업 제안 — 아직 구현 승인/실행하지 않음

사용자에게 다음 방향을 제안한 상태다. 이어서 작업하기 전에 범위를 확인할 것.

1. 5단계에서 첫 AI의 가설·근거·검색 목적도 표시해 후보 선택을 관찰 가능하게 한다.
2. 초기 프롬프트가 원인 설정뿐 아니라 기능 구현 단서도 후보로 제안하도록 보완한다.
3. iframe 등 특정 장애 검색어를 Python에 하드코딩하지 않고 실제 호출을 재검증한다.

검색 실패 후 AI가 재검색하거나 도구를 직접 선택하는 반복 처리는 6단계다. 현재 범위에 몰래 추가하지 않는다. Function Calling, 코드 수정 자동화, ITS 자동 수집·메일 발송도 미구현이다.

## 보안 정책과 한계

- .git·이력·숨김 경로·빌드/의존성 경로·링크/정션·저장소 외부 접근을 막는다. 도구에는 원본 저장소 전체 읽기나 셸 실행 권한을 주지 않는다.
- 허용 확장자: java, jsp, js, jsx, ts, tsx, xml, html, css. properties/yaml 등은 현재 제외한다.
- globals.properties와 globals2.properties는 위치·대소문자에 관계없이 명시적으로 차단한다. 향후 properties 허용 시에도 유지한다.
- 전체 파일의 민감정보 의심 패턴 검사 후 통과 파일만 추출한다. 조회 시 해시·경로·내용을 재검사한다.
- 패턴 검사에는 누락·과잉 차단 가능성이 있다. 애플리케이션 제한은 OS 차원의 격리가 아니다. 실소스 사용 시 승인과 별도 계정·권한 등 추가 검토가 필요하다.
- source_upload_approved_url은 현재 공개 테스트 URL과 일치한다. 사내 URL로 변경 시 자동 승계하지 말고 실제 사내 승인 후 설정한다. 이 값 자체가 공식 승인 시스템은 아니다.
- API 오류·로그에 인증값이나 소스 내용이 노출되지 않도록 한다.

## 실행과 검증

PyCharm: Run → Edit Configurations → main → Script parameters에서 필요한 옵션 하나로 교체하고 실행한다. 환경변수 칸에 넣는 것이 아니다.

| 옵션 | 실행 내용 |
|---|---|
| 없음 | 기존 2단계 초기 AI 분석 |
| --sample | 초기 분석의 고정 샘플, AI 호출 없음 |
| --sync-repository | 익명 Clone/Fetch |
| --prepare-sources | 로컬 커밋 검사·분석 영역 준비 |
| --search-code iframe | 소스 문자열 검색, AI 호출 없음 |
| --list-files src/main/resources/static | 디렉터리 목록 |
| --read-file src/main/resources/static/index.html --start-line 20 --end-line 28 | 행 범위 읽기 |
| --analyze-code | 5단계, AI 최대 두 번 호출 |

원격 소스가 바뀌면 sync → prepare → analyze 순서로 각각 실행한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

2026-10-13 인계 작성 시 재실행 결과: 24개 테스트 통과. 모의 API 응답 테스트이며 실제 Vertex 호출 테스트가 아니다. 사용자의 실제 실행으로 확인된 범위와 자동 테스트 범위를 구분한다.

## ERD 관련 별도 미결 사항

사용자는 ITS ERD를 Claude Artifact에 정리해 두었다:
https://claude.ai/artifact/9qNZkZVMG6qk9i8UENv4tC

사용자 브라우저의 시크릿 모드에서도 열리는 공개 페이지다. Codex 웹 도구에서는 접근 불가만 반환했으며 이유는 확인하지 못했다. 계정이 없어서라고 단정하지 않는다.

스크린샷 일부에서 bug, bug_text, project, custom_field, custom_field_string 등의 테이블을 볼 수 있었지만 전체 정의는 확보하지 못했다. 표시 설명에는 Mantis 1.2 계열 표준 스키마 기반 추론 및 실제 DB 대조 필요 안내가 있었다. 이를 실제 사내 DB의 확정 스키마로 취급하지 않는다.

사용자는 많은 테이블을 캡처하는 방법을 원하지 않는다. 원본 Artifact 소스/테이블 데이터를 재사용하는 방향을 원한다. Claude에서 해당 원본 대화나 파일을 접근할 수 있다면 전체 데이터로 검토하되, 현재 Python 프로젝트에는 ERD 파일이나 DB 연동이 없다. 화면을 보지 않고 특정 코드 보기 버튼이 있다고 단정하지 않는다.

## Claude에게 전달할 시작 요청 예시

> 이 폴더의 HANDOFF.md와 현재 코드를 먼저 확인해 줘. 지금은 5단계 코드 기반 분석을 검증 중이고, 실제 모델이 보안 헤더 검색어만 반환해 코드를 찾지 못한 상태야. 먼저 현재 상태와 보완 방향을 짧게 설명하고 확인받은 범위만 작업해 줘. 6단계 Function Calling이나 DB·ITS·메일 연동은 아직 구현하지 마.

## Claude Agent SDK 경로 통합 (2026-10-22 시작, 설계 승인됨)

별도 PoC(`devcon-agent-its-analysis` 프로젝트, 이 레포 바깥)로 Claude Agent SDK의 `query()`가 `max_turns` 안에서
Read/Glob/Grep을 반복 호출하며 스스로 검색어를 바꿔 탐색하는 걸 검증한 뒤, Vertex 직접 호출의
구조적 한계(1차 AI 키워드가 실제 소스와 안 맞으면 재검색 불가 — 바로 위 5단계 실패 사례가
그 증거)를 풀기 위해 its-error-agent에 Claude Agent SDK 경로를 통합하기로 했다. 전체 설계는
사용자 승인을 받았고 Phase 1~3까지 구현했다.

### 유지 vs 교체
- **유지**: `repository_sync.py`(익명 Clone/Fetch), `source_tools.py`(보안 필터링·커밋 고정
  스냅샷), `require_source_upload_approval()`, `safe_error_message()`, `IncidentAnalysis`/
  `CodeAnalysis`류 Pydantic 모델 철학(가설만, 확정 아님), CLI 단계별 구조.
- **교체**: `incident_analysis.py`의 1차 Gemini 호출 + `code_analysis.py`의 고정 1회
  키워드 검색·2차 Gemini 호출 → `claude_incident_analysis.py`의 Claude Agent SDK 단일
  세션(`--analyze-code-claude`)으로. 기존 `--analyze-code`(Vertex 경로)는 비교 검증을 위해
  그대로 남겨뒀다.

### 구현 완료 (Phase 1~3) — 실제 실행 검증은 아직 전
- `requirements.txt`에 `claude-agent-sdk` 추가(버전 미고정 — 설치 시 최신 버전을 확인하고
  필요하면 직접 고정할 것).
- `claude_incident_analysis.py` 신규: `ClaudeIncidentReport`(hypotheses + assessment +
  findings + additional_information + limitations) 구조화 출력(`output_format`), `cwd`를
  `prepare_sources()`가 만든 필터링된 스냅샷(`tools.root`)으로 연결, `PreToolUse` 훅으로
  `source_tools.py`의 `path_allowed`/`relative_path`를 재적용(이중 방어).
  `allowed_tools=[Read,Glob,Grep]` / `disallowed_tools=[Write,Edit,NotebookEdit,Bash,
  WebFetch,WebSearch,Task]` / `permission_mode=bypassPermissions` / `setting_sources=[]`.
- `main.py`에 `--analyze-code-claude` 옵션 추가(기존 CLI 옵션들과 동일하게 공존).
  `read_incident_text()` 헬퍼로 입력 파일 읽기 중복을 제거했다 — 기존 Vertex 경로의
  동작/출력 문구는 그대로다.
- `.claude/agents/its-error-agent-dev.md`(프로젝트 전용 서브에이전트), 루트 `CLAUDE.md`
  신규 작성.
- 검증: `py_compile`로 문법 오류만 확인했다. **실제 Claude API 호출과 실제 코드 탐색 성공
  여부는 아직 검증 전이다.** 세션 안에서 직접 실행하지 않는 원칙에 따라, 사용자가
  PyCharm/터미널에서 `ANTHROPIC_API_KEY`를 설정한 뒤 `--prepare-sources` → `--analyze-code-claude`
  순서로 직접 실행해 확인해야 한다.

### 아직 안 한 것 (다음 단계, 착수 전 확인 필요)
- Phase 4: 인용 근거 사후검증(`code_analysis.py`의 `validate_evidence()` 이식) — Claude가
  보고한 evidence가 실제로 Read/Grep한 내용과 일치하는지 사후 대조하는 장치가 아직 없다.
- Phase 5: `safe_error_message()` 마스킹을 Claude 경로의 에러 출력/로그에도 적용.
- Phase 6: 신규 경로용 자동 테스트(`tests/`, 모의 응답 기반), Vertex/Claude 비교 실행
  가이드를 `SOURCE_TOOLS.md`에 보완.
- 두 경로 비교 검증 후 Vertex 경로 제거 여부 결정 — 이번 단계에는 포함하지 않는다.

**→ 이 경로는 2026-10-22에 보류됐다.** Claude Agent SDK는 구독형이 아니라 별도 Anthropic
API 키/예산이 필요하다는 게 확인됐고, 회사가 승인한 API는 Vertex뿐이라 아래
"Vertex Function Calling 경로"로 전환했다. `--analyze-code-claude`는 비교용으로 코드는
남겨뒀지만 더 투자하지 않는다.

**→ 2026-10-26, 코드 삭제.** 사용자가 Claude API 키를 발급받지 않기로 확정해 비교용으로
남겨뒀던 `claude_incident_analysis.py`, `main.py`의 `--analyze-code-claude` 옵션,
`requirements.txt`의 `claude-agent-sdk` 의존성을 모두 제거했다. 이 섹션은 의사결정
기록으로 그대로 남겨둔다.

## Vertex(Gemini) Function Calling 반복 탐색 경로 (2026-10-22, 현재 주력)

최종 목표는 바뀌지 않았다 — 개발자가 각자 구독형 CLI(Claude Code 등)를 돌려 중복으로
이슈를 조사하는 대신, 이슈가 발생하면 무인으로 분석(이후 메일링까지)하는 "담당 직원"
같은 서비스. 이건 애초에 개인 구독 로그인으로는 못 만드는 목표라(서버/무인 실행은 항상
API·프로젝트 과금이 필요) Claude API 키 유무와 무관하게 Vertex 쪽으로 왔어야 했다.

Gemini API(Vertex 경유 포함)는 멀티턴 Function Calling을 네이티브로 지원하는데
(`SOURCE_TOOLS.md:75`에는 "아직 구현 안 했다"고만 적혀 있었다), 이번에 처음으로
`source_tools.py`의 `search_code`/`read_file`/`list_files`를 모델이 직접 반복 호출할 수
있는 도구로 등록해 구현했다. 이걸로 `incident_analysis.py`(1차 고정 호출) +
`code_analysis.py`의 `collect_code()`(고정 1회 검색)를 대체한다.

### 구조
- `incident_report.py` (신규): 벤더 중립 보고서 스키마(`IncidentReport`/`IncidentFinding`).
  Claude 경로(`claude_incident_analysis.py`)와 이번 Vertex 경로가 같은 스키마를 공유하도록
  리팩터링했다 — 중복 제거, 결과 비교 용이.
- `function_calling_analysis.py` (신규): `search_code`/`read_file`/`list_files`에 대응하는
  `types.FunctionDeclaration` 3개 선언, 반복 루프(`_run_tool_loop`)가 모델의 함수 호출을
  실행하고 결과를 돌려주며, 도구 실행 실패(`ValueError`/`KeyError`/`TypeError`)는 예외를
  던지지 않고 에러 메시지를 모델에 돌려줘 다른 인자로 재시도할 수 있게 한다. 루프 종료 후
  `response_schema=IncidentReport`로 최종 구조화 호출 1회(**Function Calling 스키마와
  response_schema를 같은 호출에 섞지 않음** — 과거 스키마 관련 HTTP 400 이력 때문,
  `HANDOFF.md:49` 참고).
- `main.py --analyze-code-agentic` (신규 CLI 옵션): 기존 `--analyze-code`/
  `--analyze-code-claude`와 동일한 패턴(`read_incident_text`/
  `require_source_upload_approval`/`load_source_tools`)으로 통합. 새 환경변수는
  `AGENTIC_MAX_TURNS`(기본 25)/`AGENTIC_MAX_SECONDS`(기본 600)뿐 — 인증은 기존
  `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`/`VERTEX_MODEL` 그대로 재사용(새 벤더
  승인 불필요).

### 검증 상태
`py_compile`로 문법만 확인했다. **실제 Vertex 호출, 실제 반복 탐색 성공 여부는 아직
검증 전**이다. 세션 안에서 직접 실행하지 않는 원칙에 따라 사용자가 PyCharm/터미널에서
`--prepare-sources` → `--analyze-code-agentic` 순서로 직접 실행해 확인해야 한다. 특히
`data/its_error1.txt`(iframe 차단 의심 사례)에서 기존 `--analyze-code`가 못 찾았던
`index.html` 근처 코드를 이번엔 찾아내는지가 핵심 확인 포인트다.

### 다음 단계 (착수 전 확인 필요)
- 세 경로(`--analyze-code`/`--analyze-code-claude`/`--analyze-code-agentic`) 실행 결과
  비교.
- `--analyze-code-agentic`이 검증되면 그때 `--analyze-code`(고정 검색)와 옛 호출부
  (`incident_analysis.py`/`code_analysis.py`의 Gemini 직접 호출부)를 정리할지 결정.
- `tests/`에 Function Calling 루프용 모의 테스트 추가.
- 메일 발송 연동은 여전히 범위 밖(최종 목표의 다음 단계, 별도 설계 필요).

## `--analyze-code-agentic` 실행 검증 및 안전장치 보강 (2026-10-22)

사용자가 실제로 `--analyze-code-agentic`을 2회 실행해 확인했다(`data/its_error1.txt`,
iframe 차단 사례). 둘 다 `index.html:24`의 iframe 코드를 찾아내 동일한 결론(iframe 삽입
제한 가능성)에 도달했다 — 기존 `--analyze-code`(고정 검색)가 못 찾던 지점을 두 번 다
찾아냈다. 다만 도구 호출 횟수(6회 vs 15회)와 근거 완성도(2개 vs 1개 인용)는 실행마다
달랐다 — **결론은 안정적이나 탐색 과정·근거 완성도는 변동한다**는 게 현재까지의 관찰이다.

이 결과를 GPT에 공유해 받은 피드백을 반영해 다음을 추가했다:
- `function_calling_analysis.py`: `validate_evidence()`(인용 근거가 실제 `read_file`
  결과와 일치하는지 사후 대조, 불일치 시 `ValueError`로 분석 전체 차단 — fail closed),
  동일 도구 호출 캐시(무한 반복 방지), 토큰 사용량 상한(`AGENTIC_MAX_TOKENS`, 기본
  200,000) 강제 중단, `ExecutionStatus(stop_reason, turns_used)`로 정상종료/한도초과를
  구분된 값으로 출력.
- `tests/test_function_calling_analysis.py` 신규: `validate_evidence()`가 정상 근거는
  통과시키고 조작된 근거(없는 경로·범위밖 줄번호·지어낸 인용·`search_code`로만 찾고
  `read_file`로 미확인한 코드·일부만 조작된 혼합 근거)는 실제로 거부하는지 검증. 처음엔
  "정상 근거를 안 막는다"만 확인하고 "진짜 허위 근거를 잡아내는가"는 테스트하지 않은
  상태로 "검증 완료"라고 보고했었는데, 이 구분을 지적받고 바로 보완했다.
- `source_tools.py`: `prepare_sources_from_directory(source_dir, commit)` 신규 —
  사내 Gitea 계정을 새로 만들지 않고, 사용자가 과거 장애 발생 시점 커밋으로 직접
  clone·checkout한 뒤 `.git`을 지운 로컬 폴더를 `prepare_sources()`와 동일한 보안
  필터(확장자 allowlist·민감정보 스캔·경로 검증)로 검사해 분석 영역에 넣는다. git
  명령어를 전혀 쓰지 않으며 `repositories/`의 git clone 슬롯과는 독립적이다. `commit`은
  git으로 재검증되지 않는 사용자 입력값이다. `main.py --prepare-sources-from-dir <경로>
  --commit <SHA>`로 호출한다. 기존 `prepare_sources()`는 이미 테스트된 함수라 리팩터링
  없이 별도 함수로 두었다(의도적 중복).
- 전체 테스트 34개 통과(`tests/`), `py_compile` 통과. **실제 `--prepare-sources-from-dir`
  실행과, 과거 실제 장애 사례(원인이 알려진 것)에 대한 정확도 검증은 아직 전이다** —
  다음 단계는 사용자가 과거 ITS 이슈 10~20건을 커밋 지정 clone으로 준비해 돌려보는 것.

## ITS 원문에 스크린샷만 있고 텍스트 설명이 부실한 경우 (2026-10-27, 메모만)

과거 이슈를 텍스트로 옮겨 적다가 발견: 실제 ITS 상당수가 글로 설명하지 않고 스크린샷만
붙여넣는 식이다. 지금(PoC) 단계는 사람이 스크린샷을 보고 텍스트로 옮겨 적는 수밖에
없지만(`issues/issue2.txt`도 이렇게 만듦), 나중에 ITS 트리거를 실시간 자동화할 때는 이
이미지를 자동으로 가져와 분석에 포함시킬 방법이 필요하다.

브라우저 DevTools로 실제 ITS 화면(CKEditor 기반)을 까본 결과, 이미지가 **최소 두 가지
경로**로 들어온다:
1. 본문에 직접 붙여넣기 → `<img src="data:image/png;base64,...">`로 HTML에 인라인
   (별도 네트워크 요청 없음, 디코드만 하면 바로 바이트를 얻음)
2. 별도 업로드 → `<img src="http://cstar.wips.co.kr/cstar/image/loadImage.wips?img=<ID>">`
   처럼 외부 이미지 서버 URL 참조 (별도 HTTP 요청으로 받아와야 함)

Gemini는 멀티모달이라 OCR 없이 이미지를 직접 입력으로 넣을 수 있어서, 기술적으로는 둘 다
바이트만 얻으면(①디코드 ②URL 요청) 그대로 분석 요청에 포함시킬 수 있을 것으로 보인다.
다만 이건 지금 코드에 전혀 없는 새 입력 경로라, 착수 전 설계 확인이 필요하다(이미지
크기/형식 제한, 허용 범위, 민감정보 포함 가능성 등 — `source_tools.py`의 텍스트 보안
필터와 동급의 검토가 필요할 것).
