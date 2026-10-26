"""선택 커밋의 소스를 검사해 추출하고, 제한된 공통 탐색 도구를 제공한다."""

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
import uuid

from repository_sync import PROJECT_DIR, load_config, reject_links, run_git, validate_local_config


ALLOWED_EXTENSIONS = {".java", ".jsp", ".js", ".jsx", ".ts", ".tsx", ".xml", ".html", ".css"}
EXCLUDED_DIRECTORIES = {"node_modules", "target", "build", "dist", "vendor", "out"}
EXCLUDED_FILENAMES = {"globals.properties", "globals2.properties"}
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILES = 10000
MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_TREE_ENTRIES = 20000
MAX_RESPONSE_BYTES = 32 * 1024
MAX_SEARCH_RESULTS = 50
MAX_READ_LINES = 100
MAX_LIST_RESULTS = 100
MAX_TOOL_SECONDS = 5
MANIFEST_NAME = ".analysis_manifest.json"
SECRET_NAME = r"(?:[A-Za-z0-9_]*(?:password|passwd|pwd|db[_-]?pass|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|secret[_-]?key|token))"
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"jdbc:", re.IGNORECASE),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"\b" + SECRET_NAME + r"\b[\"']?\s*[:=]\s*[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"\bset(?:Password|ApiKey|ClientSecret|AccessToken)\s*\(\s*[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"<(?=[^>]*\b(?:name|key)\s*=\s*[\"']" + SECRET_NAME + r"[\"'])(?=[^>]*\bvalue\s*=)[^>]*>", re.IGNORECASE),
    re.compile(r"<" + SECRET_NAME + r"\b[^>]*>\s*[^<\s]+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{10,}", re.IGNORECASE),
]


def relative_path(value: str, allow_root: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or len(value) > 512 or not value:
        raise ValueError("512자 이내의 상대 경로가 필요합니다.")
    if any(ord(char) < 32 for char in value) or any(char in value for char in '\\:<>"|?*'):
        raise ValueError("허용되지 않는 경로 형식입니다.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("절대 경로와 상위 경로 이동은 허용하지 않습니다.")
    for part in path.parts:
        if part.startswith(".") or part.endswith((".", " ")):
            raise ValueError("숨김 경로 또는 모호한 파일 이름은 허용하지 않습니다.")
        if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.IGNORECASE):
            raise ValueError("운영체제 예약 파일 이름은 허용하지 않습니다.")
    if not path.parts and not allow_root:
        raise ValueError("파일 상대 경로가 필요합니다.")
    return path


def path_allowed(path: PurePosixPath) -> bool:
    return (
        path.name.casefold() not in EXCLUDED_FILENAMES
        and path.suffix.lower() in ALLOWED_EXTENSIONS
        and not any(part.lower() in EXCLUDED_DIRECTORIES for part in path.parts[:-1])
        and not re.search(r"credential|secret|password|keystore|truststore|datasource", path.name, re.IGNORECASE)
    )


def decode_source(data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file_size")
    if b"\x00" in data:
        raise ValueError("binary")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("encoding") from None
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise ValueError("binary")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise ValueError("sensitive_content")
    return text


def json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def prepare_sources() -> dict:
    """로컬 Fetch 결과만 사용한다. 네트워크 호출이나 Checkout은 하지 않는다."""
    config = load_config()
    validate_local_config(config.local_path, config.url)
    if run_git(["check-ref-format", "refs/heads/" + config.branch], allow_failure=True).returncode:
        raise ValueError("branch 설정이 올바르지 않습니다.")
    resolved = run_git(
        ["rev-parse", "--verify", "refs/remotes/origin/" + config.branch + "^{commit}"],
        repository=config.local_path, allow_failure=True,
    )
    if resolved.returncode:
        raise ValueError("분석할 브랜치/커밋이 없습니다. 먼저 저장소 Fetch와 branch 설정을 확인하세요.")
    commit = resolved.stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("분석 커밋 형식이 올바르지 않습니다.")
    tree = run_git(["ls-tree", "-r", "-l", "-z", commit], repository=config.local_path).stdout
    if len(tree) > MAX_TREE_BYTES:
        raise ValueError("저장소 파일 목록이 준비 단계의 크기 제한을 초과했습니다.")
    entries = tree.split(b"\0")
    if len(entries) - 1 > MAX_TREE_ENTRIES:
        raise ValueError("저장소 파일 수가 준비 단계의 검사 범위를 초과했습니다.")

    base = PROJECT_DIR / "analysis_sources" / config.name
    reject_links(base)
    base.mkdir(parents=True, exist_ok=True)
    # 준비 도중 실패해도 이전 분석 영역을 가리키는 current.json은 변경하지 않는다.
    snapshot_name = commit + "-" + uuid.uuid4().hex[:8]
    snapshot = base / snapshot_name
    snapshot.mkdir()
    accepted = []
    excluded = Counter()
    total_bytes = 0
    names = set()
    for entry in entries:
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id, size = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            excluded["non_regular_file"] += 1
            continue
        try:
            path = relative_path(raw_path.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            excluded["path"] += 1
            continue
        if not path_allowed(path):
            excluded["file_policy"] += 1
            continue
        if int(size) > MAX_FILE_BYTES:
            excluded["file_size"] += 1
            continue
        if len(accepted) >= MAX_FILES or total_bytes + int(size) > MAX_TOTAL_BYTES:
            raise ValueError("분석 영역 용량 제한을 초과했습니다. 현재 분석 영역은 갱신하지 않았습니다.")
        if path.as_posix().casefold() in names:
            raise ValueError("대소문자만 다른 파일 이름은 지원하지 않습니다.")
        blob_id = object_id.decode("ascii")
        if not re.fullmatch(r"[0-9a-f]{40}", blob_id):
            raise ValueError("Git 객체 식별자가 올바르지 않습니다.")
        data = run_git(["cat-file", "blob", blob_id], repository=config.local_path).stdout
        try:
            text = decode_source(data)
        except ValueError as error:
            excluded[str(error)] += 1
            continue
        output = text.encode("utf-8")
        destination = snapshot.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(output)
        accepted.append({"path": path.as_posix(), "sha256": hashlib.sha256(output).hexdigest()})
        names.add(path.as_posix().casefold())
        total_bytes += len(output)

    manifest = {
        "version": 1, "repository": config.name, "commit": commit,
        "files": accepted, "excluded_counts": dict(excluded), "total_bytes": total_bytes,
    }
    (snapshot / MANIFEST_NAME).write_text(json_text(manifest), encoding="utf-8")
    pointer = base / "current.json"
    temporary_pointer = base / ("current-" + uuid.uuid4().hex + ".tmp")
    reject_links(pointer)
    temporary_pointer.write_text(json_text({"snapshot": snapshot_name}), encoding="utf-8")
    temporary_pointer.replace(pointer)
    return {
        "repository": config.name, "commit": commit,
        "analysis_path": str(snapshot), "accepted_files": len(accepted),
        "excluded_counts": dict(excluded), "total_bytes": total_bytes,
    }


def prepare_sources_from_directory(source_dir: Path, commit: str) -> dict:
    """Gitea 인증 없이, 사용자가 직접 특정 커밋으로 checkout한 뒤 .git을 지운 로컬
    디렉터리를 prepare_sources()와 동일한 보안 필터(허용 확장자·민감정보 스캔·경로
    검증)로 검사해 분석 영역으로 추출한다. 과거 장애 재현 테스트처럼, 이 세션이 사내
    Git 자격증명을 다루지 않고도 특정 커밋 시점의 소스를 넣어볼 때 쓴다.

    commit은 git으로 재검증되지 않는다 — 사용자가 실제로 확인한 커밋 SHA를 그대로
    적어야 한다. prepare_sources()와 달리 git 명령어를 전혀 쓰지 않으므로, 핵심 로직
    (허용/제외 판단·추출·manifest 작성)을 의도적으로 중복시켰다 — 이미 테스트된
    prepare_sources()를 공용 헬퍼로 쪼개다가 깨뜨릴 위험을 피하기 위함이다."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit은 40자리 16진수 SHA여야 합니다.")
    config = load_config()
    reject_links(source_dir)
    root = source_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source_dir은 디렉터리여야 합니다.")

    entries = sorted(path for path in root.rglob("*") if path.is_file())
    if len(entries) > MAX_TREE_ENTRIES:
        raise ValueError("디렉터리 파일 수가 준비 단계의 검사 범위를 초과했습니다.")

    base = PROJECT_DIR / "analysis_sources" / config.name
    reject_links(base)
    base.mkdir(parents=True, exist_ok=True)
    snapshot_name = commit + "-" + uuid.uuid4().hex[:8]
    snapshot = base / snapshot_name
    snapshot.mkdir()
    accepted = []
    excluded = Counter()
    total_bytes = 0
    names = set()
    for file_path in entries:
        reject_links(file_path)
        try:
            path = relative_path(file_path.relative_to(root).as_posix())
        except ValueError:
            excluded["path"] += 1
            continue
        if not path_allowed(path):
            excluded["file_policy"] += 1
            continue
        size = file_path.stat().st_size
        if size > MAX_FILE_BYTES:
            excluded["file_size"] += 1
            continue
        if len(accepted) >= MAX_FILES or total_bytes + size > MAX_TOTAL_BYTES:
            raise ValueError("분석 영역 용량 제한을 초과했습니다. 현재 분석 영역은 갱신하지 않았습니다.")
        if path.as_posix().casefold() in names:
            raise ValueError("대소문자만 다른 파일 이름은 지원하지 않습니다.")
        try:
            text = decode_source(file_path.read_bytes())
        except ValueError as error:
            excluded[str(error)] += 1
            continue
        output = text.encode("utf-8")
        destination = snapshot.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(output)
        accepted.append({"path": path.as_posix(), "sha256": hashlib.sha256(output).hexdigest()})
        names.add(path.as_posix().casefold())
        total_bytes += len(output)

    manifest = {
        "version": 1, "repository": config.name, "commit": commit,
        "files": accepted, "excluded_counts": dict(excluded), "total_bytes": total_bytes,
    }
    (snapshot / MANIFEST_NAME).write_text(json_text(manifest), encoding="utf-8")
    pointer = base / "current.json"
    temporary_pointer = base / ("current-" + uuid.uuid4().hex + ".tmp")
    reject_links(pointer)
    temporary_pointer.write_text(json_text({"snapshot": snapshot_name}), encoding="utf-8")
    temporary_pointer.replace(pointer)
    return {
        "repository": config.name, "commit": commit,
        "analysis_path": str(snapshot), "accepted_files": len(accepted),
        "excluded_counts": dict(excluded), "total_bytes": total_bytes,
    }


class SourceTools:
    """Python이 고정한 분석 영역만 읽는다. 루트는 모델의 인자로 제공하지 않는다."""

    def __init__(self, root: Path):
        reject_links(root)
        reject_links(root / MANIFEST_NAME)
        self.root = root.resolve(strict=True)
        manifest_path = self.root / MANIFEST_NAME
        if manifest_path.stat().st_size > MAX_TREE_BYTES:
            raise ValueError("분석 영역의 파일 목록이 너무 큽니다.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("분석 영역 메타데이터는 JSON 객체여야 합니다.")
        if manifest.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", "")):
            raise ValueError("분석 영역의 메타데이터가 올바르지 않습니다.")
        self.repository = manifest["repository"]
        self.commit = manifest["commit"]
        if not isinstance(self.repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.repository):
            raise ValueError("분석 영역의 저장소 식별자가 올바르지 않습니다.")
        self.files = {}
        if not isinstance(manifest.get("files"), list) or len(manifest["files"]) > MAX_FILES:
            raise ValueError("분석 대상 파일 수가 올바르지 않습니다.")
        for item in manifest["files"]:
            path = relative_path(item["path"])
            if not path_allowed(path) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("분석 대상 파일 정보가 올바르지 않습니다.")
            if path.as_posix() in self.files:
                raise ValueError("분석 대상 파일이 중복되었습니다.")
            self.files[path.as_posix()] = item["sha256"]

    def _read(self, path: str) -> str:
        relative = relative_path(path)
        name = relative.as_posix()
        if not path_allowed(relative) or name not in self.files:
            raise ValueError("허용된 분석 파일이 아닙니다.")
        target = self.root.joinpath(*relative.parts)
        reject_links(target)
        if not target.resolve(strict=True).is_relative_to(self.root):
            raise ValueError("분석 영역 외부 접근은 허용하지 않습니다.")
        if not target.is_file():
            raise ValueError("일반 파일이 아닙니다.")
        with target.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
        text = decode_source(data)
        if hashlib.sha256(data).hexdigest() != self.files[name]:
            raise ValueError("분석 파일이 준비 후 변경되었습니다. 다시 준비하세요.")
        return text

    def _result(self) -> dict:
        return {"repository": self.repository, "commit": self.commit,
                "results": [], "truncated": False, "limit_reason": None}

    @staticmethod
    def _append(result: dict, item: dict, max_results: int) -> bool:
        if len(result["results"]) >= max_results:
            result.update(truncated=True, limit_reason="result_count")
            return False
        result["results"].append(item)
        # 제한 표시를 위한 여유를 두고 최종 JSON 응답의 바이트 크기를 제한한다.
        if len(json_text(result).encode("utf-8")) > MAX_RESPONSE_BYTES - 256:
            result["results"].pop()
            result.update(truncated=True, limit_reason="response_bytes")
            return False
        return True

    def search_code(self, keyword: str, path: str = ".") -> dict:
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 100 or any(ord(c) < 32 for c in keyword):
            raise ValueError("검색어는 비어 있지 않은 100자 이내 문자열이어야 합니다.")
        scope = relative_path(path, allow_root=True).as_posix()
        if scope != "." and not any(name == scope or name.startswith(scope + "/") for name in self.files):
            raise ValueError("허용된 검색 범위가 아닙니다.")
        result = self._result()
        started = time.monotonic()
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        scanned_bytes = 0
        for name in sorted(self.files):
            if scope != "." and name != scope and not name.startswith(scope + "/"):
                continue
            if time.monotonic() - started > MAX_TOOL_SECONDS:
                result.update(truncated=True, limit_reason="time")
                break
            text = self._read(name)
            scanned_bytes += len(text.encode("utf-8"))
            if scanned_bytes > MAX_TOTAL_BYTES:
                result.update(truncated=True, limit_reason="scan_bytes")
                break
            for number, line in enumerate(text.splitlines(), 1):
                match = pattern.search(line)
                if match is None:
                    continue
                position = match.start()
                start = max(0, position - 100)
                snippet = line[start:start + 300]
                item = {"path": name, "line": number, "text": snippet,
                        "snippet_truncated": start > 0 or start + len(snippet) < len(line)}
                if not self._append(result, item, MAX_SEARCH_RESULTS):
                    return result
        return result

    def read_file(self, path: str, start_line: int, end_line: int) -> dict:
        if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
            raise ValueError("행 번호는 1 이상의 정수이며 종료 행은 시작 행 이상이어야 합니다.")
        if end_line - start_line + 1 > MAX_READ_LINES:
            raise ValueError("한 번에 읽을 수 있는 범위는 100행까지입니다.")
        name = relative_path(path).as_posix()
        lines = self._read(name).splitlines()
        if start_line > len(lines):
            raise ValueError("시작 행이 파일 범위를 벗어났습니다.")
        result = self._result()
        for number in range(start_line, min(end_line, len(lines)) + 1):
            if not self._append(result, {"path": name, "line": number, "text": lines[number - 1]}, MAX_READ_LINES):
                break
        return result

    def list_files(self, path: str = ".") -> dict:
        directory = relative_path(path, allow_root=True)
        result = self._result()
        entries = {}
        started = time.monotonic()
        scanned_bytes = 0
        for name in sorted(self.files):
            parts = PurePosixPath(name).parts
            if parts[:len(directory.parts)] != directory.parts:
                continue
            remaining = parts[len(directory.parts):]
            if not remaining:
                raise ValueError("디렉터리를 지정하세요.")
            if time.monotonic() - started > MAX_TOOL_SECONDS:
                result.update(truncated=True, limit_reason="time")
                break
            # 목록도 내용 검사를 통과한 파일만 대상으로 한다.
            text = self._read(name)
            scanned_bytes += len(text.encode("utf-8"))
            if scanned_bytes > MAX_TOTAL_BYTES:
                result.update(truncated=True, limit_reason="scan_bytes")
                break
            child = directory / remaining[0]
            entries[child.as_posix()] = "directory" if len(remaining) > 1 else "file"
        if directory.parts and not entries:
            raise ValueError("허용된 분석 디렉터리가 없습니다.")
        for name, kind in sorted(entries.items()):
            if not self._append(result, {"path": name, "kind": kind}, MAX_LIST_RESULTS):
                break
        return result


def load_source_tools() -> SourceTools:
    config = load_config()
    base = PROJECT_DIR / "analysis_sources" / config.name
    pointer = base / "current.json"
    reject_links(pointer)
    if not pointer.is_file():
        raise ValueError("먼저 --prepare-sources를 실행하세요.")
    if pointer.stat().st_size > 4096:
        raise ValueError("분석 영역 참조 정보가 너무 큽니다.")
    name = json.loads(pointer.read_text(encoding="utf-8"))["snapshot"]
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{8}", name):
        raise ValueError("분석 영역 참조 정보가 올바르지 않습니다.")
    tools = SourceTools(base / name)
    if tools.repository != config.name or not name.startswith(tools.commit + "-"):
        raise ValueError("분석 영역과 저장소 식별 정보가 일치하지 않습니다.")
    return tools
