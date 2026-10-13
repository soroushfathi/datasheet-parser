# AMS1117 runtime and quality checks — 2026-10-12

The runtime repair is deployed, but a request deadline alone does **not** establish
a successful complete-output SLA below 30 minutes. Model, reasoning, source-input
cap and independent review policy are retained: `openai/gpt-5.5`, `medium`, three
inference workers, 20,000 locally estimated input tokens, all 17 sections and 51
requirements. There is no automatic evidence expansion.

## Recorded tests

| Test | Preparation and target | Observed result |
| --- | --- | --- |
| [Original job](http://185.243.48.232:8095/?job=e5ad7e509ffc43f4af87d7e535ce03a4) | Existing parse, AMS1117 / SOT-89-3L | Completed after about 5m41s **additional recovery processing**; 27 stages already existed. This is not full cold-run timing. 17 sections, 51 requirements, 186 claims. |
| [First fresh extraction](http://185.243.48.232:8095/?job=43101cf922584c2c9e3299c75125d5db) | Eight newly rendered pages under `regions-2/pdf-glyphs-1`; no extraction checkpoints reused; AMS1117 / TO-252-2L | Complete output after about **35m27s upload-to-output**, including recovery and development pauses. The first attempt failed at about 14m43s. This does not pass the 30-minute target. 194 claims. |
| [Final-profile extraction](http://185.243.48.232:8095/?job=37671e35266b4ea8a710ecc0e7a96bcf) | Cached preparation/discovery, new extraction; AMS1117 / TO-252-2L | First attempt failed at about **27m29s upload-to-failure**, with 38/51 completed stages. A later recovery reached 49/51 and exposed finding/claim ID confusion in two reviews. These incomplete attempts are not successful latency measurements. |
| [Isolated cold benchmark](http://185.243.48.232:8095/?job=05e4627de03f4906906e7e15bf2d46f5) | Explicit multipart `fresh_run=true`, verified unique parse namespace and eight new page-render events; unchanged source bytes; AMS1117 / SOT-223-3L | First attempt failed at **19m14s**, 47/51 stages saved. Recovery reached **48/51** and failed at **23m39s upload-to-failure**. All 17 sections were extracted, but reviews 2, 9 and 14 remain incomplete. Upstream HTTP 503/504 prevented complete output. A separate tiny generation request also returned 503 with quota/credit terms in its error body. This is not a successful under-30-minute benchmark. |

All tests use the same PDF bytes, SHA-256
`a34b23f7ab5736914b6ce26c36cdb695d2c1294c7422f58de94a01c19f273243`.
The earlier page uploads with `?fresh=1` did not record a benchmark namespace, so
they cannot be called isolated cold benchmarks merely because the page URL had
that parameter. The explicit query and multipart API flags are covered by tests.

## What was fixed

- Negative findings lost their already-inspected source scope before review.
  Section 12 originally received zero aliases/images; the repaired preflight kept
  all 32 original regions and three images at approximately 6,389 input tokens.
- Target provenance could leak into source citations. Quality prompts now contain
  target identity; each request independently validates source references against
  its exact supplied evidence allowlist.
- Raw shape-valid but semantically invalid responses could become sticky caches.
  Rejections are quarantined, correction is bounded, and archived answers require
  the same request fingerprint and full semantic revalidation before reuse.
- Streaming heartbeats and long transport retries could extend old requests for
  hundreds of seconds. The demo now closes requests after a 240-second total wall
  allowance, with 180 seconds per attempt and at most two attempts. Correction
  shares the allowance; unrelated sections can continue after recoverable failures.
- Reviews confused requirement IDs such as `12.clocks` with original claim IDs,
  or referred to `INDEPENDENT:6.4` as an original claim. Request-specific schemas
  constrain references, an empty claim list requires empty support, and findings
  or independent-reader issues use null claim references with explanations.
- Four numeric bounds in the first TO-252 output silently normalized questionable
  printed numbers. The export guard withholds unconfirmed bounds, marks them
  ambiguous and keeps the model draft in audit metadata. The original artifact
  remains archived; the published result was revalidated without model calls and
  without changing its original benchmark timestamps.

Actual failures included upstream HTTP 504, `RemoteProtocolError`, connection
errors and requests cancelled at the wall deadline. Some old attempts spent 639
or 737 seconds on a failed transport before a much shorter successful retry.
These observations explain substantial delay, but do not identify which internal
component of the external router/model infrastructure caused it.

## Quality evidence and limits

All eight source pages were visually inspected. Source-scoped probes are provided
for [SOT-89](../references/evaluations/AMS1117-SOT89-source-probes.json),
[SOT-223](../references/evaluations/AMS1117-SOT223-source-probes.json) and
[TO-252](../references/evaluations/AMS1117-TO252-source-probes.json).

The recovered original and guarded first-fresh output had 19 comparable numeric
probes: 18 were located in both outputs and one (`output_capacitor`) remained a
manual representation/naming check in both. Four SOT-89-specific probes were excluded because the first
discovered package changed to TO-252. This is a **limited presence comparison**,
not an accuracy percentage, a full human gold standard, or proof that every
claim, qualifier, pin mapping and circuit edge is correct.

The guarded TO-252 result remains `needs_review` with 103 issues: these are review
findings/diagnostics, not 103 independently proven factual errors. Examples that
still need source review include package pin mapping, physical pad assignment in
the application circuits, uncertain printed numeric cells, and incomplete visual
coverage. The source-only numeric guard covers text-backed numeric tables; it
does not certify row/column roles or table-unit assignments, and does not adjudicate
image-only tables.

The final local guard revision recognizes exact prefix conversions of explicit
unit-bearing source literals, such as `10 kHz = 10000 Hz`; it does not evaluate
engineering formulas or infer units for unlabeled table cells. This revision passed
offline checks and was deployed on 2026-10-13 before testing the different
Advanced Monolithic Systems employer PDF. During the cold
test's quality audit, the model placed ripple/noise test frequencies under
`12.clocks`; the independent source review correctly flagged that classification.
These remaining mistakes prevent an overall no-regression claim.

Run the scoped comparison with locally downloaded outputs:

```powershell
uv run python scripts/compare_ams_runtime.py --before outputs/AMS1117-SOT89-recovered.json --after outputs/AMS1117-TO252-first-fresh-guarded.json --probes references/evaluations/AMS1117-SOT89-source-probes.json --output outputs/AMS1117-first-fresh-guarded-comparison.json
```

Same-package comparisons should use the corresponding package's probes. Inspect
`manual_check` and regression candidates against the source before accepting any
score. Numeric-role, condition and connectivity checks still require manual review.

Validation after the contract and explicit-unit-conversion fixes: **498 offline tests passed** and
`ruff check src tests scripts` passed. Offline tests verify software contracts,
not real extraction accuracy or provider availability.
