// acx-select-managed-repos.js -- the shared opt-in repo selector for
// org-central A-CX sweeps (CIW.3, ADR-0101 D4).
//
// A sweep hosted in withACX/.github can reach every repo in the org, and the
// org holds public forks and copies of third-party projects that A-CX does not
// run process for. This helper is the one place that answers "which repos may
// this sweep touch", so a second sweep never reimplements the filter and the
// two can never disagree.
//
// Deployed by the org-workflows mechanism to .github/scripts/ in
// withACX/.github. The calling workflow checks out its own default branch and
// loads it:
//
//   const { selectManagedRepos } =
//     require(`${process.env.GITHUB_WORKSPACE}/.github/scripts/acx-select-managed-repos.js`);
//   const targets = await selectManagedRepos({ github, core });
//   for (const { owner, repo } of targets) { /* ... */ }
//
// The list itself is NOT duplicated here. It is read from
// knowledge/acx-managed-repos.yaml in withACX/a-cx-ai-config -- the single
// tracked source, where enrollment is a reviewed diff. The sweep's own
// fine-grained token is scoped to the same list (ADR-0101 D3), and that list
// includes a-cx-ai-config, so the token that may act on the selected repos is
// exactly the token that may read the selection.
//
// FAIL LOUD, NOT QUIET. If the list cannot be read or parses to nothing, this
// throws. A sweep that cannot determine its scope must fail its run visibly,
// never silently sweep nothing (which looks identical to "everything is fine")
// and never fall back to every repo in the org. This is deliberately different
// from the missing-token case, where a workflow logs and exits 0 because the
// feature is not provisioned yet rather than broken.
//
// TRUST ANCHOR. The content of one file on one branch decides where a
// cross-repo sweep may write, so the protection on that branch IS the
// protection on the sweep: `withACX/a-cx-ai-config` requires a reviewed PR to
// change `main`, which is what makes reading the default branch acceptable and
// what makes enrollment a reviewed act. `ref` pins a branch, tag, or commit
// SHA for a caller that wants a slower, explicitly-bumped list; leaving it
// unset takes the default branch, so a list change reaches the next scheduled
// run with no workflow edit. Scope the sweep's token to the same list either
// way (ADR-0101 D3) -- the list decides intent, the token decides reach.
//
// The repo the list is read from is hardcoded and the org acted on is checked
// against it: the trusted source and the write targets can never be pointed at
// different organizations by a caller.

const LIST_OWNER = "withACX";
const LIST_REPO = "a-cx-ai-config";
const LIST_PATH = "knowledge/acx-managed-repos.yaml";

// Parse the flat `repos:` mapping out of the selector file. The file is kept
// deliberately flat (see its header) so this needs no YAML dependency: a
// top-level `repos:` key, then one `  <name>: <evidence>` line per repo until
// the next top-level key.
function parseManagedRepoList(text) {
  const names = [];
  let inRepos = false;
  for (const raw of String(text).split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (/^[A-Za-z][A-Za-z0-9_-]*:/.test(line)) {
      inRepos = /^repos:/.test(line);
      continue;
    }
    if (!inRepos) continue;
    const m = line.match(/^\s+([A-Za-z0-9._-]+):/);
    if (m) names.push(m[1]);
  }
  return names;
}

// Fetch and parse the tracked list. `ref` pins a branch/tag/SHA (default: the
// repo's default branch).
async function readManagedRepoList({ github, ref }) {
  let response;
  try {
    response = await github.rest.repos.getContent({
      owner: LIST_OWNER,
      repo: LIST_REPO,
      path: LIST_PATH,
      ref,
    });
  } catch (err) {
    throw new Error(
      `acx-select-managed-repos: cannot read ${LIST_OWNER}/${LIST_REPO}/${LIST_PATH}` +
        ` (${err.status || "?"} ${err.message}). The token needs Contents: Read on` +
        ` ${LIST_REPO}, which must itself be on the managed list.`
    );
  }
  const data = response.data;
  if (!data || data.type !== "file" || !data.content) {
    throw new Error(
      `acx-select-managed-repos: ${LIST_PATH} did not resolve to a file`
    );
  }
  const text = Buffer.from(data.content, data.encoding || "base64").toString("utf8");
  const names = parseManagedRepoList(text);
  if (names.length === 0) {
    throw new Error(
      `acx-select-managed-repos: parsed no repos from ${LIST_PATH} -- refusing to` +
        ` sweep an empty or unparseable selection`
    );
  }
  return names;
}

// Resolve the list against a live org read and return the repos a sweep may
// act on, as [{ owner, repo }]. Archived, disabled, and forked repos are
// dropped here rather than in the list, so a repo archived today leaves
// coverage today with no list edit. A listed repo missing from the live read
// (renamed, deleted, or out of the token's reach) is reported and skipped --
// it cannot be acted on either way, and failing the whole sweep over one stale
// name would take down coverage for the rest.
async function selectManagedRepos({ github, core, org = LIST_OWNER, ref }) {
  // The list is trusted because of where it is read from. Acting on a
  // different org would apply withACX's selection to repo names that merely
  // collide there, so the two can never diverge.
  if (org !== LIST_OWNER) {
    throw new Error(
      `acx-select-managed-repos: refusing to select repos in '${org}' from a list` +
        ` owned by '${LIST_OWNER}'`
    );
  }
  const names = await readManagedRepoList({ github, ref });
  const live = await github.paginate(github.rest.repos.listForOrg, {
    org,
    type: "all",
    per_page: 100,
  });
  const eligible = new Map(
    live
      .filter((r) => !r.archived && !r.disabled && !r.fork)
      .map((r) => [r.name, r])
  );

  const selected = [];
  const unreachable = [];
  for (const name of names) {
    if (eligible.has(name)) selected.push({ owner: org, repo: name });
    else unreachable.push(name);
  }

  if (core) {
    core.info(
      `acx-select-managed-repos: ${selected.length} of ${names.length} listed repos selected` +
        ` (${live.length} repos read from ${org})`
    );
    if (unreachable.length) {
      core.warning(
        `acx-select-managed-repos: listed but not eligible or not visible --` +
          ` ${unreachable.join(", ")}. Run tooling/verify-managed-repos.py in` +
          ` a-cx-ai-config to reconcile the list.`
      );
    }
  }
  return selected;
}

module.exports = {
  selectManagedRepos,
  readManagedRepoList,
  parseManagedRepoList,
  LIST_OWNER,
  LIST_REPO,
  LIST_PATH,
};
