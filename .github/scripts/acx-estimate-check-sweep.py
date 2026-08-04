#!/usr/bin/env python3
"""Org-central estimate-check sweep (CIW.4, ADR-0101 D3).

Deployed by the org-workflows mechanism to `.github/scripts/` in withACX/.github
and run by `acx-estimate-check-org.yml`. For each A-CX-managed repo the shared
selector picked, this flags every open `status:ready` issue whose board
`Estimate` is empty: it ensures the `needs-estimate` label exists, applies it,
and upserts one marker comment. Compliant issues are never touched -- no label,
no comment, no edit.

BEHAVIOR IS DELIBERATELY IDENTICAL to the per-repo `estimate-check.yml` in
a-cx-ai-config: same label, same colour and description, same marker string, and
the same comment wording. The two are the same check at different reach, so a
maintainer must never have to work out which one spoke.

WHY A SCRIPT AND NOT AN INLINE HEREDOC. The per-repo workflow carries this logic
inside a `python3 - <<'PYEOF'` heredoc. A quoted heredoc ends at the first line
equal to its delimiter, so any line that happens to read as the delimiter hands
the remainder of the script to bash with the job's token in the environment --
and a bare delimiter is valid Python, so nothing else catches it. Shipping a real
file the workflow executes removes that hazard rather than guarding against it.

THE ESTIMATE VALUES ARE NOT RE-DERIVED HERE. They come from
`board_cli.py list-field` / `field-value` in a-cx-ai-config, checked out by the
workflow and located by ACX_BOARD_CLI. Board DISCOVERY (which boards a repo is
linked to, and a per-issue item-id lookup on the fallback path) is a GraphQL read
rather than a board-FIELD read, so it runs inline here, exactly as the per-repo
workflow does.

Topology-aware, matching the per-repo workflow (RRV-05.3):

  LINKED boards (the common case): one `repository.projectsV2` query discovers a
  repo's boards, then `board_cli.py list-field` pages each board's items and
  Estimate in ~100-item batches -- a bounded page count, not one query per issue.

  NO linked board (detached mode, or no board at all): fall back to the per-issue
  read -- each issue's own `projectItems`, then the Estimate via `board_cli.py
  field-value` -- so a board the repo is not natively linked to is still found.
  This path runs only where there is no linked board to batch.

Environment:
  GH_TOKEN            org-scoped token; every `gh` call and the board CLI use it
  ACX_TARGETS_FILE    JSON [{"owner": ..., "repo": ...}] from the shared selector
  ACX_BOARD_CLI       path to the checked-out board_cli.py

Never fails the scheduled run on a transient per-repo or per-issue error: it
warns and moves on, because one unreachable repo must not take down coverage for
the rest. It DOES exit non-zero when its own inputs are unusable (no targets
file, no board CLI), which is a deployment fault rather than a data condition.

Stdlib only; Python 3.9-compatible (the A-CX floor).
"""
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

MARKER = "**Estimate check:**"
LABEL = "needs-estimate"
LABEL_COLOR = "E5B700"
LABEL_DESCRIPTION = (
    "status:ready issue held in draft pending a size -- see "
    "the github skill references/triage.md"
)
# Matches the per-repo workflow's wording exactly, with {n} the issue number.
COMMENT_TEMPLATE = (
    "{marker} issue #{n} is `status:ready` with an empty board `Estimate`. A "
    "non-empty Estimate (Fibonacci story points: 1/2/3/5/8/13) is part of the "
    "readiness contract (ADR-0089): this is a contract violation to fix by "
    "sizing the issue on the project board, or by dropping it back to "
    "`status:draft` -- never by applying a default. Remove `needs-estimate` "
    "once a real Estimate is set."
)


def gh(*args: str) -> subprocess.CompletedProcess:
    """Run a gh command, capturing output. Never raises on a non-zero exit."""
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def graphql(query: str, **variables) -> Optional[dict]:
    """One GraphQL query with BOUND variables. Returns None on any failure, so
    every caller treats an unreachable board the same way it treats an absent one.

    Variables are passed as `gh api graphql -f <name>=<value>` rather than
    interpolated into the query text -- the same pattern
    `skills/github/scripts/provider.py` uses. Repo names cannot currently contain
    a quote or backslash, so interpolation would not be exploitable today, but
    that makes GitHub's naming charset the only thing standing between a repo name
    and the query, which is not a guarantee worth depending on.
    """
    args = ["api", "graphql", "-f", "query={0}".format(query)]
    for key, value in variables.items():
        # -F types the value (so an Int! variable arrives as a number); -f keeps
        # it a string. Same distinction provider.py makes.
        flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        args += [flag, "{0}={1}".format(key, value)]
    result = gh(*args)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def board_cli(cli_path: str, *args: str) -> Optional[str]:
    """Run the shared board CLI. Returns stdout, or None when the call failed."""
    result = subprocess.run(
        ["python3", cli_path, *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write("  board_cli {0} failed: {1}\n".format(
            args[0] if args else "?", result.stderr.strip()))
        return None
    return result.stdout


def ready_issues(repo: str) -> Optional[Dict[int, str]]:
    """Open `status:ready` issues, as {number: truncated title}.

    That label IS the scope of the readiness contract's Estimate requirement. A
    buildable bug is out of scope by construction: it is ready by the bug
    exemption and never carries status:ready.
    """
    listed = gh("issue", "list", "--repo", repo, "--state", "open",
                "--label", "status:ready", "--limit", "500",
                "--json", "number,title")
    if listed.returncode != 0:
        sys.stderr.write("  could not list status:ready issues: {0}\n".format(
            listed.stderr.strip()))
        return None
    try:
        issues = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        # Say so. Returning None silently would read in the log exactly like
        # "this repo has no status:ready issues", hiding a real fault.
        sys.stderr.write(
            "  {0}: could not parse the issue list as JSON; skipping repo.\n"
            .format(repo))
        return None
    return {i["number"]: i["title"][:60] for i in issues}


def linked_boards(owner: str, name: str) -> List[Tuple[str, int]]:
    """The repo's natively-linked Projects v2 boards as [(owner, number)].

    first:100 -- far more than any repo's linked-board count in practice, so
    full pagination is unnecessary at this scale.
    """
    data = graphql(
        'query($o:String!,$n:String!){repository(owner:$o,name:$n){'
        'projectsV2(first:100){nodes{number owner{login}}}}}',
        o=owner, n=name,
    )
    nodes = ((((data or {}).get("data") or {}).get("repository") or {})
             .get("projectsV2") or {}).get("nodes") or []
    boards = []
    for node in nodes:
        login = (node.get("owner") or {}).get("login")
        number = node.get("number")
        if login and number:
            boards.append((login, number))
    return boards


def estimates_via_boards(
    cli_path: str, repo: str, boards: List[Tuple[str, int]]
) -> Tuple[Set[int], Set[int]]:
    """LINKED path: batch each board's items + Estimate through the board CLI."""
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    for bowner, bnum in boards:
        out = board_cli(cli_path, "list-field", "--owner", bowner,
                        "--number", str(bnum), "--repo", repo,
                        "--field", "Estimate")
        if out is None:
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                number = int(parts[1])
            except ValueError:
                continue
            on_board.add(number)
            if len(parts) > 2 and parts[2].strip():
                has_estimate.add(number)
    return on_board, has_estimate


def estimates_per_issue(
    cli_path: str, owner: str, name: str, numbers: List[int]
) -> Tuple[Set[int], Set[int]]:
    """FALLBACK path (no natively-linked board): per-issue item-id then value."""
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    for number in numbers:
        data = graphql(
            'query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){'
            'issue(number:$i){projectItems(first:10){nodes{id}}}}}',
            o=owner, n=name, i=number,
        )
        nodes = ((((data or {}).get("data") or {}).get("repository") or {})
                 .get("issue") or {}).get("projectItems") or {}
        nodes = nodes.get("nodes") or []
        if not nodes:
            continue  # not on any board
        on_board.add(number)
        for item in nodes:
            out = board_cli(cli_path, "field-value", "--item-id", item["id"],
                            "--field", "Estimate")
            if out is not None and out.strip():
                has_estimate.add(number)
                break
    return on_board, has_estimate


def ensure_label(repo: str) -> None:
    """Make sure `needs-estimate` exists in `repo`, without ever redefining a
    label the repo already owns.

    `gh label create --force` creates OR overwrites, which is fine in a single
    repo A-CX owns outright. Across N repos it is not: a repo that already uses
    `needs-estimate` for its own purpose, with its own color and description,
    would have that definition silently replaced by this sweep. So only create
    when the label is absent, and when it is present leave its definition alone
    and say so.
    """
    listed = gh("label", "list", "--repo", repo, "--json",
                "name,color,description", "--limit", "500")
    existing = None
    if listed.returncode == 0 and listed.stdout.strip():
        try:
            for entry in json.loads(listed.stdout):
                if entry.get("name") == LABEL:
                    existing = entry
                    break
        except json.JSONDecodeError:
            existing = None

    if existing is None:
        # --force still guards the race where a concurrent run created it first.
        gh("label", "create", LABEL, "--repo", repo, "--color", LABEL_COLOR,
           "--description", LABEL_DESCRIPTION, "--force")
        return

    if ((existing.get("color") or "").lstrip("#").upper() != LABEL_COLOR.upper()
            or (existing.get("description") or "") != LABEL_DESCRIPTION):
        sys.stderr.write(
            "  {0}: '{1}' already exists with a different definition "
            "(color={2!r}, description={3!r}); applying the label and leaving "
            "its definition unchanged.\n".format(
                repo, LABEL, existing.get("color"),
                existing.get("description")))


def flag_issue(owner: str, name: str, number: int, title: str) -> None:
    """Apply the label and upsert the one marker comment on a violation."""
    repo = "{0}/{1}".format(owner, name)
    ensure_label(repo)
    gh("issue", "edit", str(number), "--repo", repo, "--add-label", LABEL)

    comment = COMMENT_TEMPLATE.format(marker=MARKER, n=number)
    # `sort_by(.created_at)` so "the marker comment" is deterministic. The API's
    # array order is not guaranteed, and if two comments ever start with the
    # marker the sweep must keep updating the SAME one rather than alternating.
    existing = gh("api", "repos/{0}/{1}/issues/{2}/comments".format(
        owner, name, number),
        "--jq", '[.[] | select(.body | startswith("{0}"))] '
                '| sort_by(.created_at) | [.[] | {{id, body}}]'.format(MARKER))
    comment_id = None
    if existing.returncode == 0 and existing.stdout.strip():
        try:
            found = json.loads(existing.stdout)
            if found:
                comment_id = found[0]["id"]
        except (json.JSONDecodeError, KeyError, IndexError):
            comment_id = None

    if comment_id:
        gh("api", "-X", "PATCH",
           "repos/{0}/{1}/issues/comments/{2}".format(owner, name, comment_id),
           "-f", "body={0}".format(comment))
        print("  #{0} {1!r}: needs-estimate applied, comment refreshed.".format(
            number, title))
    else:
        gh("issue", "comment", str(number), "--repo", repo, "--body", comment)
        print("  #{0} {1!r}: needs-estimate applied, comment posted.".format(
            number, title))


def sweep_repo(cli_path: str, owner: str, name: str) -> Tuple[int, int, int]:
    """Check one repo. Returns (flagged, compliant, skipped_no_board)."""
    repo = "{0}/{1}".format(owner, name)
    ready = ready_issues(repo)
    if ready is None:
        return (0, 0, 0)
    print("{0}: {1} open status:ready issue(s) to check.".format(
        repo, len(ready)))
    if not ready:
        return (0, 0, 0)

    boards = linked_boards(owner, name)
    if boards:
        on_board, has_estimate = estimates_via_boards(cli_path, repo, boards)
    else:
        on_board, has_estimate = estimates_per_issue(
            cli_path, owner, name, list(ready))

    flagged = compliant = skipped_no_board = 0
    for number, title in ready.items():
        if number not in on_board:
            # Not on any board -- a board-conditional no-op, the same rule every
            # other Estimate/Actual write in the pipeline follows.
            skipped_no_board += 1
            continue
        if number in has_estimate:
            compliant += 1
            continue
        flag_issue(owner, name, number, title)
        flagged += 1
    return (flagged, compliant, skipped_no_board)


def main() -> int:
    targets_file = os.environ.get("ACX_TARGETS_FILE", "")
    cli_path = os.environ.get("ACX_BOARD_CLI", "")

    # These two are deployment faults, not data conditions: a sweep that cannot
    # find its scope or the shared CLI must fail visibly rather than report a
    # clean run over nothing.
    if not targets_file or not os.path.isfile(targets_file):
        sys.stderr.write(
            "ERROR: ACX_TARGETS_FILE is unset or missing ({0!r}). The selector "
            "step must write the managed repo list before this runs.\n".format(
                targets_file))
        return 1
    if not cli_path or not os.path.isfile(cli_path):
        sys.stderr.write(
            "ERROR: ACX_BOARD_CLI is unset or missing ({0!r}). The a-cx-ai-config "
            "checkout must provide skills/github/scripts/board_cli.py.\n".format(
                cli_path))
        return 1

    with open(targets_file, "r", encoding="utf-8") as handle:
        targets = json.load(handle)
    if not targets:
        # The selector throws rather than returning empty, so an empty list here
        # means every listed repo was archived, disabled, forked, or invisible.
        print("No managed repos selected -- nothing to sweep.")
        return 0

    print("Sweeping {0} A-CX-managed repo(s): {1}".format(
        len(targets),
        ", ".join(str(t.get("repo", "?")) for t in targets)))

    totals = [0, 0, 0]
    for target in targets:
        # The destructuring is INSIDE the try on purpose: a target missing its
        # owner/repo key must cost that one entry, not the whole sweep. Outside
        # the try it would raise KeyError and abort every remaining repo, which
        # is the opposite of what this loop promises.
        try:
            owner, name = target["owner"], target["repo"]
            result = sweep_repo(cli_path, owner, name)
        except Exception as exc:  # one bad repo must not abort the sweep
            sys.stderr.write("  {0!r}: error ({1}); continuing.\n".format(
                target, exc))
            continue
        totals = [a + b for a, b in zip(totals, result)]

    print("\nDone. {0} flagged, {1} already compliant, {2} skipped "
          "(not on a linked board).".format(*totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
