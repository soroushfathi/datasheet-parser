# Live PDF sample page

The sample page runs the existing `budget-v1` extraction pipeline through the
supplied API router, using **`cx/gpt-6-sol`, medium reasoning, three inference
workers**. It is separate from the static case study and the Codex CLI workflow.

Public page: http://185.243.48.232:8095

## User flow

1. Upload a PDF, at most 20 MiB and 100 pages.
2. A background job ingests the PDF and discovers target combinations.
   Demo discovery explicitly requests one entry per printed ordering code, preserving
   table row order; grouped codes are rejected before creating a sample run.
3. The page displays the catalog count and **the first catalog entry**: ordering
   code, package and temperature grade. Entries are model/package/grade combinations,
   not necessarily distinct chip families. No second target is silently substituted.
4. If the first entry is ambiguous, stop with an explanation. Otherwise create
   exactly one run and extract, blindly reread and review all 17 sections.
5. Show incremental real events, elapsed wall time and the 51 logical inference-stage
   states. These stage counts are processing progress, not extraction accuracy.
6. Download the complete schema-validated JSON with evidence and unresolved issues.
   `needs_review` and `reviewed` retain their existing meanings; neither certifies
   engineering correctness.

## Implementation

- `src/datasheet_parser/web_demo.py`: FastAPI API and durable Mongo job queue.
- `landing/demo.{html,css,js}`: RTL frontend, terminal progress, section states,
  result preview and JSON download. Browser polls every two seconds using an
  incremental event cursor. It never sends model requests directly.
- `OpenAIChatProvider`: explicit OpenAI-compatible Chat Completions transport,
  vision inputs, strict JSON Schema, streaming assembly and reported usage.
  Refused/truncated/invalid outputs fail validation. Transient transport errors
  retry a bounded number of times. No fallback model or automatic evidence expansion.
- Mongo database `datasheet_demo`: separate from development data. PDFs/results
  use GridFS; job/event records use `demo_jobs`/`demo_events`. Checkpoints live
  under `/var/lib/datasheet-demo`, outside the public assets.
- One job runs at a time; at most two jobs wait. Each run has three inference
  workers. Start **one Uvicorn process**; the queue is not distributed.
- Job URLs contain random 128-bit IDs and act as access links. The browser can
  close/reload and recover the accepted run from `?job=...`. After a service restart,
  queued/running jobs resume from persisted run/checkpoints. A failed job with a
  valid single-target run exposes **Continue this run**; explicitly resuming it
  reuses completed sections and checkpoints instead of discovering a new target.

Review requests that exceed the existing input estimate cap first serialize drafts
as sparse column tables. Repeated long text can use an exact shared string pool.
This is lossless draft serialization: claim IDs, values, units, conditions, source
citations and the complete evidence packet stay intact. If the complete request
still exceeds its fixed cap, `requirement-v1` review partitions divide the section's
requirements into fitting groups. Every part receives the **same complete frozen
source packet** and both readers' **entire answers for its requirements**. Global
pins, circuits and notes remain intact. A requirement is never split into partial
claim rows, because that could create false missing-fact decisions.

All parts are preflighted before any paid request. Completed, validated parts are
saved separately and resumed; final review/coverage is marked complete only after
all parts validate and merge. A negative issue vetoes positive claim support. Part
cache identity includes source-image bytes, aliases, schema, policy, model,
reasoning and output budget. Main progress still represents 51 logical stages;
`review:10:part:1` events provide detail without inflating that count.

No evidence expansion or larger context cap is triggered. If one whole requirement
with its frozen evidence still cannot fit, a typed `review_budget_exceeded` error
reports the section, estimate and program cap before inference.

New optimized runs use [the quality-v1 source workflow](quality-v1.md). Application
figures are inventoried before inference and receive separate extraction, independent
reading and review requests; events such as `extract:2:figure:3` show this detail.
The main progress remains 51 logical stages and does not imply 51 physical API calls.
Raw text repairs, pin normalization, semantic coverage and review reconciliation are
auditable in result metadata. Earlier jobs retain their stored versions on resume.

## Local start

```powershell
uv sync --extra web --extra dev
# Set these in a private, ignored environment file or the process environment:
# DATASHEET_PROVIDER=openai-chat
# DATASHEET_MODEL=cx/gpt-6-sol
# DATASHEET_REASONING=medium
# DATASHEET_WORKERS=3
# DATASHEET_DATABASE=datasheet_demo
# OPENAI_BASE_URL=https://ai-router.intelcad.ai/v1
# OPENAI_API_KEY=<server-only key>
uv run --extra web python -m uvicorn datasheet_parser.web_demo:create_app --factory --port 8095
```

`Settings.load()` reads `.env`. The CLI's existing Codex default is unchanged.
Use `--provider openai-chat` with the same server-only API environment when testing
the router from the CLI. `openai` remains the original Responses API adapter.

## Deployment on dana

The service runs as `datasheet-demo` on port 8095. Code/venv live in
`/opt/datasheet-demo`, writable state in `/var/lib/datasheet-demo`; secrets are in
`/etc/datasheet-demo.env`, owned by root with mode 0600. The service unit is
`landing/deploy/datasheet-demo.service`. No existing nginx, Docker or Mongo service
configuration is changed. The original landing still uses its own port 8094.

```sh
systemctl status datasheet-demo
journalctl -u datasheet-demo --since today
curl http://127.0.0.1:8095/healthz
```

The Mongo URI is private server configuration. If the Mongo container's address
changes, update that URI and restart only this demo service. The API exposes a
static allowlist, not the repository or work directory. Upstream error bodies and
credentials are never included in public errors. Upload body/page/dimension limits,
a bounded queue and service CPU/memory limits constrain the public sample runner.

This is a public demonstration, with bearer job links and HTTP on the requested
port. A wider deployment should add HTTPS, user quotas and retention policies.

## Tests

```powershell
uv run --extra web --extra dev pytest -q
uv run --extra web --extra dev ruff check src tests
```

Tests cover first-target policy (including an ambiguous first target), single-run
creation, event cursors, restart reuse, invalid PDFs, queue limits, secret/static
isolation, explicit resume/double-enqueue prevention, vision/schema transport, usage
accounting and truncation/refusal rejection. Core tests also cover draft round trips,
bounded review compaction, requirement partition coverage, partial resume and cache
identity with unchanged source packets. Concurrent GridFS duplicates are accepted
only after the winning artifact is complete and its bytes/checksum are verified.

Failures expose allowlisted codes and safe numerical details, not upstream bodies:
program budget, provider/network, storage and output validation are distinguished.
Scheduler diagnostics persist the actual failing logical stage and safe source
frames. PDF content, prompts, credentials and arbitrary exception messages never
appear in public errors.

## Verified live test (2026-10-10)

Uploaded the supplied `max3232.pdf` through the deployed browser page. Discovery
returned 29 ordering-code/package entries and selected only `MAX3232CD`, SOIC-16
package `D`, 0 to 70 C. The page displayed the count and choice before extraction,
real incremental events and partial section results, and recovered after reload.
Explicit browser resume and service-restart checkpoint recovery were exercised.

The finished run (`b3749caafc5342d5`, demo job
`2e835ffe2aad4dfb8c6f0dc6c3342786`) reached 51/51 inference stages. Downloading JSON
through the page and validating it with `ExtractionResult` confirmed all 17
sections and the exact set of 51 requirement IDs: 329 claims, 147 recorded issues,
status `needs_review`. These counts are not an accuracy score or human approval.
The browser console had no errors or warnings. Automated checks: **151 tests
passed**, Ruff passed, and the frontend passed Node's syntax check.

During this test, review section 15 exceeded the 20,000-token estimated input cap.
Lossless draft packing reduced that same request from 22,085 to 19,722 estimated
input tokens without dropping its evidence packet. Completed checkpoints were
reused. A stalled router request for review section 9 completed after retrying only
that unfinished stage. The displayed total was about 30 minutes, including a
pause for development and recovery; this is **not a clean uninterrupted latency
benchmark**. A fresh controlled run is needed for a latency or accuracy comparison
against the existing gold standard.

[Tested live output](http://185.243.48.232:8095/?job=2e835ffe2aad4dfb8c6f0dc6c3342786)

## Recovered production run (2026-10-12)

The reported `CY7C64225.pdf` job (`b756c7fa586a413ba560ef9091984d97`, run
`7d7e1e6d2a2b4afa`) had completed 50 logical stages and repeatedly stopped before
review section 10 could reach inference. Its packed request estimate was 20,577
against the program's unchanged 20,000 estimated-input-token cap.

The new planner produced two requirement groups: `10.protocols` (17,353 estimated
input tokens) and `10.address` + `10.levels` (19,035). Both retained the same 253
source aliases/regions and seven images. The live page's Continue button resumed
that same run: scheduler telemetry confirmed **50 stages reused, one logical
stage executed**, with two successful review-part API requests. The downloaded
JSON validated all 17 sections and the exact 51 requirement IDs: 327 claims,
167 recorded issues, status `needs_review`, and no unreviewed sections.

Reported API input usage was 18,612 and 20,294 tokens respectively; the local
preflight is an estimate, not an exact billing/context measure. It must not be
presented as a hard limit on provider-reported tokens. The two recorded model
requests took about 56 and 280 seconds. The page's lifetime clock includes the
previous day's paused run and is not an active-processing latency benchmark.

The recovery page had no browser console errors/warnings, and JSON download was
tested through the UI. Offline regression checks include lossless scope/source
coverage, failed-part resume, changed-image cache invalidation, invalid model IDs,
GridFS races and safe failure diagnostics. The complete suite passed **202 tests**
and Ruff passed.

[Recovered live output](http://185.243.48.232:8095/?job=b756c7fa586a413ba560ef9091984d97)

## Quality rerun of CY7C64225 (2026-10-12)

The original PDF was freshly ingested using the repaired glyph/index contracts.
The new job selected the same first target, `CY7C64225-28PVXC / 28-pin SSOP (PV)`,
and completed all 51 logical stages using the same model/reasoning and worker count.
JSON was downloaded through the live page and validated: 17 sections, the exact
51 requirements, 283 claims, 201 issues, `needs_review`, no unreviewed sections.
The completed page had no browser console errors/warnings; 474 offline tests and
Ruff passed. Both port 8094 and port 8095 services remained active.

Application figure coverage improved from 2 to 7, Table 7 temperature inequalities
were repaired, the complete 17-value baud list was retained, and the Figure 3/4
capacitor wiring was corrected. Total issues rose from 167 to 201 as more circuits
were read and additional structural errors became visible. Fifty unknown-terminal
findings and unresolved WAKE aliases remain; this is not a human-certified output
or an aggregate accuracy score. The detailed before/after evidence and limitations
are in [the quality comparison](quality-v1.md#cy7c64225-live-comparison-2026-10-12).

The displayed 92:48 includes router failures, development pauses and targeted
source rereads. It is not a clean latency benchmark. Recorded repeated gateway
504 failures were recovered using complete-requirement review parts with the same
frozen source, input cap, model and reasoning. Successful checkpoints were reused;
the original job/output was not overwritten.

[New CY7C64225 output](http://185.243.48.232:8095/?job=4b8a50b34bb54029980a9dc33d8231f8)

## Discovery without a printed ordering SKU (2026-10-12)

Job `e5ad7e509ffc43f4af87d7e535ce03a4` uploaded an 8-page `AMS1117.pdf`.
Its discovery request succeeded in about 12 seconds, but returned an empty catalog:
the SKU-preservation instruction was interpreted as requiring complete ordering
codes and SKU-specific grades. The web service then raised generic `ValueError`
for zero targets, incorrectly presenting this as a validation failure. It was not
a timeout, quota failure or malformed JSON response.

Discovery now explicitly allows null ordering codes/grades for model/package
combinations established by source package information or pinouts. Literal SKUs
still remain separate, unsupported voltage/package Cartesian products are forbidden,
and unresolved combinations remain ambiguous. A real discovery-only recheck of
the same parse returned three supported package candidates: SOT-89-3L, SOT-223-3L
and TO-252-2L, with null ordering codes. Output-voltage/package linkage and unreadable
package codes remain unresolved. This test does not certify the final 17-section
extraction, which was not automatically rerun.

An empty catalog now has the specific safe failure code `no_model_targets`.
Only that pre-run failure with a complete saved parse can be explicitly resumed
through the page to rediscover targets. Other pre-run validation failures remain
non-resumable. The original failed catalog is privately archived; no SKU or grade
is fabricated to bypass validation.
The fix passed 481 offline tests and Ruff. Browser verification confirmed that
the affected job displays the specific message and an enabled Continue button;
the button was not pressed during the discovery-only verification.

## Review scope and bounded streaming

The AMS1117 section 12 failure was traced to an empty review packet: the original
reader inspected 32 regions, but absence findings had no positive citations, so
the review planner discarded that scope. Raw target provenance IDs were still in
the prompt and the model cited them despite their absence from the source packet.

`review_safety.py` keeps target identity separate from source citations and retains
previously supplied reader evidence for negative/uncertain findings without cited
claims. No automatic evidence expansion or larger input cap is introduced. Truly
empty reads/reviews are performed by code, remain unresolved, and explicitly say
`skipped_no_source`; they do not support claims or fabricate inspected regions.
The UI distinguishes this state from a performed source review.

`Engine.validated_call` quarantines a schema-valid response that fails semantic
source/claim checks before allowing one bounded correction with the same evidence.
The raw rejected checkpoint is archived privately. A correction that cannot fit the
unchanged input cap is not sent. Known canonical region IDs are accepted only if
they are exactly in that request's source allowlist; target provenance alone is
never sufficient. Completed section/review records remain reusable.

For `openai-chat`, `DATASHEET_LLM_STAGE_DEADLINE` enables a cancellable async
transport. Its monotonic wall deadline includes SSE heartbeats, retries and backoff;
the stream and client close on cancellation. Semantic correction shares the same
remaining wall allowance. The demo uses a 240-second total allowance, a 240-second
attempt limit and at most two transport attempts. Network phase timeouts permit a
quiet reasoning phase within that allowance; they are not the total wall deadline.
Attempt start/failure, first event and first content are safe progress events, with
no prompt/body/credentials in public logs.

`DATASHEET_ISOLATE_STAGE_FAILURES=true` freezes `failure_isolation=section-v1` in
new runs. Recoverable section failures block their dependencies while independent
sections finish. Authentication/storage/internal failures retain fail-fast behavior.
A partial run cannot be exported as a complete result. Existing run policies remain
unchanged unless an explicit repair migration is recorded.

Failed discovery calls caused by transport, deadline or quota errors can also be
manually resumed when a complete parse was already saved. A pre-run recovery uses
the current configured model and updates its displayed model. Existing runs retain
their frozen provider/model/reasoning; a changed demo default does not rewrite them.
Authentication, validation and budget failures before a run remain non-resumable.

The API accepts `POST /api/jobs?fresh=true` for a cold preparation/inference test.
This records a unique parse namespace for the same unchanged PDF bytes, rebuilding
the parse rather than overwriting or reusing earlier records. The page's `?fresh=1`
mode forwards this option during upload. Runtime comparisons must verify actual
render events and checkpoint telemetry, not infer cold execution from a new job ID.
The UI clock uses the latest processing attempt, while benchmark reports measure
the whole upload-to-final-output wall time, including retries/recovery/queue waits.

Source-scoped AMS1117 comparison probes are in
`references/evaluations/AMS1117-SOT89-source-probes.json`; run
`scripts/compare_ams_runtime.py` against the recovered and fresh JSONs. All eight
PDF pages were visually inspected for these probes. They are regression checks,
not a full human gold standard, and numeric/regex matches require manual review
of roles, qualifiers and connectivity before claiming engineering correctness.

An explicit source-inspection refusal with no supported claims is retained as
`declined_source_inspection`, with unresolved issues for its requirements and the
raw review in metadata. It is not source-reviewed. Unknown source/claim references
remain errors. An archived response may be revalidated only against the exact same
request fingerprint and the current semantic validator; changed evidence cannot
reuse that response. Legacy target prompts retain their previous contracts.

Quality reviews now use a request-specific JSON schema: supported claims and
non-null issue claim references are restricted to original claim IDs, and issue
requirements are restricted to the current review scope. An empty original claim
list requires an empty support list. Errors in the independent reader or findings
use a null claim reference with an explicit explanation. This prevents confusion
such as returning `12.clocks` as a supported claim or `INDEPENDENT:6.4` as an
original claim. The actual constrained schema is included in input preflight and
checkpoint fingerprints for both ordinary and partitioned reviews. The semantic
validator still checks claim/requirement associations and source allowlists.

`numeric_source_guard.py` adds a necessary numeric-literal check at quality export.
For text-backed numeric tables, proposed bounds absent from the claim's supplied,
cited source are withheld and
marked ambiguous, with the original model draft retained in audit metadata. Printed
tolerances and scientific literals are recognized; formulas are not evaluated into
invented ratings. This is a conservative guard, not a row/role/units certificate.
For example, a truncated printed `.530` cannot silently become a clean `1.530`
bound. The underlying extraction records and PDF evidence are preserved.
Image-only tables and drawings are left to visual review. Exact SI prefix scaling
is recognized for explicit unit-bearing literals. The guard does not validate row
alignment or infer units for unlabeled table cells; those need separate checks.

Comparisons must align model/package targets. Discovery can return a different first
package when no ordering table fixes the order; package-specific measurements must
then be checked against the selected package's source and excluded from a claimed
cross-package regression score. Separate SOT-89 and TO-252 source probes are included.

See [AMS1117 measured runtime and scoped quality checks](ams1117-runtime-validation.md)
for actual job links, timing definitions and quality limitations. A failed run under
30 minutes is not a successful full-output benchmark.

## Compatible-route schema serialization (2026-10-13)

The deployed model is `cx/gpt-6-sol` with medium reasoning. A vision/schema smoke
passed, but an actual 21-target catalog request returned renamed keys despite
native `response_format=json_schema`. This demonstrates that a small capability
smoke alone cannot certify enforcement for production schemas. The route's internal
handling is not observable, so the observed response establishes a contract failure,
not which upstream component discarded or ignored the format.

Requests for `cx/` models now repeat the exact JSON Schema in the text message while
continuing to send native `response_format`. This is a serialization aid, never a
relaxation of validation or a converter that guesses missing fields. Strict JSON
Schema, source allowlists and semantic checks remain mandatory. The appended schema
is included in token preflight, saved prompts and checkpoint fingerprints, including
figure requests, compact/partitioned reviews and bounded semantic correction.
Source text cannot suppress the appended contract. Existing non-`cx/` prompts retain
their previous fingerprints.

A failed discovery with a saved parse can be manually continued after a format or
transport repair. It does not automatically queue another paid attempt. An existing
run remains pinned to its recorded provider/model/reasoning when the server default
changes; a discovery-only job displays the current model on manual recovery.

The current demo uses 240 seconds for both attempt and total request wall allowance;
an early retry shares that total. The earlier 180-second attempt cut a response that
first produced content at 151 seconds. Increasing the attempt allowance does not
increase the 240-second total cap or prove an under-30-minute complete-output SLA.

The same live test exposed `gateway_timeout` inside an HTTP-200 SSE error event.
The SDK raises `APIError` for that event, rather than `APIStatusError`; previously
that bypassed transport retry and section isolation. Known stream gateway codes
now receive bounded retry and retain a typed recoverable `gateway_error`. Unknown
API error codes fail without a guessed retry. No upstream error message enters
public progress or diagnostics. Tests cover both paths and strict validation.

A Windows cache-sharing race found by the full offline suite is also covered:
verification briefly retries a denied file open after atomic replacement, then
checks the winning bytes' SHA-256. Corrupt or absent winners are never accepted.
Validation of the deployed repairs: **512 offline tests passed** and
`ruff check src tests scripts` passed. These verify software behavior, not model
accuracy or an end-to-end latency guarantee.
