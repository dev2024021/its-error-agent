import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from google.auth.exceptions import GoogleAuthError
from google.genai import errors
from pydantic import ValidationError

from function_calling_analysis import EvidenceValidationError, ReportValidationError, analyze_incident_agentic
from incident_analysis import analyze_incident, sample_analysis
from repository_sync import sync_repository
from source_tools import json_text, load_source_tools, prepare_sources, prepare_sources_from_directory
from code_analysis import MAX_INCIDENT_BYTES, analyze_code, collect_code, require_source_upload_approval


class _TeeStdout:
    """콘솔과 로그 파일 양쪽에 동시에 출력한다. PowerShell의 Tee-Object/Start-Transcript가
    이 환경(PyCharm 터미널 통합)에서 한글을 깨뜨리는 문제를 겪어서, 셸에 기대지 않고
    Python이 직접 UTF-8로 파일에 쓰도록 했다 — 콘솔 인코딩과 완전히 무관해진다."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for stream in self._streams:
            stream.write(data)
            stream.flush()

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


CODE_SUGGESTION_DISCLAIMER = (
    "이 제안 코드는 자동으로 검증·적용되지 않았습니다. 실제 반영 전 개발자가 직접 "
    "확인하고 테스트해야 합니다."
)


def safe_error_message(message: str, incident_text: str) -> str:
    """서버의 설명만 표시하고 입력 문구, 인증 값, 주소와 인용된 값은 가린다."""
    for value in [incident_text, *incident_text.splitlines()]:
        if value.strip():
            message = message.replace(value, "[입력 내용 생략]")
    for name, value in os.environ.items():
        if value and any(part in name.upper() for part in ["GOOGLE", "VERTEX", "TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL"]):
            message = message.replace(value, "[설정 값 생략]")
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer [생략]", message)
    message = re.sub(r"https?://\S+", "[주소 생략]", message)
    message = re.sub(r"\b(?:AIza[\w-]+|ya29\.[\w.-]+)\b", "[인증 값 생략]", message)
    message = re.sub(r'''"[^"\n]*"|'[^'\n]*' ''', "[인용 값 생략]", message, flags=re.VERBOSE)
    return message[:2000]


def print_elapsed(started: float) -> float:
    elapsed = time.monotonic() - started
    print(f"소요 시간: {elapsed:.1f}초")
    return elapsed


def print_cost_summary(usage) -> None:
    if usage is None:
        print("(비용 정보 없음 — 첫 호출 전에 실패했을 가능성이 있습니다)")
        return
    cost_text = (
        f"${usage.estimated_cost_usd:.4f} (추정치, 실제 청구서 기준 아님)"
        if usage.estimated_cost_usd is not None
        else "모델 단가 미등록으로 추정 생략 — Google Cloud 결제 콘솔에서 확인하세요."
    )
    print(
        f"호출 {usage.call_count}회 / 입력 {usage.prompt_tokens} 토큰 / "
        f"출력 {usage.output_tokens} 토큰 / 예상 비용: {cost_text}"
    )


def read_incident_text(path: Path) -> str:
    """장애 입력 파일을 읽는다. 실패 사유를 담은 ValueError를 던지며, 호출부가 그대로
    출력하고 종료 코드 1을 반환하면 된다."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"입력 파일을 찾을 수 없습니다: {path}") from None
    except UnicodeDecodeError:
        raise ValueError("입력 파일을 UTF-8로 읽을 수 없습니다. 파일 인코딩을 확인하세요.") from None
    except OSError:
        raise ValueError("입력 파일을 읽을 수 없습니다. 파일 권한과 상태를 확인하세요.") from None
    if not text.strip():
        raise ValueError("입력 파일에 장애 내용이 없습니다.")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="ITS 장애 내용의 초기 분석")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--sample", action="store_true", help="Vertex 호출 없이 WT 예제용 고정 샘플 출력")
    modes.add_argument("--sync-repository", action="store_true", help="3단계: 분석 대상 저장소 Clone/Fetch만 수행")
    modes.add_argument("--prepare-sources", action="store_true", help="4단계: 로컬 커밋의 소스 검사 및 분석 영역 구성")
    modes.add_argument("--prepare-sources-from-dir", metavar="PATH", help="4단계 대안: .git 없이 특정 커밋으로 checkout해둔 로컬 디렉터리를 같은 보안 필터로 검사해 분석 영역으로 구성")
    modes.add_argument("--search-code", metavar="KEYWORD", help="분석 영역에서 문자열 검색")
    modes.add_argument("--read-file", metavar="PATH", help="분석 파일의 지정 행 범위 읽기")
    modes.add_argument("--list-files", nargs="?", const=".", metavar="PATH", help="분석 디렉터리 목록 조회")
    modes.add_argument("--analyze-code", action="store_true", help="5단계: 장애 해석, 소스 검색 및 코드 기반 AI 분석")
    modes.add_argument("--analyze-code-agentic", action="store_true", help="Vertex Function Calling 경로: 모델이 search_code/read_file/list_files를 반복 호출하며 탐색")
    parser.add_argument("--path", default=".", help="검색 범위 상대 경로 (--search-code 전용)")
    parser.add_argument("--start-line", type=int, help="읽기 시작 행, 1부터 시작")
    parser.add_argument("--end-line", type=int, help="읽기 종료 행, 포함")
    parser.add_argument("--commit", help="--prepare-sources-from-dir 전용: 해당 디렉터리의 커밋 SHA(git으로 재검증되지 않음, 직접 확인한 값)")
    parser.add_argument("--issue-file", help="장애 입력 파일 경로. 생략하면 data/its_error1.txt를 사용한다(--analyze-code/--analyze-code-agentic 전용)")
    parser.add_argument("--source-label", help="분석 영역 선택: --prepare-sources-from-dir로 준비한 폴더 이름(예: issue-before-fix_2). 생략하면 repository_config.json 기준 저장소를 사용한다")
    args = parser.parse_args()
    issue_path = Path(args.issue_file) if args.issue_file else Path(__file__).resolve().parent / "data" / "its_error1.txt"
    if args.prepare_sources_from_dir is not None and args.commit is None:
        parser.error("--prepare-sources-from-dir에는 --commit이 필요합니다.")
    if args.commit is not None and args.prepare_sources_from_dir is None:
        parser.error("--commit은 --prepare-sources-from-dir과 함께 사용하세요.")
    if args.read_file is not None:
        if args.start_line is None or args.end_line is None:
            parser.error("--read-file에는 --start-line과 --end-line이 필요합니다.")
    elif args.start_line is not None or args.end_line is not None:
        parser.error("행 번호는 --read-file과 함께 사용하세요.")
    if args.path != "." and args.search_code is None:
        parser.error("--path는 --search-code와 함께 사용하세요.")
    if (args.prepare_sources or args.prepare_sources_from_dir is not None or args.search_code is not None
            or args.read_file is not None or args.list_files is not None):
        print("=== 4단계: 공통 소스 탐색 (AI 호출 없음) ===")
        try:
            if args.prepare_sources:
                result = prepare_sources()
            elif args.prepare_sources_from_dir is not None:
                result = prepare_sources_from_directory(Path(args.prepare_sources_from_dir), args.commit)
            else:
                tools = load_source_tools(args.source_label)
                if args.search_code is not None:
                    result = tools.search_code(args.search_code, args.path)
                elif args.read_file is not None:
                    result = tools.read_file(args.read_file, args.start_line, args.end_line)
                else:
                    result = tools.list_files(args.list_files)
            print(json_text(result))
        except (OSError, UnicodeError, KeyError, TypeError):
            print("소스 도구 실행 실패: 분석 영역의 파일과 메타데이터 상태를 확인하세요.")
            return 1
        except (ValueError, RuntimeError) as error:
            print(f"소스 도구 실행 실패: {error}")
            return 1
        return 0
    if args.sync_repository:
        print("=== 3단계: 분석 대상 저장소 연결 ===")
        try:
            sync_repository()
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, (ValueError, RuntimeError)) and not isinstance(error, OSError):
                print(error)
            else:
                print("설정 파일 또는 로컬 저장소 경로에 접근할 수 없습니다.")
            return 1
        return 0

    if args.analyze_code_agentic:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{datetime.now():%Y%m%d_%H%M%S}.log"
        log_file = log_path.open("w", encoding="utf-8")
        sys.stdout = _TeeStdout(sys.stdout, log_file)
        print(f"(실행 기록 저장 위치: {log_path})")
        print("=== Vertex Function Calling 기반 장애 분석 (반복 탐색) ===")
        input_path = issue_path
        try:
            incident_text = read_incident_text(input_path)
        except ValueError as error:
            print(error)
            return 1
        print("관련 코드의 근거를 확인하지만 실행 환경의 장애 원인을 확정하지 않습니다.")
        try:
            require_source_upload_approval()
            tools = load_source_tools(args.source_label)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            print("분석 준비 실패: 분석 영역과 저장소 소스 전송 승인 설정을 확인하세요.")
            return 1
        print(f"분석 기준: {tools.repository} / {tools.commit}")
        print("Vertex AI 호출 중 (반복 탐색 포함, 시간이 걸릴 수 있습니다)...")
        started = time.monotonic()
        try:
            report, tool_calls, usage, status = analyze_incident_agentic(incident_text, tools)
        except ValidationError as error:
            print("응답 검증 실패: 필수 값, 타입 또는 가설 ID 참조가 올바르지 않습니다.")
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            return 1
        except EvidenceValidationError as error:
            print(f"분석 실패(인용 검증): {error}")
            print("※ 아래는 검증을 통과하지 못한 미확인 결과입니다 — 신뢰하지 말고 디버깅 참고용으로만 보세요.")
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            print(json_text({
                "tool_calls": [
                    {
                        "name": r.name, "args": r.args, "ok": r.ok, "cached": r.cached,
                        "result": r.result,
                    }
                    for r in error.records
                ],
                "unvalidated_analysis": error.report.model_dump(),
            }))
            return 1
        except ReportValidationError as error:
            print(f"분석 실패(최종 보고서 검증): {error.interpretation}")
            print("※ 아래는 우리 스키마 검증을 통과하지 못한 원시 응답입니다 — 신뢰하지 말고 참고용으로만 보세요.")
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            print(json_text({"unvalidated_raw_report": error.raw_report}))
            return 1
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            print(f"분석 실패: {error}")
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            return 1
        except GoogleAuthError:
            print("Google 인증에 실패했습니다. ADC 또는 인증 파일 설정을 확인하세요.")
            print_elapsed(started)
            return 1
        except errors.APIError as error:
            print(f"Vertex AI 호출 실패 (상태 코드: {error.code}).")
            print("서버 오류 설명(값 가림): " + safe_error_message(error.message or "설명 없음", incident_text))
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            return 1
        except httpx.HTTPError as error:
            print("Vertex AI 통신에 실패했습니다. 네트워크와 프록시 설정을 확인하세요.")
            print_elapsed(started)
            print_cost_summary(getattr(error, "usage_summary", None))
            return 1
        elapsed_seconds = print_elapsed(started)
        print(f"=== 코드 근거 검증 완료 (원인 확정 아님) — 종료 사유: {status.stop_reason} ===")
        if status.retried_for_missed_keywords:
            print("(안내: 선언한 탐색 키워드 중 실행 안 된 게 있어 한 번 더 확인시켰습니다)")
        if status.escalated:
            print(f"(안내: 1차 분석이 불충분해 상위 모델({status.escalation_model})로 재시도했습니다)")
        if status.stop_reason == "repeated_calls":
            print("(안내: 모델이 같은 도구 호출을 반복해 턴 낭비를 막기 위해 탐색을 조기 종료했습니다)")
        has_code_suggestion = any(finding.example_code_change for finding in report.findings)
        if has_code_suggestion:
            print(f"*** {CODE_SUGGESTION_DISCLAIMER} ***")
        print(f"(탐색 {status.turns_used}턴)")
        print_cost_summary(usage)
        print(json_text({
            "repository": tools.repository, "commit": tools.commit,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "execution": {
                "stop_reason": status.stop_reason,
                "turns_used": status.turns_used,
                "retried_for_missed_keywords": status.retried_for_missed_keywords,
                "escalated": status.escalated,
                "escalation_model": status.escalation_model,
            },
            "tool_calls": [
                {
                    "name": r.name, "args": r.args, "ok": r.ok, "cached": r.cached,
                    "duration_seconds": round(r.duration_seconds, 3), "result": r.result,
                }
                for r in tool_calls
            ],
            "usage": {
                "model": usage.model,
                "call_count": usage.call_count,
                "prompt_tokens": usage.prompt_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
            },
            "code_suggestion_disclaimer": CODE_SUGGESTION_DISCLAIMER if has_code_suggestion else None,
            "analysis": report.model_dump(),
        }))
        return 0

    input_path = issue_path
    try:
        incident_text = read_incident_text(input_path)
    except ValueError as error:
        print(error)
        return 1

    if args.analyze_code:
        print("=== 5단계: 코드 기반 AI 분석 ===")
        try:
            require_source_upload_approval()
            tools = load_source_tools(args.source_label)
            if len(incident_text.encode("utf-8")) > MAX_INCIDENT_BYTES:
                raise ValueError("장애 입력이 16KiB 제한을 초과했습니다.")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            print("코드 분석 준비 실패: 분석 영역과 저장소 소스 전송 승인 설정을 확인하세요.")
            return 1
    else:
        print("=== 2단계: ITS 장애 초기 분석 ===")
    if args.analyze_code:
        print("관련 코드의 근거를 확인하지만 실행 환경의 장애 원인을 확정하지 않습니다.")
    else:
        print("소스 확인 전 가설입니다. 검색 키워드는 후보이며 파일이나 클래스의 존재를 보장하지 않습니다.")
    if args.sample:
        print("샘플 모드: WT 예제용 고정 데이터이며 AI 분석 결과가 아닙니다.")
    else:
        print("Vertex AI 호출 중...")
    try:
        analysis = sample_analysis() if args.sample else analyze_incident(incident_text)
        if args.analyze_code:
            print("장애 해석 완료. 공통 도구로 코드 수집 중...")
            context = collect_code(tools, analysis)
            line_count = sum(len(item["lines"]) for item in context["snippets"])
            print(f"분석 기준: {tools.repository} / {tools.commit}")
            print(f"검색어 {len(context['searched_keywords'])}개, 코드 범위 {len(context['snippets'])}개, {line_count}행 수집")
            print("코드 기반 AI 분석 중..." if context["snippets"] else "관련 코드 없음: 코드 기반 AI 호출 생략")
            report = analyze_code(incident_text, analysis, context)
            print("=== 코드 근거 검증 완료 (원인 확정 아님) ===")
            print(json_text({
                "repository": tools.repository, "commit": tools.commit,
                "searched_keywords": context["searched_keywords"],
                "collection_limitations": context["limitations"],
                "analysis": report.model_dump(),
            }))
            return 0
    except ValidationError:
        print("응답 검증 실패: 필수 값, 타입 또는 가설 ID 참조가 올바르지 않습니다.")
        return 1
    except ValueError:
        print("설정 또는 구조화 응답 검증에 실패했습니다. 환경변수, 데이터 제한, 코드 인용 범위를 확인하세요.")
        return 1
    except (OSError, RuntimeError, KeyError, TypeError):
        print("분석 영역의 파일 또는 메타데이터를 읽을 수 없습니다. 소스를 다시 준비하세요.")
        return 1
    except GoogleAuthError:
        print("Google 인증에 실패했습니다. ADC 또는 인증 파일 설정을 확인하세요.")
        return 1
    except errors.APIError as error:
        print(f"Vertex AI 호출 실패 (상태 코드: {error.code}).")
        # 서버 메시지는 분류에만 사용하고 요청/인증 정보가 들어갈 수 있는 원문은 출력하지 않는다.
        message = (error.message or "").lower()
        if error.code == 400:
            if any(term in message for term in ["too many states", "too complex", "complexity", "too many nesting"]):
                print("서버 오류 분류: 구조화 응답 스키마의 복잡도 제한 초과.")
            elif any(term in message for term in ["response_schema", "responseschema", "schema"]):
                print("서버 오류 분류: 구조화 응답 스키마 관련 요청 오류.")
            else:
                print("서버 오류 분류: 기타 요청 형식/설정 오류. 아직 정확한 원인을 확인하지 못했습니다.")
            if not args.analyze_code:
                print("서버 오류 설명(값 가림): " + safe_error_message(error.message or "설명 없음", incident_text))
        elif error.code in (401, 403):
            print("인증, 프로젝트 권한과 API 활성화 여부를 확인하세요.")
        elif error.code == 404:
            print("프로젝트, 리전과 모델을 확인하세요.")
        else:
            print("호출 한도 또는 서버 상태를 확인하세요.")
        return 1
    except httpx.HTTPError:
        print("Vertex AI 통신에 실패했습니다. 네트워크와 프록시 설정을 확인하세요.")
        return 1
    print("=== 검증된 구조화 결과 ===")
    print(analysis.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
