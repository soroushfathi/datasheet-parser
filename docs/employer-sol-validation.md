# Employer ds1117.pdf — GPT-6 Sol live validation (2026-10-13)

The live demo uses `cx/gpt-6-sol`, medium reasoning, three inference workers,
`quality-v1`, and the existing 20,000 locally estimated request-input token cap.
The first discovered target is selected; evidence does not automatically expand.
This report separates runtime/transport success from extraction correctness.

## Source and target

The employer's [original upload](http://185.243.48.232:8095/?job=cee8d7d92b01456a864867660366d74a)
is `ds1117.pdf`, eight pages, **Advanced Monolithic Systems, Inc. AMS1117**,
SHA-256 `189a2651878a87d590b768eaa9b44217a3fdf460352ce6ecaff127221282a3f0`.
Its ordering table explicitly lists 21 combinations: seven rows and three packages,
with TO-252 first. The selected first ordering code is **AMS1117CD / TO-252**.
The operating junction range is not a named temperature grade.

This is a different source from the previously tested UMW AMS1117 PDF. For example,
this source prints a 15 V absolute input rating, TO-252 thetaJA 80 °C/W and a lead
soldering condition of 265 °C for 25 seconds. UMW reference probes must not score it.
All eight source pages were visually checked. Separate
[source-scoped probes](../references/evaluations/AMS1117-AdvancedMonolithic-TO252-source-probes.json)
include numeric presence and manual role, applicability, condition and circuit checks.
They are not a complete human gold standard or an accuracy percentage.

## Repairs exposed by the live test

- The initial vision/schema smoke passed, but the actual catalog response renamed
  required fields despite native `response_format`. The exact output schema is now
  repeated in the text for `cx/` requests; strict shape/source/semantic validation
  remains mandatory. Token preflight and fingerprints include that serialization.
- The first catalog response began at approximately 151 seconds and was cut at the
  former 180-second attempt limit. Attempt allowance is now 240 seconds inside the
  same unchanged 240-second total request allowance.
- An HTTP-200 SSE `gateway_timeout` surfaced as SDK `APIError`, bypassing the old
  transport catch. Known in-band gateway codes now receive bounded retry and section
  isolation; unknown error codes are not guessed to be retryable.
- Completed stages persist across manual recovery. Existing runs keep their frozen
  provider/model/reasoning; changing the demo default never silently mixes models.

## Measurements

The [recovered development run](http://185.243.48.232:8095/?job=9d0e72aa47b0499cb912cc2452b8556d)
uses a unique preparation namespace with all eight pages newly rendered. Its wall
clock includes the initial timeout, invalid catalog, transport failure, manual
recovery and repair pauses. Such timing must not be relabeled as a clean cold run.
A failed run under 30 minutes does not pass the complete-output latency target.

The recovered development run completed at 2026-10-13 12:06:44 UTC:
**31m58s upload-to-output**, including all pauses and recovery. This does not pass
an upload-to-output target below 30 minutes. The final recovery processed only two
remaining stages in approximately 71 seconds; that is not full-run latency.

It produced 17 sections, 51 findings, 147 claims and 203 diagnostics with status
`needs_review`. All output source references resolved to exported evidence, and a
browser-download test matched the persisted JSON exactly. Successful validated
inferences, including discovery, reported 593,000 input and 72,217 output tokens
(665,217 total); failed-call usage was not available, so this is a lower bound for
that job, not the total project's billing or a dollar cost.

The scoped presence matcher located 10 of 23 numeric probes. This is **not 43% accuracy**:
it checks values/units/pages and can miss representation differences or match a
wrong numeric role. For example, >10mA in the regulation guarantee cannot certify
the table's MAX 10mA cell. Manual audit confirmed missing pin/table/plot coverage
and an incorrect lead-width interpretation; preserving uncertainty does not make
those omissions disappear.

A [fresh final-profile run](http://185.243.48.232:8095/?job=9def14037e64412f9392dee71dfd2235)
started at 12:07:56 UTC, with a new preparation namespace, all eight pages newly
rendered and no extraction checkpoints reused. Its final timing and aligned
source-probe comparison follow.

### Fresh final-profile result

The fresh run completed at 12:17:25 UTC: **9m29s upload-to-output** (569.345s),
without a stopped job, manual recovery or reused inference checkpoints. All eight
pages were rendered under a new namespace. It produced 17 sections, 51 findings,
144 claims and210 diagnostics, with status `needs_review`. All exported source
references resolved, and the browser-downloaded JSON exactly matched the saved
artifact. This sample passes the runtime target; it does not guarantee the same
latency for all PDFs or provider conditions.

Router-reported usage for 51 successful inferences including discovery was
598,538 input and 74,070 output tokens (672,608 total). Local token estimates are
preflight proxies and exclude unobservable gateway overhead; provider-reported
usage does not establish an actual invoice or dollar cost.

The raw numeric presence comparison showed 21 unchanged probes and two regression
candidates. Manual inspection resolved both candidates as unit-string mismatches:
fresh claim 7.2 retains 6.48–6.73, with `inches(millimeters)` instead of literal `mm`.
That is not lost numeric width data. This does **not** certify the dimensions'
roles, min/typ/max structure or every other claim. A real qualifier problem remains:
fresh claim 9.2 states 1A without retaining the general-description `up to` bound.
Pins are still empty, electrical-table rows remain incompletely retrieved, and
section 16 lacks the six source plots. A no-quality-loss claim is therefore unsupported.

Machine-readable [live measurements](../references/evaluations/employer-sol-live-validation.json)
and [scoped comparison with manual adjudication](../references/evaluations/employer-sol-live-comparison.json)
are saved in source control. The full downloaded outputs are kept locally under
`outputs/employer-sol-{recovered,cold}.json` and available through the public job
result links; they are generated drafts, not human-reviewed gold standards.

## Observed quality limits

An early source audit found incomplete input recall even with the working model:
section 1 received a pin-connection crop that omitted the TO-252 numbering/labels,
while section 16 received the performance heading without the six actual plots.
The output correctly retains uncertainty rather than inventing those values, but
that does not establish complete source coverage. Improvements to initial visual
selection and crop completeness are still required; no automatic expanded retrieval
was enabled to conceal the gap.

The independent review also flagged application-circuit connectivity around Figure 3's
line resistance and omitted capacitor conditions/formula details. A review diagnostic
is not automatically a confirmed factual error: adjudicate it against the source.
A complete schema-valid output can still be `needs_review`. This migration does not
prove that quality improved over a prior valid employer output, because the original
employer job failed before producing one.

Validation: **512 offline tests passed**, `ruff check src tests scripts` passed.
Offline tests verify software behavior, not engineering accuracy or provider uptime.
