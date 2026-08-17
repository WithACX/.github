#!/usr/bin/env python3
"""Org-central estimate-check sweep (CIW.4, ADR-0101 D3).

Deployed by the org-workflows mechanism to `.github/scripts/` in withACX/.github
and run by `acx-estimate-check-org.yml`. For each A-CX-managed repo the shared
selector picked, this flags every open `status:ready` issue whose board
`Estimate` is empty: it ensures the `needs-estimate` label exists, applies it,
and upserts one marker comment. An issue that is on no board is never touched --
no label, no comment, no edit.

SELF-HEALING, NOT ONE-WAY (#1788). A compliant read now REMOVES a
`needs-estimate` the issue is still carrying and retires the marker comment,
and so does an exemption. Without that, the check could only ever add: an issue
sized AFTER being flagged kept a false flag until a human removed it, which the
/deliver-plan Stage 4 ordering (the issue must exist before its board Estimate
can be written) made systematic rather than rare. A false `needs-estimate`
corrupts the readiness signal on compliant work, so leaving one in place forever
is that error made durable.

REMOVAL DEMANDS THE SAME POSITIVE EVIDENCE FLAGGING DEMANDS. It happens only on
an Estimate this run actually READ, or on an exemption decided from the issue's
own labels. An unverified read, an unread board, and an off-board issue hold the
label exactly as they hold the flag (#1646): the fail-closed rule is symmetric,
and "we could not read it" is not evidence in either direction.

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

A FAILED READ IS NOT AN EMPTY ESTIMATE (#1646), also matching the per-repo
workflow. Board reads fail transiently -- secondary rate limiting is the observed
cause -- and judging the Estimate by "the call returned something" made a failed
read indistinguishable from an unset field, which is how compliant issues got
`needs-estimate` applied. So reads retry with backoff, every failure is logged,
and an issue whose Estimate or board membership could not be READ is counted
`unverified` and never flagged. When a discovered board fails to answer at all, no
issue in that repo is flagged for the run: absence from that board's rows is not
evidence of an unset Estimate. Fail closed toward NOT labeling -- a false
`needs-estimate` corrupts the readiness signal on compliant work.

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
  ACX_READ_ATTEMPTS   optional; attempts per board read (default 3, clamped 1-10)
  ACX_RETRY_BACKOFF   optional; seconds between attempts, times the attempt
                      number, plus jitter (default 2, clamped 0-60). Dialed to 0
                      by the tests. A non-numeric value for either is reported
                      and ignored, never raised.
  ACX_GH_RETRIES      set to "1" for the board CLI child process, so the provider
                      layer inside it does not retry underneath this script's own
                      retry loop (#1673). Not an operator control.

A SWEEP THAT VERIFIED NOTHING IS NOT A PASS (#1445), the same rule #1247 gave
the per-repo workflow, adapted to this one's per-repo loop. "Not on a linked
board" is a single negative fact, so 100% of a non-empty `status:ready` set
landing in that bucket reads exactly like a clean repo -- and here the totals are
summed across every managed repo, so one credential fault would zero the whole
org's coverage behind a single green line. So each repo now tracks POSITIVE
evidence that board data was actually READ (a discovered board answered with at
least one item row, or at least one per-issue membership probe answered), and a
repo whose whole ready set went unchecked with no such evidence is reported
INCONCLUSIVE rather than clean, with a `::warning::` naming the likely cause. The
aggregate then distinguishes verified repos from inconclusive ones.

EXIT CODE (the question #1445 left open, decided here). Unlike the per-repo
check, this sweep is schedule-only -- no `issues.labeled` trigger -- so a red job
costs one weekly annotation rather than noise on every issue edit. It therefore
exits 1 when EVERY swept repo was inconclusive, which is the credential-fault
shape (a token that lost Projects: Read reaches no board anywhere) and is a
deployment fault by the same reasoning as the two input guards below. A repo or
two going inconclusive beside repos that read fine stays exit 0 with the warning:
one unreachable repo must not take down coverage for the rest, which is this
script's standing rule.

The residual assumption, stated here so a red run is not misdiagnosed: "not one
board answered with a single row, anywhere" is treated as a credential fault,
and an org-wide emptying of every managed board would look identical from
inside one run. Check the token's Projects: Read first -- it is overwhelmingly
the likelier cause -- but rule out a legitimate mass board change before
concluding the credential broke.

So: never fails the scheduled run on a transient per-repo or per-issue error --
it warns and moves on. It DOES exit non-zero when its own inputs are unusable (no
targets file, no board CLI) or when the run as a whole verified nothing.

Stdlib only; Python 3.9-compatible (the A-CX floor).
"""
import json
import os
import random
import subprocess
import sys
import time
from typing import Dict, List, NamedTuple, Optional, Set, Tuple


class Evidence(NamedTuple):
    """Positive proof that board data was actually READ, not merely absent.

    Counted independently of the `ready` set (#1445), which is what keeps "the
    board answered, none of these issues are on it" distinguishable from
    "nothing answered at all". Both are all-skips; only the second verified
    nothing.
    """
    list_field_ok: int = 0   # discovered boards whose list-field call succeeded
    board_rows: int = 0      # item rows parsed from any board, ready or not
    probe_ok: int = 0        # per-issue projectItems queries that answered

def _knob(name, default, low, high, cast):
    """Read one numeric knob, clamped, never raising.

    A bad value must not take the sweep down: this script's contract is to warn
    and continue on a data condition, and crashing on a typo in a debug override
    would be a worse outcome than ignoring it.
    """
    raw = os.environ.get(name) or ""
    if not raw:
        return default
    try:
        return min(high, max(low, cast(raw)))
    except ValueError:
        sys.stderr.write("  ignoring invalid {0}={1!r}; using {2}\n".format(
            name, raw, default))
        return default


# Read retries (#1646). Every read here is idempotent, so retrying a non-zero
# exit is free; a persistent fault costs ATTEMPTS attempts and is then reported.
# Deliberately NOT conditional on the error text: the observed failure (secondary
# rate limiting) surfaces through several layers with no stable wording, and
# matching on wording would silently stop retrying the moment it changed.
ATTEMPTS = _knob("ACX_READ_ATTEMPTS", 3, 1, 10, int)
BACKOFF = _knob("ACX_RETRY_BACKOFF", 2.0, 0.0, 60.0, float)


def _sleep_before_retry(attempt: int) -> None:
    """Linear backoff plus jitter.

    The jitter is not decoration. The failure being retried is secondary rate
    limiting caused by several runs firing at once, and a fixed schedule would
    have all of them retry in lockstep inside the window GitHub needs to
    recover -- deepening the very block this is recovering from.
    """
    time.sleep(BACKOFF * attempt + random.uniform(0, BACKOFF))


MARKER = "**Estimate check:**"
LABEL = "needs-estimate"
# Exempt from this sweep entirely -- see ready_issues() for the rule and ADR-0119.
DEFERRED_LABEL = "priority:3-deferred"
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
    "`status:draft` -- never by applying a default. This label is removed "
    "automatically once a real Estimate is set and this check reads it "
    "(#1788); removing it by hand is not part of the fix."
)
# What the marker comment is rewritten to when the flag is removed (#1788).
# Matches the per-repo workflow's wording exactly. It keeps the MARKER prefix on
# purpose: the violation upsert finds this same comment again and turns it back
# into a violation notice, so an issue never accumulates a second marker.
RESOLVED_TEMPLATE = (
    "{marker} resolved -- issue #{n} no longer needs a size flag: {reason}. "
    "`needs-estimate` was removed automatically. This comment is the record of "
    "that removal; it is rewritten as a fresh violation notice if the issue is "
    "ever flagged again."
)
REASON_ESTIMATE_SET = "its board `Estimate` is now set"
REASON_EXEMPT = (
    "it is exempt from the Estimate requirement (the github skill's "
    "references/triage.md, \"Readiness model\")"
)


def buildable_bug(labels: Set[str]) -> bool:
    """The readiness model's buildable bug -- exempt from the Estimate (#1690).

    `bug` + any `priority:*` + no `investigate`. Kept as a named predicate so the
    sweep and the per-repo `estimate-check.yml` state the same three conditions,
    and so a test can assert the exemption directly. The rule itself belongs to
    the github skill's references/triage.md, "Readiness model"; it is applied
    here, never restated.
    """
    return ("bug" in labels
            and any(lab.startswith("priority:") for lab in labels)
            and "investigate" not in labels)


def gh(*args: str) -> subprocess.CompletedProcess:
    """Run a gh command, capturing output. Never raises on a non-zero exit."""
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def graphql(query: str, label: str = "graphql", **variables) -> Optional[dict]:
    """One GraphQL query with BOUND variables, retried on failure.

    Returns None when every attempt failed, and SAYS SO on stderr: a silent None
    is the #1646 defect, where a failed read was read as a data condition. Callers
    must treat None as "unknown", never as "absent".

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
    error = ""
    for attempt in range(1, ATTEMPTS + 1):
        result = gh(*args)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                error = "response was not JSON"
        else:
            error = result.stderr.strip()
        if attempt < ATTEMPTS:
            _sleep_before_retry(attempt)
    sys.stderr.write("  {0} failed after {1} attempt(s): {2}\n".format(
        label, ATTEMPTS, error))
    return None


# ONE retry authority per read (#1673). board_cli.py reads through
# provider.run_gh, which retries transient failures itself (retries=4). Left alone,
# that inner loop nests under board_cli()'s loop below and one Estimate read costs
# ATTEMPTS x 4 gh calls -- amplifying requests against the same secondary rate limit
# the retry exists to ride out, across every managed repo in one job.
# ACX_GH_RETRIES=1 makes the inner layer single-attempt, so the loop below is the
# only authority and the total stays at ATTEMPTS. The outer loop is the one kept
# because it is not conditional on error text, where the inner one matches stderr
# wording that can change under it. Do NOT add a third retry layer.
# A board_cli.py older than the knob simply ignores it and retries as before -- the
# amplification returns, nothing breaks.
BOARD_CLI_ENV = dict(os.environ, ACX_GH_RETRIES="1")


def board_cli(cli_path: str, *args: str) -> Optional[str]:
    """Run the shared board CLI, retried on failure.

    Returns stdout, or None when every attempt failed. None means the read's
    result is UNKNOWN -- never "the field is empty" (#1646).

    This loop is the SOLE retry authority for the read: BOARD_CLI_ENV carries
    ACX_GH_RETRIES=1, so one logical read costs ATTEMPTS gh invocations rather than
    ATTEMPTS x 4 (#1673).

    THE RAW STDERR ECHO BELOW IS AUDITED, NOT ACCIDENTAL (#1446, decided while
    delivering #1445). It pipes board_cli.py's stderr into the Actions log while
    GH_TOKEN holds a PAT, so the question is whether it can echo the token. It
    cannot as the chain stands: board_cli.py takes no token on its command line
    and every stderr write there is `ERROR: {e}` over a local ValueError or a
    provider.GhError; provider.run_gh injects the token through the subprocess
    ENVIRONMENT (GH_TOKEN), never argv, checks the return code by hand rather
    than with subprocess check=True, and its one argv-bearing message joins the
    gh SUBCOMMAND args, which never include a credential. The echo is therefore
    kept unsanitized -- sanitizing it would blunt the diagnostics this retry loop
    exists to surface. The ONE way that changes is GH_DEBUG (or --verbose) on the
    gh calls beneath, which makes gh trace request headers to stderr; nothing in
    the chain sets either today, and adding one means revisiting this echo.
    """
    error = ""
    for attempt in range(1, ATTEMPTS + 1):
        result = subprocess.run(
            ["python3", cli_path, *args], capture_output=True, text=True,
            env=BOARD_CLI_ENV)
        if result.returncode == 0:
            return result.stdout
        error = result.stderr.strip()
        if attempt < ATTEMPTS:
            _sleep_before_retry(attempt)
    sys.stderr.write("  board_cli {0} failed after {1} attempt(s): {2}\n".format(
        args[0] if args else "?", ATTEMPTS, error))
    return None


class ReadyScope(NamedTuple):
    """One repo's `status:ready` scope, split by what each rule does with it.

    `stale_exempt` is the removal path's input (#1788): issues the two
    exemptions hold back from the per-issue loop that are nonetheless still
    carrying a `needs-estimate`, as {number: title}. Excluding them from the
    loop stopped the flagging; only removal undoes a flag already applied.

    `flagged_now` is every scoped issue already carrying the label, read from
    the same listing that decides scope so the removal path costs no extra
    request.
    """
    ready: Dict[int, str]
    deferred: List[int]
    bug_exempt: List[int]
    flagged_now: Set[int]
    stale_exempt: Dict[int, str]


def ready_issues(repo: str) -> Optional[ReadyScope]:
    """Open `status:ready` issues as a ReadyScope, or None when unreadable.

    That label IS the scope of the readiness contract's Estimate requirement,
    minus the contract's own closed list of exemptions, which this function
    applies to the LIST so an exempt issue never reaches the per-issue loop.

    A BUILDABLE BUG is EXEMPT -- `bug` + any `priority:*`, no `investigate` --
    and the exemption is TESTED here rather than assumed (#1534, #1690). This
    function previously asserted that a buildable bug "is ready by the bug
    exemption and never carries status:ready" and filtered on nothing. The
    AUT-05 autonomous grant (ADR-0100) sets `status:ready` on exactly such
    bugs, so the assumption made the sweep flag the issues it was written to
    leave alone. The contract keys the exemption on the LANE, not on which gate
    granted readiness (the github skill's references/triage.md, "Readiness
    model" -- cited, never restated), so a granted bug is exempt too.

    `priority:3-deferred` is EXEMPT (ADR-0119 D5), on the same rule and for the
    same reason as the per-repo `estimate-check.yml`: a deferred issue is never
    selected for automatic execution by any tool, in any mode (the canonical
    statement is the github skill's references/triage.md, "Readiness model" --
    cited, never restated), and `needs-estimate` exists to feed the readiness
    predicates that pick work. Removing the label re-fires the per-repo check on
    its issues.labeled trigger, so no coverage is lost. Excluded here rather than
    in sweep_repo so a deferred issue never reaches the per-issue loop at all,
    named in this repo's own log line, and RETURNED so the run's final summary
    can roll the count up across every managed repo the way it rolls up every
    other outcome -- an operator reading only the last line otherwise has to
    scroll back through N per-repo lines to learn how many issues were exempt.
    """
    listed = gh("issue", "list", "--repo", repo, "--state", "open",
                "--label", "status:ready", "--limit", "500",
                "--json", "number,title,labels")
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

    # One label-set build per issue, reused by both exemptions below: the set is
    # what every rule here reads, and rebuilding it per rule scales with the
    # number of rules rather than with the input.
    labeled = [(i["number"], {lab.get("name", "") for lab in (i.get("labels") or [])})
               for i in issues]

    deferred = sorted(n for n, labels in labeled if DEFERRED_LABEL in labels)
    bug_exempt = sorted(n for n, labels in labeled
                        if DEFERRED_LABEL not in labels
                        and buildable_bug(labels))
    if deferred:
        print("{0}: {1} open status:ready issue(s) exempt as {2}: {3}".format(
            repo, len(deferred), DEFERRED_LABEL,
            ", ".join("#{0}".format(n) for n in deferred)))
    if bug_exempt:
        print("{0}: {1} open status:ready issue(s) exempt as a buildable "
              "bug: {2}".format(
                  repo, len(bug_exempt),
                  ", ".join("#{0}".format(n) for n in bug_exempt)))
    exempt = set(deferred) | set(bug_exempt)
    titles = {i["number"]: i["title"][:60] for i in issues}
    flagged_now = {n for n, labels in labeled if LABEL in labels}
    return ReadyScope(
        ready={n: t for n, t in titles.items() if n not in exempt},
        deferred=deferred,
        bug_exempt=bug_exempt,
        flagged_now=flagged_now,
        stale_exempt={n: titles[n] for n in sorted(exempt & flagged_now)},
    )


def linked_boards(owner: str, name: str) -> Tuple[List[Tuple[str, int]], bool]:
    """The repo's natively-linked Projects v2 boards as ([(owner, number)], ok).

    The second value says whether the discovery query ANSWERED. An empty list
    with ok=True is "this repo has no linked board"; an empty list with ok=False
    is "we do not know", and the caller must not read the fallback path's
    all-skips as a clean repo on the strength of it (#1445).

    first:100 -- far more than any repo's linked-board count in practice, so
    full pagination is unnecessary at this scale.

    The owner's `login` needs inline fragments: `ProjectV2.owner` is the
    `ProjectV2Owner` INTERFACE, which has no `login` field, so a bare
    `owner{login}` is a schema error that fails the whole query instead of
    returning partial data -- and an always-failing discovery query silently
    turned the batch path below into dead code (#1646). The login is used as the
    board CLI's --owner, so the field cannot simply be dropped.
    """
    data = graphql(
        'query($o:String!,$n:String!){repository(owner:$o,name:$n){'
        'projectsV2(first:100){nodes{number owner{'
        '... on Organization{login} ... on User{login}}}}}}',
        label="board discovery for {0}/{1}".format(owner, name),
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
    return boards, data is not None


def estimates_via_boards(
    cli_path: str, repo: str, boards: List[Tuple[str, int]]
) -> Tuple[Set[int], Set[int], List[str], Evidence]:
    """LINKED path: batch each board's items + Estimate through the board CLI.

    Third return value: the discovered boards that did NOT answer. While that
    list is non-empty, an issue's absence from `has_estimate` may just be a
    missing board's rows, so the caller holds every flag (#1646).

    Fourth: the positive read evidence this path produced (#1445).
    """
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    failed_boards: List[str] = []
    list_field_ok = 0
    board_rows = 0
    for bowner, bnum in boards:
        out = board_cli(cli_path, "list-field", "--owner", bowner,
                        "--number", str(bnum), "--repo", repo,
                        "--field", "Estimate")
        if out is None:
            failed_boards.append("{0} #{1}".format(bowner, bnum))
            continue
        list_field_ok += 1
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                number = int(parts[1])
            except ValueError:
                continue
            board_rows += 1
            on_board.add(number)
            if len(parts) > 2 and parts[2].strip():
                has_estimate.add(number)
    return (on_board, has_estimate, failed_boards,
            Evidence(list_field_ok=list_field_ok, board_rows=board_rows))


def estimates_per_issue(
    cli_path: str, owner: str, name: str, numbers: List[int]
) -> Tuple[Set[int], Set[int], Set[int], Evidence]:
    """FALLBACK path (no natively-linked board): per-issue item-id then value.

    Third return value: issues whose membership or Estimate could not be READ.
    `field-value` prints an empty line and exits 0 for a genuinely unset field, so
    a None return here is unambiguously a failed read, and conflating the two is
    the #1646 defect.

    Fourth: the positive read evidence this path produced (#1445) -- here, the
    per-issue membership probes that answered at all.
    """
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    unverified: Set[int] = set()
    probe_ok = 0
    for number in numbers:
        data = graphql(
            'query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){'
            'issue(number:$i){projectItems(first:10){nodes{id}}}}}',
            label="projectItems probe for #{0}".format(number),
            o=owner, n=name, i=number,
        )
        if data is None:
            unverified.add(number)  # membership unknown, not absent
            continue
        probe_ok += 1
        nodes = (((data.get("data") or {}).get("repository") or {})
                 .get("issue") or {}).get("projectItems") or {}
        nodes = nodes.get("nodes") or []
        if not nodes:
            continue  # not on any board
        on_board.add(number)
        read_failed = False
        for item in nodes:
            out = board_cli(cli_path, "field-value", "--item-id", item["id"],
                            "--field", "Estimate")
            if out is None:
                read_failed = True
                continue
            if out.strip():
                has_estimate.add(number)
                break
        else:
            if read_failed:
                unverified.add(number)
    return on_board, has_estimate, unverified, Evidence(probe_ok=probe_ok)


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


def marker_comment_id(owner: str, name: str,
                      number: int) -> Optional[int]:
    """The id of THIS sweep's own marker comment on the issue, or None.

    Shared by the flag and the un-flag paths so both address the SAME comment:
    one upserts it into the violation wording, the other rewrites that same body
    into the resolved wording. Two lookups would be two chances to drift into
    posting a second marker.

    `sort_by(.created_at)` so "the marker comment" is deterministic. The API's
    array order is not guaranteed, and if two comments ever start with the
    marker the sweep must keep updating the SAME one rather than alternating.
    """
    existing = gh("api", "repos/{0}/{1}/issues/{2}/comments".format(
        owner, name, number),
        "--jq", '[.[] | select(.body | startswith("{0}"))] '
                '| sort_by(.created_at) | [.[] | {{id, body}}]'.format(MARKER))
    if existing.returncode != 0 or not existing.stdout.strip():
        return None
    try:
        found = json.loads(existing.stdout)
        return found[0]["id"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def clear_flag(owner: str, name: str, number: int, title: str,
               reason: str) -> bool:
    """Remove a stale `needs-estimate` and retire its marker comment (#1788).

    Returns True when the removal call SUCCEEDED, so the run's `cleared` count
    reports removals that were made rather than removals that were attempted.
    Deliberately not a claim that the label was there to begin with:
    `--remove-label` is idempotent, so a zero exit does not distinguish
    "removed it" from "it was already gone". Both call sites gate on
    `flagged_now`, read from the same listing that decided scope, so the only
    way to reach here on an unlabeled issue is a race with someone removing it
    by hand in the same seconds -- which over-counts `cleared` by one and
    changes nothing else.

    The removal half of the self-healing rule this module's docstring states.
    Callers reach here ONLY with positive evidence -- an Estimate this run read,
    or an exemption read off the issue's own labels -- never from an unverified
    read, an unread board, or an off-board issue.

    The comment is REWRITTEN, never deleted: deleting is destructive and loses
    the history a reader needs to understand why the label came and went. When
    there is no marker comment at all -- the label was applied by hand, or its
    comment was removed -- the label is still dropped and NOTHING is posted,
    because a first comment from this sweep saying it removed a label it never
    applied is noise on someone else's issue.
    """
    repo = "{0}/{1}".format(owner, name)
    # The comment claims the label was removed, so it is written ONLY after the
    # removal succeeded. Posting it on a failed edit would leave the issue
    # carrying `needs-estimate` under a comment saying it does not -- a false
    # record of this sweep's own action, which is worse than the stale flag it
    # was trying to clear. flag_issue()'s `--add-label` is deliberately left
    # unchecked by comparison: its comment states a fact about the ESTIMATE,
    # which is true whether or not the label landed.
    edit = gh("issue", "edit", str(number), "--repo", repo,
              "--remove-label", LABEL)
    if edit.returncode != 0:
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate removal FAILED ({2}); the label "
            "stands and the marker comment is left as it was. The next sweep "
            "retries.\n".format(number, title, edit.stderr.strip()))
        return False
    comment_id = marker_comment_id(owner, name, number)
    if comment_id:
        gh("api", "-X", "PATCH",
           "repos/{0}/{1}/issues/comments/{2}".format(owner, name, comment_id),
           "-f", "body={0}".format(RESOLVED_TEMPLATE.format(
               marker=MARKER, n=number, reason=reason)))
        print("  #{0} {1!r}: needs-estimate removed ({2}); marker comment "
              "retired.".format(number, title, reason))
    else:
        print("  #{0} {1!r}: needs-estimate removed ({2}); no marker comment "
              "to retire.".format(number, title, reason))
    return True


def flag_issue(owner: str, name: str, number: int, title: str) -> None:
    """Apply the label and upsert the one marker comment on a violation."""
    repo = "{0}/{1}".format(owner, name)
    ensure_label(repo)
    gh("issue", "edit", str(number), "--repo", repo, "--add-label", LABEL)

    comment = COMMENT_TEMPLATE.format(marker=MARKER, n=number)
    comment_id = marker_comment_id(owner, name, number)

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


def vacuity_cause(boards: List[Tuple[str, int]], disc_ok: bool,
                  evidence: Evidence) -> str:
    """Why this repo verified nothing -- most likely cause first (#1445).

    Naming a cause is the whole point of the annotation: "inconclusive" without
    one sends an operator reading 40 repos' worth of log instead of checking one
    token's permissions.
    """
    if not disc_ok:
        return ("the board-discovery query failed -- check "
                "ORG_ESTIMATE_CHECK_TOKEN's Projects: Read permission and its "
                "resource owner")
    if boards and evidence.list_field_ok == 0:
        return ("board_cli.py list-field failed on all {0} discovered "
                "board(s)".format(len(boards)))
    if boards and evidence.list_field_ok < len(boards):
        # Partial failure: calling this "zero items" would send an operator
        # looking at the wrong thing.
        return ("{0} of {1} discovered board(s) failed to answer and the rest "
                "returned zero items".format(
                    len(boards) - evidence.list_field_ok, len(boards)))
    if boards:
        return "every discovered board returned zero items"
    return "every per-issue projectItems query failed"


def sweep_repo(cli_path: str, owner: str,
               name: str) -> Tuple[int, int, int, int, int, int, int, int]:
    """Check one repo.

    Returns (flagged, compliant, skipped_no_board, unverified, deferred_exempt,
    bug_exempt, inconclusive, cleared) -- `inconclusive` a 0/1 flag so `main()`
    can sum it like the rest, and `cleared` the stale `needs-estimate` labels
    this repo's run removed (#1788). The two exemptions are counted SEPARATELY
    rather than summed: they
    are different rules with different remedies, and an operator reading the
    rollup needs to know which one held an issue back (#1690).
    A repo is INCONCLUSIVE when this run produced no verified Estimate for it
    AND no evidence that any board answered (#1445), including the case where
    its issue list could not be read at all.
    """
    repo = "{0}/{1}".format(owner, name)
    listed = ready_issues(repo)
    if listed is None:
        # ready_issues() already said why on stderr. The repo was not swept, so
        # it must not roll up as a clean one.
        print("{0}: INCONCLUSIVE -- the status:ready issue list could not be "
              "read; nothing was verified in this repo.".format(repo))
        return (0, 0, 0, 0, 0, 0, 1, 0)
    ready = listed.ready
    exempt_count = len(listed.deferred)
    bug_exempt_count = len(listed.bug_exempt)
    print("{0}: {1} open status:ready issue(s) to check.".format(
        repo, len(ready)))

    # SELF-HEALING, the exemption direction (#1788). An issue an exemption now
    # holds back can still be carrying a flag from before that exemption applied
    # to it -- #1690's buildable-bug exemption landed after AUT-05 had already
    # granted such bugs `status:ready` and this sweep had already flagged them,
    # and an issue deferred after being flagged is the same shape.
    #
    # Runs BEFORE any board read and independently of one: the exemption is
    # decided entirely on the issue's own labels, so its evidence is complete no
    # matter what the boards do this run. That is also why it is not gated on
    # the empty-ready early return below.
    cleared = 0
    for number, title in listed.stale_exempt.items():
        if clear_flag(owner, name, number, title, REASON_EXEMPT):
            cleared += 1

    if not ready:
        # Nothing to verify is not the same as verifying nothing: an empty ready
        # set is a legitimate clean zero, exactly as it is per-repo.
        return (0, 0, 0, 0, exempt_count, bug_exempt_count, 0, cleared)

    boards, disc_ok = linked_boards(owner, name)
    unverified: Set[int] = set()
    failed_boards: List[str] = []
    if boards:
        on_board, has_estimate, failed_boards, evidence = estimates_via_boards(
            cli_path, repo, boards)
    else:
        on_board, has_estimate, unverified, evidence = estimates_per_issue(
            cli_path, owner, name, list(ready))

    flagged = compliant = skipped_no_board = unverified_count = 0
    for number, title in ready.items():
        if number in unverified:
            # The read FAILED: neither compliant nor violating, so never flagged
            # (#1646). Named per issue -- silence is what made the false
            # positives unexplainable after the fact.
            unverified_count += 1
            print("  #{0} {1!r}: board Estimate could not be read; left "
                  "untouched (unverified, not a violation).".format(number, title))
            continue
        if failed_boards and number not in has_estimate:
            # A discovered board never answered, so NEITHER conclusion is
            # available for this issue: the unread board could hold its Estimate,
            # or its only membership. Deliberately not keyed on `on_board` --
            # membership itself comes only from the boards that DID answer, so an
            # issue whose sole item lives on the unread board would otherwise be
            # reported as off-board.
            unverified_count += 1
            print("  #{0} {1!r}: {2} discovered board(s) did not answer ({3}); "
                  "left untouched (unverified).".format(
                      number, title, len(failed_boards),
                      ", ".join(failed_boards)))
            continue
        if number not in on_board:
            # Not on any board -- a board-conditional no-op, the same rule every
            # other Estimate/Actual write in the pipeline follows.
            skipped_no_board += 1
            continue
        if number in has_estimate:
            compliant += 1
            # SELF-HEALING, the compliant direction (#1788). The Estimate was
            # READ and is non-empty, so a `needs-estimate` still on the issue is
            # now false and this check is what put it there. A compliant issue
            # that is not flagged is still touched not at all.
            if number in listed.flagged_now and clear_flag(
                    owner, name, number, title, REASON_ESTIMATE_SET):
                cleared += 1
            continue
        flag_issue(owner, name, number, title)
        flagged += 1

    # A 100% unchecked run over a non-empty ready set, with no evidence that any
    # board was actually READ, means this repo verified nothing (#1445). Keep
    # this predicate on one named line: the regression test neutralizes it by
    # name to prove it is load-bearing. `unverified_count` joins the skip count
    # because an issue whose read failed was not verified either.
    if boards:
        board_evidence = evidence.list_field_ok > 0 and evidence.board_rows > 0
    else:
        board_evidence = disc_ok and evidence.probe_ok > 0
    unread = skipped_no_board + unverified_count
    verified_nothing = unread == len(ready) and not board_evidence

    if verified_nothing:
        cause = vacuity_cause(boards, disc_ok, evidence)
        # One unindented line -- GitHub renders a workflow command no other way.
        print("::warning title=Estimate check verified nothing in {0}::All {1} "
              "open status:ready issue(s) went unchecked -- {2} not on a linked "
              "board, {3} whose board read failed -- and no board data was read "
              "at all, so no Estimate was verified. Likely cause: {4}. Treat "
              "this repo as inconclusive, not as a pass. See "
              "knowledge/token-registry.md in a-cx-ai-config.".format(
                  repo, len(ready), skipped_no_board, unverified_count, cause))
        print("{0}: INCONCLUSIVE -- verified nothing. 0 of {1} status:ready "
              "issue(s) had a readable board Estimate; all {2} went unchecked "
              "({3} off-board, {4} unreadable). This is not a pass.".format(
                  repo, len(ready), unread, skipped_no_board, unverified_count))

    return (flagged, compliant, skipped_no_board, unverified_count,
            exempt_count, bug_exempt_count, 1 if verified_nothing else 0,
            cleared)


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

    totals = [0, 0, 0, 0, 0, 0, 0, 0]
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
            # Not swept at all, so it counts as inconclusive rather than
            # silently dropping out of both sides of the verified/unreadable
            # split the summary below reports (#1445). Say so on STDOUT as well:
            # the aggregate points a reader at the per-repo lines, and this is
            # the one inconclusive repo that would otherwise have none -- which
            # would send an operator to the token when the fault is in the
            # targets file.
            print("{0!r}: INCONCLUSIVE -- this target could not be swept at "
                  "all ({1}); that is a targets-file or selector fault, not a "
                  "board read.".format(target, exc))
            totals[6] += 1
            continue
        totals = [a + b for a, b in zip(totals, result)]

    inconclusive = totals[6]
    verified = len(targets) - inconclusive
    if inconclusive:
        # Deliberately does NOT name a cause itself. Repos reach this count two
        # ways -- no board answered, or the target could not be swept at all --
        # and asserting one of them here would misdirect the other.
        print("::warning title=Estimate check verified nothing in {0} of {1} "
              "repo(s)::{0} swept repo(s) produced no verified Estimate. Their "
              "per-repo lines above name the cause for each. The totals below "
              "cover the {2} repo(s) that were actually read -- they are not "
              "org-wide coverage.".format(inconclusive, len(targets), verified))
    if inconclusive == len(targets):
        # Every repo unreadable is the credential-fault shape, not a data
        # condition: one token that lost Projects: Read reaches no board
        # anywhere. Exit non-zero -- this sweep is schedule-only, so a red job
        # costs one weekly annotation (the exit-code question #1445 left open).
        print("\nINCONCLUSIVE -- verified nothing. 0 of {0} swept repo(s) "
              "produced a verified Estimate, so nothing was verified anywhere. "
              "This is not a pass.".format(len(targets)))
        return 1

    summary = ("\nDone. {0} flagged, {1} already compliant, {2} skipped "
               "(not on a linked board).".format(*totals))
    if totals[3]:
        # These issues were skipped ON PURPOSE and must not read as compliant
        # (#1646). One unindented line -- GitHub renders a workflow command no
        # other way.
        print("::warning title=Estimate check could not read every board "
              "Estimate::{0} issue(s) were left untouched because their board "
              "Estimate could not be read after {1} attempt(s). They are neither "
              "verified compliant nor flagged. A read failure is most often "
              "secondary rate limiting; the next scheduled sweep resolves "
              "it.".format(totals[3], ATTEMPTS))
        summary += " {0} unverified (board read failed).".format(totals[3])
    if totals[4]:
        # Same shape the per-repo estimate-check.yml prints, so the two summaries
        # stay readable side by side.
        summary += " {0} exempt ({1}, ADR-0119).".format(totals[4], DEFERRED_LABEL)
    if totals[5]:
        # Counted apart from the deferred exemption above: same "left alone on
        # purpose" outcome, different rule (#1690).
        summary += " {0} exempt (buildable bug, #1690).".format(totals[5])
    if totals[7]:
        # Same conditional-clause shape as the two exemptions above, so an
        # ordinary run's summary stays byte-for-byte what it was (#1788).
        summary += " {0} cleared (stale needs-estimate removed, #1788).".format(
            totals[7])
    if inconclusive:
        # Only when there is something to report, so an ordinary run's summary
        # stays byte-for-byte what it was -- the same rule the exempt clause
        # above follows. This is the clause that keeps "N repos verified"
        # distinguishable from "N repos unreadable" (#1445).
        summary += " {0} repo(s) verified, {1} inconclusive (no board data " \
                   "read).".format(verified, inconclusive)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
