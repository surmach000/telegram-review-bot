import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reviewbot.core import BotError
from reviewbot.artifact_proxy import allowed_path
from reviewbot.test_runner import TestRunner, prepare_cache, summarize_reports


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "head").mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        (self.cache / "cache-ready.json").write_text("{}")
        self.cfg = {"test_artifact_cache": str(self.cache), "test_timeout_seconds": 30}

    def payload(self, failed=False, build=0, integration=0, no_tests=False):
        files = {"exit-codes.txt": "build={}\nintegration={}\n".format(build, integration)}
        if not no_tests:
            files["./target/surefire-reports/TEST-Example.xml"] = (
                '<testsuite tests="2" failures="{}" errors="0" skipped="0">'.format(int(failed))
                + '<testcase classname="Example" name="testExample">' + ('<failure>failure</failure>' if failed else '')
                + '</testcase></testsuite>')
            files["./target/failsafe-reports/TEST-ExampleIT.xml"] = '<testsuite tests="1" failures="0" errors="0" skipped="0"/>'
        return subprocess.CompletedProcess([], 0, stdout=archive({"results.tar": archive(files)}))

    def test_real_counts_and_it_are_reported_without_host_secret_mounts(self):
        with patch("reviewbot.test_runner.docker", return_value=0) as docker, \
                patch("reviewbot.test_runner.subprocess.run", return_value=self.payload()):
            result = TestRunner(self.cfg).run(self.root, "server")
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["unit"]["tests"], 2)
        self.assertEqual(result["integration"]["tests"], 1)
        self.assertEqual(result["image"], "maven:3.9.6-eclipse-temurin-17")
        args = next(c.args[0] for c in docker.call_args_list if c.args[0][0] == "create" and "/runner.sh" in c.args[0])
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges", args)
        self.assertIn("10001:10001", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertNotIn("REVIEW_TELEGRAM_TOKEN", " ".join(args))
        self.assertNotIn("REVIEW_GITLAB_TOKEN", " ".join(args))
        self.assertIn("--internal", next(c.args[0] for c in docker.call_args_list if c.args[0][:2] == ["network", "create"]))
        self.assertIn("TEST_DB_NAME=edo_gateway_registry_it_review", args)

    def test_failures_are_not_converted_to_success(self):
        with patch("reviewbot.test_runner.docker", return_value=0), \
                patch("reviewbot.test_runner.subprocess.run", return_value=self.payload(failed=True, build=1)):
            result = TestRunner(self.cfg).run(self.root, "server")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["failedTests"][0]["test"], "testExample")

    def test_no_tests_never_means_pass(self):
        with patch("reviewbot.test_runner.docker", return_value=0), \
                patch("reviewbot.test_runner.subprocess.run", return_value=self.payload(no_tests=True)):
            result = TestRunner(self.cfg).run(self.root, "auth-service")
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_dependency_or_build_failure_is_distinguished(self):
        with patch("reviewbot.test_runner.docker", return_value=0), \
                patch("reviewbot.test_runner.subprocess.run", return_value=self.payload(no_tests=True, build=1, integration=1)):
            result = TestRunner(self.cfg).run(self.root, "auth-service")
        self.assertEqual(result["status"], "BUILD_OR_INFRA_FAILURE")

    def test_timeout_stops_container_and_cleans_resources(self):
        def command(args, **kwargs):
            if args[0] == "start":
                raise BotError("DOCKER_TEST_TIMEOUT")
            return 0
        with patch("reviewbot.test_runner.docker", side_effect=command) as docker:
            result = TestRunner(self.cfg).run(self.root, "gateway")
        self.assertEqual(result["reason"], "DOCKER_TEST_TIMEOUT")
        self.assertTrue(any(c.args[0][:2] == ["rm", "-f"] for c in docker.call_args_list))
        self.assertTrue(any(c.args[0][:2] == ["volume", "rm"] for c in docker.call_args_list))

    def test_gateway_hostname_resolves_locally_without_enabling_network(self):
        with patch("reviewbot.test_runner.docker", return_value=0) as docker, \
                patch("reviewbot.test_runner.subprocess.run", return_value=self.payload()):
            TestRunner(self.cfg).run(self.root, "gateway")
        args = next(c.args[0] for c in docker.call_args_list if c.args[0][0] == "create" and "/runner.sh" in c.args[0])
        self.assertTrue(args[args.index("--network") + 1].endswith("-network"))
        self.assertIn("review-tests:127.0.0.1", args)

    def test_cache_has_no_settings_or_snapshots(self):
        source, target = self.root / "m2", self.root / "artifacts"
        source.mkdir()
        (source / "settings.xml").write_text("secret")
        (source / "library.jar").write_bytes(b"jar")
        (source / "library.pom").write_text("pom")
        snapshot = source / "1.0-SNAPSHOT"
        snapshot.mkdir()
        (snapshot / "snapshot.jar").write_bytes(b"jar")
        self.assertEqual(prepare_cache(source, target), 2)
        self.assertFalse((target / "settings.xml").exists())
        self.assertFalse((target / "1.0-SNAPSHOT").exists())

    def test_artifact_proxy_restricts_requests_to_maven_paths_and_file_types(self):
        self.assertTrue(allowed_path("/maven2/org/example/library/1.0/library-1.0.pom"))
        self.assertTrue(allowed_path("/maven2/org/example/library/maven-metadata.xml.sha1"))
        for path in ("https://evil.example/a.jar", "/maven2/../../secret.jar", "/maven2/a.jar?token=secret",
                     "/maven2/%2e%2e/secret.jar", "/maven2/a.exe", "/maven2//evil.jar", "/other/a.jar"):
            self.assertFalse(allowed_path(path), path)

    def test_report_parser_never_extracts_arbitrary_archive_paths(self):
        data = archive({"../../outside.txt": "malicious", "target/surefire-reports/TEST-X.xml":
                        '<testsuite tests="1" failures="0" errors="0" skipped="1"/>'})
        totals, failures = summarize_reports(data)
        self.assertEqual(totals["unit"]["skipped"], 1)
        self.assertEqual(failures, [])
        self.assertFalse((self.root / "outside.txt").exists())
