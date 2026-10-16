# REAgraph benchmarks

Three small, self-contained benchmarks. Each prints real numbers measured on the
machine you run it on. They exist to show where REAgraph spends time and where it
saves it, not to produce a marketing figure, so read the caveats at the bottom
before quoting anything.

Run any of them with the project's virtualenv:

```
.venv/bin/python bench/bench_scale.py
.venv/bin/python bench/bench_quality.py
.venv/bin/python bench/bench_cache.py
```

All three are stdlib-only and clean up any temporary files they create.

## bench_scale.py

Measures how core graph operations scale as the graph grows: build/merge time
and memory against node and edge count, on synthetic graphs of increasing size.
Answers "does this stay usable as the graph gets big, and how does it grow?".

## bench_quality.py

Measures the quality of the graph analysis itself (for example community
detection or alignment) against synthetic graphs with a known ground truth, so
the output can be scored rather than eyeballed. Answers "is the structure it
finds the structure that's actually there?".

## bench_cache.py

Measures the cache win. REAgraph's expensive step is *analysis*: running a REA
tool over an artifact to produce a graph fragment. The same artifact analysed the
same way always yields the same fragment, so `reagraph.cache` +
`reagraph.cached.cache_analysis` turn a repeat analysis into a content-addressed
lookup. The first analysis of an artifact is a cache miss (run once, store it);
every later one is a hit that returns the stored fragment without calling the
analyzer at all.

The benchmark analyses the same artifact N times (default 10), once without the
cache and once with it, and reports:

- analysis calls made (N without the cache vs 1 with it) and the share avoided,
- wall time of each run and the resulting speedup,
- the cache-hit lookup latency on its own (sub-millisecond),
- raw backend get/put throughput over many small entries, and
- a projection of the time saved at realistic REA latencies.

It points `$REAGRAPH_CACHE` at a fresh temporary directory so it never touches
your real cache, and deletes it on exit.

Env knobs:

- `REA_LATENCY` -- seconds per modeled analysis call (default `0.2`)
- `REA_REPEATS` -- how many times the same artifact is analysed (default `10`)
- `REA_THROUGHPUT_OPS` -- entries for the get/put throughput test (default `2000`)

Example (reference run, numbers are machine-dependent):

```
  WITHOUT cache :   10 analysis calls   wall 2.035s
  WITH cache    :    1 analysis calls   wall 207.84ms   (1 miss + 9 hits)
  calls avoided : 9 of 10 (90%)
  speedup       : 9.8x
  per hit (median): 81.5us  -- vs 200.00ms to re-analyse (2,455x cheaper)
```

The win is `(repeats - 1) * real-REA-latency`, and it compounds every time an
artifact is re-analysed -- across sessions, across teammates (with a shared
backend), and in CI.

## Caveats (read before quoting any number)

- **Synthetic graphs.** bench_scale and bench_quality run on generated graphs
  with known properties, not real-world artifacts. Real inputs vary.
- **REA latency is modeled, not real.** bench_cache does not run a real REA
  analysis. It stands one in as a function that sleeps for `REA_LATENCY` (default
  0.2s). A real REA analysis takes seconds to minutes, so the raw time and
  speedup numbers are only as meaningful as the latency you plug in. What is real
  is the *shape* of the win: the cache turns N analyses into 1, and the saving is
  `(N - 1) * real-REA-latency`.
- **Numbers are machine-dependent.** Wall times, throughput, and hit latency
  depend on your CPU, disk, and load. Re-run locally rather than trusting the
  figures above; treat them as orders of magnitude, not guarantees.

## bench_e2e.py (full workflow, end to end)

`python bench/bench_e2e.py` runs the complete pipeline over a real MCP stdio
transport -- the real reagraph client -> adapter -> bridge -> cache ->
summarize / coverage / diff / attest -- timing each stage. The REA analysis engine
is a stand-in MCP server (`bench/fake_rea_server_e2e.py`, which speaks real MCP and
returns a realistic JavaScript Application Graph); set
`REAGRAPH_REA_CMD="npx -y rea-agents@latest"` to run the identical workflow against
real REA. Stages: pull v1 (cold) -> pull v1 (cache hit) -> summary -> coverage ->
pull v2 -> diff v1/v2 -> attest v2 vs source (the v2 build carries an injected node
that diff and attest must surface).

Caveats: the analysis is a stand-in with a modeled latency (`REA_LATENCY`; real REA
takes seconds to minutes). The cache win shown end to end is modest because both
pulls still pay the MCP spawn/handshake even on a hit (skipping the spawn when every
tool is already cached is a known optimization); the true win scales with real
analysis latency, which `bench_cache.py` isolates.
