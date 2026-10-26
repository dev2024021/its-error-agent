# its-error-agent

ITS(장애 접수) 자연어 설명을 받아, 해당 시점의 소스코드를 AI가 직접 탐색해서 원인
가설과 근거를 찾아주는 장애 분석 도구. 처음 합류하신 분은 이 문서만 읽으면 전체 그림이
잡히도록 구성했고, 더 자세한 내용은 각 섹션 끝의 링크를 따라가면 됩니다.

## 1. 개요

**최종 목표**: 개발자 각자가 구독형 CLI(Claude Code 등)를 돌려가며 중복으로 이슈를
조사하는 대신, ITS에 장애가 접수되면 **무인으로 자동 분석하고 메일까지 보내는 서비스**.

**지금 단계(이 레포의 범위)**: 그 중 핵심 난이도("AI가 실제로 코드를 뒤져서 쓸만한
원인을 찾아내는가")만 검증하는 로컬 PoC. ITS 연동, 메일 발송, DB, 웹 화면, 다중 사용자
처리는 **의도적으로 범위 밖**입니다 — AI 분석 엔진 자체가 믿을 만한지부터 확인하는 게
우선이라서요.

## 2. 전체 흐름

```
장애 설명(텍스트 파일)
        │
        ▼
실제 소스 가져오기 ── repository_sync.py (익명 Git Clone)
        │              또는 prepare_sources_from_directory() (.git 없는 로컬 폴더)
        ▼
보안 필터링 ── source_tools.py
        │        (허용 확장자만 / 민감정보 패턴 있으면 제외 / 경로 검증)
        ▼
필터링된 소스 스냅샷 (analysis_sources/)
        │
        ▼
AI가 반복 탐색 ── function_calling_analysis.py
        │           (search_code / read_file / list_files 도구를 모델이 스스로 반복 호출)
        ▼
구조화된 분석 보고서 (가설 + 근거 + 수정 제안, "확정 아님")
```

**지금 AI 경로는 3개가 공존합니다** (`main.py`의 CLI 옵션으로 구분):

| 옵션 | 상태 | 비고 |
|---|---|---|
| `--analyze-code` | 구버전, 비교용 유지 | 1차 AI가 검색어 생성 → Python이 고정 1회 검색. 검색어가 틀리면 재시도 불가 — 이게 애초에 바꾸려던 구조 |
| **`--analyze-code-agentic`** | **주력** | Vertex(Gemini) Function Calling. 모델이 직접 반복 탐색 — 지금 메인으로 검증 중인 경로 |

> Claude Agent SDK 경로는 구독형 인증으로 쓸 수 없고 별도 API 키 예산이 필요하다는 게
> 확인돼 2026-10-26에 코드를 삭제했다. 기록은 `HANDOFF.md` 참고.

## 3. 빠른 시작

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:GOOGLE_CLOUD_PROJECT = "..."
$env:GOOGLE_CLOUD_LOCATION = "global"
$env:GOOGLE_GENAI_USE_VERTEXAI = "true"

# 저장소 준비 (둘 중 하나)
.\.venv\Scripts\python.exe .\main.py --sync-repository          # 익명 Git Clone
.\.venv\Scripts\python.exe .\main.py --prepare-sources
# 또는: .git 없이 받은 로컬 소스 사용
.\.venv\Scripts\python.exe .\main.py --prepare-sources-from-dir <경로> --commit <SHA>

# 분석 실행 (실제 과금됨)
.\.venv\Scripts\python.exe .\main.py --analyze-code-agentic --issue-file issues\<파일>.txt

# 테스트
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

> **`--prepare-sources-from-dir`용 디렉터리는 이 프로그램이 만들어주지 않습니다.**
> 실제 과거 장애 시점 소스로 테스트하려면, 사내 Gitea 계정으로 해당 레포를 먼저
> `clone`해둔 상태에서 각자 아래처럼 `git archive`로 그 커밋 시점 파일만(`.git` 없이)
> 뽑아야 합니다:
> ```powershell
> cd <clone해둔 레포 경로>
> git archive <커밋 SHA> | tar -x -C <원하는 출력 경로>
> ```
> 뽑은 결과 폴더를 `--prepare-sources-from-dir <출력 경로> --commit <커밋 SHA>`에
> 넘기면 된다. `repositories/`·`analysis_sources/`·`historical_sources/`는
> `.gitignore`에 들어있어 GitHub에는 올라가지 않으므로, 이 과정은 팀원마다 각자
> 로컬에서 반복해야 한다.

**PyCharm에서는** Run Configuration의 Script parameters에 옵션을, Environment variables에
아래를 세미콜론(`;`)으로 구분해서 넣고 실행합니다(그대로 복사해서 `GOOGLE_CLOUD_PROJECT`만
자기 값으로 바꾸면 됩니다):

```
PYTHONUNBUFFERED=1;GOOGLE_GENAI_USE_VERTEXAI=TRUE;GOOGLE_CLOUD_PROJECT=<자신의 GCP 프로젝트 ID>;GOOGLE_CLOUD_LOCATION=global;VERTEX_MODEL=gemini-3.8-flash
```

- `GOOGLE_CLOUD_PROJECT`는 **반드시 각자 자신의 GCP 프로젝트 ID로 바꿔서** 쓴다 — 사내 값이라도
  이 문서나 레포에는 실제 값을 적지 않는다.
- `VERTEX_MODEL`을 생략하면 기본값 `gemini-3.1-flash-lite`가 쓰인다. 더 싸지만 정확도가
  떨어질 수 있다(비용 차이 10~40배 — `261022_agentic-analysis-devlog.md` 참고).
- `--analyze-code-agentic`의 실행 상한(`AGENTIC_MAX_TURNS` 등 비용/턴/시간 상한)은 선택
  환경변수이며 전체 목록과 기본값은 `CLAUDE.md`를 참고한다.

## 4. 꼭 알아야 할 설계 결정

- **왜 Vertex Function Calling인가**: Claude Agent SDK는 구독형 인증이 막혀있고, 자체
  Function Calling 루프를 Gemini 위에 직접 구현하는 게 공수가 크지 않았음 (도구가 3개뿐이고
  이미 보안 검증된 상태였어서). Gitea MCP도 검토했으나 보안 필터를 다시 만들어야 하고
  쓰기 도구가 기본 포함돼 채택 안 함.
- **왜 자체 도구(search_code/read_file/list_files)인가**: `source_tools.py`가 이미
  확장자 allowlist·민감정보 스캔·경로 검증까지 갖춘 **검증된 읽기 전용 도구**였기
  때문에, 이걸 모델에 그대로 노출하는 게 가장 안전하고 빠른 선택이었음.
- **분석 결과는 항상 "미확인 가설"**: `assessment`는 `hypotheses_only`(코드 근거
  있음) / `insufficient_evidence`(없음) 둘뿐. 원인을 확정된 사실처럼 표현하지 않음.
- **인용 근거는 사후 검증됨**: AI가 "이 코드가 근거다"라고 인용하면,
  `validate_evidence()`가 실제로 그 파일을 읽었는지·인용이 글자 단위로 맞는지 대조한다.
  틀리면 분석 전체를 막는다(fail closed).

## 5. 핵심 보안 불변식 (바꾸려면 먼저 확인받을 것)

- `source_tools.py`의 확장자 allowlist, `globals.properties` 류 차단, 민감정보 패턴
  스캔을 느슨하게 만들지 않는다.
- `repository_sync.py`는 익명 HTTPS만 지원한다. 개인 Git 자격증명을 다루는 코드를
  추가하지 않는다.
- 원본 저장소(`repositories/`)에 쓰기/커밋/체크아웃 변경을 하지 않는다. 분석은 항상
  `analysis_sources/`의 필터링된 스냅샷에서만 수행한다.
- 소스를 AI로 보내기 전 승인(`require_source_upload_approval()`)을 우회하지 않는다.

> 자세한 운영 규칙은 `CLAUDE.md` 참고.

## 6. 지금까지 검증된 것 / 안 된 것

**검증됨** (2026-10-22, 실제 사내 코드 + 실제 과거 장애로):
- 반복 탐색 구조 자체가 작동한다 — 고정 1회 검색으로는 못 찾던 원인을 찾아낸 사례 확보.
- 인용 근거 검증이 실제로 틀린 인용을 잡아낸 사례 확보.
- 보안 필터가 실데이터(민감정보 포함 파일)에서도 작동 확인.

**아직 안 된 것**:
- 정확도가 모델 등급에 따라 크게 갈림(작은 모델 lite는 불안정, 큰 모델 flash는 비용
  10~40배) — 표본 1건이라 통계적 결론은 아님. 과거 이슈를 더 모아 표본을 늘려야 함.
- ITS 트리거, 메일 발송, 8개 서비스 전체 확장 — 전부 AI와 무관한 일반 백엔드 작업으로
  분리해서 아직 손 안 댐.

> 자세한 실행 기록은 `261022_agentic-analysis-devlog.md`, 세션별 인수인계 로그는
> `HANDOFF.md`, 4단계 소스 탐색 도구 상세는 `SOURCE_TOOLS.md` 참고.

## 7. 디렉터리 가이드

| 경로 | 내용 |
|---|---|
| `main.py` | 유일한 CLI 진입점 |
| `incident_report.py` | AI 경로 공용 보고서 스키마 |
| `function_calling_analysis.py` | Vertex Function Calling 핵심 로직(주력 경로) |
| `source_tools.py` | 보안 필터링 + 읽기 전용 탐색 도구 |
| `repository_sync.py` | 익명 Git Clone/Fetch |
| `repository_config.json` | 분석 대상 저장소 설정(서비스명/URL/승인 여부) |
| `issues/` | 테스트용 장애 입력 텍스트 모음 |
| `historical_sources/` | `.git` 없이 받아온 과거 커밋 시점 소스(테스트용) |
| `analysis_sources/` | 보안 필터링된 분석 스냅샷(자동 생성) |
| `tests/` | unittest, `-m unittest discover -s tests -v`로 실행 |
