# Diff-quality fixtures

These fixtures feed `bench/bench_diff_quality.py`, the DIFF-QUALITY benchmark. It
scores how well `reagraph.diff.diff(graphA, graphB)` recovers the TRUE semantic
change between two builds -- especially the case where two builds are semantically
identical but byte-different, where the ideal diff is empty.

You do not need any fixtures to try the benchmark: run it with no argument and it
synthesizes a tiny corpus in a temp dir, scores it, and self-asserts. Point it at
a manifest to score a real corpus:

```
.venv/bin/python bench/bench_diff_quality.py                 # self-test
.venv/bin/python bench/bench_diff_quality.py path/to/manifest.json
```

## Manifest format

A manifest is a single JSON object with a `pairs` array. Each pair names two graph
files to diff and, optionally, the change you expect between them.

```json
{
  "pairs": [
    {
      "name": "fmt",
      "a": "graphs/base.json",
      "b": "graphs/fmt.json",
      "expected": {"added": [], "removed": [], "changed": []},
      "note": "reformatted, semantically identical -> ideal diff is empty"
    },
    {
      "name": "feat",
      "a": "graphs/base.json",
      "b": "graphs/feat.json",
      "expected": {
        "added": [["endpoint", "notes-collect.evil.example/sync", "src/main.js"]],
        "removed": [],
        "changed": []
      },
      "note": "one endpoint added"
    }
  ]
}
```

### Fields

- `name` -- a short label for the pair, used in the output table.
- `a`, `b` -- paths to the two graph files (A is the base build, B the newer one).
  Paths are resolved **relative to the manifest's own directory**.
- `expected` -- optional. The semantic change you expect, as three lists of
  descriptor tuples: `added`, `removed`, `changed`. Each tuple is
  `[rea_kind, label, source_file]` -- the stable node fields REAgraph stores
  (`rea_kind`, `label`, `source_file`), NOT a node id and NOT a byte offset.
  When present, the pair is additionally scored (precision/recall/F1) against it;
  when absent, only the ground-truth-free scoring runs.
- `note` -- optional free text; ignored by the benchmark.

A descriptor tuple is a **multiset** member: if two nodes genuinely share the same
`(rea_kind, label, source_file)`, list the tuple twice to expect both.

### Graph file format

Each graph file is REAgraph node-link JSON (the `reagraph.memory` format), loaded
with `reagraph.memory.load`:

```json
{
  "directed": true,
  "multigraph": false,
  "nodes": [
    {"id": "a1", "label": "main", "rea_kind": "module",
     "source_file": "src/main.js", "confidence": "EXTRACTED"}
  ],
  "links": [
    {"source": "a1", "target": "a2", "relation": "calls",
     "confidence": "EXTRACTED"}
  ]
}
```

Only `id`, `rea_kind`, `label`, and `source_file` matter for scoring; other fields
are tolerated and ignored. Missing `rea_kind`/`label`/`source_file` are treated as
`""` (the benchmark never crashes on a missing field). The `edges` spelling is
accepted in place of `links`.

> **Why descriptors, not ids.** Real REA node ids are per-build: analyse the same
> source twice and every id can differ. `diff` reports changes by id, so on two
> identical-but-rebuilt graphs it reports "everything added, everything removed".
> The benchmark projects those id changes to descriptor multisets and cancels that
> churn, so a reformatted build scores a descriptor-level spurious count of 0. A
> good fixture corpus therefore uses **realistically churned ids across builds** --
> do not reuse the same ids in A and B, or you hide the very problem being measured.

## What the benchmark reports

Per pair and in aggregate:

- **raw-id spurious** -- id-level changes `diff` reports beyond the true semantic
  count. Large for a rebuilt-but-identical pair (the raw #4520 shape).
- **descriptor-level spurious** -- churn-cancelled descriptor changes that are not
  real. 0 for a semantically-identical pair.
- **precision / recall / F1** of churn-cancelled added+removed detection against
  the true descriptor delta (and against `expected`, when provided).
- an aggregate **F1** (micro-averaged over pairs) and a **noise floor** (mean
  descriptor-level spurious over semantically-identical pairs).

The last line is greppable for an optimizer loop:

```
HEADLINE: aggregate_f1=<x> noise_floor=<y>
```
