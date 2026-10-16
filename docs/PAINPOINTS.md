# Reverse-engineering pain points and how REAgraph relates to them

This table maps documented reverse-engineering pain points to what REAgraph does
about each. The pain points are reported, not independently verified here; where a
specific REA issue is cited it is linked. For each row the "what REAgraph does"
column is tied to a real module or a stated roadmap item, and the verdict is honest
about whether REAgraph fixes, mitigates, or does not address the problem. REAgraph
is an independent project and is not affiliated with or endorsed by REA.

| # | Reported pain point | What REAgraph does about it | Module / roadmap | Verdict |
|---|---------------------|-----------------------------|------------------|---------|
| 1 | Weak reliability on large software; partial results and timeouts | Does not make the sensor faster. Each finding that does come back is persisted, so repeated runs compound instead of restarting from zero; the planned shared cache keyed by artifact hash means work done once is not re-paid. | `memory` (today); shared-cache (roadmap item 4) | Mitigates |
| 2 | Incomplete findings are misleading; missing a finding does not prove absence | The planned coverage module tracks what the graph actually covers versus what is only inferred or unseen, so gaps are explicit rather than silently read as absence. Confidence (EXTRACTED vs INFERRED) is carried on every node and edge today so unverified parts are visible. | `coverage` (roadmap item 3); confidence in `memory`/`ingest` (today) | Mitigates |
| 3 | Setup depends on Ghidra / Hopper / IDA and varies by OS | REA's own toolchain setup is upstream and out of scope for REAgraph. A hosted or cached path (the shared cache and the lazy re-analysis direction) can mitigate it by serving prior results without a local sensor install for already-analyzed artifacts. | out of scope (upstream); shared-cache / lazy-RE (roadmap) | Mitigates (partly); otherwise upstream |
| 4 | Gigabyte-scale output is hard to digest | The planned summarize module produces a bounded, navigable map of the accumulated graph instead of a full dump; graphify query/path/explain already let an agent ask targeted questions rather than reading everything. | `summarize` (planned); graphify query (today) | Mitigates |
| 5 | Privacy and legal: results go to the model provider and need authorization | Out of scope. REAgraph is a local memory and structure layer and does not change who runs the sensor or where its data flows. Keeping the graph as a local file does avoid adding a new remote sink, but authorization and provider data handling remain the user's and the sensor's responsibility. | out of scope (upstream) | Out of scope |
| 6 | Findings evaporate per session; cost does not compound | Core purpose. `reagraph add` folds each finding into a durable `reagraph.json` with dedup and confidence upgrade, so findings persist across sessions and accumulate. The agent can query memory before paying for another probe. | `memory` (today) | Fixes (for persistence/compounding) |
| 7 | Byte-offset version diff reports everything changed when code moved ([REA #4520](https://github.com/morluto/rea/issues/4520)) | `reagraph diff` identifies nodes by id and edges by `(source, target, relation)`, never by byte offset, so a module that only moved reads as unchanged and only real added / removed / rewired structure shows up. | `diff` (today) | Fixes (this diff class) |
| 8 | Provider-selection friction blocks tasks that should not need deep analysis ([REA #4555](https://github.com/morluto/rea/issues/4555)) | Provider selection is REA's own concern and is upstream. REAgraph can reduce how often deep analysis is needed at all, by answering from accumulated memory first, but it does not remove the selection step itself. | out of scope (upstream); `memory` reduces demand | Out of scope (demand reduced) |
| 9 | Crash on bad metadata discards partial findings (REA Swift ZodError [#4542](https://github.com/morluto/rea/issues/4542)) | The planned live-ingest bridge is designed to fail open: a bad or partial finding is merged for what it does carry rather than taking down the run, so partial work is kept instead of discarded. The ingest layer today is already tolerant of missing fields and never raises on them. | `bridge` fail-open (roadmap item 1); tolerant `ingest` (today) | Mitigates |

## Notes

- Pain points 5, and the setup and provider-selection parts of 3 and 8, are
  upstream of REAgraph or outside its scope. They are listed for honesty, not
  because REAgraph claims to solve them.
- "Fixes" is used only for problems REAgraph directly removes within its layer:
  persistence and compounding (6) and the byte-offset diff class (7). Everything
  else is "mitigates" or "out of scope".
- Rows that point to planned modules (coverage, summarize, bridge) describe roadmap
  work, not shipped behavior. See [ARCHITECTURE.md](ARCHITECTURE.md) and
  [DESIGN.md](DESIGN.md) for what exists today versus what is planned.
