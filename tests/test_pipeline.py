import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "shipment-intake": {
        "path": "services/shipment_intake/ci_touch.txt",
        "image": "logistics-shipment-intake",
    },
    "carrier-sync": {
        "path": "services/carrier_sync/ci_touch.txt",
        "image": "logistics-carrier-sync",
    },
    "label-generation": {
        "path": "services/label_generation/ci_touch.txt",
        "image": "logistics-label-generation",
    },
    "customer-notification": {
        "path": "services/customer_notification/ci_touch.txt",
        "image": "logistics-customer-notification",
    },
}


def run_cmd(args, check=True, **kwargs):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, **kwargs)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


@pytest.fixture(autouse=True)
def clean_runner_and_docker_state():
    for d in (".ci_runs", ".ci_cache", ".ci_artifacts"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    run_cmd(["docker", "compose", "down", "--remove-orphans"], check=False)
    yield
    run_cmd(["docker", "compose", "down", "--remove-orphans"], check=False)
    for d in (".ci_runs", ".ci_cache", ".ci_artifacts"):
        shutil.rmtree(ROOT / d, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def ensure_git_repo_ready():
    run_cmd(["git", "config", "user.email", "ci-grader@example.invalid"], check=False)
    run_cmd(["git", "config", "user.name", "CI Grader"], check=False)
    if run_cmd(["git", "rev-parse", "--is-inside-work-tree"], check=False).returncode != 0:
        run_cmd(["git", "init"])
    if run_cmd(["git", "rev-parse", "HEAD"], check=False).returncode != 0:
        run_cmd(["git", "add", "."])
        run_cmd(["git", "commit", "-m", "initial assessment baseline"])


def commit_change(relative_path, text):
    path = ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    run_cmd(["git", "add", relative_path])
    run_cmd(["git", "commit", "-m", f"grader change {relative_path}"])
    return run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()


def run_pipeline(event="push", ref="refs/heads/main", base_ref="main"):
    args = [sys.executable, "ci_runner.py", "--event", event]
    if event == "push":
        args += ["--ref", ref]
    if event == "pull_request":
        args += ["--base-ref", base_ref]
    result = run_cmd(args, check=False)
    report_path = ROOT / ".ci_runs" / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return result, report


def image_exists(image, sha):
    return run_cmd(["docker", "image", "inspect", f"{image}:{sha}"], check=False).returncode == 0


def remove_images_for_sha(sha):
    for spec in SERVICES.values():
        run_cmd(["docker", "image", "rm", "-f", f"{spec['image']}:{sha}"], check=False)


def assert_runner_succeeded(result, report):
    assert result.returncode == 0, result.stdout + result.stderr
    assert report is not None, "runner did not produce a report"
    assert report.get("triggered") is True, "workflow did not trigger for the simulated event"


def jobs_for_service(report, service):
    matches = []
    normalized = service.replace("-", "_")
    for job in report.get("jobs", []):
        haystack = " ".join([
            str(job.get("job", "")),
            str(job.get("label", "")),
            json.dumps(job.get("matrix", {}), sort_keys=True),
            json.dumps(job.get("steps", []), sort_keys=True),
        ]).lower()
        if service in haystack or normalized in haystack:
            if job.get("started_at") and job.get("finished_at") and job.get("status") == "success":
                matches.append(job)
    return matches


def windows_overlap(a, b):
    return max(a["started_at"], b["started_at"]) < min(a["finished_at"], b["finished_at"])


def test_single_service_change_builds_exactly_one_sha_tagged_image():
    sha = commit_change(SERVICES["shipment-intake"]["path"], f"single {os.urandom(4).hex()}")
    result, report = run_pipeline()
    try:
        assert_runner_succeeded(result, report)
        assert image_exists("logistics-shipment-intake", sha), "no new image was built for the changed service"
        for service, spec in SERVICES.items():
            if service != "shipment-intake":
                assert not image_exists(spec["image"], sha), f"unchanged service {service} received an image for this commit"
    finally:
        remove_images_for_sha(sha)


def test_multi_service_change_builds_images_with_real_parallel_overlap():
    token = os.urandom(4).hex()
    for service in ("carrier-sync", "label-generation"):
        path = ROOT / SERVICES[service]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.read_text() + f"multi {token}\n" if path.exists() else f"multi {token}\n")
        run_cmd(["git", "add", SERVICES[service]["path"]])
    run_cmd(["git", "commit", "-m", "grader multi service change"])
    sha = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
    result, report = run_pipeline()
    try:
        assert_runner_succeeded(result, report)
        assert image_exists("logistics-carrier-sync", sha), "changed carrier-sync service did not produce a SHA-tagged image"
        assert image_exists("logistics-label-generation", sha), "changed label-generation service did not produce a SHA-tagged image"
        first_jobs = jobs_for_service(report, "carrier-sync")
        second_jobs = jobs_for_service(report, "label-generation")
        assert first_jobs, "could not find an executed build job for carrier-sync in the report"
        assert second_jobs, "could not find an executed build job for label-generation in the report"
        assert any(windows_overlap(a, b) for a in first_jobs for b in second_jobs), "changed services built one after another instead of overlapping in wall-clock time"
    finally:
        remove_images_for_sha(sha)


def test_noop_change_outside_services_and_shared_builds_zero_images():
    sha = commit_change("docs/pipeline-note.txt", f"docs only {os.urandom(4).hex()}")
    result, report = run_pipeline()
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        if report is not None:
            for spec in SERVICES.values():
                assert not image_exists(spec["image"], sha), "an unrelated change produced a service image"
    finally:
        remove_images_for_sha(sha)


def test_shared_dependency_change_rebuilds_every_service():
    sha = commit_change("shared/ci_touch.txt", f"shared {os.urandom(4).hex()}")
    result, report = run_pipeline()
    try:
        assert_runner_succeeded(result, report)
        for service, spec in SERVICES.items():
            assert image_exists(spec["image"], sha), f"shared dependency change did not rebuild {service}"
    finally:
        remove_images_for_sha(sha)


def test_pull_request_never_deploys_or_pushes():
    sha = commit_change(SERVICES["customer-notification"]["path"], f"pr {os.urandom(4).hex()}")
    result, report = run_pipeline(event="pull_request")
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert report is not None, "runner did not produce a report for pull request validation"
        executed_details = json.dumps([
            step.get("detail", "")
            for job in report.get("jobs", [])
            for step in job.get("steps", [])
            if step.get("status") == "success"
        ]).lower()
        forbidden = ["docker compose up", "docker push", "pushed", "registry push"]
        assert not any(term in executed_details for term in forbidden), "pull request run attempted deployment or registry release behavior"
        running = run_cmd(["docker", "ps", "--format", "{{.Image}}"], check=False).stdout
        assert sha not in running, "pull request run left a SHA-tagged service container running"
    finally:
        remove_images_for_sha(sha)


def test_newer_run_for_same_branch_supersedes_stale_work():
    sha = commit_change(SERVICES["shipment-intake"]["path"], f"concurrency {os.urandom(4).hex()}")
    first, first_report = run_pipeline()
    second, second_report = run_pipeline()
    try:
        assert first.returncode == 0, first.stdout + first.stderr
        assert second.returncode == 0, second.stdout + second.stderr
        assert first_report is not None and second_report is not None, "runner reports were not produced"
        concurrency = second_report.get("concurrency") or {}
        assert concurrency.get("cancel_in_progress") is True, "workflow did not request stale runs for the same branch to be superseded"
        assert second_report.get("cancelled_previous") is True, "second run did not supersede the earlier run for the same branch"
    finally:
        remove_images_for_sha(sha)
