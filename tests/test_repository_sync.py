import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import repository_sync as repo


class RepositorySyncTests(unittest.TestCase):
    def test_personal_git_environment_is_not_inherited(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.dict(repo.os.environ, {
            "GIT_ASKPASS": "personal-helper", "GIT_CONFIG_PARAMETERS": "private-options",
            "GIT_TRACE": "1", "GCM_CREDENTIAL_STORE": "personal-store",
        }), patch.object(repo.subprocess, "run", return_value=completed) as call:
            repo.run_git(["ls-remote", "https://example.invalid/test.git"])
        environment = call.call_args.kwargs["env"]
        for key in ["GIT_ASKPASS", "GIT_CONFIG_PARAMETERS", "GIT_TRACE", "GCM_CREDENTIAL_STORE"]:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], repo.os.devnull)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        command = call.call_args.args[0]
        self.assertIn("credential.helper=", command)
        self.assertIn("http.extraHeader=", command)
        self.assertIn("http.sslVerify=true", command)

    def test_config_rejects_credentials_and_path_escape(self):
        base = {
            "service": "WT", "name": "sampleRepo", "url": "https://example.invalid/r.git",
            "local_path": "repositories/sampleRepo", "branch": "main", "auth_mode": "anonymous",
        }
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with patch.object(repo, "PROJECT_DIR", project):
                for update in [
                    {"url": "https://user:secret@example.invalid/r.git"},
                    {"local_path": "repositories/../../outside"},
                    {"local_path": "C:/outside"},
                    {"auth_mode": "personal"},
                ]:
                    (project / "repository_config.json").write_text(json.dumps(base | update), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        repo.load_config()

    def test_local_credential_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / ".git").mkdir()
            config = '[core]\n bare = false\n[remote "origin"]\n url = https://example.invalid/r.git\n fetch = +refs/heads/*:refs/remotes/origin/*\n'
            (target / ".git/config").write_text(config, encoding="utf-8")
            repo.validate_local_config(target, "https://example.invalid/r.git")
            for extra in ['[credential]\n helper = manager\n', '[include]\n path = private.config\n', '[http]\n extraHeader = Authorization: secret\n']:
                (target / ".git/config").write_text(config + extra, encoding="utf-8")
                with self.assertRaises(ValueError):
                    repo.validate_local_config(target, "https://example.invalid/r.git")

    def test_git_error_does_not_expose_raw_message(self):
        result = subprocess.CompletedProcess([], 128, stdout=b"", stderr=b"secret-token")
        with patch.object(repo.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                repo.run_git(["ls-remote", "https://example.invalid/r.git"])
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
