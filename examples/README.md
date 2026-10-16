# REAgraph examples

A self-contained demo of the two things REAgraph does today: **persistent
memory** and a **byte-agnostic structural diff**.

## Run it

```bash
bash examples/demo.sh
```

The script uses `./.venv/bin/reagraph` if it exists, otherwise falls back to
`python -m reagraph.cli`. It builds a fresh graph at `/tmp/reagraph_demo.json`
(wiped at the start, so it is safely re-runnable).

## What it shows

- **`add` → persistent memory.** A reverse-engineering finding
  (`sample_findings/login.jag.json`, a small REA JAG doc of a login/auth path)
  is folded into a durable graph. The finding stops living in a chat session and
  starts compounding.
- **`stats` → coverage.** Node/edge counts and the observed (`EXTRACTED`) vs
  inferred (`INFERRED`) confidence split of the accumulated graph.
- **`diff` → byte-agnostic version diff.** `login_v2.jag.json` is the *same*
  program with every symbol relocated (addresses and file layout all shifted),
  plus **one** added exfil edge (`session_create → analytics.evil.example`). A
  byte-offset diff would report "everything changed". REAgraph diffs by
  node/edge identity, so it reports the truth: mostly unchanged, one node added,
  one edge added, `session_create` rewired — the real signal isn't buried.
