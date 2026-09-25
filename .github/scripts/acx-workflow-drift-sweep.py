#!/usr/bin/env python3
"""Org-central workflow-drift sweep (#2949, ADR-0101 D1/D2).

Deployed by the org-workflows mechanism to `.github/scripts/` in withACX/.github
and run by `acx-workflow-drift-org.yml`. For each A-CX-managed repo the shared
selector picked, it reads that repo's installed copies of the per-repo A-CX
workflows and helpers over the API and hands them to
`check_workflow_drift.py`, the one implementation of the comparison. This file
fetches and reports; it never compares content itself.

Which paths are read comes from `check_workflow_drift.py`'s own
WORKFLOW_ASSETS and GITHUB_SCRIPT_ASSETS, imported from the a-cx-ai-config
checkout, so the sweep cannot track a different file set than the check.

A path that returns 404 is not installed, which the check reports as clean: a
repo that never opted in is not in drift. Any other failed read makes that
repo UNREAD, and an unread repo fails the run. A read that failed is not
evidence that the copy matches.

Environment:
  GH_TOKEN             org-scoped token; every `gh` call uses it
  ACX_TARGETS_FILE     JSON [{"owner": ..., "repo": ...}] from the shared selector
  ACX_DRIFT_CHECK      path to the checked-out check_workflow_drift.py
  ACX_GITHUB_SCRIPTS   path to the checked-out skills/github/scripts/

Exit codes: 0 every swept repo read and clean; 1 drift, an unread repo, or a
copy the check could not compare; 2 unusable inputs (targets file, check
script, or scripts dir missing, or an empty target list).

Stdlib only; Python 3.9-compatible (the A-CX floor).
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RAW_ACCEPT = "Accept: application/vnd.github.raw"


def fail_input(msg: str) -> int:
    print("::error::{}".format(msg))
    return 2


def load_install_paths(check_script: Path) -> List[str]:
    """The install destinations the check itself tracks."""
    spec = importlib.util.spec_from_file_location("check_workflow_drift",
                                                  str(check_script))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load {}".format(check_script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    paths = list(mod.WORKFLOW_ASSETS.values()) + list(
        mod.GITHUB_SCRIPT_ASSETS.values())
    return sorted(set(paths))


def fetch(owner: str, repo: str, path: str) -> Tuple[str, bytes, str]:
    """Return ("ok", content, "") / ("absent", b"", "") / ("error", b"", why)."""
    proc = subprocess.run(
        ["gh", "api", "-H", RAW_ACCEPT,
         "repos/{}/{}/contents/{}".format(owner, repo, path)],
        capture_output=True)
    if proc.returncode == 0:
        return "ok", proc.stdout, ""
    err = proc.stderr.decode("utf-8", "replace").strip()
    if "HTTP 404" in err:
        return "absent", b"", ""
    return "error", b"", err.splitlines()[-1] if err else "gh exited {}".format(
        proc.returncode)


def check_repo(owner: str, repo: str, paths: List[str], check_script: Path,
               scripts_dir: Path) -> Tuple[str, List[Dict[str, str]], str]:
    """Return (verdict, results, detail); verdict is clean|drift|unread|unchecked."""
    with tempfile.TemporaryDirectory(prefix="acx-drift-") as tmp:
        root = Path(tmp)
        for rel in paths:
            state, content, why = fetch(owner, repo, rel)
            if state == "error":
                return "unread", [], "{}: {}".format(rel, why)
            if state == "ok":
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        proc = subprocess.run(
            [sys.executable, str(check_script), "--repo-path", str(root),
             "--github-scripts", str(scripts_dir), "--json"],
            capture_output=True, text=True)
    try:
        results = json.loads(proc.stdout)["results"]
    except (ValueError, KeyError, TypeError):
        return "unchecked", [], "check_workflow_drift.py exited {} without JSON: {}".format(
            proc.returncode, proc.stderr.strip()[-300:])
    if proc.returncode == 1:
        return "drift", results, ""
    if proc.returncode == 0:
        return "clean", results, ""
    return "unchecked", results, "check_workflow_drift.py exited {}".format(
        proc.returncode)


def main() -> int:
    targets_file = os.environ.get("ACX_TARGETS_FILE", "")
    check_env = os.environ.get("ACX_DRIFT_CHECK", "")
    scripts_env = os.environ.get("ACX_GITHUB_SCRIPTS", "")
    if not targets_file or not Path(targets_file).is_file():
        return fail_input("ACX_TARGETS_FILE is unset or missing: {!r}".format(
            targets_file))
    check_script = Path(check_env) if check_env else None
    if check_script is None or not check_script.is_file():
        return fail_input("ACX_DRIFT_CHECK is unset or missing: {!r}".format(
            check_env))
    scripts_dir = Path(scripts_env) if scripts_env else None
    if scripts_dir is None or not scripts_dir.is_dir():
        return fail_input("ACX_GITHUB_SCRIPTS is unset or missing: {!r}".format(
            scripts_env))
    try:
        targets = json.loads(Path(targets_file).read_text(encoding="utf-8"))
    except ValueError as exc:
        return fail_input("ACX_TARGETS_FILE is not JSON: {}".format(exc))
    if not isinstance(targets, list) or not targets:
        return fail_input("the selector returned no repos -- a sweep that "
                          "covers nothing is not a pass")
    try:
        paths = load_install_paths(check_script)
    except Exception as exc:  # noqa: BLE001 -- any import failure is an input fault
        return fail_input("cannot read the tracked paths from {}: {}".format(
            check_script, exc))

    failing: List[str] = []
    for t in targets:
        owner, repo = t.get("owner", ""), t.get("repo", "")
        slug = "{}/{}".format(owner, repo)
        verdict, results, detail = check_repo(owner, repo, paths, check_script,
                                              scripts_dir)
        counts: Dict[str, int] = {}
        for r in results:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        summary = ", ".join("{} {}".format(v, k) for k, v in sorted(counts.items()))
        if verdict == "clean":
            print("{}: clean ({})".format(slug, summary or "nothing tracked"))
            continue
        failing.append(slug)
        if verdict == "drift":
            for r in results:
                if r["state"] in ("drift", "missing"):
                    print("::error::{}: {} differs from its source asset".format(
                        slug, r["path"]))
            print("{}: DRIFT ({}) -- run /setup-repo update in that repo".format(
                slug, summary))
        elif verdict == "unread":
            print("::error::{}: UNREAD -- {}".format(slug, detail))
        else:
            print("::error::{}: UNCHECKED -- {}".format(slug, detail))
            for r in results:
                if r["state"] == "unchecked":
                    print("  {} {}".format(r["path"], r["detail"]))

    swept = len(targets)
    if failing:
        print("\n{} of {} managed repo(s) failed: {}".format(
            len(failing), swept, ", ".join(failing)))
        return 1
    print("\nAll {} managed repo(s) clean.".format(swept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
