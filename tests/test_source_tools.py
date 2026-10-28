import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import source_tools as source


COMMIT = "a" * 40


class SourceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def tools(self, files):
        entries = []
        for name, text in files.items():
            data = text.encode("utf-8")
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
        (self.root / source.MANIFEST_NAME).write_text(json.dumps({
            "version": 1, "repository": "sampleRepo", "commit": COMMIT, "files": entries,
        }), encoding="utf-8")
        return source.SourceTools(self.root)

    def test_search_read_and_list_are_generic(self):
        tools = self.tools({
            "src/View.jsp": "<html>\n<iframe></iframe>\n</html>\n",
            "src/Auth.java": "class Auth {}\n",
        })
        match = tools.search_code("IFRAME")["results"][0]
        self.assertEqual((match["path"], match["line"]), ("src/View.jsp", 2))
        self.assertEqual(tools.search_code("not_present")["results"], [])
        self.assertEqual(tools.search_code("Auth", "src/Auth.java")["results"][0]["line"], 1)
        self.assertEqual(tools.read_file("src/View.jsp", 2, 2)["results"][0]["text"], "<iframe></iframe>")
        # 루트는 자식이 "src" 하나뿐이라, 갈림길이 나오는 src 안까지 자동으로 내려가서 보여준다.
        root_listing = tools.list_files()
        self.assertEqual(root_listing["resolved_path"], "src")
        self.assertEqual(
            sorted(root_listing["results"], key=lambda item: item["path"]),
            [{"path": "src/Auth.java", "kind": "file"}, {"path": "src/View.jsp", "kind": "file"}],
        )
        self.assertEqual(len(tools.list_files("src")["results"]), 2)

    def test_list_files_skips_single_child_directory_chains(self):
        """src/main/java/com처럼 갈림길 없는 외길 디렉터리 체인은, 실제로 선택지가
        갈리는 지점(여기서는 com 밑에 a/b 두 패키지)까지 한 번의 호출로 건너뛴다."""
        tools = self.tools({
            "src/main/java/com/a/Foo.java": "class Foo {}",
            "src/main/java/com/b/Bar.java": "class Bar {}",
        })
        result = tools.list_files()
        self.assertEqual(result["resolved_path"], "src/main/java/com")
        self.assertEqual(
            sorted(item["path"] for item in result["results"]),
            ["src/main/java/com/a", "src/main/java/com/b"],
        )

    def test_arbitrary_paths_are_rejected(self):
        tools = self.tools({"Safe.java": "class Safe {}"})
        for path in ["../main.py", "C:/secret.java", "C:\\secret.java", ".git/config", "Safe.java:stream", "/Safe.java", "NUL.java", "missing.java"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                tools.read_file(path, 1, 1)
        for path in ["../", ".git", "missing"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                tools.list_files(path)

    def test_read_range_and_types_are_validated(self):
        tools = self.tools({"Safe.java": "line1\nline2\n"})
        for start, end in [(0, 1), (2, 1), (1, 101), (True, 2), (1, "2"), (3, 3)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                tools.read_file("Safe.java", start, end)
        self.assertEqual(len(tools.read_file("Safe.java", 1, 5)["results"]), 2)

    def test_sensitive_content_is_blocked_before_any_line_is_returned(self):
        tools = self.tools({"Settings.java": 'class Settings {}\nString password = "FAKE_SECRET_TEST_ONLY";\n'})
        for action in [lambda: tools.read_file("Settings.java", 1, 1), lambda: tools.search_code("class"), lambda: tools.list_files()]:
            with self.assertRaises(ValueError):
                action()

    def test_common_secret_formats_are_excluded(self):
        for text in [
            'String dbPassword = "FAKE_PASSWORD";',
            'const API_KEY = "FAKE_KEY";',
            '<property value="FAKE_PASSWORD" name="password"/>',
            'String url = "jdbc:postgresql://example.invalid/db";',
            '-----BEGIN RSA PRIVATE KEY-----',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                source.decode_source(text.encode("utf-8"))

    def test_modified_file_and_unregistered_file_are_blocked(self):
        tools = self.tools({"Safe.java": "class Safe {}"})
        (self.root / "Other.java").write_text("class Other {}", encoding="utf-8")
        with self.assertRaises(ValueError):
            tools.read_file("Other.java", 1, 1)
        (self.root / "Safe.java").write_text("class Changed {}", encoding="utf-8")
        with self.assertRaises(ValueError):
            tools.read_file("Safe.java", 1, 1)

    def test_globals_files_are_blocked_even_if_properties_are_allowed(self):
        with patch.object(source, "ALLOWED_EXTENSIONS", source.ALLOWED_EXTENSIONS | {".properties"}):
            for name in ["globals.properties", "src/config/globals2.properties", "config/GLOBALS.PROPERTIES"]:
                with self.subTest(path=name), self.assertRaises(ValueError):
                    self.tools({name: "display.name=test"})

    def test_link_access_is_rejected(self):
        tools = self.tools({"Safe.java": "class Safe {}"})
        # Windows의 링크 생성 권한과 무관하게 공통 링크 검사 호출을 검증한다.
        with patch.object(source, "reject_links", side_effect=ValueError("link")):
            with self.assertRaises(ValueError):
                tools.read_file("Safe.java", 1, 1)

    def test_search_count_and_response_size_limits(self):
        tools = self.tools({"Many.java": "match\n" * 60})
        result = tools.search_code("match")
        self.assertEqual(len(result["results"]), source.MAX_SEARCH_RESULTS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["limit_reason"], "result_count")
        self.assertLessEqual(len(source.json_text(result).encode("utf-8")), source.MAX_RESPONSE_BYTES)
        tools = self.tools({"Long.java": "가" * 12000})
        result = tools.read_file("Long.java", 1, 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["limit_reason"], "response_bytes")
        self.assertLessEqual(len(source.json_text(result).encode("utf-8")), source.MAX_RESPONSE_BYTES)

    def test_literal_keyword_and_unicode_position(self):
        tools = self.tools({"Safe.java": "ß <iframe>\n[frame]\n"})
        result = tools.search_code("[frame]")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["line"], 2)

    def test_prepare_extracts_only_checked_regular_files(self):
        blobs = [
            ("src/Safe.java", b"class Safe {}", b"100644"),
            ("src/Settings.java", b'String password = "FAKE_SECRET_TEST_ONLY";', b"100644"),
            ("src/security.xml", b'<config><property name="password" value="FAKE_XML_SECRET"/></config>', b"100644"),
            ("application.yaml", b"password: fake", b"100644"),
            ("src/config/globals.properties", b"display.name=test", b"100644"),
            ("src/config/GLOBALS2.PROPERTIES", b"display.name=test", b"100644"),
            (".git/config", b"fake", b"100644"),
            ("src/Link.java", b"../../outside", b"120000"),
            ("src/Binary.java", b"\x00\x01", b"100644"),
            ("src/Legacy.java", b"\xff", b"100644"),
        ]
        tree = b""
        objects = {}
        for number, (name, data, mode) in enumerate(blobs, 1):
            oid = f"{number:040x}"
            objects[oid] = data
            tree += mode + b" blob " + oid.encode() + b" " + str(len(data)).encode() + b"\t" + name.encode() + b"\0"

        def git(arguments, **kwargs):
            command = arguments[0]
            if command == "check-ref-format":
                output = b""
            elif command == "rev-parse":
                output = COMMIT.encode()
            elif command == "ls-tree":
                output = tree
            elif command == "cat-file":
                output = objects[arguments[2]]
            else:
                self.fail("Unexpected Git operation")
            return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

        config = SimpleNamespace(name="sampleRepo", branch="main", local_path=self.root / "repo", url="https://example.invalid/r.git")
        with patch.object(source, "PROJECT_DIR", self.root), patch.object(source, "load_config", return_value=config), patch.object(source, "validate_local_config"), patch.object(source, "run_git", side_effect=git), patch.object(source, "ALLOWED_EXTENSIONS", source.ALLOWED_EXTENSIONS | {".properties"}):
            result = source.prepare_sources()
            self.assertEqual(result["accepted_files"], 1)
            tools = source.load_source_tools()
            self.assertEqual(tools.read_file("src/Safe.java", 1, 1)["results"][0]["text"], "class Safe {}")
        snapshot = Path(result["analysis_path"])
        all_data = b"".join(path.read_bytes() for path in snapshot.rglob("*") if path.is_file())
        self.assertNotIn(b"FAKE_SECRET", all_data)
        self.assertNotIn(b"FAKE_XML_SECRET", all_data)
        self.assertFalse((snapshot / ".git").exists())
        self.assertFalse((snapshot / "src/config/globals.properties").exists())
        self.assertFalse((snapshot / "src/config/GLOBALS2.PROPERTIES").exists())

    def test_prepare_from_directory_extracts_only_checked_regular_files(self):
        with tempfile.TemporaryDirectory() as source_name:
            source_dir = Path(source_name)
            files = {
                "src/Safe.java": "class Safe {}",
                "src/Settings.java": 'String password = "FAKE_SECRET_TEST_ONLY";',
                "src/security.xml": '<config><property name="password" value="FAKE_XML_SECRET"/></config>',
                "application.yaml": "password: fake",
                "src/config/globals.properties": "display.name=test",
                "src/config/GLOBALS2.PROPERTIES": "display.name=test",
                ".git/config": "fake",
            }
            for name, text in files.items():
                target = source_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            (source_dir / "src" / "Binary.java").write_bytes(b"\x00\x01")

            config = SimpleNamespace(name="sampleRepo", branch="main", local_path=self.root / "repo", url="https://example.invalid/r.git")
            with patch.object(source, "PROJECT_DIR", self.root), patch.object(source, "load_config", return_value=config), patch.object(source, "ALLOWED_EXTENSIONS", source.ALLOWED_EXTENSIONS | {".properties"}):
                result = source.prepare_sources_from_directory(source_dir, COMMIT)
                self.assertEqual(result["accepted_files"], 1)
                self.assertEqual(result["repository"], source_dir.name)
                tools = source.load_source_tools(label=source_dir.name)
                self.assertEqual(tools.read_file("src/Safe.java", 1, 1)["results"][0]["text"], "class Safe {}")
            snapshot = Path(result["analysis_path"])
            all_data = b"".join(path.read_bytes() for path in snapshot.rglob("*") if path.is_file())
            self.assertNotIn(b"FAKE_SECRET", all_data)
            self.assertNotIn(b"FAKE_XML_SECRET", all_data)
            self.assertFalse((snapshot / ".git").exists())
            self.assertFalse((snapshot / "src/config/globals.properties").exists())
            self.assertFalse((snapshot / "src/config/GLOBALS2.PROPERTIES").exists())

    def test_prepare_from_directory_labels_dont_collide(self):
        """서로 다른 source_dir 이름으로 두 번 prepare하면 각자 독립된
        analysis_sources/<폴더 이름>/current.json을 가져서, 뒤에 준비한 쪽이 앞서
        준비한 쪽의 분석 대상을 밀어내지 않아야 한다."""
        with tempfile.TemporaryDirectory() as first_name, tempfile.TemporaryDirectory() as second_name:
            first_dir, second_dir = Path(first_name), Path(second_name)
            (first_dir / "First.java").write_text("class First {}", encoding="utf-8")
            (second_dir / "Second.java").write_text("class Second {}", encoding="utf-8")

            with patch.object(source, "PROJECT_DIR", self.root):
                source.prepare_sources_from_directory(first_dir, COMMIT)
                source.prepare_sources_from_directory(second_dir, COMMIT)

                first_tools = source.load_source_tools(label=first_dir.name)
                second_tools = source.load_source_tools(label=second_dir.name)
                self.assertEqual(first_tools.read_file("First.java", 1, 1)["results"][0]["text"], "class First {}")
                self.assertEqual(second_tools.read_file("Second.java", 1, 1)["results"][0]["text"], "class Second {}")

    def test_prepare_from_directory_rejects_bad_commit_and_missing_dir(self):
        with tempfile.TemporaryDirectory() as source_name:
            source_dir = Path(source_name)
            (source_dir / "Safe.java").write_text("class Safe {}", encoding="utf-8")
            config = SimpleNamespace(name="sampleRepo", branch="main", local_path=self.root / "repo", url="https://example.invalid/r.git")
            with patch.object(source, "PROJECT_DIR", self.root), patch.object(source, "load_config", return_value=config):
                with self.assertRaises(ValueError):
                    source.prepare_sources_from_directory(source_dir, "not-a-commit")
                with self.assertRaises(FileNotFoundError):
                    source.prepare_sources_from_directory(source_dir / "missing", COMMIT)

    def test_prepare_from_directory_checks_for_links(self):
        with tempfile.TemporaryDirectory() as source_name:
            source_dir = Path(source_name)
            (source_dir / "Safe.java").write_text("class Safe {}", encoding="utf-8")
            config = SimpleNamespace(name="sampleRepo", branch="main", local_path=self.root / "repo", url="https://example.invalid/r.git")
            with patch.object(source, "PROJECT_DIR", self.root), patch.object(source, "load_config", return_value=config):
                with patch.object(source, "reject_links", side_effect=ValueError("link")):
                    with self.assertRaises(ValueError):
                        source.prepare_sources_from_directory(source_dir, COMMIT)


if __name__ == "__main__":
    unittest.main()
