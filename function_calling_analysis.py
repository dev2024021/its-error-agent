"""Vertex(Gemini) Function Calling 경로: incident_analysis.py의 1차 호출 + code_analysis.py의
고정 1회 검색·2차 호출을, 모델이 직접 반복 호출하는 Function Calling 루프로 대체한다.
source_tools.py의 보안 검증된 도구(search_code/read_file/list_files)를 그대로 모델에게
노출하고, 탐색이 끝나면 별도 호출로 구조화 보고서(response_schema)를 받는다 — Function
Calling 스키마와 response_schema를 같은 호출에 섞지 않는다(과거 스키마 관련 HTTP 400
이력 때문)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from google import genai
from google.genai import types

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

StopReason = Literal["model_finished", "max_turns", "max_seconds", "max_tokens", "max_cost", "anomalous_call_cost"]


@dataclass
class ExecutionStatus:
    stop_reason: StopReason
    turns_used: int
    retried_for_missed_keywords: bool = False

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
    description="분석 영역의 한 디렉터리 바로 아래 항목을 나열한다.",
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
        contents.append(response.candidates[0].content)
        response_parts = []
        for call in response.function_calls:
            args = dict(call.args or {})
            key = _call_key(call.name, args)
            cached_result = seen.get(key)
            started = time.monotonic()
            if cached_result is not None:
                result, ok, cached = cached_result, True, True
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
            payload = {**result, "note": "동일한 호출을 이미 수행했습니다. 새로운 정보가 아닙니다. 다른 검색어나 경로를 시도하세요."} if cached else result
            response_parts.append(types.Part.from_function_response(name=call.name, response=payload))
        contents.append(types.Content(role="tool", parts=response_parts))
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
        raise ValueError("검증 가능한 구조화 응답이 없습니다. 응답 차단 또는 형식 검증 실패 가능성이 있습니다.")
    return IncidentReport.model_validate(response.parsed)


def analyze_incident_agentic(
    incident_text: str, tools: SourceTools
) -> tuple[IncidentReport, list[ToolCallRecord], UsageSummary, ExecutionStatus]:
    if len(incident_text.encode("utf-8")) > MAX_INCIDENT_BYTES:
        raise ValueError("장애 입력이 16KiB 제한을 초과했습니다.")
    missing = [name for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION") if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("실행 환경변수 누락: " + ", ".join(missing))
    model = os.environ.get("VERTEX_MODEL", DEFAULT_MODEL).strip()
    if not model:
        raise ValueError("VERTEX_MODEL 값이 비어 있습니다.")
    max_turns = int(os.environ.get("AGENTIC_MAX_TURNS", DEFAULT_MAX_TURNS))
    max_seconds = int(os.environ.get("AGENTIC_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    max_tokens = int(os.environ.get("AGENTIC_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    max_cost_usd = float(os.environ.get("AGENTIC_MAX_COST_USD", DEFAULT_MAX_COST_USD))
    max_cost_per_call_usd = float(os.environ.get("AGENTIC_MAX_COST_PER_CALL_USD", DEFAULT_MAX_COST_PER_CALL_USD))
    deadline = time.monotonic() + max_seconds

    def _check_cost_before_final_call(cost_so_far: float | None) -> None:
        if cost_so_far is not None and cost_so_far > max_cost_usd:
            raise RuntimeError(
                f"예상 비용이 이미 상한(${max_cost_usd})을 넘어 최종 보고서 호출 없이 중단했습니다. "
                f"지금까지 추정 비용: ${cost_so_far:.4f}"
            )

    usage = UsageTracker(model)
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=build_prompt(incident_text))]),
    ]
    records: list[ToolCallRecord] = []

    with genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"].strip(),
        location=os.environ["GOOGLE_CLOUD_LOCATION"].strip(),
        http_options=types.HttpOptions(api_version="v1", timeout=60_000),
    ) as client:
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

    return report, records, usage.summary(), status
