# 4단계 소스 탐색 실행 안내

실행 진입점은 `main.py`입니다. PyCharm의 **Run → Edit Configurations → main → Script parameters**에 아래 옵션 중 하나를 입력하고 실행하세요. 옵션을 지우면 기존 2단계 장애 분석을 실행합니다.

| 순서 | Script parameters | 설명 |
|---|---|---|
| 1 | `--sync-repository` | 익명 Fetch. 원격에 변경이 생겼을 때 실행 |
| 2 | `--prepare-sources` | 로컬 원격 브랜치를 커밋 SHA로 고정하고 소스 검사·추출 |
| 3 | `--search-code iframe` | 검사된 소스에서 대소문자 구분 없이 일반 문자열 검색 |
| 4 | `--list-files src/main/resources/static` | 해당 경로 바로 아래 목록 조회 |
| 5 | `--read-file src/main/resources/static/index.html --start-line 20 --end-line 28` | 지정 행 읽기. 시작·종료 행 모두 포함 |

터미널에서는 다음과 같이 실행합니다.

```powershell
.\.venv\Scripts\python.exe .\main.py --prepare-sources
.\.venv\Scripts\python.exe .\main.py --search-code iframe
.\.venv\Scripts\python.exe .\main.py --list-files src/main/resources/static
.\.venv\Scripts\python.exe .\main.py --read-file src/main/resources/static/index.html --start-line 20 --end-line 28
```

검색 범위를 좁히려면 `--search-code iframe --path src/main/resources/static`을 사용합니다. 경로는 분석 영역 기준 상대 경로이며 `/`로 구분합니다.

## 데이터 흐름

- `repositories/sampleRepo/.git`: 원본 Git 객체. Python의 준비 기능만 접근하며 소스 도구는 읽지 않습니다.
- `analysis_sources/sampleRepo/<커밋SHA>-<식별자>/`: 검사 통과한 소스와 파일 해시 목록.
- `analysis_sources/sampleRepo/current.json`: 현재 선택한 분석 영역을 가리킵니다.
- 도구 결과에는 저장소 이름, 커밋 SHA, 상대 경로, 행 번호가 포함됩니다.
- Fetch 후에는 `--prepare-sources`를 다시 실행해야 새 커밋을 검색합니다. 기존 분석 영역은 자동으로 삭제하지 않습니다.
- 준비 실패 시 기존 분석 영역 선택은 유지됩니다. 도구 결과의 커밋 SHA로 분석 시점을 확인하세요.
- 모든 4단계 옵션은 Vertex AI를 호출하지 않습니다. 검색어와 순서는 아직 사용자가 지정하며 장애별 절차는 하드코딩하지 않습니다.

## 접근 정책과 제한

- 허용 확장자: `.java`, `.jsp`, `.js`, `.jsx`, `.ts`, `.tsx`, `.xml`, `.html`, `.css`.
- `.git`, 숨김 경로, 빌드·의존성 폴더, 민감한 파일명은 제외합니다.
- YAML, properties, 인증서, 바이너리 등은 기본 허용 대상이 아닙니다. 따라서 `application.yaml`도 제외됩니다.
- `globals.properties`, `globals2.properties`는 위치와 대소문자에 관계없이 명시적으로 차단합니다. 향후 `.properties`를 허용하더라도 이 두 파일은 제외됩니다.
- 파일 전체를 검사해 DB 연결 문자열, 비밀키, 일부 토큰 형식, 비밀번호 등의 의심 패턴이 있으면 파일 전체를 제외합니다.
- UTF-8과 UTF-8 BOM만 지원합니다. 다른 인코딩은 제외 건수에 표시합니다.
- Git 심볼릭 링크와 서브모듈은 추출하지 않습니다. 파일 조회 시 경로 이탈·링크·정션과 준비 후 변경도 검사합니다.
- 파일 1MiB, 전체 소스 10MiB, 추출 1,000파일, Git 목록 검사 5,000항목으로 제한합니다.
- 검색 50건, 목록 100개, 읽기 100행, 도구 응답 JSON 32KiB로 제한합니다.
- 검색과 목록 조회에는 5초의 실행 시간 기준도 적용합니다. 결과 제한 시 `truncated`와 `limit_reason`을 반환합니다.
- 읽기 범위가 파일 끝을 넘으면 실제 마지막 행까지만 반환합니다. 한 행이 응답 크기보다 크면 해당 행을 반환하지 않고 제한 도달을 표시합니다.

민감정보 검사는 패턴 기반이므로 누락과 과잉 차단이 가능합니다. 분석 폴더는 운영체제로 격리된 공간이 아닙니다. 사내 소스를 적용하거나 Vertex AI로 전송하기 전에 별도 승인과 실행 계정·접근 권한 검토가 필요합니다.

## 자동 검증

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

테스트는 임시 파일을 사용하며 원본 저장소를 수정하거나 Push하지 않습니다. 경로 이탈, 민감정보 제외, 행 범위, 응답 제한, 파일 변조와 5단계 응답 검증을 확인합니다. AI 호출은 모의 응답으로 검증하며 실제 Vertex AI 연결 테스트와는 다릅니다.

## 5단계 코드 기반 AI 분석

PyCharm에서 기존 `main` 실행 구성의 **Script parameters**를 `--analyze-code`로 교체하고 실행하세요. 기존 Vertex AI 환경변수를 그대로 사용합니다.

1. `data/its_error1.txt`로 초기 가설과 검색 후보를 생성합니다(첫 번째 AI 호출).
2. Python이 기존 공통 도구로 후보 문자열을 검색하고 주변 코드를 수집합니다.
3. 검사된 코드 일부로 원인 가설, 수정 제안, 추가 확인 사항을 생성합니다(두 번째 AI 호출).
4. Pydantic 검증 후 인용된 파일·행·코드가 실제 전달한 내용과 일치하는지 확인합니다.

관련 코드가 없으면 두 번째 호출을 생략하고 근거 부족으로 반환합니다. 인용 검증은 코드의 존재와 일치를 확인하는 기능이며 원인 해석의 정확성을 보장하지 않습니다. 결과의 모든 원인은 미확인 가설입니다.

검색 후보 최대 12개, 후보별 일치 위치 2개, 주변 코드 범위 8개, 코드 문맥 24KiB, 장애 입력 16KiB, 요청 데이터 48KiB로 제한합니다. 후보와 수집 한도 때문에 관련 코드를 놓칠 수 있으며 결과에 수집 한계를 표시합니다.

현재 준비된 분석 영역만 사용하며 Fetch나 소스 준비를 자동 실행하지 않습니다. 저장소 갱신 후에는 `--sync-repository`, `--prepare-sources`, `--analyze-code`를 각각 실행하세요.

`repository_config.json`의 `source_upload_approved_url`은 현재 공개 테스트 저장소에 대한 전송 승인 설정입니다. 저장소 URL과 다르면 분석을 중단합니다. 사내 Gitea로 바꿀 때는 실제 보안 승인을 받은 뒤 이 값을 설정해야 합니다.

Gemini Function Calling과 반복 탐색은 아직 구현하지 않았습니다. 이번 단계에서는 모델이 생성한 후보로 Python이 제한된 검색을 한 번 수행합니다.
