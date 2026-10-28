"""Vertex(Gemini) Function Calling 경로: incident_analysis.py의 1차 호출 + code_analysis.py의
고정 1회 검색·2차 호출을, 모델이 직접 반복 호출하는 Function Calling 루프로 대체한다.
source_tools.py의 보안 검증된 도구(search_code/read_file/list_files)를 그대로 모델에게
노출하고, 탐색이 끝나면 별도 호출로 구조화 보고서(response_schema)를 받는다 — Function
Calling 스키마와 response_schema를 같은 호출에 섞지 않는다(과거 스키마 관련 HTTP 400
이력 때문)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import ValidationError as PydanticValidationError

from code_analysis import MAX_INCIDENT_BYTES
from incident_report import SYSTEM_INSTRUCTION, IncidentReport, build_prompt
from source_tools import SourceTools


DEFAULT_MAX_TURNS = 25
DEFAULT_MAX_SECONDS = 600
DEFAULT_MAX_TOKENS = 200_000
DEFAULT_MAX_COST_USD = 0.5
# 평소 관찰된 호출 1건당 비용(실측: 1건당 대략 $0.001~0.01)보다 훨씬 큰 값. 정상
# 범위를 넘는 단일 호출은 모델 설정 오류 등 뭔가 잘못됐다는 신호로 보고 즉시 중단한다
# — 누적 상한(AGENTIC_MAX_COST_USD)과는 별개의, "이번 한 건이 이상한가"를 보는 체크다.
DEFAULT_MAX_COST_PER_CALL_USD = 0.1
DEFAULT_MODEL = "gemini-3.1-flash-lite"
# 1차가 insufficient_evidence(또는 인용 검증 실패)로 끝났을 때 재시도할 상위 모델.
# 빈 문자열로 두면 escalation 자체를 끈다.
DEFAULT_ESCALATION_MODEL = "gemini-3.8-flash"

# 아래 AGENTIC_* 상한은 전부 환경변수로 덮어쓸 수 있다 — 로컬 테스트와 운영 환경의
# 상한이 다르다면(실제로 다를 가능성이 높다) 코드를 바꾸지 말고 배포 환경의 환경변수
# 값만 다르게 설정하면 된다. 이 상수들은 "로컬에서 안전하게 테스트할 때" 기준 기본값일
# 뿐, 운영 상한을 의미하지 않는다.
#
# 비용 상한은 "하드 실시간 차단"이 아니라 "직전까지 알 수 있는 정보로 미리 확인 +
# 호출 완료 후 이상 비용 감지"다 — 호출 1건의 정확한 비용은 응답을 받아야 알 수 있어서,
# 그 호출 자체의 과금을 사전에 막을 방법은 없다. AGENTIC_MAX_COST_PER_CALL_USD는 그
# "이미 발생한 비용이 비정상적으로 컸는지"를 사후 감지해 다음 호출을 막는 안전장치다.

StopReason = Literal[
    "model_finished", "max_turns", "max_seconds", "max_tokens", "max_cost",
    "anomalous_call_cost", "repeated_calls",
]
# 모델이 (도구+인자가 완전히 같은) 호출을 이만큼 반복하면, 스스로 멈추지 않을 것으로
# 보고 탐색을 조기 종료한다 — 턴 예산을 똑같은 빈 검색에 계속 낭비하는 걸 막는다.
MAX_DUPLICATE_CALLS = 2


@dataclass
class ExecutionStatus:
    stop_reason: StopReason
    turns_used: int
    retried_for_missed_keywords: bool = False
    escalated: bool = False
    escalation_model: str | None = None

# 2026-10 기준 확인한 Vertex AI 모델별 100만 토큰당 단가(USD). 추정치 계산에만 쓰며,
# 실제 과금은 Google Cloud 결제 콘솔이 기준이다. 모델이 이 표에 없으면 토큰 수만 보여주고
# 추정 비용은 생략한다.
PRICING_PER_MILLION_TOKENS_USD: dict[str, dict[str, float]] = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    # 2026-12-31까지 도입 가격, 이후 $1.5/$7.5로 인상 예정(Google 공식 발표 기준).
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
}


@dataclass
class UsageSummary:
    model: str
    call_count: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None


class UsageTracker:
    """generate_content 호출마다 response.usage_metadata를 누적한다."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.call_count = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0

    def add(self, response: types.GenerateContentResponse) -> float | None:
        """누적치에 더하고, 이번 호출 한 건만의 추정 비용을 반환한다(단가를 모르는
        모델이면 None — 이 경우 건당 비용 이상 감지는 건너뛴다)."""
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        self.call_count += 1
        prompt = getattr(usage, "prompt_token_count", None) or 0
        output = getattr(usage, "candidates_token_count", None) or 0
        self.prompt_tokens += prompt
        self.output_tokens += output
        self.total_tokens += getattr(usage, "total_token_count", None) or 0
        price = PRICING_PER_MILLION_TOKENS_USD.get(self.model)
        if price is None:
            return None
        return prompt / 1_000_000 * price["input"] + output / 1_000_000 * price["output"]

    def estimated_cost_usd(self) -> float | None:
        """지금까지 누적된 호출만으로 계산한 추정 비용. 모델이 단가표에 없으면 판단할
        수 없으므로 None을 반환한다(달러 상한 체크는 이 경우 건너뛴다)."""
        price = PRICING_PER_MILLION_TOKENS_USD.get(self.model)
        if price is None:
            return None
        return self.prompt_tokens / 1_000_000 * price["input"] + self.output_tokens / 1_000_000 * price["output"]

    def summary(self) -> UsageSummary:
        estimated_cost_usd = self.estimated_cost_usd()
        return UsageSummary(
            model=self.model,
            call_count=self.call_count,
            prompt_tokens=self.prompt_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )


def _merge_usage(usage1: UsageTracker, usage2: UsageTracker) -> UsageSummary:
    """1차·escalation 두 모델의 사용량을 합산한다. 모델이 서로 달라 토큰 단가가
    다르므로, 비용은 각 트래커가 자기 모델 기준으로 계산한 값을 더한다(둘 다 단가를
    아는 경우에만 합산 비용을 보여준다)."""
    s1, s2 = usage1.summary(), usage2.summary()
    cost = (
        s1.estimated_cost_usd + s2.estimated_cost_usd
        if s1.estimated_cost_usd is not None and s2.estimated_cost_usd is not None
        else None
    )
    return UsageSummary(
        model=f"{s1.model} -> {s2.model} (escalated)",
        call_count=s1.call_count + s2.call_count,
        prompt_tokens=s1.prompt_tokens + s2.prompt_tokens,
        output_tokens=s1.output_tokens + s2.output_tokens,
        total_tokens=s1.total_tokens + s2.total_tokens,
        estimated_cost_usd=cost,
    )


SEARCH_CODE_DECLARATION = types.FunctionDeclaration(
    name="search_code",
    description="분석 영역에서 대소문자 구분 없이 문자열을 검색한다.",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "검색어"},
            "path": {"type": "string", "description": "검색 범위 상대 경로. 생략하면 전체 영역."},
        },
        "required": ["keyword"],
    },
)

READ_FILE_DECLARATION = types.FunctionDeclaration(
    name="read_file",
    description="분석 영역의 한 파일에서 지정한 행 범위를 읽는다 (한 번에 최대 100행).",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "분석 영역 기준 상대 파일 경로"},
            "start_line": {"type": "integer", "description": "시작 행 번호 (1부터 시작)"},
            "end_line": {"type": "integer", "description": "종료 행 번호 (포함)"},
        },
        "required": ["path", "start_line", "end_line"],
    },
)

LIST_FILES_DECLARATION = types.FunctionDeclaration(
    name="list_files",
    description=(
        "분석 영역의 한 디렉터리 아래 항목을 나열한다. 자식이 하나뿐인 디렉터리가 "
        "연달아 있으면(예: src/main/java/com처럼 갈림길 없는 외길) 자동으로 갈림길이 "
        "나오는 지점까지 내려가서 보여준다 — 결과의 resolved_path로 실제 보여준 "
        "디렉터리를 확인한다."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "분석 영역 기준 상대 디렉터리 경로. 생략하면 루트."},
        },
    },
)

SOURCE_TOOL = types.Tool(
    function_declarations=[SEARCH_CODE_DECLARATION, READ_FILE_DECLARATION, LIST_FILES_DECLARATION]
)


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    ok: bool
    duration_seconds: float
    result: dict[str, Any]
    cached: bool = False


def _execute(tools: SourceTools, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "search_code":
        return tools.search_code(args["keyword"], args.get("path", "."))
    if name == "read_file":
        return tools.read_file(args["path"], int(args["start_line"]), int(args["end_line"]))
    if name == "list_files":
        return tools.list_files(args.get("path", "."))
    raise ValueError(f"알 수 없는 도구 호출: {name}")


def _call_key(name: str, args: dict[str, Any]) -> tuple:
    return (name, tuple(sorted((str(key), str(value)) for key, value in args.items())))


def _available_lines(records: list[ToolCallRecord]) -> dict[str, dict[int, str]]:
    """실제로 read_file이 돌려준 행만 '모델이 진짜로 본 내용'으로 인정한다. search_code
    결과는 일부만 보여주는 스니펫이라 인용 근거의 기준으로 쓰지 않는다."""
    available: dict[str, dict[int, str]] = {}
    for record in records:
        if record.name != "read_file" or not record.ok:
            continue
        for row in record.result.get("results", []):
            path, line, text = row.get("path"), row.get("line"), row.get("text")
            if path is None or line is None:
                continue
            available.setdefault(path, {})[line] = text
    return available


class EvidenceValidationError(RuntimeError):
    """validate_evidence() 실패 시 디버깅용으로 미검증 보고서와 도구 호출 기록을 함께
    들고 있는 예외. 이 안의 report/records는 검증을 통과하지 못했으므로 신뢰할 수 있는
    결과로 취급하지 않는다 — 어디가 왜 틀렸는지 진단하는 용도로만 쓴다."""

    def __init__(self, message: str, report: IncidentReport, records: list[ToolCallRecord]) -> None:
        super().__init__(message)
        self.report = report
        self.records = records


class ReportValidationError(RuntimeError):
    """최종 보고서 호출의 원시 JSON은 받았지만, 우리 스키마(IncidentReport)의 검증
    규칙(필드 길이·가설 ID 일관성 등)을 어겨서 response.parsed가 None이 된 경우. 모델이
    실제로 찾아낸 가설·근거 내용 자체는 버리지 않고 raw_report에 그대로 담아 올린다 —
    턴·비용을 써서 얻은 결과를 스키마 하나 어겼다고 통째로 날리지 않기 위함. raw_report는
    우리 검증을 거치지 않은 원시 JSON이므로, 신뢰할 수 있는 결과로 취급하지 않는다."""

    def __init__(self, message: str, raw_report: dict, interpretation: str) -> None:
        super().__init__(message)
        self.raw_report = raw_report
        self.interpretation = interpretation


def validate_evidence(report: IncidentReport, records: list[ToolCallRecord]) -> None:
    """findings[].evidence가 실제로 read_file로 읽은 내용과 글자 단위로 일치하는지 대조한다.
    모델이 읽지 않은 파일/행을 근거로 들거나 인용을 지어냈으면 분석 전체를 막는다
    (its-error-agent의 code_analysis.py: validate_evidence()와 같은 "fail closed" 원칙)."""
    available = _available_lines(records)
    for finding in report.findings:
        for evidence in finding.evidence:
            lines = available.get(evidence.path, {})
            numbers = range(evidence.start_line, evidence.end_line + 1)
            if not all(number in lines for number in numbers):
                raise ValueError(
                    "AI가 실제로 읽지 않은 파일/행을 근거로 반환했습니다: "
                    f"{evidence.path}:{evidence.start_line}-{evidence.end_line}"
                )
            actual = "\n".join(lines[number] for number in numbers).strip()
            if evidence.quote.strip() != actual:
                raise ValueError(
                    "AI의 코드 인용이 실제 읽은 코드와 일치하지 않습니다: "
                    f"{evidence.path}:{evidence.start_line}-{evidence.end_line}"
                )


def _declared_search_keywords(report: IncidentReport) -> list[str]:
    """모델이 스스로 '이걸 탐색하겠다'고 적어둔 키워드를 전부 모은다(가설별
    source_search_targets, 중복 제거, 순서 유지)."""
    keywords: list[str] = []
    seen_cf: set[str] = set()
    for hypothesis in report.hypotheses:
        for target in hypothesis.source_search_targets:
            for keyword in target.keywords:
                key = keyword.casefold()
                if key not in seen_cf:
                    seen_cf.add(key)
                    keywords.append(keyword)
    return keywords


def _missed_keywords(declared: list[str], records: list[ToolCallRecord]) -> list[str]:
    """선언한 키워드 중 실제 도구 호출(검색어·경로)에 한 번도 등장하지 않은 것만 골라낸다
    — Claude Code의 TODO 추적과 같은 역할: '계획은 세웠지만 실행 안 한 것'을 코드로 찾아낸다."""
    attempted = " ".join(
        str(record.args.get("keyword", "")) + " " + str(record.args.get("path", ""))
        for record in records
    ).casefold()
    return [keyword for keyword in declared if keyword.casefold() not in attempted]


def _run_tool_loop(
    client: genai.Client,
    model: str,
    tools: SourceTools,
    contents: list[types.Content],
    records: list[ToolCallRecord],
    max_turns: int,
    deadline: float,
    max_tokens: int,
    max_cost_usd: float,
    max_cost_per_call_usd: float,
    usage: UsageTracker,
) -> tuple[list[types.Content], list[ToolCallRecord], list[str], ExecutionStatus]:
    limitations: list[str] = []
    seen: dict[tuple, dict[str, Any]] = {}
    turns_used = 0
    duplicate_calls = 0
    stop_reason: StopReason = "model_finished"
    config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, tools=[SOURCE_TOOL], temperature=0.2)

    for _ in range(max_turns):
        if time.monotonic() > deadline:
            stop_reason = "max_seconds"
            limitations.append("탐색 시간 제한에 도달해 반복 탐색을 중단했습니다.")
            break
        if usage.total_tokens > max_tokens:
            stop_reason = "max_tokens"
            limitations.append("토큰 사용량 상한에 도달해 반복 탐색을 중단했습니다.")
            break
        cost_so_far = usage.estimated_cost_usd()
        if cost_so_far is not None and cost_so_far > max_cost_usd:
            stop_reason = "max_cost"
            limitations.append(f"예상 비용 상한(${max_cost_usd})에 도달해 반복 탐색을 중단했습니다.")
            break
        turns_used += 1
        response = client.models.generate_content(model=model, contents=contents, config=config)
        call_cost = usage.add(response)
        if call_cost is not None and call_cost > max_cost_per_call_usd:
            stop_reason = "anomalous_call_cost"
            limitations.append(
                f"단일 요청 비용이 평소보다 비정상적으로 커서(${call_cost:.4f}) 즉시 중단했습니다."
            )
            break
        if not response.function_calls:
            break
        print(f"  [탐색 턴 {turns_used}/{max_turns}] 모델이 도구 {len(response.function_calls)}개 호출 요청")
        contents.append(response.candidates[0].content)
        response_parts = []
        for call in response.function_calls:
            args = dict(call.args or {})
            arg_summary = args.get("keyword") or args.get("path") or ""
            print(f"    - {call.name}({arg_summary!r})" if arg_summary else f"    - {call.name}()")
            key = _call_key(call.name, args)
            cached_result = seen.get(key)
            started = time.monotonic()
            if cached_result is not None:
                result, ok, cached = cached_result, True, True
                duplicate_calls += 1
            else:
                cached = False
                try:
                    result = _execute(tools, call.name, args)
                    ok = True
                    seen[key] = result
                except (ValueError, KeyError, TypeError) as error:
                    result = {"error": str(error)}
                    ok = False
            records.append(ToolCallRecord(call.name, args, ok, time.monotonic() - started, result, cached))
            # 반복 호출엔 실제 결과를 다시 섞어주지 않는다 — result 필드가 같이 보이면
            # "그래도 뭔가 응답이 왔다"고 여기고 계속 반복하는 경향이 있어서, 에러처럼
            # 보이는 짧은 메시지만 돌려준다.
            payload = {"error": "이미 같은 조건으로 호출했습니다. 결과는 바뀌지 않습니다. 다른 검색어나 경로를 시도하세요."} if cached else result
            response_parts.append(types.Part.from_function_response(name=call.name, response=payload))
        contents.append(types.Content(role="tool", parts=response_parts))
        if duplicate_calls >= MAX_DUPLICATE_CALLS:
            stop_reason = "repeated_calls"
            limitations.append(
                f"모델이 동일한 도구 호출을 {duplicate_calls}회 반복해(멈추지 않을 것으로 판단) 탐색을 중단했습니다."
            )
            break
    else:
        stop_reason = "max_turns"
        limitations.append(f"최대 {max_turns}회 반복 한도에 도달해 탐색을 중단했습니다.")

    return contents, records, limitations, ExecutionStatus(stop_reason, turns_used)


def _final_report(
    client: genai.Client,
    model: str,
    contents: list[types.Content],
    limitations: list[str],
    max_cost_per_call_usd: float,
    usage: UsageTracker,
) -> IncidentReport:
    closing = (
        "탐색을 마칩니다. 지금까지 실제로 호출한 도구 결과만 근거로 삼아 지정된 스키마 "
        "형식의 최종 보고서를 작성하세요."
    )
    if limitations:
        closing += "\n\n탐색 과정의 한계: " + "; ".join(limitations)
    final_contents = [*contents, types.Content(role="user", parts=[types.Part.from_text(text=closing)])]

    response = client.models.generate_content(
        model=model,
        contents=final_contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=IncidentReport,
            temperature=0.2,
        ),
    )
    call_cost = usage.add(response)
    if call_cost is not None and call_cost > max_cost_per_call_usd:
        raise RuntimeError(
            f"최종 보고서 호출 비용이 평소보다 비정상적으로 큽니다(${call_cost:.4f}). "
            "결과를 신뢰할 수 없어 중단합니다."
        )
    if response.parsed is None:
        reason = None
        if response.candidates:
            reason = getattr(response.candidates[0], "finish_reason", None)
        raw_text = getattr(response, "text", None)
        # SDK가 조용히 포기한 이유를 직접 재현해서 알아낸다 — 모델이 실제로 뭐라고
        # 답했는지, 어느 검증 규칙에 걸렸는지를 사람이 읽을 수 있게 남긴다.
        interpretation = "원시 응답 없음 — 응답이 완전히 비어있거나 차단됨"
        if raw_text:
            try:
                IncidentReport.model_validate_json(raw_text)
                interpretation = "원시 응답은 스키마 검증을 통과했습니다(원인 불명 — SDK 내부 문제 가능성)."
            except PydanticValidationError as validation_error:
                details = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in validation_error.errors()
                )
                interpretation = f"모델 응답이 다음 검증 규칙에 걸렸습니다 — {details}"
                # 우리 스키마 규칙은 어겼어도, 모델이 실제로 찾아낸 가설·근거는 버리지
                # 않는다 — 턴·비용을 써서 얻은 내용이라 JSON으로만 파싱되면 참고용으로
                # 올려본다(우리 검증은 통과 못 했으므로 신뢰할 수 있는 결과는 아니다).
                try:
                    raw_report = json.loads(raw_text)
                except ValueError:
                    raw_report = None
                if raw_report is not None:
                    raise ReportValidationError(
                        f"최종 보고서가 검증 규칙을 어겼습니다 — {details}", raw_report, details,
                    ) from validation_error
            except ValueError as json_error:
                interpretation = f"모델 응답이 JSON 자체가 아니었습니다: {json_error}"
        raise ValueError(
            "검증 가능한 구조화 응답이 없습니다. 응답 차단 또는 형식 검증 실패 가능성이 있습니다. "
            f"(finish_reason: {reason}) {interpretation}\n"
            f"원시 응답(최대 2000자): {(raw_text or '')[:2000]}"
        )
    return IncidentReport.model_validate(response.parsed)


def _analyze_once(
    client: genai.Client,
    model: str,
    tools: SourceTools,
    incident_text: str,
    deadline: float,
    max_turns: int,
    max_tokens: int,
    max_cost_usd: float,
    max_cost_per_call_usd: float,
    usage: UsageTracker,
) -> tuple[IncidentReport, list[ToolCallRecord], ExecutionStatus]:
    """한 모델로 전체 분석(반복 탐색 → 자기교정 재시도 → 최종 보고서 → 인용 검증)을
    한 번 수행한다. 인용 검증 실패 시 EvidenceValidationError를 던진다."""

    def _check_cost_before_final_call(cost_so_far: float | None) -> None:
        if cost_so_far is not None and cost_so_far > max_cost_usd:
            raise RuntimeError(
                f"예상 비용이 이미 상한(${max_cost_usd:.4f})을 넘어 최종 보고서 호출 없이 중단했습니다. "
                f"지금까지 추정 비용: ${cost_so_far:.4f}"
            )

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=build_prompt(incident_text))]),
    ]
    records: list[ToolCallRecord] = []

    contents, records, limitations, status = _run_tool_loop(
        client, model, tools, contents, records, max_turns, deadline, max_tokens,
        max_cost_usd, max_cost_per_call_usd, usage,
    )
    if status.stop_reason == "anomalous_call_cost":
        raise RuntimeError("탐색 중 단일 요청 비용이 비정상적으로 커서 중단했습니다. 모델 설정을 확인하세요.")
    # 탐색 루프와 별개로, 마지막 구조화 호출 직전에도 누적 비용 상한을 한 번 더
    # 확인한다 — 이 호출은 루프 안의 상한 체크를 거치지 않고 누적 맥락을 통째로
    # 다시 보내므로, 여기서 막지 않으면 상한이 사실상 없는 것과 같아진다.
    _check_cost_before_final_call(usage.estimated_cost_usd())
    print("  [탐색 종료] 최종 보고서 작성 중...")
    report = _final_report(client, model, contents, limitations, max_cost_per_call_usd, usage)

    # Claude Code의 TODO 추적과 같은 역할: 모델이 스스로 "이 키워드를 확인하겠다"고
    # 적어놓고 근거 없음(insufficient_evidence)으로 끝냈다면, 실제로 그 키워드를
    # 확인했는지 코드로 대조한다. 빠뜨린 게 있으면 한 번만 더 돌게 해서 직접
    # 확인시킨다 — "계획만 세우고 실행 안 함" 패턴에 대한 구조적 안전망이다.
    if report.assessment == "insufficient_evidence":
        missed = _missed_keywords(_declared_search_keywords(report), records)
        if missed:
            nudge = (
                "앞서 다음 키워드를 탐색하겠다고 스스로 밝혔지만 실제로 search_code나 "
                "read_file로 확인하지 않았습니다: " + ", ".join(missed) + ". "
                "결론 내리기 전에 이 키워드들을 반드시 직접 확인한 뒤 다시 답하세요."
            )
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=nudge)]))
            remaining_turns = max(3, max_turns - status.turns_used)
            contents, records, retry_limitations, retry_status = _run_tool_loop(
                client, model, tools, contents, records, remaining_turns, deadline, max_tokens,
                max_cost_usd, max_cost_per_call_usd, usage,
            )
            if retry_status.stop_reason == "anomalous_call_cost":
                raise RuntimeError("재확인 중 단일 요청 비용이 비정상적으로 커서 중단했습니다. 모델 설정을 확인하세요.")
            limitations = limitations + retry_limitations
            status = ExecutionStatus(
                retry_status.stop_reason, status.turns_used + retry_status.turns_used, retried_for_missed_keywords=True
            )
            _check_cost_before_final_call(usage.estimated_cost_usd())
            report = _final_report(client, model, contents, limitations, max_cost_per_call_usd, usage)

    # "읽지 않은 코드를 근거로 들었는지" 사후 대조 — 통과 못 하면 분석 전체를 막는다.
    # 디버깅할 수 있게 미검증 report/records를 예외에 함께 담아 올린다.
    try:
        validate_evidence(report, records)
    except ValueError as error:
        raise EvidenceValidationError(str(error), report, records) from error

    return report, records, status


def analyze_incident_agentic(
    incident_text: str, tools: SourceTools
) -> tuple[IncidentReport, list[ToolCallRecord], UsageSummary, ExecutionStatus]:
    """기본 모델(VERTEX_MODEL)로 1차 분석하고, 근거가 불충분하거나(insufficient_evidence)
    인용 검증에 실패하면 더 큰 모델(AGENTIC_ESCALATION_MODEL)로 처음부터 다시 분석한다
    (대화 맥락은 이어가지 않음 — UsageTracker가 모델 1개 기준으로 단가를 계산하는 구조라,
    맥락을 이으며 모델을 바꾸면 비용 계산이 꼬인다).

    AGENTIC_MAX_SECONDS/AGENTIC_MAX_COST_USD는 1차+escalation 전체에 공유되는 총 상한이다
    (모델별로 따로 주지 않는다) — 독립적으로 주면 escalation이 일어날 때마다 소요 시간·
    비용이 그대로 배가되는데, 무인 배치에서 ITS 접수가 몰리면 그 지연이 누적될 수 있어
    위험하다. AGENTIC_MAX_TURNS/AGENTIC_MAX_TOKENS는 모델마다 독립적으로 적용한다(탐색
    시도 자체는 각 모델에게 온전히 주는 게 맞다고 보고 공유하지 않았다)."""
    if len(incident_text.encode("utf-8")) > MAX_INCIDENT_BYTES:
        raise ValueError("장애 입력이 16KiB 제한을 초과했습니다.")
    missing = [name for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION") if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("실행 환경변수 누락: " + ", ".join(missing))
    model = os.environ.get("VERTEX_MODEL", DEFAULT_MODEL).strip()
    if not model:
        raise ValueError("VERTEX_MODEL 값이 비어 있습니다.")
    escalation_model = os.environ.get("AGENTIC_ESCALATION_MODEL", DEFAULT_ESCALATION_MODEL).strip()
    # "" 또는 "none"이면 escalation을 끈다. 빈 문자열만 허용하면 Windows에서 문제가
    # 생긴다 — Win32 API는 환경변수를 빈 문자열로 설정하면 그 변수 자체를 삭제해버려서
    # (PowerShell의 $env:X = ""도 마찬가지), 다시 읽으면 "없음"이 아니라 기본값으로
    # 돌아가버린다. "none"은 그 문제가 없는 명시적인 끔 신호다.
    if escalation_model.lower() in ("", "none"):
        escalation_model = ""
    max_turns = int(os.environ.get("AGENTIC_MAX_TURNS", DEFAULT_MAX_TURNS))
    max_seconds = int(os.environ.get("AGENTIC_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    max_tokens = int(os.environ.get("AGENTIC_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    max_cost_usd = float(os.environ.get("AGENTIC_MAX_COST_USD", DEFAULT_MAX_COST_USD))
    max_cost_per_call_usd = float(os.environ.get("AGENTIC_MAX_COST_PER_CALL_USD", DEFAULT_MAX_COST_PER_CALL_USD))
    overall_deadline = time.monotonic() + max_seconds

    with genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"].strip(),
        location=os.environ["GOOGLE_CLOUD_LOCATION"].strip(),
        http_options=types.HttpOptions(api_version="v1", timeout=60_000),
    ) as client:
        usage1 = UsageTracker(model)
        usage2: UsageTracker | None = None
        try:
            return _analyze_with_escalation(
                client, model, escalation_model, tools, incident_text, overall_deadline,
                max_turns, max_tokens, max_cost_usd, max_cost_per_call_usd, usage1,
            )
        except Exception as error:
            # 실패해도 그때까지 쓴 비용은 알아야 한다 — main.py가 success/failure
            # 양쪽에서 똑같이 비용을 보여줄 수 있도록 예외에 실어서 올린다. 이미
            # _analyze_with_escalation 안에서 (escalation까지 간 경우) 합산 비용을
            # 실어뒀다면 그걸 덮어쓰지 않는다.
            if not hasattr(error, "usage_summary"):
                error.usage_summary = usage1.summary()
            raise


def _analyze_with_escalation(
    client: genai.Client, model: str, escalation_model: str, tools: SourceTools,
    incident_text: str, overall_deadline: float, max_turns: int, max_tokens: int,
    max_cost_usd: float, max_cost_per_call_usd: float, usage1: UsageTracker,
) -> tuple[IncidentReport, list[ToolCallRecord], UsageSummary, ExecutionStatus]:
    stage1_error: EvidenceValidationError | None = None
    try:
        report, records, status = _analyze_once(
            client, model, tools, incident_text, overall_deadline,
            max_turns, max_tokens, max_cost_usd, max_cost_per_call_usd, usage1,
        )
    except EvidenceValidationError as error:
        report, records, status = None, None, None
        stage1_error = error

    if stage1_error is None and report.assessment != "insufficient_evidence":
        return report, records, usage1.summary(), status  # 1차로 충분

    if not escalation_model:
        if stage1_error is not None:
            raise stage1_error
        return report, records, usage1.summary(), status  # escalation 꺼짐 — 1차 결과 그대로

    spent = usage1.estimated_cost_usd() or 0.0
    remaining_cost = max_cost_usd - spent
    time_left = overall_deadline - time.monotonic()
    if remaining_cost <= 0 or time_left <= 0:
        if stage1_error is not None:
            raise stage1_error
        return report, records, usage1.summary(), status  # 예산/시간 부족 — escalation 생략

    print(f"=== 1차 분석({model})이 불충분해 상위 모델({escalation_model})로 재시도합니다 ===")
    usage2 = UsageTracker(escalation_model)
    # 2차(상위 모델)에서 EvidenceValidationError/RuntimeError가 나면 그대로 전파한다.
    # 가장 신뢰도 높은 모델로도 실패했다는 뜻이라, 1차의 (이미 불충분하거나 틀린)
    # 결과로 조용히 대체하는 것보다 실패를 그대로 드러내는 쪽이 맞다. 이 경우에도
    # 1차+2차 합산 비용을 예외에 실어서 올린다.
    try:
        report2, records2, status2 = _analyze_once(
            client, escalation_model, tools, incident_text, overall_deadline,
            max_turns, max_tokens, remaining_cost, max_cost_per_call_usd, usage2,
        )
    except Exception as error:
        error.usage_summary = _merge_usage(usage1, usage2)
        raise
    merged_usage = _merge_usage(usage1, usage2)
    final_status = ExecutionStatus(
        status2.stop_reason, status2.turns_used, status2.retried_for_missed_keywords,
        escalated=True, escalation_model=escalation_model,
    )
    return report2, records2, merged_usage, final_status
