import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

import code_analysis as code
from incident_analysis import sample_analysis


def context():
    return {
        "repository": "sampleRepo", "commit": "a" * 40,
        "searched_keywords": ["iframe"], "limitations": [],
        "snippets": [{"path": "src/index.html", "lines": [
            {"line": 23, "text": "<main>"},
            {"line": 24, "text": "<iframe></iframe>"},
            {"line": 25, "text": "</main>"},
        ]}],
    }


def report():
    return code.CodeAnalysis.model_validate({
        "assessment": "hypotheses_only",
        "findings": [{
            "hypothesis": "iframe 삽입 제한의 영향을 받았을 가능성",
            "evidence": [{"path": "src/index.html", "start_line": 24, "end_line": 24,
                          "quote": "<iframe></iframe>", "explanation": "iframe 사용만 확인되며 차단 원인은 미확인"}],
            "suggested_changes": ["실제 응답 헤더 확인 후 표시 방식을 검토"],
        }],
        "additional_information": ["브라우저 콘솔과 실제 응답 헤더 확인"],
        "limitations": ["전체 소스와 실행 환경은 확인하지 않음"],
    })


class CodeAnalysisTests(unittest.TestCase):
    def test_valid_code_evidence_is_accepted(self):
        code.validate_evidence(report(), context())

    def test_unknown_file_line_gap_and_fabricated_quote_are_rejected(self):
        for update in [
            {"path": "Invented.java"}, {"start_line": 1, "end_line": 1},
            {"quote": "invented code"}, {"start_line": 25, "end_line": 24},
        ]:
            data = report().model_dump()
            data["findings"][0]["evidence"][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                code.validate_evidence(code.CodeAnalysis.model_validate(data), context())
        incomplete = context()
        incomplete["snippets"][0]["lines"].pop(1)
        with self.assertRaises(ValueError):
            code.validate_evidence(report(), incomplete)

    def test_confirmed_cause_and_inconsistent_status_are_rejected(self):
        for update in [{"assessment": "confirmed"}, {"assessment": "insufficient_evidence"}, {"findings": []}]:
            data = report().model_dump() | update
            with self.assertRaises(ValidationError):
                code.CodeAnalysis.model_validate(data)

    def test_no_code_skips_code_analysis_api(self):
        empty = context()
        empty["snippets"] = []
        with patch.object(code.genai, "Client") as client:
            result = code.analyze_code("incident", sample_analysis(), empty)
        client.assert_not_called()
        self.assertEqual(result.assessment, "insufficient_evidence")

    def test_request_limits_stop_before_api(self):
        with patch.object(code.genai, "Client") as client:
            with self.assertRaises(ValueError):
                code.analyze_code("a" * (code.MAX_INCIDENT_BYTES + 1), sample_analysis(), context())
            large = context()
            large["snippets"][0]["lines"][0]["text"] = "a" * code.MAX_REQUEST_BYTES
            with patch.object(code, "require_source_upload_approval"), self.assertRaises(ValueError):
                code.analyze_code("incident", sample_analysis(), large)
        client.assert_not_called()

    def test_structured_api_request_and_validation(self):
        settings = {"GOOGLE_CLOUD_PROJECT": "test-project", "GOOGLE_CLOUD_LOCATION": "global"}
        with patch.dict(os.environ, settings), patch.object(code, "require_source_upload_approval"), patch.object(code.genai, "Client") as factory:
            generate = factory.return_value.__enter__.return_value.models.generate_content
            generate.return_value = SimpleNamespace(parsed=report())
            result = code.analyze_code("incident", sample_analysis(), context())
            self.assertEqual(result.findings[0].evidence[0].start_line, 24)
            arguments = generate.call_args.kwargs
            self.assertIs(arguments["config"].response_schema, code.CodeAnalysis)
            self.assertEqual(arguments["config"].response_mime_type, "application/json")
            self.assertEqual(json.loads(arguments["contents"])["source_context"]["commit"], "a" * 40)
            bad = report().model_dump()
            bad["findings"][0]["evidence"][0]["quote"] = "invented"
            generate.return_value = SimpleNamespace(parsed=code.CodeAnalysis.model_validate(bad))
            with self.assertRaises(ValueError):
                code.analyze_code("incident", sample_analysis(), context())
            generate.return_value = SimpleNamespace(parsed=None)
            with self.assertRaises(ValueError):
                code.analyze_code("incident", sample_analysis(), context())

    def test_collection_uses_candidates_and_deduplicates_code(self):
        tools = SimpleNamespace(repository="sampleRepo", commit="a" * 40)
        tools.search_code = Mock(return_value={"results": [{"path": "src/index.html", "line": 24}], "truncated": False})
        tools.read_file = Mock(return_value={"results": [
            {"path": "src/index.html", "line": 24, "text": "<iframe></iframe>"}], "truncated": False})
        result = code.collect_code(tools, sample_analysis())
        self.assertEqual(len(result["snippets"]), 1)
        self.assertEqual(tools.search_code.call_args_list[0].args, ("원문보기",))
        self.assertIn("iframe", result["searched_keywords"])
        self.assertEqual(tools.read_file.call_count, 1)

    def test_keyword_and_context_size_limits_are_reported(self):
        initial = sample_analysis()
        initial.hypotheses[0].source_search_targets[0].keywords = [f"term{i}" for i in range(10)]
        initial.hypotheses[0].source_search_targets[1].keywords = [f"term{i}" for i in range(10, 20)]
        tools = SimpleNamespace(repository="sampleRepo", commit="a" * 40)
        tools.search_code = Mock(return_value={"results": [], "truncated": False})
        result = code.collect_code(tools, initial)
        self.assertEqual(tools.search_code.call_count, code.MAX_KEYWORDS)
        self.assertTrue(result["limitations"])
        tools.search_code = Mock(return_value={"results": [{"path": "src/Long.java", "line": 1}], "truncated": False})
        tools.read_file = Mock(return_value={"results": [{"line": 1, "text": "a" * code.MAX_CONTEXT_BYTES}], "truncated": False})
        result = code.collect_code(tools, initial)
        self.assertEqual(result["snippets"], [])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), code.MAX_CONTEXT_BYTES)

    def test_upload_approval_is_bound_to_repository_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repository_config.json").write_text(json.dumps({"source_upload_approved_url": "https://example.invalid/public.git"}), encoding="utf-8")
            with patch.object(code, "PROJECT_DIR", root), patch.object(code, "load_config", return_value=SimpleNamespace(url="https://example.invalid/private.git")):
                with self.assertRaises(ValueError):
                    code.require_source_upload_approval()


if __name__ == "__main__":
    unittest.main()
