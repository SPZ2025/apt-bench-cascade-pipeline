# Changes

## v2 — 2026-10-28

- Preserve all baseline-one keep passages; remove the half-of-total cap.
- Restrict paragraph parents to non-passing passages (drop or unresolved need_more).
- Enforce zero or one paragraph per parent, 100–300 E5 tokens, with whole paragraphs preferred.
- Select approximately 200 sentences per author after semantic review and source-ordered uniform sampling. Record shortfalls without lowering standards.
- Preserve source text, exact coordinates, full parent-passage / natural-paragraph backgrounds and cross-target non-overlap.
- Extract the pipeline from the original corpus workspace into a portable project with local inputs and model paths.
- Preserve v1 data and reviews as historical results. No new v2 corpus review is claimed by this release.
