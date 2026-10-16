# REAgraph attest

> Status: shipped and tested. `reagraph.fingerprint`, `reagraph.align`,
> `reagraph.attest`, and the `reagraph attest --source ... --shipped ...` command
> are in the tree. This was roadmap item 5 ("Structural alignment to source").
> It is strongest where symbols/strings survive (JS/Electron/.NET/WASM) and
> abstains rather than guessing elsewhere, so treat it as high-signal triage, not
> an oracle (see Limitations).

## The question

There is one question that almost nobody can answer about software they run:

> Does the shipped artifact match the source I reviewed?

You read the source. You approved the pull request. Then a build pipeline, a
package registry, a vendoring step, or a compromised dependency turns that source
into the bytes that actually ship, and the link between what you reviewed and what
you run is never checked again. This is the gap that xz-utils, event-stream, and
SolarWinds all walked through: in each case the published source looked fine and
the shipped artifact carried code that was never in the source anyone reviewed.

The textbook answer is a byte-for-byte reproducible build: rebuild from source in
a controlled environment and check that the output hashes match. That works when
you can get it, but reproducible builds are brittle and most teams cannot achieve
them. Toolchain versions, timestamps, build paths, locale, parallelism ordering,
and non-deterministic codegen all move bytes without changing meaning. A single
differing byte makes a hash comparison say "different" and tells you nothing about
whether the difference matters.

attest answers the same question structurally instead of by bytes. It compares the
*shape* of the program (its symbols, call relationships, and string references)
rather than its exact bytes, so it tolerates the normal, benign transforms a build
applies: minification, inlining, dead-code elimination, and renaming. The goal is
not "are these identical" but "is everything in the shipped artifact explained by
something in the source I reviewed, and if not, what is left over."

## How it works

attest overlays two graphs of the same program and aligns them.

1. **Source graph.** Build a knowledge graph of the reviewed source with graphify
   (source becomes nodes and edges: functions, calls, string literals, module
   structure).
2. **Shipped-artifact graph.** Build a graph of the bytes that actually ship by
   pointing REA at the artifact through REAgraph (`reagraph pull <artifact>`). REA
   is the reverse-engineering sensor; REAgraph folds its findings into a node-link
   graph in the same format as the source graph.
3. **Align the two graphs.** Match nodes and edges across the two graphs:
   - **Name and string match** where symbols survive. If a function name, an
     exported symbol, or a distinctive string literal is present on both sides,
     that is a high-confidence anchor. Minified and stripped builds lose some of
     these, but many do not (see limitations).
   - **Structural fingerprints where names do not survive.** For code that was
     renamed or minified past recognition, attest compares Weisfeiler-Lehman
     fingerprints: a hash of each node's local neighborhood (its callers, callees,
     and their shapes), iterated so that the fingerprint captures structure a few
     hops out. Two functions with the same call-neighborhood fingerprint are
     structurally the same even if every identifier was renamed.
   - **Seed-propagation for stripped binaries.** After the confident name and
     unique-fingerprint matches (the "anchors"), alignment grows the mapping
     outward: an unmatched node pairs with the one whose already-matched neighbors
     best correspond, committed only when the choice is mutually unique. This
     recovers interior structure when no symbols survive, at a lower confidence
     tier, and still abstains on a tie rather than guessing.
   - **God-node anchoring.** The large, high-degree hubs of each graph (the "god
     nodes" graphify already identifies) are matched first and used as fixed
     reference points, so the rest of the alignment is constrained and does not
     drift.
   - **Every match carries a confidence.** A name-and-string match is stronger
     than a fingerprint-only match. Confidence rides on each matched pair.
   - **Ambiguity abstains.** When two source candidates fit one shipped node
     equally well, attest records no match rather than guessing. An honest "could
     not resolve" is worth more than a confident wrong answer.

4. **Compute the residual.** After alignment, the residual is everything in the
   shipped graph with no confident source explanation: shipped nodes and edges
   that nothing in the reviewed source accounts for. The residual is the
   supply-chain signal.

5. **Score.** The integrity score is the confidence-weighted fraction of the
   shipped graph that source explains. A score near 1.0 means almost all of the
   artifact traces back to reviewed source; a lower score means a larger
   unexplained residual to look at.

```
   source graph (graphify)          shipped graph (REA via reagraph pull)
   +---------------------+          +---------------------+
   |  reviewed source    |          |  actual artifact    |
   |  functions, calls,  |          |  recovered funcs,   |
   |  strings, modules   |          |  calls, strings     |
   +----------+----------+          +----------+----------+
              |                                |
              +---------------+----------------+
                              |
                           align
             (name/string + WL fingerprints +
              god-node anchoring, confidence-weighted,
              abstain on ambiguity)
                              |
              +---------------+----------------+
              |                                |
         residual                      integrity score
   shipped nodes/edges with      confidence-weighted fraction
   no confident source            of shipped graph explained
   explanation                    by source
```

## Reading the output

attest produces three buckets plus the score.

- **Shipped-but-unexplained (the one that matters).** Shipped nodes with no
  confident source match. This is where injected code, hidden telemetry or
  exfiltration, and silently vendored libraries show up: they are in the artifact
  but not in the source you reviewed. Read this bucket first.
- **Unexplained edges.** New flows between nodes that *are* individually explained.
  Both endpoints trace back to source, but the relationship between them does not:
  a call that the source never makes, a new path from input handling to a network
  sink. A malicious change can be entirely made of known-looking nodes wired
  together in a way the source never did.
- **Source-only.** Nodes present in the source graph but not in the shipped graph.
  Usually benign: tree-shaken, dead-code-eliminated, or simply not built into this
  artifact. Worth a glance to confirm the absence is expected, not a surprise.

The integrity score summarizes the whole comparison in one number, but the buckets
are where the work is. A high score with a small, well-understood residual is the
good case; the residual is what you actually investigate.

## Honest limitations

Read these before trusting any output. attest is a triage tool, not an oracle.

- **It is strong where symbols and strings survive, weak where they do not.**
  JavaScript, Electron, .NET, and WASM artifacts keep enough names, exports, and
  string literals that alignment has solid anchors. Stripped native binaries
  (optimized C/C++/Rust with symbols removed) are much harder: fewer anchors,
  heavier reliance on structural fingerprints alone, and correspondingly lower and
  noisier confidence. Treat native-binary results as weaker evidence.
- **"Unexplained" means "could not confidently match," not "proven malicious."**
  A node lands in the residual because alignment found no confident source
  explanation. That can be injected code. It can equally be a legitimate build
  transform attest did not follow, a vendored dependency that is fine, or simply a
  match attest abstained on. attest surfaces candidates with evidence and a
  confidence; it does not accuse. A human decides what the residual means.
- **Alignment quality bounds everything.** Every bucket and the score are only as
  good as the match. Poor alignment inflates the residual with false positives and
  can also hide real problems inside low-confidence matches. The confidence
  numbers are there precisely so you weight the output instead of trusting it flat.
- **It inherits REA's coverage limits.** attest can only reason about the part of
  the artifact REA actually recovered. Whatever the sensor misses, attest cannot
  compare. As with the rest of REAgraph, absence of a finding is not proof of
  absence.

The honest framing: attest is a high-signal triage tool that turns "does the
shipped artifact match the source I reviewed" from a question nobody answers into
a ranked, evidence-carrying list of what in the artifact your source does not
explain. It points you at the right few things to look at. It does not make the
judgment for you.
