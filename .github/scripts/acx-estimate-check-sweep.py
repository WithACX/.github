#!/usr/bin/env python3
"""Org-central estimate-check sweep (CIW.4, ADR-0101 D3).

Deployed by the org-workflows mechanism to `.github/scripts/` in withACX/.github
and run by `acx-estimate-check-org.yml`. For each A-CX-managed repo the shared
selector picked, this flags every open `status:ready` issue whose board
`Estimate` is empty OR off the Fibonacci scale: it ensures the `needs-estimate`
label exists, applies it, and upserts one marker comment. An issue that is on no
board is never touched -- no label, no comment, no edit.

TWO VIOLATION SHAPES, ONE LABEL (#1920). The sweep used to ask a single question,
is the Estimate non-empty, so an ILLEGAL value read as compliant. It now also
refuses a value off the scale, and both shapes carry `needs-estimate`: the remedy
is identical (size the issue to a legal value, or drop it to `status:draft`), the
self-healing removal clears either one the moment a legal Estimate is read, and a
second label would split one readiness signal into two. The comment names the
offending value, because "off-scale" with no number is not actionable.

THE SCALE IS READ, NEVER RESTATED. `github_config.FIBONACCI_POINTS`, imported
from the same a-cx-ai-config checkout that supplies ACX_BOARD_CLI, parses the
`scale:` key in knowledge/github-project-template.yaml. So this sweep, the
per-repo workflow, and provider.set_field's pre-write gate validate against one
list. An unloadable scale is a deployment fault and exits non-zero, alongside a
missing targets file and a missing board CLI.

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

EVERY MUTATION IS VERIFIED BEFORE THE LOG CLAIMS IT (#1828). The label add, the
comment post, the comment PATCH, and the label create each check their own
return code at the call site; a failure prints what failed instead of asserting
`needs-estimate applied`, and a violation whose label did not land is counted in
`flag_failed` rather than in `flagged`. Deliberately NOT a global `check=True`
exception policy: a transient comment-post failure must not abort the sweep,
which would contradict the warn-and-continue contract every other error path
here keeps. Per-call-site verification with an honest log line and count is the
shape. This is the mutation-side instance of the class #1247, #1445 and #1646
fixed on the reporting and read sides.

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


class RepoTotals(NamedTuple):
    """One repo's outcome counts, and the shape main() sums across repos.

    NAMED rather than a positional tuple (#1920). This started as a 6-tuple and
    reached nine fields; at that width every reader of `main()` was decoding bare
    indices, and inserting or reordering a field had nothing -- no static check,
    no runtime error -- to catch a silent off-by-one in a check that writes labels
    across every managed repo. `Evidence` above set the precedent in this same
    file. `add()` sums two of these field-wise so main() never zips positionally.

    `inconclusive` is a 0/1 flag so it sums like the rest. `flagged_off_scale` is
    a SUBSET of `flagged`, not a separate bucket: it is carried so the org rollup
    can print the same breakdown the per-repo estimate-check.yml prints.

    `flag_failed` is the opposite of a subset: it is DISJOINT from `flagged` and
    counts violations whose `needs-estimate` label could not be applied (#1828).
    It is the mutation-side twin of `unverified`, which #1646 added on the read
    side for the same reason -- an attempt that failed must not be reported as an
    outcome that happened.
    """
    flagged: int = 0
    compliant: int = 0
    skipped_no_board: int = 0
    unverified: int = 0
    deferred_exempt: int = 0
    bug_exempt: int = 0
    inconclusive: int = 0
    cleared: int = 0
    flagged_off_scale: int = 0
    flag_failed: int = 0

    def add(self, other: "RepoTotals") -> "RepoTotals":
        return RepoTotals(*[a + b for a, b in zip(self, other)])


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
# The login a GitHub Actions installation token posts as. The fallback identity
# for the marker-comment author predicate (#1841) -- see posting_login().
BOT_LOGIN = "github-actions[bot]"
# How marker_comment_lookup() classifies what it found. Four states, not a
# nullable id: "no marker comment" and "a marker comment somebody else wrote"
# demand opposite actions, and collapsing them is the #1841 defect.
MARKER_OWN = "own"            # this check wrote it -- safe to rewrite in place
MARKER_FOREIGN = "foreign"    # somebody else wrote it -- never touched
MARKER_NONE = "none"          # the issue carries no marker comment at all
MARKER_UNREADABLE = "unreadable"  # the comments read FAILED -- not a data fact
# Exempt from this sweep entirely -- see ready_issues() for the rule and ADR-0119.
DEFERRED_LABEL = "priority:3-deferred"
LABEL_COLOR = "E5B700"
LABEL_DESCRIPTION = (
    "status:ready issue held in draft pending a size -- see "
    "the github skill references/triage.md"
)
# Matches the per-repo workflow's wording exactly, with {n} the issue number,
# {shape} the violation shape (empty, or off-scale naming the value -- #1920),
# and {scale} the Fibonacci scale rendered from FIBONACCI_POINTS.
COMMENT_TEMPLATE = (
    "{marker} issue #{n} is `status:ready` with {shape}. "
    "An Estimate on the Fibonacci story-point scale ({scale}) "
    "is part of the "
    "readiness contract (ADR-0089): this is a contract violation to fix by "
    "sizing the issue on the project board, or by dropping it back to "
    "`status:draft` -- never by applying a default. This label is removed "
    "automatically once a real Estimate is set and this check reads it "
    "(#1788); removing it by hand is not part of the fix."
)
SHAPE_EMPTY = "an empty board `Estimate`"
SHAPE_OFF_SCALE = "an off-scale board `Estimate` of `{value}`"
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
REASON_ESTIMATE_SET = "its board `Estimate` is now set to a legal value"

# The Fibonacci scale, READ from a-cx-ai-config rather than restated (#1920).
# Populated by load_scale() in main(), from the github_config.py that sits beside
# the ACX_BOARD_CLI this sweep already depends on; that module parses
# knowledge/github-project-template.yaml's `scale:` key, so this sweep, the
# per-repo workflow, and provider.set_field's pre-write gate all validate against
# one list. It stays a module global rather than being threaded through every
# call because the scale is a property of the ORG's board contract, identical for
# every repo in one sweep -- not per-repo state.
#
# "Stdlib only" above is unchanged in substance: github_config imports nothing
# outside the stdlib at module scope, and this sweep already executes that same
# checkout's board_cli.py as a subprocess.
FIBONACCI_POINTS: Tuple[int, ...] = ()


def load_scale(cli_path: str) -> Tuple[int, ...]:
    """Import FIBONACCI_POINTS from the checked-out a-cx-ai-config beside cli_path.

    Raises on failure. main() turns that into the same non-zero exit an unusable
    ACX_BOARD_CLI already produces: an unreadable scale is an unusable INPUT, not
    a transient per-repo error, and silently disabling the off-scale half of the
    check would leave a green run that verified only half of what it claims.

    ESTIMATE_CHECK_SCRIPTS_DIR overrides the derived directory for the regression
    harness, which stands a stub board_cli.py in a temp dir with no repo around
    it. Same name and the same test-seam-not-an-operator-control convention as
    the per-repo workflow, so the two read alike side by side.
    """
    scripts_dir = (os.environ.get("ESTIMATE_CHECK_SCRIPTS_DIR")
                   or os.path.dirname(os.path.abspath(cli_path)))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from github_config import FIBONACCI_POINTS as points  # noqa: E402
    return tuple(points)


def on_scale(raw: str) -> bool:
    """True when a board Estimate string is a legal story point (#1920).

    A value that will not parse as a number is OFF the scale, not unreadable:
    this is only ever handed a value a board read actually RETURNED, so an
    unparseable one is a real illegal value on the board. Failed reads never
    reach here -- they are `unverified` and are never flagged (#1646).
    """
    try:
        return float(raw) in FIBONACCI_POINTS
    except (TypeError, ValueError):
        return False
REASON_EXEMPT = (
    "it is exempt from the Estimate requirement (the github skill's "
    "references/triage.md, \"Readiness model\")"
)


def buildable_bug(labels: Set[str]) -> bool:
    """The readiness model's buildable bug -- exempt from the Estimate (#1690).

    `bug` + any `priority:*` + no `investigate` + no explicit `status:draft`.
    A copy, because this sweep runs where the github skill is not installed:
    `tests/tooling/test_bug_lane_copies_parity.py` pins it, and the per-repo
    `estimate-check.yml` copy, to `provider.classify_bug_lane`.
    """
    return ("bug" in labels
            and any(lab.startswith("priority:") for lab in labels)
            and "investigate" not in labels
            and "status:draft" not in labels)


def gh(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a gh command, capturing output. Never raises on a non-zero exit.

    `check=True` ECHOES a non-zero exit to stderr and still returns the
    completed process, so the caller decides what a failure means. It never
    raises and never aborts the sweep: this file's contract is warn-and-continue,
    and a transient comment-post failure must not cost the remaining repos
    (#1828). Deliberately NOT a global default -- the read paths above are
    already retried and already report their own exhaustion, so echoing there
    would double-report every retried blip. Opting in per call site is what makes
    the MUTATION paths below verifiable without changing the reads.

    Before #1828 this helper had no `check` parameter at all, so no call site
    could ask for a failure to be reported and every mutation's return code was
    discarded -- the log then asserted `needs-estimate applied` for a label that
    may never have landed.

    THE ECHO IS AUDITED, ON THE SAME TERMS AS board_cli()'s BELOW, and this
    reasoning is stated here rather than by reference because the boundary
    condition is what a future reader has to know. Two halves:

      - The ARGV half. `args[:3]` is the gh subcommand (`issue edit`,
        `label create`, `api -X`), and no call site in this file puts a
        credential in argv: `GH_TOKEN` reaches gh through the process
        ENVIRONMENT, and the one token this sweep passes explicitly
        (ORG_ESTIMATE_CHECK_TOKEN, via BOARD_CLI_ENV) also travels by env.
      - The STDERR half. `result.stderr` is gh's own text, which on an ordinary
        API failure is GitHub's error JSON -- a 401/403/404/422 or a rate-limit
        message -- and carries no credential material. THAT HOLDS ONLY WHILE
        GH_DEBUG AND --verbose STAY UNSET on these calls, because either makes
        gh trace request headers (Authorization included) to stderr. Nothing in
        this file sets either today; adding one means revisiting this echo and
        the identical one in the per-repo twin.
    """
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.stderr.write("  gh {0}... failed: {1}\n".format(
            " ".join(args[:3]), result.stderr.strip()))
    return result


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
    exemption and never carries status:ready" and filtered on nothing. Any
    granting path may set `status:ready` on exactly such a bug, so the
    assumption made the sweep flag the issues it was written to leave alone.
    The contract keys the exemption on the LANE, not on which gate granted
    readiness (the github skill's references/triage.md, "Readiness model" --
    cited, never restated), so such a bug is exempt too.

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
) -> Tuple[Set[int], Set[int], List[str], Evidence, Dict[int, str]]:
    """LINKED path: batch each board's items + Estimate through the board CLI.

    Third return value: the discovered boards that did NOT answer. While that
    list is non-empty, an issue's absence from `has_estimate` may just be a
    missing board's rows, so the caller holds every flag (#1646).

    Fourth: the positive read evidence this path produced (#1445).

    Fifth: issue number -> the non-empty but OFF-SCALE value read for it (#1920).
    The VALUE, not a bare set, so the comment can name what is actually on the
    board; "off-scale" with no number tells the reader nothing they can act on.
    """
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    off_scale: Dict[int, str] = {}
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
                if not on_scale(parts[2].strip()):
                    off_scale[number] = parts[2].strip()
    return (on_board, has_estimate, failed_boards,
            Evidence(list_field_ok=list_field_ok, board_rows=board_rows),
            off_scale)


def estimates_per_issue(
    cli_path: str, owner: str, name: str, numbers: List[int]
) -> Tuple[Set[int], Set[int], Set[int], Evidence, Dict[int, str]]:
    """FALLBACK path (no natively-linked board): per-issue item-id then value.

    Third return value: issues whose membership or Estimate could not be READ.
    `field-value` prints an empty line and exits 0 for a genuinely unset field, so
    a None return here is unambiguously a failed read, and conflating the two is
    the #1646 defect.

    Fourth: the positive read evidence this path produced (#1445) -- here, the
    per-issue membership probes that answered at all.

    Fifth: issue number -> the non-empty but OFF-SCALE value read for it (#1920),
    the same shape estimates_via_boards() returns.
    """
    on_board: Set[int] = set()
    has_estimate: Set[int] = set()
    off_scale: Dict[int, str] = {}
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
                if not on_scale(out.strip()):
                    off_scale[number] = out.strip()
                break
        else:
            if read_failed:
                unverified.add(number)
    return (on_board, has_estimate, unverified, Evidence(probe_ok=probe_ok),
            off_scale)


def ensure_label(repo: str) -> bool:
    """Make sure `needs-estimate` exists in `repo`, without ever redefining a
    label the repo already owns.

    Returns True when the label is known to exist -- it was already there, or
    this call created it. False means the create FAILED and is reported as such
    (#1828); the caller does not stop on it, because the label add that follows
    is the mutation whose result actually decides whether the issue is flagged.

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
        created = gh("label", "create", LABEL, "--repo", repo, "--color",
                     LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force",
                     check=True)
        if created.returncode != 0:
            sys.stderr.write(
                "  {0}: could not create '{1}' ({2}); the label add below is "
                "what decides whether this issue is flagged.\n".format(
                    repo, LABEL, created.stderr.strip()))
            return False
        return True

    if ((existing.get("color") or "").lstrip("#").upper() != LABEL_COLOR.upper()
            or (existing.get("description") or "") != LABEL_DESCRIPTION):
        sys.stderr.write(
            "  {0}: '{1}' already exists with a different definition "
            "(color={2!r}, description={3!r}); applying the label and leaving "
            "its definition unchanged.\n".format(
                repo, LABEL, existing.get("color"),
                existing.get("description")))
    return True


_POSTING_LOGIN = []  # type: List[str]


def posting_login() -> str:
    """The login this sweep's own comments are authored by (#1841).

    Resolved from the AMBIENT token -- the same `GH_TOKEN` `gh issue comment`
    posts with -- so the answer describes the identity that will actually appear
    on the comment, not whichever token happened to read the board.

    Two token shapes reach this code and they answer differently, which is why
    neither a hardcoded login nor a bare live lookup is enough on its own:

      - a USER PAT (`ORG_ESTIMATE_CHECK_TOKEN`, this sweep's normal case)
        answers `gh api user` with that user's login;
      - a GitHub Actions INSTALLATION token (what the per-repo twin runs on)
        cannot call `/user` at all -- the call fails outright -- and posts as
        `github-actions[bot]`.

    So the FAILURE of the call is itself the signal, and BOT_LOGIN is the only
    login an installation token can be posting as. Resolved once and memoized:
    the answer cannot change inside a run, and a sweep spans many repos.

    A wrong answer here is not a security hole -- it can only make this check
    read its OWN comment as foreign, which the callers handle by doing nothing.
    It is never able to make somebody else's comment read as ours.

    TWO KNOWN DEGRADATIONS, both failing toward silence rather than damage, and
    both stated here so a later reader does not "fix" one into something worse:

      - A TRANSIENT failure that outlasts the retries lands this sweep on
        BOT_LOGIN, which is the wrong answer for a PAT. Every comment the sweep
        previously posted then reads as foreign for that run, so the #1788
        self-healing goes quiet until the next run. That is why the call is
        retried on the same budget as every other read here rather than
        attempted once: this file's whole read discipline exists because
        secondary rate limiting under concurrent runs is the observed fault.
      - ROTATING `ORG_ESTIMATE_CHECK_TOKEN` to a different user makes every
        marker comment posted under the old login permanently foreign to the new
        one. The sweep stops retiring those specific comments and says so on
        stderr; a human clears them. Deliberately not detected here -- guessing
        at "a login that used to be us" is exactly the author-agnostic match
        this function exists to remove.
    """
    if _POSTING_LOGIN:
        return _POSTING_LOGIN[0]
    login = ""
    for attempt in range(1, ATTEMPTS + 1):
        probe = gh("api", "user", "--jq", ".login")
        if probe.returncode == 0 and probe.stdout.strip():
            login = probe.stdout.strip()
            break
        if attempt < ATTEMPTS:
            _sleep_before_retry(attempt)
    if not login:
        # Never silent: an unlogged fallback is indistinguishable from a live
        # resolution that happened to return the bot, and the two have very
        # different consequences for a PAT-authenticated sweep.
        sys.stderr.write(
            "  `gh api user` did not answer after {0} attempt(s); assuming this "
            "check posts as {1!r}. Correct under a GitHub Actions installation "
            "token, which cannot call /user at all. If this sweep is running on "
            "a user PAT, its own marker comments will read as foreign this run "
            "and will be left alone rather than refreshed.\n".format(
                ATTEMPTS, BOT_LOGIN))
        login = BOT_LOGIN
    _POSTING_LOGIN.append(login)
    return login


def marker_comments(owner: str, name: str,
                    number: int) -> Optional[List[Dict[str, object]]]:
    """Every marker-prefixed comment on the issue, oldest first, with its author.

    Returns a list of `{"id": ..., "login": ...}` entries, or None when the READ
    FAILED. Those are different facts and the callers act on them differently:
    an empty list means the issue genuinely carries no marker comment, while None
    means this run does not know, and a run that does not know must not post.

    `sort_by(.created_at)` so "the marker comment" is deterministic. The API's
    array order is not guaranteed, and if two comments ever start with the
    marker the sweep must keep updating the SAME one rather than alternating.
    """
    existing = gh("api", "repos/{0}/{1}/issues/{2}/comments".format(
        owner, name, number),
        "--jq", '[.[] | select(.body | startswith("{0}"))] '
                '| sort_by(.created_at) '
                '| [.[] | {{id, login: .user.login}}]'.format(MARKER))
    if existing.returncode != 0:
        return None
    if not existing.stdout.strip():
        return []
    try:
        found = json.loads(existing.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(found, list):
        return None
    return [entry for entry in found if isinstance(entry, dict)]


def marker_comment_lookup(owner: str, name: str,
                          number: int) -> Tuple[str, Optional[int]]:
    """Classify the issue's marker comments against this sweep's own identity.

    Returns `(state, comment_id)` where state is one of MARKER_OWN (with the id
    of the comment to rewrite), MARKER_FOREIGN, MARKER_NONE, MARKER_UNREADABLE.

    THE AUTHOR PREDICATE IS THE POINT (#1841). This lookup used to select purely
    on the body prefix, and MARKER is a fixed string visible in this file. Anyone
    who could comment on an issue -- a broader permission than label-write on
    most repos -- could post a comment starting with it and have the next sweep
    PATCH it, overwriting their text with this check's body while GitHub kept
    THEIR name on it. A PATCH does not change authorship, so the result read as
    an official notice attributed to whoever was overwritten: a confused-deputy
    write to content this sweep does not own.

    IT FAILS TOWARD MATCHING, NEVER TOWARD POSTING. The obvious hazard in adding
    a predicate is getting it wrong in the quiet direction: a filter that matches
    nothing makes flag_issue() believe there is no marker comment and POST a new
    one, on every run, forever. So FOREIGN and UNREADABLE are reported as their
    own states rather than folded into "nothing found", and neither call site
    posts on them. If the identity resolution is ever wrong, this sweep's own
    prior comment reads as FOREIGN and the sweep goes quiet -- which is a visible
    log line and a stale comment, not an unbounded comment thread.

    Shared by the flag and the un-flag paths so both address the SAME comment:
    one upserts it into the violation wording, the other rewrites that same body
    into the resolved wording. Two lookups would be two chances to drift.
    """
    found = marker_comments(owner, name, number)
    if found is None:
        return (MARKER_UNREADABLE, None)
    mine = posting_login()
    for entry in found:
        if entry.get("login") == mine and entry.get("id"):
            return (MARKER_OWN, entry["id"])
    if found:
        return (MARKER_FOREIGN, None)
    return (MARKER_NONE, None)


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
    state, comment_id = marker_comment_lookup(owner, name, number)
    if state == MARKER_OWN:
        gh("api", "-X", "PATCH",
           "repos/{0}/{1}/issues/comments/{2}".format(owner, name, comment_id),
           "-f", "body={0}".format(RESOLVED_TEMPLATE.format(
               marker=MARKER, n=number, reason=reason)))
        print("  #{0} {1!r}: needs-estimate removed ({2}); marker comment "
              "retired.".format(number, title, reason))
    elif state == MARKER_FOREIGN:
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate removed ({2}); a comment starting "
            "with the marker is present but was NOT written by this check "
            "({3}), so nothing was retired and it is left exactly as it is "
            "(#1841).\n".format(number, title, reason, posting_login()))
    elif state == MARKER_UNREADABLE:
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate removed ({2}); the comments read "
            "FAILED, so no marker comment was retired. The next sweep "
            "retries.\n".format(number, title, reason))
    else:
        print("  #{0} {1!r}: needs-estimate removed ({2}); no marker comment "
              "to retire.".format(number, title, reason))
    return True


def flag_issue(owner: str, name: str, number: int, title: str,
               off_scale_value: Optional[str] = None) -> bool:
    """Apply the label and upsert the one marker comment on a violation.

    `off_scale_value` names the illegal value when this is the OFF-SCALE shape
    (#1920); None is the original empty-Estimate shape. One label covers both
    deliberately -- the remedy is the same, and the self-healing removal clears
    either the moment a legal Estimate is read.

    Returns True when the LABEL ADD succeeded, so the run's `flagged` count
    reports flags that were applied rather than flags that were attempted
    (#1828). The label is what the count and the log line both claim, and it is
    the readiness signal the rest of the pipeline reads; the marker comment is
    explanatory. So the comment's own outcome is verified and reported on its own
    line, and it does NOT change this return value -- which is the same split the
    MARKER_FOREIGN branch below has always made, where the label lands and no
    comment is written.

    Every mutation here passes `check=True` at its own call site rather than the
    file switching to a global one: a failed comment post must not abort the
    sweep, which would contradict the warn-and-continue contract this file's
    every other error path keeps.
    """
    repo = "{0}/{1}".format(owner, name)
    # The create's own result is CONSUMED, not merely reported: an add that fails
    # against a label that does not exist is a different fault from one that
    # fails against a label that does, and an operator reading the log should not
    # have to correlate two lines to tell them apart.
    label_ready = ensure_label(repo)
    added = gh("issue", "edit", str(number), "--repo", repo,
               "--add-label", LABEL, check=True)
    if added.returncode != 0:
        cause = ("" if label_ready else
                 " The label could not be created either, so it does not exist "
                 "on this repo.")
        # The mirror of clear_flag()'s rule, in the other direction: the comment
        # tells the reader the issue has been flagged, so writing it over a label
        # that did not land would leave a false record of this sweep's own
        # action. Nothing is written, nothing is counted, and the next sweep --
        # which recomputes the violation from the board -- retries the whole
        # thing.
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate NOT applied -- the label add FAILED "
            "({2}).{3} No comment was written, and this issue is counted as a "
            "failed flag rather than a flagged one. The next sweep "
            "retries.\n".format(number, title, added.stderr.strip(), cause))
        return False

    shape = (SHAPE_OFF_SCALE.format(value=off_scale_value)
             if off_scale_value is not None else SHAPE_EMPTY)
    comment = COMMENT_TEMPLATE.format(
        marker=MARKER, n=number, shape=shape,
        scale="/".join(str(p) for p in FIBONACCI_POINTS))
    state, comment_id = marker_comment_lookup(owner, name, number)

    if state == MARKER_OWN:
        patched = gh("api", "-X", "PATCH",
                     "repos/{0}/{1}/issues/comments/{2}".format(
                         owner, name, comment_id),
                     "-f", "body={0}".format(comment), check=True)
        if patched.returncode != 0:
            sys.stderr.write(
                "  #{0} {1!r}: needs-estimate applied, but the comment refresh "
                "FAILED ({2}); the existing marker comment is left as it was. "
                "The next sweep retries.\n".format(
                    number, title, patched.stderr.strip()))
        else:
            print("  #{0} {1!r}: needs-estimate applied, comment "
                  "refreshed.".format(number, title))
    elif state == MARKER_NONE:
        posted = gh("issue", "comment", str(number), "--repo", repo,
                    "--body", comment, check=True)
        if posted.returncode != 0:
            sys.stderr.write(
                "  #{0} {1!r}: needs-estimate applied, but the comment post "
                "FAILED ({2}); no comment was written this run. The next sweep "
                "retries.\n".format(number, title, posted.stderr.strip()))
        else:
            print("  #{0} {1!r}: needs-estimate applied, comment posted.".format(
                number, title))
    elif state == MARKER_FOREIGN:
        # Neither rewrite nor post. Rewriting is the #1841 confused-deputy write;
        # posting a second one would repeat on every run, because the foreign
        # comment never becomes ours. The LABEL still landed, so the readiness
        # signal is intact -- only the explanatory comment is suppressed.
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate applied, but a comment starting with "
            "the marker is already present and was NOT written by this check "
            "({2}). It is left untouched and no second comment was posted "
            "(#1841); remove that comment to let this check speak again.\n"
            .format(number, title, posting_login()))
    else:
        sys.stderr.write(
            "  #{0} {1!r}: needs-estimate applied, but the comments read "
            "FAILED, so no comment was written this run. The next sweep "
            "retries.\n".format(number, title))
    return True


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


def sweep_repo(cli_path: str, owner: str, name: str) -> RepoTotals:
    """Check one repo.

    Returns (flagged, compliant, skipped_no_board, unverified, deferred_exempt,
    bug_exempt, inconclusive, cleared, flagged_off_scale, flag_failed) --
    `flag_failed` the violations whose label add FAILED, disjoint from `flagged`
    (#1828), `inconclusive` a 0/1
    flag so `main()` can sum it like the rest, `cleared` the stale
    `needs-estimate` labels this repo's run removed (#1788), and
    `flagged_off_scale` the subset of `flagged` that carried an OFF-SCALE
    Estimate rather than an empty one (#1920). That last one is a SUBSET, not a
    separate bucket: it is reported apart so the org rollup can say the same
    thing the per-repo workflow's summary says, which is what keeps the two
    checks readable side by side. The two exemptions are counted SEPARATELY
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
        return RepoTotals(inconclusive=1)
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
        return RepoTotals(deferred_exempt=exempt_count,
                          bug_exempt=bug_exempt_count, cleared=cleared)

    boards, disc_ok = linked_boards(owner, name)
    unverified: Set[int] = set()
    failed_boards: List[str] = []
    if boards:
        (on_board, has_estimate, failed_boards, evidence,
         off_scale) = estimates_via_boards(cli_path, repo, boards)
    else:
        (on_board, has_estimate, unverified, evidence,
         off_scale) = estimates_per_issue(cli_path, owner, name, list(ready))

    flagged = compliant = skipped_no_board = unverified_count = 0
    flagged_off_scale = 0   # the #1920 subset of `flagged`, reported apart
    flag_failed = 0         # the #1828 bucket, DISJOINT from `flagged`
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
        if number in has_estimate and number not in off_scale:
            compliant += 1
            # SELF-HEALING, the compliant direction (#1788). The Estimate was
            # READ and is non-empty, so a `needs-estimate` still on the issue is
            # now false and this check is what put it there. A compliant issue
            # that is not flagged is still touched not at all.
            if number in listed.flagged_now and clear_flag(
                    owner, name, number, title, REASON_ESTIMATE_SET):
                cleared += 1
            continue
        # Violation -- EMPTY or OFF-SCALE (#1920). Counted only when the label
        # actually landed (#1828): `flag_issue` returns False when the add
        # failed, and it has already said what failed on stderr.
        if flag_issue(owner, name, number, title, off_scale.get(number)):
            flagged += 1
            if number in off_scale:
                flagged_off_scale += 1
        else:
            flag_failed += 1

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

    return RepoTotals(
        flagged=flagged, compliant=compliant,
        skipped_no_board=skipped_no_board, unverified=unverified_count,
        deferred_exempt=exempt_count, bug_exempt=bug_exempt_count,
        inconclusive=1 if verified_nothing else 0, cleared=cleared,
        flagged_off_scale=flagged_off_scale, flag_failed=flag_failed)


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

    # The Estimate scale, from the same checkout (#1920). A third deployment
    # fault of the same class as the two above: without it the off-scale half of
    # this check cannot run, and a run that silently skipped half its contract
    # while printing a clean summary is exactly the vacuous-pass shape this file
    # already refuses elsewhere.
    global FIBONACCI_POINTS
    try:
        FIBONACCI_POINTS = load_scale(cli_path)
    except Exception as exc:
        sys.stderr.write(
            "ERROR: could not load the Estimate scale from the a-cx-ai-config "
            "checkout beside {0!r}: {1}. It must provide "
            "skills/github/scripts/github_config.py and "
            "knowledge/github-project-template.yaml.\n".format(cli_path, exc))
        return 1
    print("Estimate scale: {0}".format(
        "/".join(str(p) for p in FIBONACCI_POINTS)))

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

    totals = RepoTotals()
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
            totals = totals.add(RepoTotals(inconclusive=1))
            continue
        totals = totals.add(result)

    inconclusive = totals.inconclusive
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
               "(not on a linked board).".format(
                   totals.flagged, totals.compliant, totals.skipped_no_board))
    if totals.flagged_off_scale:
        # Same clause, same wording, same position as the per-repo
        # estimate-check.yml prints (#1920). The sweep's own docstring binds
        # the two checks to identical behavior, and a rollup that dropped this
        # breakdown would leave org-wide off-scale volume visible on one
        # surface and invisible on the other.
        summary += (" {0} of the flagged carried an OFF-SCALE Estimate "
                    "rather than an empty one (#1920).".format(totals.flagged_off_scale))
    if totals.unverified:
        # These issues were skipped ON PURPOSE and must not read as compliant
        # (#1646). One unindented line -- GitHub renders a workflow command no
        # other way.
        print("::warning title=Estimate check could not read every board "
              "Estimate::{0} issue(s) were left untouched because their board "
              "Estimate could not be read after {1} attempt(s). They are neither "
              "verified compliant nor flagged. A read failure is most often "
              "secondary rate limiting; the next scheduled sweep resolves "
              "it.".format(totals.unverified, ATTEMPTS))
        summary += " {0} unverified (board read failed).".format(totals.unverified)
    if totals.flag_failed:
        # The mutation-side twin of the read-side warning above (#1828). These
        # issues ARE violations -- their Estimate was read -- but the label did
        # not land, so they are not flagged and must not be counted as such.
        # One unindented line -- GitHub renders a workflow command no other way.
        print("::warning title=Estimate check could not apply every "
              "needs-estimate label::{0} issue(s) were identified as violations "
              "but their `needs-estimate` label could not be applied. They are "
              "NOT counted as flagged. A failed mutation is most often a "
              "permissions gap on that repo or secondary rate limiting; the "
              "next scheduled sweep retries it.".format(totals.flag_failed))
        summary += " {0} flag attempt(s) FAILED (label not applied).".format(
            totals.flag_failed)
    if totals.deferred_exempt:
        # Same shape the per-repo estimate-check.yml prints, so the two summaries
        # stay readable side by side.
        summary += " {0} exempt ({1}, ADR-0119).".format(totals.deferred_exempt, DEFERRED_LABEL)
    if totals.bug_exempt:
        # Counted apart from the deferred exemption above: same "left alone on
        # purpose" outcome, different rule (#1690).
        summary += " {0} exempt (buildable bug, #1690).".format(totals.bug_exempt)
    if totals.cleared:
        # Same conditional-clause shape as the two exemptions above, so an
        # ordinary run's summary stays byte-for-byte what it was (#1788).
        summary += " {0} cleared (stale needs-estimate removed, #1788).".format(
            totals.cleared)
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
