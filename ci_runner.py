#!/usr/bin/env python3
"""Local GitHub Actions workflow runner — assessment harness. DO NOT MODIFY.

Executes .github/workflows/ci.yml the way GitHub Actions would: trigger
filtering (branches and paths), needs-ordered jobs run in parallel waves,
matrix expansion (static or fromJSON of a job output), if-conditions, run
steps with $GITHUB_OUTPUT, job outputs, a cache action, artifact handoff
between jobs, and concurrency groups. Writes a machine-readable run report
to .ci_runs/report.json after every run so tests (and you) can inspect
exactly what executed, in which wave, and when.

Usage:
    python3 ci_runner.py --event push --ref refs/heads/main
    python3 ci_runner.py --event push --ref refs/heads/main --before <sha> --sha <sha>
    python3 ci_runner.py --event pull_request --base-ref main

When run inside a git checkout, --sha defaults to HEAD and --before to
HEAD~1, so `git diff --name-only $BEFORE $SHA` inside a step sees the real
change set of the commit being "pushed".

Supported workflow syntax (anything outside this subset is an error):
    on: push / pull_request / workflow_dispatch, with optional
        branches: [...], paths: [...], paths-ignore: [...]
    jobs.<id>: needs, if, outputs, strategy.matrix, steps
        strategy.matrix: flat key: [list] product, or key: ${{ fromJSON(<expr>) }}
        an empty matrix list skips the job
    steps: name, id, if, run, env, uses (actions/checkout, actions/setup-python,
           actions/cache, actions/upload-artifact, actions/download-artifact)
        run steps may append key=value lines to $GITHUB_OUTPUT
    expressions: github.event_name / github.ref / github.ref_name / github.sha /
           github.event.before / github.run_id, secrets.<K>, matrix.<k>,
           steps.<id>.outputs.<k>, needs.<id>.result, needs.<id>.outputs.<k>,
           hashFiles('<glob>'), fromJSON(<expr>), contains(<a>, <b>),
           always(), success(), failure(), ==, !=, &&, ||, !
    workflow-level: env, concurrency {group, cancel-in-progress}
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RUNS_DIR = ROOT / ".ci_runs"
CACHE_DIR = ROOT / ".ci_cache"
ART_DIR = ROOT / ".ci_artifacts"
MAX_PARALLEL = 4
_LOCK = threading.Lock()


def log(msg):
    with _LOCK:
        print(msg, flush=True)


def git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def load_secrets():
    secrets = {}
    f = ROOT / ".secrets.json"
    if f.exists():
        secrets.update(json.loads(f.read_text()))
    for k, v in os.environ.items():
        if k.startswith("CI_SECRET_"):
            secrets[k[len("CI_SECRET_"):]] = v
    return secrets


def hash_files(pattern):
    h = hashlib.sha256()
    for p in sorted(ROOT.glob(pattern)):
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _from_json(text):
    try:
        return json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"fromJSON: invalid JSON {text!r}: {exc}")


def eval_expr(expr, ctx):
    """Evaluate the supported ${{ ... }} expression subset. Returns a value."""
    e = expr.strip()
    if e.startswith("${{") and e.endswith("}}"):
        e = e[3:-2].strip()
    e = e.replace("!=", " __NE__ ")
    e = re.sub(r"hashFiles\('([^']+)'\)", lambda m: repr(hash_files(m.group(1))), e)
    e = e.replace("always()", "True")
    e = e.replace("success()", repr(bool(ctx.get("_success", True))))
    e = e.replace("failure()", repr(bool(ctx.get("_failure", False))))
    e = e.replace("cancelled()", "False")
    e = re.sub(r"github\.event\.before", lambda m: repr(str(ctx.get("github", {}).get("event", {}).get("before", ""))), e)
    e = re.sub(r"github\.([A-Za-z_]+)",
               lambda m: repr(str(ctx.get("github", {}).get(m.group(1), ""))), e)
    e = re.sub(r"secrets\.([A-Za-z0-9_]+)",
               lambda m: repr(str(ctx.get("secrets", {}).get(m.group(1), ""))), e)
    e = re.sub(r"matrix\.([A-Za-z0-9_-]+)",
               lambda m: repr(str(ctx.get("matrix", {}).get(m.group(1), ""))), e)
    e = re.sub(r"steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)",
               lambda m: repr(str(ctx.get("steps", {}).get(m.group(1), {}).get(m.group(2), ""))), e)
    e = re.sub(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)",
               lambda m: repr(str(ctx.get("needs_outputs", {}).get(m.group(1), {}).get(m.group(2), ""))), e)
    e = re.sub(r"needs\.([A-Za-z0-9_-]+)\.result",
               lambda m: repr(str(ctx.get("needs", {}).get(m.group(1), "skipped"))), e)
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"!(?!=)", " not ", e)
    e = e.replace("__NE__", "!=")
    helpers = {"true": True, "false": False, "null": None,
               "fromJSON": _from_json, "toJSON": json.dumps,
               "contains": lambda a, b: (str(b) in a) if isinstance(a, (list, tuple)) else (str(b).lower() in str(a).lower()),
               "startsWith": lambda a, b: str(a).startswith(str(b)),
               "endsWith": lambda a, b: str(a).endswith(str(b)),
               "join": lambda a, sep=",": sep.join(str(x) for x in a)}
    try:
        return eval(e, {"__builtins__": {}}, helpers)
    except Exception as exc:
        raise RuntimeError(f"cannot evaluate expression '{expr}': {exc}")


def truthy(expr, ctx):
    return bool(eval_expr(expr, ctx))


def interpolate(text, ctx):
    if not isinstance(text, str):
        return text
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(eval_expr(m.group(1), ctx)), text)


def _glob_match(path, pattern):
    pattern = str(pattern)
    if pattern.endswith("/**"):
        pattern = pattern[:-3] + "/*"
    if "**" in pattern:
        rx = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
        return re.fullmatch(rx, path) is not None
    return fnmatch.fnmatch(path, pattern)


def is_triggered(on, event, ref_name, base_ref, changed_files):
    """Returns (triggered: bool, reason: str)."""
    if isinstance(on, str):
        on = {on: None}
    if isinstance(on, list):
        on = {e: None for e in on}
    if event not in on:
        return False, f"event '{event}' is not in on:"
    cfg = on.get(event) or {}
    branches = cfg.get("branches")
    if branches:
        target = base_ref if event == "pull_request" else ref_name
        if not any(fnmatch.fnmatch(target, str(b)) for b in branches):
            return False, f"ref '{target}' does not match branches filter"
    paths = cfg.get("paths")
    ignore = cfg.get("paths-ignore")
    if paths and changed_files is not None:
        if not any(_glob_match(f, p) for f in changed_files for p in paths):
            return False, "no changed file matches paths filter"
    if ignore and changed_files is not None and changed_files:
        if all(any(_glob_match(f, p) for p in ignore) for f in changed_files):
            return False, "every changed file matches paths-ignore"
    return True, "ok"


def expand_matrix(strategy, ctx):
    """Returns (combos, note). An empty list value yields no combos (job is skipped)."""
    matrix = (strategy or {}).get("matrix")
    if not matrix:
        return [None], ""
    if isinstance(matrix, str):
        matrix = eval_expr(matrix, ctx)
        if not isinstance(matrix, dict):
            raise RuntimeError("strategy.matrix expression must evaluate to an object")
    keys = [k for k in matrix.keys() if k not in ("include", "exclude")]
    combos = [{}]
    for k in keys:
        vals = matrix[k]
        if isinstance(vals, str):
            vals = eval_expr(vals, ctx)
        if not isinstance(vals, list):
            raise RuntimeError(f"matrix.{k} must be a list, got {type(vals).__name__}")
        if not vals:
            return [], f"matrix.{k} is empty"
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    return combos, ""


def topo_waves(jobs):
    """Returns (order, wave_of) where jobs in the same wave have no dependency on each other."""
    order, seen, wave_of = [], set(), {}

    def visit(name, stack):
        if name in seen:
            return
        if name in stack:
            raise RuntimeError(f"circular 'needs' involving job '{name}'")
        needs = jobs[name].get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        for n in needs:
            if n not in jobs:
                raise RuntimeError(f"job '{name}' needs unknown job '{n}'")
            visit(n, stack | {name})
        seen.add(name)
        wave_of[name] = 1 + max([wave_of[n] for n in needs], default=0)
        order.append(name)

    for name in jobs:
        visit(name, set())
    return order, wave_of


def _parse_output_file(path):
    outputs = {}
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return outputs
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+)<<(\S+)\s*$", line)
        if m:
            key, delim, buf = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            outputs[key] = "\n".join(buf)
        elif "=" in line:
            key, val = line.split("=", 1)
            outputs[key.strip()] = val
        i += 1
    return outputs


def run_step(step, ctx, job_env, record):
    uses = step.get("uses", "")
    swith = {k: interpolate(v, ctx) for k, v in (step.get("with") or {}).items()}
    if uses.startswith("actions/checkout") or uses.startswith("actions/setup-python"):
        record["detail"] = uses
        return True
    if uses.startswith("actions/cache"):
        key = str(swith.get("key", ""))
        if not key:
            record["detail"] = "cache: missing key"
            return False
        hit = (CACHE_DIR / key).exists()
        if not hit:
            (CACHE_DIR / key).mkdir(parents=True, exist_ok=True)
        record["detail"] = f"cache key={key} hit={str(hit).lower()}"
        record["cache_key"] = key
        record["cache_hit"] = hit
        return {"cache-hit": "true" if hit else "false"}
    if uses.startswith("actions/upload-artifact"):
        name = str(swith.get("name", ""))
        paths = str(swith.get("path", ""))
        if not name or not paths:
            record["detail"] = "upload-artifact: 'name' and 'path' are required"
            return False
        dest = ART_DIR / name
        dest.mkdir(parents=True, exist_ok=True)
        matched = 0
        for pat in paths.splitlines():
            pat = pat.strip()
            if not pat:
                continue
            for p in ROOT.glob(pat):
                if p.is_file():
                    shutil.copy2(p, dest / p.name)
                    matched += 1
        record["detail"] = f"uploaded artifact '{name}' ({matched} files)"
        record["artifact"] = name
        if matched == 0:
            record["detail"] = f"upload-artifact '{name}': no files matched path"
            return False
        return True
    if uses.startswith("actions/download-artifact"):
        name = str(swith.get("name", ""))
        src = ART_DIR / name
        if not name or not src.exists():
            record["detail"] = f"download-artifact: artifact '{name}' not found"
            record["artifact"] = name
            return False
        target = ROOT / str(swith.get("path", "."))
        target.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            shutil.copy2(p, target / p.name)
        record["detail"] = f"downloaded artifact '{name}'"
        record["artifact"] = name
        return True
    if uses:
        record["detail"] = f"unsupported action '{uses}'"
        return False
    cmd = step.get("run")
    if cmd is None:
        record["detail"] = "step has neither 'run' nor 'uses'"
        return False
    cmd = interpolate(cmd, ctx)
    gh = ctx.get("github", {})
    env = dict(os.environ)
    env.update({"GITHUB_ACTIONS": "true", "CI": "true",
                "GITHUB_SHA": str(gh.get("sha", "")), "GITHUB_REF": str(gh.get("ref", "")),
                "GITHUB_REF_NAME": str(gh.get("ref_name", "")), "GITHUB_EVENT_NAME": str(gh.get("event_name", "")),
                "GITHUB_BASE_REF": str(gh.get("base_ref", "")), "GITHUB_RUN_ID": str(gh.get("run_id", "")),
                "GITHUB_WORKSPACE": str(ROOT)})
    env.update({k: str(interpolate(v, ctx)) for k, v in job_env.items()})
    env.update({k: str(interpolate(v, ctx)) for k, v in (step.get("env") or {}).items()})
    fd, out_path = tempfile.mkstemp(prefix="gh_output_")
    os.close(fd)
    env["GITHUB_OUTPUT"] = out_path
    proc = subprocess.run(cmd, shell=True, cwd=ROOT, env=env,
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    record["detail"] = out[-2000:].strip()
    record["exit_code"] = proc.returncode
    outputs = _parse_output_file(out_path)
    try:
        os.unlink(out_path)
    except OSError:
        pass
    if proc.returncode != 0:
        return False
    if outputs:
        record["outputs"] = outputs
        return outputs
    return True


def run_job(name, spec, github, secrets, needs_results, needs_outputs, wf_env, matrix, wave, report):
    needs = spec.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    all_needs_ok = all(needs_results.get(n) == "success" for n in needs)
    any_needs_failed = any(needs_results.get(n) == "failure" for n in needs)
    ctx = {"github": github, "secrets": secrets, "matrix": matrix or {},
           "steps": {}, "needs": needs_results, "needs_outputs": needs_outputs,
           "_success": all_needs_ok, "_failure": any_needs_failed}
    label = name if not matrix else f"{name} ({', '.join(str(v) for v in matrix.values())})"
    jrec = {"job": name, "label": label, "matrix": matrix, "wave": wave, "steps": [],
            "status": "success", "outputs": {}, "started_at": None, "finished_at": None}
    cond = spec.get("if")
    if cond is not None:
        # Like GitHub Actions: an explicit `if` without a status function
        # still implicitly requires all needed jobs to have succeeded.
        has_status_fn = any(fn in str(cond) for fn in
                            ("always()", "success()", "failure()", "cancelled()"))
        should_run = truthy(str(cond), ctx) and (all_needs_ok or has_status_fn)
    else:
        should_run = all_needs_ok
    if not should_run:
        jrec["status"] = "skipped"
        with _LOCK:
            report["jobs"].append(jrec)
        log(f"[SKIP] {label}")
        return "skipped", {}
    log(f"[JOB ] {label}  (wave {wave})")
    jrec["started_at"] = time.time()
    job_env = dict(wf_env)
    job_env.update(spec.get("env") or {})
    failed = False
    for i, step in enumerate(spec.get("steps") or []):
        sname = step.get("name") or step.get("uses") or f"step-{i + 1}"
        srec = {"name": sname, "status": "success"}
        sctx = dict(ctx)
        sctx["_success"] = not failed
        sctx["_failure"] = failed
        scond = step.get("if")
        if scond is not None:
            has_status_fn = any(fn in str(scond) for fn in
                                ("always()", "success()", "failure()", "cancelled()"))
            if not truthy(str(scond), sctx) or (failed and not has_status_fn):
                srec["status"] = "skipped"
                jrec["steps"].append(srec)
                log(f"  [skip] {label}: {sname}")
                continue
        elif failed:
            srec["status"] = "skipped"
            jrec["steps"].append(srec)
            log(f"  [skip] {label}: {sname}")
            continue
        result = run_step(step, sctx, job_env, srec)
        if isinstance(result, dict):
            sid = step.get("id")
            if sid:
                ctx["steps"].setdefault(sid, {}).update(result)
            result = True
        if not result:
            srec["status"] = "failure"
            failed = True
            log(f"  [FAIL] {label}: {sname}: {srec.get('detail', '')[:200]}")
        else:
            log(f"  [ ok ] {label}: {sname}")
        jrec["steps"].append(srec)
    jrec["status"] = "failure" if failed else "success"
    outputs = {}
    for k, v in (spec.get("outputs") or {}).items():
        try:
            outputs[k] = str(interpolate(str(v), ctx))
        except RuntimeError as exc:
            outputs[k] = ""
            log(f"  [warn] {label}: output '{k}': {exc}")
    jrec["outputs"] = outputs
    jrec["finished_at"] = time.time()
    with _LOCK:
        report["jobs"].append(jrec)
    return jrec["status"], outputs


def main():
    ap = argparse.ArgumentParser(description="Run .github/workflows/ci.yml locally")
    ap.add_argument("--event", required=True,
                    choices=["push", "pull_request", "workflow_dispatch"])
    ap.add_argument("--ref", default="refs/heads/feature/local")
    ap.add_argument("--base-ref", default="main")
    ap.add_argument("--sha", default=None, help="commit being run (default: git HEAD, else a fixed fake)")
    ap.add_argument("--before", default=None, help="commit before the push (default: git HEAD~1, else zeros)")
    ap.add_argument("--max-parallel", type=int, default=MAX_PARALLEL)
    args = ap.parse_args()

    if not WORKFLOW_PATH.exists():
        print(f"workflow file not found: {WORKFLOW_PATH}")
        return 2
    wf = yaml.safe_load(WORKFLOW_PATH.read_text())
    # PyYAML parses the bare key `on:` as boolean True
    on = wf.get("on", wf.get(True))
    if on is None:
        print("workflow has no 'on:' trigger block")
        return 2

    sha = args.sha or git("rev-parse", "HEAD") or "4f9c2d1ab7e84f9c2d1ab7e84f9c2d1ab7e84f9c"
    before = args.before or git("rev-parse", "HEAD~1") or "0" * 40
    changed_files = None
    if git("rev-parse", "--is-inside-work-tree") == "true" and not set(before) <= {"0"}:
        diff = git("diff", "--name-only", before, sha)
        changed_files = [line for line in diff.splitlines() if line.strip()]
    ref_name = args.ref.rsplit("/", 1)[-1]
    run_id = str(int(time.time()))
    github = {"event_name": args.event, "ref": args.ref, "ref_name": ref_name,
              "sha": sha, "base_ref": args.base_ref, "run_id": run_id,
              "event": {"before": before, "after": sha}}
    secrets = load_secrets()

    RUNS_DIR.mkdir(exist_ok=True)
    report = {"event": args.event, "ref": args.ref, "sha": sha, "before": before,
              "changed_files": changed_files, "triggered": False, "trigger_reason": "",
              "jobs": [], "waves": {}, "concurrency": None}

    triggered, reason = is_triggered(on, args.event, ref_name, args.base_ref, changed_files)
    report["trigger_reason"] = reason
    if not triggered:
        print(f"workflow not triggered for event={args.event} ref={args.ref}: {reason}")
        (RUNS_DIR / "report.json").write_text(json.dumps(report, indent=2))
        return 0
    report["triggered"] = True
    # Artifacts are per-run, exactly like GitHub Actions.
    shutil.rmtree(ART_DIR, ignore_errors=True)

    conc = wf.get("concurrency")
    if isinstance(conc, dict):
        gctx = {"github": github, "secrets": secrets}
        group = interpolate(str(conc.get("group", "")), gctx)
        cancel = bool(conc.get("cancel-in-progress", False))
        report["concurrency"] = {"group": group, "cancel_in_progress": cancel}
        hist = RUNS_DIR / "history.jsonl"
        if cancel and hist.exists():
            lines = hist.read_text().strip().splitlines()
            if lines and json.loads(lines[-1]).get("group") == group:
                report["cancelled_previous"] = True
                print(f"[conc] cancelled in-progress run in group '{group}'")
        with hist.open("a") as f:
            f.write(json.dumps({"group": group}) + "\n")

    jobs = wf.get("jobs") or {}
    wf_env = wf.get("env") or {}
    order, wave_of = topo_waves(jobs)
    results, outputs_of = {}, {}
    exit_code = 0
    waves = {}
    for name in order:
        waves.setdefault(wave_of[name], []).append(name)
    report["waves"] = {str(w): names for w, names in sorted(waves.items())}

    def run_one(name):
        spec = jobs[name]
        ctx = {"github": github, "secrets": secrets, "needs": results, "needs_outputs": outputs_of}
        try:
            combos, note = expand_matrix(spec.get("strategy"), ctx)
        except RuntimeError as exc:
            jrec = {"job": name, "label": name, "matrix": None, "wave": wave_of[name],
                    "steps": [], "status": "failure", "outputs": {}, "detail": str(exc)}
            with _LOCK:
                report["jobs"].append(jrec)
            log(f"[FAIL] {name}: {exc}")
            return name, "failure", {}
        if not combos:
            jrec = {"job": name, "label": name, "matrix": None, "wave": wave_of[name],
                    "steps": [], "status": "skipped", "outputs": {}, "detail": note}
            with _LOCK:
                report["jobs"].append(jrec)
            log(f"[SKIP] {name}: {note}")
            return name, "skipped", {}
        statuses, merged = [], {}
        if len(combos) > 1:
            with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
                futs = [pool.submit(run_job, name, spec, github, secrets, results, outputs_of,
                                    wf_env, m, wave_of[name], report) for m in combos]
                for f in futs:
                    st, out = f.result()
                    statuses.append(st)
                    merged.update(out)
        else:
            st, out = run_job(name, spec, github, secrets, results, outputs_of,
                              wf_env, combos[0], wave_of[name], report)
            statuses.append(st)
            merged.update(out)
        if "failure" in statuses:
            return name, "failure", merged
        if all(s == "skipped" for s in statuses):
            return name, "skipped", merged
        return name, "success", merged

    for w in sorted(waves):
        names = waves[w]
        if len(names) > 1:
            with ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
                outcomes = list(pool.map(run_one, names))
        else:
            outcomes = [run_one(names[0])]
        for name, status, out in outcomes:
            results[name] = status
            outputs_of[name] = out
            if status == "failure":
                exit_code = 1
    report["results"] = results
    report["outputs"] = outputs_of
    (RUNS_DIR / "report.json").write_text(json.dumps(report, indent=2))
    print(f"\nrun complete: {results}")
    print(f"report: {RUNS_DIR / 'report.json'}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
