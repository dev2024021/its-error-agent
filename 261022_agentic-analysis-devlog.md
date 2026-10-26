# 2026-10-22 작업 기록 — Vertex Function Calling 기반 장애 분석 파이프라인

이 문서는 오늘 하루 진행한 내용을 한눈에 보기 위한 요약이다. 코드 레벨 세부사항은
`CLAUDE.md`/`HANDOFF.md`가 항상 최신 기준이며, 이 문서는 "오늘 무엇을 왜 했는지"에
집중한다.

## 1. 방향 결정 — 왜 Vertex Function Calling인가

- **출발점**: ITS 장애 분석을 AI가 반복적으로 코드를 탐색하며 자동 수행하게 만들고 싶었음
  (기존 `incident_analysis.py`+`code_analysis.py`는 "1차 AI가 검색어 생성 → Python이 고정
  1회 검색 → 2차 AI 분석" 구조라, 1차 검색어가 틀리면 재시도 불가 — `HANDOFF.md` 기존 실패
  사례).
- **Claude Agent SDK 시도** → 구독형(Pro/Max) 인증이 공식적으로 막혀있고 별도 API 키 예산이
  필요하다는 걸 확인. 회사 승인 API는 Vertex뿐이라 보류(`claude_incident_analysis.py`는
  비교용으로 코드만 남겨둠).
- **Gitea MCP 검토** → 실제로 존재하긴 하나, 보안 필터링을 다시 만들어야 하고 쓰기 도구가
  기본 포함돼 위험 — 채택 안 함.
- **최종 선택**: Gemini(Vertex AI) 네이티브 Function Calling + 기존 `source_tools.py`의
  검증된 읽기 전용 도구(`search_code`/`read_file`/`list_files`)를 그대로 노출. 회사 승인
  범위 안에서, "모델이 스스로 반복 탐색"하는 구조를 구현.

## 2. 구현 — 핵심 모듈

| 파일 | 역할 |
|---|---|
| `incident_report.py` | 벤더 중립 보고서 스키마(`IncidentReport`), Claude/Vertex 경로가 공유 |
| `function_calling_analysis.py` | Function Calling 선언 3개, 반복 탐색 루프, 비용/토큰 상한, 인용 검증, 자기교정 재시도 |
| `source_tools.py` | (기존 자산 재사용) + `prepare_sources_from_directory()` 신규 — `.git` 없는 로컬 디렉터리도 같은 보안 필터로 분석 영역화 |
| `main.py` | `--analyze-code-agentic`, `--prepare-sources-from-dir`, `--issue-file` 옵션 추가 |

## 3. 안전장치 — 오늘 하루 동안 쌓은 것

| 장치 | 내용 |
|---|---|
| 인용 근거 사후검증 (`validate_evidence`) | AI가 인용한 코드가 실제 `read_file` 결과와 글자 단위로 일치하는지 대조. 불일치 시 `EvidenceValidationError`로 분석 전체 차단(fail closed) |
| 동일 호출 캐시 | 같은 (도구+인자) 반복 호출 시 재실행 대신 캐시 결과 + "새 정보 아님" 안내 |
| 누적 비용 상한 (`AGENTIC_MAX_COST_USD`, 기본 $0.5) | 탐색 루프 중 + 최종 보고서 호출 직전 양쪽에서 확인 |
| 건당 비용 이상 감지 (`AGENTIC_MAX_COST_PER_CALL_USD`, 기본 $0.1) | 단일 요청이 평소보다 비정상적으로 비싸면 누적 상한과 무관하게 즉시 중단 |
| 턴/시간/토큰 상한 | `AGENTIC_MAX_TURNS`(25) / `AGENTIC_MAX_SECONDS`(600) / `AGENTIC_MAX_TOKENS`(200,000) |
| 자기교정 재시도 (Claude Code TODO 추적과 같은 역할) | `insufficient_evidence`로 끝났는데 모델이 스스로 적은 탐색 계획(`source_search_targets`)을 실제로 실행 안 했으면, 그 키워드를 콕 집어 한 번 더 확인시킴 |
| 실패 시 진단 출력 | `EvidenceValidationError`에 미검증 report/records를 담아, 실패해도 "뭘 읽고 뭘 잘못 인용했는지" 볼 수 있게 함 |

테스트: `tests/test_function_calling_analysis.py`, `tests/test_source_tools.py`에 반영,
전체 39개 통과.

## 4. 리소스 상한 재조정 (실측 기반)

테스트용 공개 샘플(`sampleRepo`, 9개 파일)에 맞춰져 있던 상한이 실제 사내 저장소
(약 1만 파일)엔 턱없이 작았음. 실측 후 조정:

| 상수 | 이전 | 변경 |
|---|---|---|
| `MAX_TREE_ENTRIES` | 5,000 | 20,000 |
| `MAX_FILES` | 1,000 | 10,000 |
| `MAX_TOTAL_BYTES` | 10MB | 200MB |

실측 결과: 원본 9,952개 파일 중 6,991개 통과(120MB), `globals.properties`류는 0건,
`sensitive_content` 패턴으로 16개 파일이 실제로 걸러짐 — 보안 필터가 실데이터에서도
작동 확인.

## 5. 실제 과거 장애로 검증

- **방법**: 사내 Gitea 계정을 새로 만들지 않고, 사용자가 `git archive`로 특정 커밋 시점
  소스를 `.git` 없이 추출(`issue-archive/issue-before-fix_1.zip`) → `historical_sources/`에
  압축 해제 → `--prepare-sources-from-dir`로 보안 필터 통과시켜 분석 영역 구성.
- **실제 이슈**: WT 서비스 "원문보기 오류"(이 콘텐츠는 차단되었습니다) —
  `issues/issue-before-fix_1.txt`에 실제 ITS 접수 원문 그대로 반영.
- **실제 원인(정답, 분석엔 미제공)**: 화학 구조식 에디터를 Marvin→Ketcher로 바꾸며 추가한
  CSP `child-src 'self' blob:` 설정이 원문보기(다른 도메인 PDF iframe)에도 적용되어 차단.
  `src/main/resources/wintelips2/spring/com/context-security.xml` 수정으로 해결.

### 결과 비교 (같은 이슈, 같은 커밋, 5회 실행)

| 실행 | 모델 | 결과 | 비용 |
|---|---|---|---|
| 1~4차 | gemini-3.1-flash-lite | 1회만 원인 파일 도달(인용 불일치로 검증 실패), 3회는 못 찾음 | $0.005~0.018 |
| 5차 | gemini-3.8-flash | **원인 파일 발견 + 인용 검증 통과**, 핵심 메커니즘(`child-src`) 정확히 짚음 | $0.19 |

5차 실행은 `max_tokens` 상한에 걸려 중간에 끊겼음에도 핵심 원인을 찾아냈다.

## 6. 핵심 결론 / 인사이트

1. **아키텍처 방향(Vertex Function Calling)은 유효하다** — 기존 고정 1회 검색의 구조적
   한계(재탐색 불가)를 실제로 해결했다.
2. **모델 등급(lite vs 3.8-flash)이 정확도에 큰 영향을 준다** — "harness(자기교정 로직)만
   있으면 lite로도 충분할 것"이라는 가설은 이번 표본에서는 기각 쪽에 가깝다. 다만 표본이
   아직 1건이라 통계적으로 확정은 아니다.
3. **비용 차이가 10~40배** 나는 만큼, "평소엔 lite로 싸게, insufficient_evidence로
   끝나면 더 큰 모델로 재시도"하는 단계적(escalation) 전략이 현실적 대안으로 떠오름 —
   아직 미결정, 다음 논의 대상.
4. 인용 근거 검증(`validate_evidence`)이 실전에서 실제로 틀린 인용을 잡아낸 사례가
   나왔다 — "그럴듯하지만 미묘하게 틀린 답"을 막는 장치가 이론이 아니라 실제로 작동함을
   확인.

## 7. 다음 단계 (미결정, 착수 전 확인 필요)

- 모델 escalation 전략 설계 여부 결정
- 과거 이슈 추가 확보 후 표본 수 늘리기 (지금은 1건)
- Claude SDK 경로(Phase 4~6, 보류 중) 재개 여부
- ITS 트리거·메일 발송 연동 (AI와 무관한 별도 엔지니어링, 범위 밖 유지 중)
- `repository_config.json`을 실제 서비스 정보로 갱신할지 여부

## 8. 소스 Clone 자동화 전략 (논의만, 착수 전)

지금은 사용자가 매번 `git archive`로 과거 커밋 소스를 직접 추출해주는 방식으로 테스트
중인데, "무인 자동화"가 최종 목표인 만큼 이 수동 과정은 언젠가 반드시 자동화돼야 한다.
오늘은 방향만 논의하고 코드는 건드리지 않았다.

**결론**: 읽기 전용 서비스 계정 방향이 맞다. 이건 "더 나은 대안을 찾을 문제"가 아니라
ITS 트리거가 실제로 들어오면 사람이 매번 개입할 수 없으므로 언젠가는 필요한 것.

**추천**: Gitea Access Token보다 **Deploy Key(SSH, 저장소 1개 전용, 읽기 전용 설정 가능)**.
Deploy Key는 구조적으로 저장소 하나에만 묶여서, 실수로 다른 레포까지 접근 범위가 넓어질
여지가 없다. 8개 서비스로 확장할 경우 서비스당 Deploy Key 하나씩 — 저장소 단위로 권한이
딱 떨어져 더 안전하다.

**내재된 위험 3가지**:
1. `repository_sync.py`의 `load_config()`가 `auth_mode != "anonymous"`를 아예 거부하도록
   하드코딩돼 있음 — `CLAUDE.md`의 핵심 보안 불변식 중 하나라 코드 수정 전 설계 확인 단계를
   다시 거쳐야 함. 단, 그 규칙의 취지는 "개발자 개인 자격증명을 실수로 끌어다 쓰지 말자"였지
   "서비스 전용 읽기 전용 키도 금지"는 아니었음 — 성격이 다른 자격증명이다.
2. 자격증명 보관/로그 노출 관리가 새로 생기지만, `safe_error_message()`가 이미
   `TOKEN`/`SECRET`/`CREDENTIAL` 이름 패턴의 환경변수 값을 로그에서 마스킹하므로 환경변수
   이름만 잘 지으면(`GITEA_DEPLOY_KEY` 등) 기존 로직을 그대로 재사용할 수 있음.
3. Gitea 서비스 계정 생성·Deploy Key 발급 자체는 AI가 할 수 없음 — 사용자 또는 사내
   인프라팀이 직접 해야 함.

**시점**: 지금 당장 급하지 않다. "과거 이슈로 정확도 표본 늘리기" 단계에는 현재 수동
방식으로 충분하고, 실제로 필요해지는 시점은 "ITS 트리거 자동화"를 만들 때다 — 그때 다시
설계 단계부터 확인받고 진행한다.
