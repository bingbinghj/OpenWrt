"""Execute failure paths with mock commands; never build or publish remotely."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or ("D:/Git/bin/bash.exe" if Path("D:/Git/bin/bash.exe").exists() else None)
BUILD = yaml.safe_load((ROOT / ".github/workflows/x86_64.yml").read_text(encoding="utf-8"))


def step_script(name):
    return next(step["run"] for step in BUILD["jobs"]["build"]["steps"] if step["name"] == name)


@unittest.skipUnless(BASH, "Bash is required for workflow execution tests")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for name in ("bin", "openwrt", "logs", "firmware"):
            (self.path / name).mkdir()
        self.env = dict(os.environ, GITHUB_WORKSPACE=self.path.as_posix(),
                        GITHUB_OUTPUT=(self.path / "output").as_posix(),
                        MAKE_CALLS=(self.path / "make-calls").as_posix(),
                        GH_CALLS=(self.path / "gh-calls").as_posix(),
                        GITHUB_REPOSITORY="test/repo", GITHUB_SHA="a" * 40,
                        GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", BUILD_TARGET="x86-64")
        # Bash receives PATH as a POSIX value, not Windows' semicolon-separated PATH.
        self.prefix = 'export PATH="$(pwd)/bin:$PATH"\n'

    def executable(self, name, content):
        path = self.path / "bin" / name
        path.write_text("#!/bin/bash\n" + content + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run_step(self, name):
        return subprocess.run([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                               self.prefix + step_script(name)], cwd=self.path, env=self.env,
                              text=True, encoding="utf-8", capture_output=True)

    def test_all_inline_shell_scripts_parse(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if "run" in step:
                        with self.subTest(path=path.name, step=step["name"]):
                            result = subprocess.run([BASH, "-n"], input=step["run"], text=True,
                                                    encoding="utf-8", capture_output=True)
                            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_compile_cannot_report_success_through_tee(self):
        self.executable("make", 'printf "%s\\n" "$*" >> "$MAKE_CALLS"\necho "simulated build failure"\nexit 17')
        result = self.run_step("Compile the firmware")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.path / "output").exists())
        calls = (self.path / "make-calls").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], "-j1 V=s")
        self.assertIn("simulated build failure", (self.path / "logs/build-retry.log").read_text())

    def test_successful_verbose_retry_marks_compile_success(self):
        self.executable("make", 'printf "%s\\n" "$*" >> "$MAKE_CALLS"\n[[ "$*" == "-j1 V=s" ]]')
        result = self.run_step("Compile the firmware")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.path / "output").read_text().strip(), "status=success")

    def test_failed_asset_upload_never_publishes_draft(self):
        self.executable("gh", 'printf "%s\\n" "$*" >> "$GH_CALLS"\nexit 23')
        (self.path / "firmware/test.img.gz").write_bytes(b"fixture")
        result = self.run_step("Publish complete firmware release")
        self.assertNotEqual(result.returncode, 0)
        calls = (self.path / "gh-calls").read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--draft", calls[0])
        self.assertFalse((self.path / "output").exists())

    def test_successful_upload_publishes_before_saving_tag(self):
        self.executable("gh", 'printf "%s\\n" "$*" >> "$GH_CALLS"')
        (self.path / "firmware/test.img.gz").write_bytes(b"fixture")
        result = self.run_step("Publish complete firmware release")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.path / "gh-calls").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("--draft=false", calls[1])
        self.assertTrue((self.path / "output").read_text().startswith("tag=immortalwrt-x86-64-"))

    def test_missing_image_is_rejected(self):
        directory = self.path / "openwrt/bin/targets/x86/64"
        directory.mkdir(parents=True)
        (directory / "profiles.json").write_text("{}")
        result = self.run_step("Prepare firmware artifacts")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.path / "output").exists())


if __name__ == "__main__":
    unittest.main()
