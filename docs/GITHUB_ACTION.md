# Diff your releases in CI — the REAgraph GitHub Action

A reusable composite action that runs REAgraph's structural, byte-agnostic diff
on two build graphs and surfaces the result: a Markdown summary on the run and,
on pull requests, a bot comment with the **real** change — not the thousands of
per-build-id phantom ones a byte/offset diff would report.

The action lives at [`.github/actions/reagraph-diff`](../.github/actions/reagraph-diff/action.yml).
A copy-pasteable example consumer workflow is
[`.github/workflows/release-diff.yml`](../.github/workflows/release-diff.yml).

## What it does

It runs the same command you would run locally:

```bash
reagraph diff "$base" "$head" --by auto
```

`--by auto` detects REA graphs and matches nodes by a build-stable signature
(kind + range-normalized label + source file), so two real REA builds diff to the
TRUE delta instead of reporting the whole app as changed because node ids embed a
per-build hash. For stable-id graphs (a graphify source graph, or the same build)
it keys on node id. It then:

- writes a heading + the diff (in a fenced block) + a one-line takeaway to the
  **job summary** (`$GITHUB_STEP_SUMMARY`);
- on a `pull_request`, posts or updates a **PR comment** with the same block
  (a stable `<!-- reagraph-diff -->` marker means re-runs update the comment
  instead of piling up duplicates);
- exposes the change count as an **output** so later steps can gate on it.

## Two modes

### 1. Pre-exported graphs (no REA needed)

Commit or publish two reagraph/REA node-link JSON graphs and point the action at
them. This is the zero-dependency path — nothing but `pip install reagraph` runs.

```yaml
- uses: actions/checkout@v4
- uses: Graphify-Labs/REAgraph/.github/actions/reagraph-diff@v1
  with:
    base: graphs/v1.json
    head: graphs/v2.json
```

### 2. Pull from REA in CI (needs REA)

Analyze two actual builds with REA inside the workflow, then diff the graphs it
produces. This needs Docker (for the pinned REA image that `reagraph setup-rea`
builds) and your two build artifacts.

```yaml
- run: |
    python -m pip install "reagraph[live] @ git+https://github.com/Graphify-Labs/REAgraph@main"
    reagraph setup-rea
- env:
    REAGRAPH_REA_CMD: 'docker run --rm -i -v "$PWD":/work:ro reagraph-rea:latest'
  run: |
    reagraph pull /work/build-prev/app.asar --graph base.json --no-cache
    reagraph pull /work/build-new/app.asar  --graph head.json  --no-cache
- uses: Graphify-Labs/REAgraph/.github/actions/reagraph-diff@v1
  with:
    base: base.json
    head: head.json
```

Honest about requirements: mode 1 needs only Python. Mode 2 additionally needs
Docker and REA (via `reagraph setup-rea`), plus the build artifacts to analyze.

## Inputs

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `base` | yes | — | Path to the base build graph (reagraph/REA node-link JSON). |
| `head` | yes | — | Path to the head build graph. |
| `by` | no | `auto` | Node identity for matching: `auto`, `id`, or `signature`. |
| `comment` | no | `true` | When `true` and the event is a `pull_request`, post/update a PR comment. |
| `reagraph-version` | no | git install from the repo | How to install reagraph. Pin to a PyPI spec (e.g. `reagraph==0.1.0`) once published. |

A missing `base`/`head` path fails the step with a clear message.

## Outputs

| Output | Meaning |
| --- | --- |
| `changed` | Real changes = added + removed + rewired nodes. |
| `added` / `removed` / `rewired` | The individual node counts. |

Example — gate a release on there being changes:

```yaml
- id: diff
  uses: Graphify-Labs/REAgraph/.github/actions/reagraph-diff@v1
  with:
    base: graphs/v1.json
    head: graphs/v2.json
- run: echo "Shipped ${{ steps.diff.outputs.changed }} real change(s)"
```

## PR-comment behavior

When the triggering event is a pull request and `comment` is `true`, the action
uses `actions/github-script` to find a prior comment carrying the
`<!-- reagraph-diff -->` marker and **updates it**, or creates one if none
exists. Grant the job `pull-requests: write`:

```yaml
permissions:
  contents: read
  pull-requests: write
```

## Copy-paste: diff on every release

```yaml
name: release-diff
on:
  release:
    types: [published]
permissions:
  contents: read
  pull-requests: write
jobs:
  diff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Graphify-Labs/REAgraph/.github/actions/reagraph-diff@v1
        with:
          base: graphs/previous.json
          head: graphs/current.json
          by: auto
          comment: "true"
```

## A note on installation

reagraph is not on PyPI yet, so the action installs it from the repo by default
(`reagraph @ git+https://github.com/Graphify-Labs/REAgraph@main`). Once it is
published, set `reagraph-version` to a pinned release (e.g. `reagraph==0.1.0`)
for reproducible CI runs.
