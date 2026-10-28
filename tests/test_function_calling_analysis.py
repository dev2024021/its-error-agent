"""function_calling_analysis.validate_evidence()의 신뢰성 테스트.

이 함수의 진짜 목적은 "정상 근거를 잘못 막지 않는 것"이 아니라 "허위/미확인 근거를
실제로 잡아내는 것"이다. 정상 사례 1개와, 의도적으로 조작한 여러 실패 사례를 함께
검증해야 이 레이어가 실제로 작동한다고 말할 수 있다."""

import copy
import unittest

import function_calling_analysis as fc
import incident_report


def _read_file_record(path: str, rows: list[tuple[int, str]], ok: bool = True, cached: bool = False) -> fc.ToolCallRecord:
    return fc.ToolCallRecord(
        name="read_file",
        args={"path": path, "start_line": rows[0][0], "end_line": rows[-1][0]},
        ok=ok,
        duration_seconds=0.001,
        cached=cached,
        result={"results": [{"path": path, "line": line, "text": text} for line, text in rows]} if ok else {"error": "허용된 분석 파일이 아닙니다."},
    )


def tool_calls() -> list[fc.ToolCallRecord]:
    return [
        _read_file_record("src/index.html", [
            (23, "<main>"),
            (24, "<iframe></iframe>"),
            (25, "</main>"),
        ]),
    ]


def report() -> incident_report.IncidentReport:
    return incident_report.IncidentReport.model_validate({
        "hypotheses": [{
            "id": "H1",
            "description": "iframe 삽입 제한의 영향을 받았을 가능성",
            "incident_basis": ["콘텐츠가 차단되어 있습니다."],
            "technical_basis": "iframe 사용만 확인되며 차단 원인은 미확인",
            "assumptions": [],
            "source_search_targets": [],
        }],
        "assessment": "hypotheses_only",
        "findings": [{
            "hypothesis_id": "H1",
            "evidence": [{
                "path": "src/index.html", "start_line": 24, "end_line": 24,
                "quote": "<iframe></iframe>", "explanation": "iframe 사용만 확인되며 차단 원인은 미확인",
            }],
            "suggested_changes": ["실제 응답 헤더 확인 후 표시 방식을 검토"],
        }],
        "additional_information": [{
            "hypothesis_ids": ["H1"], "question": "실제 응답 헤더 확인",
            "how_to_check": "브라우저 개발자 도구에서 확인",
        }],
        "limitations": ["전체 소스와 실행 환경은 확인하지 않음"],
    })


class ValidateEvidenceTests(unittest.TestCase):
    def test_valid_evidence_is_accepted(self):
        fc.validate_evidence(report(), tool_calls())

    def test_fabricated_or_inconsistent_evidence_is_rejected(self):
        for update in [
            {"path": "Invented.java"},             # 존재하지 않는 파일 경로
            {"start_line": 100, "end_line": 100},   # 읽은 범위 밖의 줄 번호
            {"quote": "invented code"},              # 존재하지 않는 인용 문구
            {"start_line": 25, "end_line": 24},      # 뒤집힌(모순된) 범위
        ]:
            data = report().model_dump()
            data["findings"][0]["evidence"][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                fc.validate_evidence(incident_report.IncidentReport.model_validate(data), tool_calls())

    def test_evidence_not_confirmed_by_read_file_is_rejected(self):
        """search_code 스니펫만으로는 근거가 성립하지 않아야 한다 — read_file로 실제
        전체 행을 읽은 기록이 있어야 인용을 신뢰한다."""
        search_only = [fc.ToolCallRecord(
            name="search_code", args={"keyword": "iframe"}, ok=True, duration_seconds=0.001,
            result={"results": [{"path": "src/index.html", "line": 24, "text": "<iframe></iframe>"}]},
        )]
        with self.assertRaises(ValueError):
            fc.validate_evidence(report(), search_only)

    def test_failed_read_file_call_does_not_count_as_confirmed(self):
        failed = [_read_file_record("src/index.html", [(24, "<iframe></iframe>")], ok=False)]
        with self.assertRaises(ValueError):
            fc.validate_evidence(report(), failed)

    def test_one_fabricated_finding_rejects_the_whole_report(self):
        """근거 여러 개 중 하나만 조작돼도 보고서 전체를 막아야 한다(fail closed) —
        정상 근거가 섞여 있다고 통과시키지 않는다."""
        data = report().model_dump()
        fabricated = copy.deepcopy(data["findings"][0])
        fabricated["evidence"][0]["quote"] = "존재하지 않는 코드"
        data["findings"].append(fabricated)
        with self.assertRaises(ValueError):
            fc.validate_evidence(incident_report.IncidentReport.model_validate(data), tool_calls())

    def test_cached_tool_call_result_is_still_usable_as_evidence(self):
        """동일 호출 캐시(cached=True)로 돌아온 결과도 실제로 읽은 것과 동일하게
        유효한 근거로 인정돼야 한다."""
        cached = [_read_file_record("src/index.html", [
            (23, "<main>"), (24, "<iframe></iframe>"), (25, "</main>"),
        ], cached=True)]
        fc.validate_evidence(report(), cached)

    def test_real_run_output_passes_regression(self):
        """2026-10-22 실제 --analyze-code-agentic 실행(iframe 사례)에서 나온 결과를
        그대로 고정해 회귀 테스트한다."""
        records = [
            _read_file_record("src/main/resources/static/index.html", [(24, '<iframe id="originalFrame" title="특허 원문"></iframe>')]),
            _read_file_record("src/main/resources/static/js/patent.js", [(22, "originalFrame.src = originalUrl.href;")]),
        ]
        real_report = incident_report.IncidentReport.model_validate({
            "hypotheses": [{
                "id": "H1",
                "description": "원문보기 기능에서 iframe에 로드하려는 콘텐츠의 보안 정책이 표시를 차단하고 있을 가능성",
                "incident_basis": ["이 콘텐츠는 차단되어 있습니다.", "문제를 해결하려면 사이트 소유자에게 문의하세요."],
                "technical_basis": "브라우저가 iframe 내 콘텐츠를 차단할 때 흔히 발생하는 메시지입니다.",
                "assumptions": ["원문보기 대상 서버가 iframe 내 표시를 제한하는 보안 헤더를 전송하고 있다."],
                "source_search_targets": [],
            }],
            "assessment": "hypotheses_only",
            "findings": [{
                "hypothesis_id": "H1",
                "evidence": [
                    {
                        "path": "src/main/resources/static/index.html", "start_line": 24, "end_line": 24,
                        "quote": '<iframe id="originalFrame" title="특허 원문"></iframe>',
                        "explanation": "원문보기 결과를 표시하기 위해 iframe 요소를 사용하고 있습니다.",
                    },
                    {
                        "path": "src/main/resources/static/js/patent.js", "start_line": 22, "end_line": 22,
                        "quote": "originalFrame.src = originalUrl.href;",
                        "explanation": "API로부터 받은 URL을 iframe의 src로 설정하고 있습니다.",
                    },
                ],
                "suggested_changes": ["원문보기 대상 서버의 응답 헤더 확인", "iframe 내 콘텐츠 표시를 위한 서버 측 보안 정책 조정 검토"],
            }],
            "additional_information": [{
                "hypothesis_ids": ["H1"], "question": "실패한 요청의 응답 헤더 확인",
                "how_to_check": "브라우저 개발자 도구 Network 탭에서 확인",
            }],
            "limitations": ["실제 서버 응답 헤더 및 네트워크 통신 내용을 확인할 수 없음"],
        })
        fc.validate_evidence(real_report, records)


def insufficient_evidence_report(keywords: list[str]) -> incident_report.IncidentReport:
    return incident_report.IncidentReport.model_validate({
        "hypotheses": [{
            "id": "H1",
            "description": "원인 불명",
            "incident_basis": ["콘텐츠가 차단되어 있습니다."],
            "technical_basis": "확인 필요",
            "assumptions": [],
            "source_search_targets": [{"keywords": keywords, "purpose": "원인 확인"}] if keywords else [],
        }],
        "assessment": "insufficient_evidence",
        "findings": [],
        "additional_information": [{
            "hypothesis_ids": ["H1"], "question": "실제 응답 헤더 확인",
            "how_to_check": "브라우저 개발자 도구에서 확인",
        }],
        "limitations": ["코드에서 직접적인 근거를 찾지 못함"],
    })


class MissedKeywordDetectionTests(unittest.TestCase):
    """Claude Code의 TODO 추적과 같은 역할을 하는 '계획 vs 실행' 대조 로직 테스트.
    모델이 스스로 적은 탐색 계획(source_search_targets)을 실제로 실행했는지 코드로
    확인한다 — 안 했으면 한 번 더 돌게 만드는 안전망의 기반이 되는 함수들이다."""

    def test_keywords_not_attempted_are_flagged(self):
        report = insufficient_evidence_report(["Content-Security-Policy", "child-src"])
        declared = fc._declared_search_keywords(report)
        self.assertEqual(declared, ["Content-Security-Policy", "child-src"])
        attempted = [fc.ToolCallRecord(
            name="search_code", args={"keyword": "iframe"}, ok=True, duration_seconds=0.001, result={"results": []}
        )]
        self.assertEqual(fc._missed_keywords(declared, attempted), ["Content-Security-Policy", "child-src"])

    def test_keyword_actually_searched_is_not_flagged(self):
        report = insufficient_evidence_report(["child-src"])
        declared = fc._declared_search_keywords(report)
        attempted = [fc.ToolCallRecord(
            name="search_code", args={"keyword": "child-src"}, ok=True, duration_seconds=0.001, result={"results": []}
        )]
        self.assertEqual(fc._missed_keywords(declared, attempted), [])

    def test_keyword_matched_via_read_file_path_is_not_flagged(self):
        """검색이 아니라 read_file로 직접 열어본 파일 경로에 등장해도 확인한 걸로 본다."""
        report = insufficient_evidence_report(["context-security"])
        declared = fc._declared_search_keywords(report)
        attempted = [fc.ToolCallRecord(
            name="read_file", args={"path": "spring/context-security.xml", "start_line": 1, "end_line": 10},
            ok=True, duration_seconds=0.001, result={"results": []},
        )]
        self.assertEqual(fc._missed_keywords(declared, attempted), [])

    def test_no_declared_keywords_means_nothing_missed(self):
        report = insufficient_evidence_report([])
        self.assertEqual(fc._declared_search_keywords(report), [])
        self.assertEqual(fc._missed_keywords([], []), [])

    def test_duplicate_keywords_across_hypotheses_are_deduplicated(self):
        report = incident_report.IncidentReport.model_validate({
            "hypotheses": [
                {
                    "id": "H1", "description": "가설1", "incident_basis": [], "technical_basis": "x",
                    "assumptions": [], "source_search_targets": [{"keywords": ["CSP"], "purpose": "확인"}],
                },
                {
                    "id": "H2", "description": "가설2", "incident_basis": [], "technical_basis": "y",
                    "assumptions": [], "source_search_targets": [{"keywords": ["csp", "child-src"], "purpose": "확인"}],
                },
            ],
            "assessment": "insufficient_evidence",
            "findings": [],
            "additional_information": [{"hypothesis_ids": ["H1"], "question": "q", "how_to_check": "h"}],
            "limitations": ["제한"],
        })
        self.assertEqual(fc._declared_search_keywords(report), ["CSP", "child-src"])


class MergeUsageTests(unittest.TestCase):
    """escalation 시 1차·2차 모델의 사용량을 합산하는 _merge_usage()가 토큰은 항상
    더하고, 비용은 두 모델 단가를 다 아는 경우에만 더하는지 확인한다."""

    def test_tokens_and_cost_are_summed_when_both_models_priced(self):
        usage1 = fc.UsageTracker("gemini-3.1-flash-lite")
        usage1.call_count, usage1.prompt_tokens, usage1.output_tokens, usage1.total_tokens = 2, 1000, 200, 1200
        usage2 = fc.UsageTracker("gemini-3.8-flash")
        usage2.call_count, usage2.prompt_tokens, usage2.output_tokens, usage2.total_tokens = 3, 2000, 400, 2400

        merged = fc._merge_usage(usage1, usage2)

        self.assertEqual(merged.call_count, 5)
        self.assertEqual(merged.prompt_tokens, 3000)
        self.assertEqual(merged.output_tokens, 600)
        self.assertEqual(merged.total_tokens, 3600)
        self.assertAlmostEqual(merged.estimated_cost_usd, usage1.estimated_cost_usd() + usage2.estimated_cost_usd())
        self.assertIn("escalated", merged.model)

    def test_cost_is_none_when_either_model_unpriced(self):
        usage1 = fc.UsageTracker("gemini-3.1-flash-lite")
        usage1.call_count, usage1.prompt_tokens, usage1.output_tokens, usage1.total_tokens = 1, 100, 10, 110
        usage2 = fc.UsageTracker("unknown-model")
        usage2.call_count, usage2.prompt_tokens, usage2.output_tokens, usage2.total_tokens = 1, 100, 10, 110

        merged = fc._merge_usage(usage1, usage2)

        self.assertIsNone(merged.estimated_cost_usd)
        self.assertEqual(merged.call_count, 2)


if __name__ == "__main__":
    unittest.main()
