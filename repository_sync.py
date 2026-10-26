"""분석 저장소 연결. 개인 Git 설정을 읽지 않고 익명 HTTPS로만 접근한다."""

import configparser
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RepositoryConfig:
    service: str
    name: str
    url: str
    local_path: Path
    branch: str


@dataclass(frozen=True)
class RepositoryState:
    path: Path
    branch: str
    commit: str | None


def reject_links(path: Path) -> None:
    for candidate in [path, *path.parents]:
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("저장소 경로에 링크 또는 정션을 사용할 수 없습니다.")


def load_config() -> RepositoryConfig:
    try:
        data = json.loads((PROJECT_DIR / "repository_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("repository_config.json을 읽을 수 없습니다. 파일과 JSON 형식을 확인하세요.") from None
    if not isinstance(data, dict):
        raise ValueError("저장소 설정은 JSON 객체여야 합니다.")
    for key in ["service", "name", "url", "local_path", "branch", "auth_mode"]:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"저장소 설정의 {key} 문자열이 필요합니다.")
    if data["auth_mode"] != "anonymous":
        raise ValueError("현재는 anonymous 인증 방식만 지원합니다.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", data["name"]):
        raise ValueError("저장소 name은 영문, 숫자, 밑줄, 하이픈으로 지정하세요.")
    url = data["url"]
    try:
        remote = urlsplit(url)
        valid_url = (
            remote.scheme == "https" and remote.hostname and remote.path not in ("", "/")
            and not remote.username and not remote.password
            and not remote.query and not remote.fragment
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
        remote.port  # 잘못된 포트 형식 검증
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("인증정보가 포함되지 않은 HTTPS 저장소 URL이 필요합니다.")
    relative = PurePosixPath(data["local_path"])
    if (relative.is_absolute() or ".." in relative.parts or "\\" in data["local_path"]
            or ":" in data["local_path"] or len(relative.parts) < 2
            or relative.parts[0] != "repositories"):
        raise ValueError("local_path는 repositories 아래의 상대 경로로 지정하세요.")
    target = PROJECT_DIR.joinpath(*relative.parts)
    reject_links(target)
    return RepositoryConfig(data["service"], data["name"], url, target, data["branch"])


def validate_local_config(path: Path, expected_url: str) -> None:
    """기존 저장소 설정으로 인증·외부 실행·경로 우회가 유입되는 것을 막는다."""
    reject_links(path / ".git")
    reject_links(path / ".git/config")
    reject_links(path / ".git/objects")
    reject_links(path / ".git/refs")
    if not (path / ".git").is_dir():
        raise ValueError("대상 경로는 일반 Clone 저장소가 아닙니다. 덮어쓰지 않습니다.")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string((path / ".git/config").read_text(encoding="utf-8"))
    except (OSError, ValueError, configparser.Error):
        raise ValueError("로컬 Git 설정을 안전하게 읽을 수 없습니다.") from None
    allowed_core = {
        "repositoryformatversion": {"0"}, "filemode": {"true", "false"},
        "bare": {"false"}, "logallrefupdates": {"true", "false"},
        "symlinks": {"true", "false"}, "ignorecase": {"true", "false"},
    }
    if parser.defaults():
        raise ValueError("익명 모드에서 허용하지 않는 Git 설정이 있습니다.")
    for section in parser.sections():
        values = dict(parser[section])
        if section == "core":
            valid = all(key in allowed_core and value.lower() in allowed_core[key]
                        for key, value in values.items())
        elif section == 'remote "origin"':
            valid = values == {"url": expected_url, "fetch": "+refs/heads/*:refs/remotes/origin/*"}
        elif re.fullmatch(r'branch "[^"\r\n]+"', section):
            valid = (set(values) == {"remote", "merge"} and values["remote"] == "origin"
                     and values["merge"].startswith("refs/heads/"))
        else:
            valid = False
        if not valid:
            raise ValueError("익명 모드에서 허용하지 않는 로컬 Git 설정 또는 origin 불일치가 있습니다.")
    if not parser.has_section('remote "origin"'):
        raise ValueError("로컬 origin 설정이 없습니다.")
    if any((path / ".git/objects/info" / name).exists()
           for name in ["alternates", "http-alternates"]):
        raise ValueError("외부 Git 객체 저장소 연결은 허용하지 않습니다.")


def run_git(arguments: list[str], repository: Path | None = None,
            allow_failure: bool = False) -> subprocess.CompletedProcess:
    # 부모 환경의 Git 옵션, AskPass, HOME의 인증 파일을 사용하지 않는다.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("GIT_", "GCM_"))
    }
    with tempfile.TemporaryDirectory(prefix="its-git-") as isolated:
        environment.update({
            "HOME": isolated, "USERPROFILE": isolated, "XDG_CONFIG_HOME": isolated,
            "CURL_HOME": isolated,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_NO_REPLACE_OBJECTS": "1",
        })
        command = [
            "git", "-c", "credential.helper=", "-c", "core.askPass=",
            "-c", "http.extraHeader=", "-c", "http.followRedirects=false",
            "-c", "http.sslVerify=true",
            "-c", "core.hooksPath=" + isolated,
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0",
        ]
        if os.name == "nt":
            # Windows 샌드박스의 Schannel 자격 증명 핸들 오류를 피한다.
            command.extend(["-c", "http.sslBackend=openssl"])
        if repository is not None:
            command.extend(["--git-dir=" + str(repository / ".git")])
        try:
            result = subprocess.run(
                [*command, *arguments], cwd=isolated, env=environment,
                capture_output=True, timeout=60, check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("Git 실행 파일을 찾을 수 없습니다.") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError("Git 작업 시간이 60초를 초과했습니다.") from None
    if result.returncode and not allow_failure:
        # 원문 stderr에는 서버/인증정보가 포함될 수 있으므로 출력하지 않는다.
        raise RuntimeError("Git 작업 실패: 익명 접근 허용 여부, 네트워크, 인증서 또는 저장소 상태를 확인하세요.")
    return result


def sync_repository() -> RepositoryState:
    config = load_config()
    branch_check = run_git(["check-ref-format", "refs/heads/" + config.branch], allow_failure=True)
    if branch_check.returncode:
        raise ValueError("branch 설정이 올바른 Git 브랜치 이름이 아닙니다.")
    if config.local_path.exists():
        validate_local_config(config.local_path, config.url)
    # 항상 URL로 직접 조회한다. 리다이렉트와 기존 PC 인증은 허용하지 않는다.
    remote = run_git(["ls-remote", "--heads", "--", config.url])
    remote_refs = remote.stdout.decode("utf-8", errors="strict").splitlines()
    selected_ref = "refs/heads/" + config.branch
    selected = [line.split("\t", 1)[0] for line in remote_refs
                if line.endswith("\t" + selected_ref)]
    if remote_refs and not selected:
        raise ValueError("설정한 브랜치가 원격 저장소에 없습니다. branch 설정을 확인하세요.")
    print("익명 HTTPS 저장소 조회 성공 (개인 Git 인증 설정 제외).")
    if not config.local_path.exists():
        config.local_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(["clone", "--no-checkout", "--template=", "--", config.url, str(config.local_path)])
        print("Clone 완료. 원본 작업 파일은 Checkout하지 않습니다.")
        validate_local_config(config.local_path, config.url)
    else:
        run_git([
            "fetch", "--no-tags", "--no-recurse-submodules", "--prune", "--", config.url,
            "+refs/heads/*:refs/remotes/origin/*",
        ], repository=config.local_path)
        print("Fetch 완료. 기존 작업 파일은 변경하지 않습니다.")
    print(f"로컬 경로: {config.local_path}")
    if not remote_refs:
        print("원격 브랜치가 없습니다. 연결은 성공했지만 분석할 커밋은 없습니다.")
        return RepositoryState(config.local_path, config.branch, None)
    result = run_git(["rev-parse", "--verify", "refs/remotes/origin/" + config.branch + "^{commit}"],
                     repository=config.local_path)
    commit = result.stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("분석 커밋을 확인할 수 없습니다.")
    print(f"분석 기준: {config.branch} / {commit}")
    return RepositoryState(config.local_path, config.branch, commit)
