"""Fixed Docker test recipes. MR code never receives host credentials or Docker socket."""
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree

from .core import BotError


IMAGES = {"gateway": "maven:3.9.9-eclipse-temurin-17",
          "auth-service": "maven:3.9.9-eclipse-temurin-17",
          "server": "maven:3.9.6-eclipse-temurin-17"}
DB_IMAGE = "postgres:16"
REDIS_IMAGE = "valkey/valkey:8"


def prepare_cache(source, destination):
    """Copy artifacts only, never settings.xml, credentials or local reactor snapshots."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    count = 0
    for artifact in source.rglob("*"):
        if (not artifact.is_file() or artifact.is_symlink() or artifact.suffix not in (".jar", ".pom")
                or "SNAPSHOT" in str(artifact.relative_to(source))):
            continue
        target = destination / artifact.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != artifact.stat().st_size:
            shutil.copyfile(str(artifact), str(target))
        count += 1
    if not count:
        raise BotError("MAVEN_ARTIFACT_CACHE_EMPTY")
    (destination / "cache-ready.json").write_text(json.dumps({"artifacts": count, "created": time.time()}), encoding="utf-8")
    return count


def summarize_reports(raw):
    totals = {kind: {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "reports": 0}
              for kind in ("unit", "integration")}
    failures = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            name = member.name
            if not member.isfile() or member.size > 5 * 1024 * 1024 or not name.endswith(".xml"):
                continue
            kind = ("unit" if "/surefire-reports/TEST-" in name else
                    "integration" if "/failsafe-reports/TEST-" in name else None)
            if kind is None:
                continue
            root = ElementTree.fromstring(archive.extractfile(member).read())
            if root.tag != "testsuite":
                continue
            totals[kind]["reports"] += 1
            for key in ("tests", "failures", "errors", "skipped"):
                totals[kind][key] += int(root.get(key, "0"))
            for test in root.findall("testcase"):
                if test.find("failure") is not None or test.find("error") is not None:
                    failures.append({"kind": kind, "class": test.get("classname"), "test": test.get("name")})
    return totals, failures


def docker(args, timeout=120, log=None):
    try:
        with (open(str(log), "wb") if log else open(os.devnull, "wb")) as output:
            result = subprocess.run(["docker"] + list(args), stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
        return result.returncode
    except subprocess.TimeoutExpired:
        raise BotError("DOCKER_TEST_TIMEOUT") from None
    except OSError:
        raise BotError("DOCKER_UNAVAILABLE") from None


class TestRunner:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, folder, project):
        folder = Path(folder).resolve()
        destination = folder / "test-results"
        destination.mkdir()
        result = {"status": "NOT_RUN", "platform": "Linux", "java": 17,
                  "nativeWindows": "NOT_RUN", "nativeCryptoPro": "NOT_RUN",
                  "source": "head (pinned MR commit)", "unit": {}, "integration": {}}
        prefix = "mr-tests-" + uuid.uuid4().hex[:16]
        names = {key: prefix + "-" + key for key in ("volume", "network", "outbound", "proxy", "db", "redis", "runner")}
        try:
            image = IMAGES.get(project)
            if not image:
                raise BotError("TEST_RECIPE_NOT_CONFIGURED")
            result["image"] = image
            cache = Path(self.cfg["test_artifact_cache"]).resolve()
            if not (cache / "cache-ready.json").is_file():
                raise BotError("PREPARE_TEST_ARTIFACT_CACHE_FIRST")
            if docker(["info"], log=destination / "docker-info.log"):
                raise BotError("DOCKER_UNAVAILABLE")
            for command in (["volume", "create", names["volume"]],
                            ["network", "create", "--internal", names["network"]],
                            ["network", "create", names["outbound"]]):
                if docker(command):
                    raise BotError("DOCKER_TEST_SETUP_FAILED")
            init = ["run", "--rm", "--network", "none", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                    "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", "128",
                    "--mount", "type=bind,src={},dst=/source,readonly".format(folder / "head"),
                    "--mount", "type=bind,src={},dst=/cache,readonly".format(cache),
                    "--mount", "type=volume,src={},dst=/work".format(names["volume"]),
                    "--entrypoint", "sh", image, "-c",
                    "mkdir -p /work/project /work/m2 /work/home; cp -R /source/. /work/project/; "
                    "cp -R /cache/. /work/m2/; chown -R 10001:10001 /work"]
            if docker(init, timeout=300, log=destination / "prepare.log"):
                raise BotError("DOCKER_TEST_COPY_FAILED")
            scripts = Path(__file__).resolve().parent
            proxy = ["create", "--name", names["proxy"], "--network", names["outbound"],
                     "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     "--read-only", "--pids-limit", "64", "--memory", "256m",
                     "--mount", "type=bind,src={},dst=/artifact_proxy.py,readonly".format(scripts / "artifact_proxy.py"),
                     "python:3.12-slim", "python", "-B", "/artifact_proxy.py"]
            for command in (proxy, ["network", "connect", "--alias", "maven-artifacts", names["network"], names["proxy"]],
                            ["start", names["proxy"]]):
                if docker(command):
                    raise BotError("MAVEN_ARTIFACT_PROXY_START_FAILED")
            integration = project in ("server", "auth-service")
            database = "edo_auth_it_review" if project == "auth-service" else "edo_gateway_registry_it_review"
            if integration:
                db = ["run", "-d", "--name", names["db"], "--network", names["network"],
                      "--user", "999:999", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                      "--read-only", "--tmpfs", "/var/lib/postgresql/data:uid=999,gid=999,mode=0700",
                      "--tmpfs", "/var/run/postgresql:uid=999,gid=999,mode=0770", "--tmpfs", "/tmp",
                      "-e", "POSTGRES_DB=" + database, "-e", "POSTGRES_USER=review_it",
                      "-e", "POSTGRES_PASSWORD=disposable-review-password", DB_IMAGE]
                if docker(db):
                    raise BotError("DISPOSABLE_POSTGRES_START_FAILED")
                ready = False
                for _ in range(30):
                    if docker(["exec", names["db"], "pg_isready", "-U", "review_it", "-d", database]) == 0:
                        ready = True
                        break
                    time.sleep(1)
                if not ready:
                    raise BotError("DISPOSABLE_POSTGRES_NOT_READY")
                if docker(["run", "-d", "--name", names["redis"], "--network", "container:" + names["db"],
                           "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                           "--read-only", "--tmpfs", "/data:uid=10001,gid=10001", REDIS_IMAGE,
                           "valkey-server", "--save", "", "--appendonly", "no"]):
                    raise BotError("DISPOSABLE_VALKEY_START_FAILED")
            # Fixed script lives outside the MR tree. MR may influence Maven/tests, not Docker options.
            script = Path(__file__).resolve().parent / "run-maven-tests.sh"
            args = ["create", "--name", names["runner"], "--network",
                    "container:" + names["db"] if integration else names["network"],
                    "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--read-only", "--pids-limit", "1024", "--memory", "4g", "--cpus", "2",
                    "--tmpfs", "/tmp:exec,size=512m", "--workdir", "/work/project",
                    "--mount", "type=volume,src={},dst=/work".format(names["volume"]),
                    "--mount", "type=bind,src={},dst=/runner.sh,readonly".format(script),
                    "--mount", "type=bind,src={},dst=/runner-settings.xml,readonly".format(scripts / "test-maven-settings.xml"),
                    "-e", "HOME=/work/home", "-e", "MAVEN_CONFIG=/work/home/.m2",
                    "-e", "JAVA_TOOL_OPTIONS=-Duser.home=/work/home -Djava.util.prefs.userRoot=/work/home/.java/.userPrefs",
                    "-e", "MAVEN_OPTS=-Xmx1536m", "-e", "RUN_INTEGRATION=" + ("true" if integration else "false")]
            if integration:
                for variable in ("TEST_DB_URL=jdbc:postgresql://127.0.0.1:5432/" + database, "TEST_DB_NAME=" + database,
                                 "TEST_DB_USERNAME=review_it", "TEST_DB_PASSWORD=disposable-review-password",
                                 "TEST_DB_DISPOSABLE=true", "TEST_LK_REDIS_PORT=6379"):
                    args += ["-e", variable]
            else:
                # With --network none Docker provides no hostname address automatically.
                # Root locking legitimately needs a resolvable local hostname.
                args += ["--hostname", "review-tests", "--add-host", "review-tests:127.0.0.1"]
            args += ["--entrypoint", "sh", image, "/runner.sh"]
            if docker(args):
                raise BotError("DOCKER_TEST_RUNNER_CREATE_FAILED")
            result["containerExitCode"] = docker(["start", "-a", names["runner"]],
                timeout=self.cfg.get("test_timeout_seconds", 1800), log=destination / "maven.log")
            # Stream a single archive as data; never follow links or extract arbitrary container paths.
            copied = subprocess.run(["docker", "cp", names["runner"] + ":/work/results.tar", "-"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
            if copied.returncode or len(copied.stdout) > 60 * 1024 * 1024:
                raise BotError("TEST_RESULTS_UNAVAILABLE")
            with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as outer:
                member = next((m for m in outer if m.isfile() and m.name == "results.tar" and m.size <= 50 * 1024 * 1024), None)
                if member is None:
                    raise BotError("TEST_RESULTS_INVALID")
                raw = outer.extractfile(member).read()
            totals, failures = summarize_reports(raw)
            result.update(totals)
            result["failedTests"] = failures
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                member = archive.getmember("exit-codes.txt")
                values = dict(line.split("=", 1) for line in archive.extractfile(member).read().decode().splitlines())
            result["buildExitCode"] = int(values["build"])
            result["integrationExitCode"] = int(values["integration"]) if integration else None
            if failures:
                result["status"] = "FAILED"
            elif result["buildExitCode"] or (integration and result["integrationExitCode"]):
                result["status"] = "BUILD_OR_INFRA_FAILURE"
            elif not totals["unit"]["tests"] or (integration and not totals["integration"]["tests"]):
                result["status"] = "INCOMPLETE"
            else:
                result["status"] = "PASSED_WITH_SKIPS" if any(t["skipped"] for t in totals.values()) else "PASSED"
        except Exception as failure:
            result["status"] = "NOT_COMPLETED"
            result["reason"] = str(failure) if isinstance(failure, BotError) else "TEST_RUNNER_FAILURE"
        finally:
            for key in ("runner", "redis", "db", "proxy"):
                try:
                    docker(["rm", "-f", names[key]])
                except BotError:
                    pass
            for kind, key in (("volume", "volume"), ("network", "network"), ("network", "outbound")):
                try:
                    docker([kind, "rm", names[key]])
                except BotError:
                    pass
        (destination / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
