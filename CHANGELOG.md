# Changes

## v2.1 — 2026-10-28

- Review one paragraph candidate per unresolved parent per round; try whole natural paragraphs before excerpts, stop at the first usable hit. Agents receive only the current candidate; exports retain the full parent passage.
- Sample sentence candidates in rounds: initially 1.5 times the target (300 by default), then at most 100 per refill, adjusted to the gap and observed usable yield.
- Stop at 200 unique approved sentences or pool exhaustion; never dispatch the same sentence twice. Full paragraph backgrounds may be reused.
- Record reviewed/unreviewed counts, preserve completed rounds and reject premature export. New protocol and prompt bindings require a new run directory.

## v2 — 2026-10-28

- Preserve all baseline-one keep passages; remove the half-of-total cap.
- Restrict paragraph parents to non-passing passages (drop or unresolved need_more).
- Enforce zero or one paragraph per parent, 100–300 E5 tokens, with whole paragraphs preferred.
- Select approximately 200 sentences per author after semantic review and source-ordered uniform sampling. Record shortfalls without lowering standards.
- Preserve source text, exact coordinates, full parent-passage / natural-paragraph backgrounds and cross-target non-overlap.
- Extract the pipeline from the original corpus workspace into a portable project with local inputs and model paths.
- Preserve v1 data and reviews as historical results. No new v2 corpus review is claimed by this release.
