# Live PDF sample page

The sample page runs the existing `budget-v1` extraction pipeline through the
supplied API router, using **`openai/gpt-5.5`, medium reasoning, three inference
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
5. Show incremental real events, elapsed wall time and the 51 inference-stage
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
citations and the complete evidence packet stay intact. No evidence expansion or
larger context cap is triggered. If the packed request still cannot fit, it fails.

## Local start

```powershell
uv sync --extra web --extra dev
# Set these in a private, ignored environment file or the process environment:
# DATASHEET_PROVIDER=openai-chat
# DATASHEET_MODEL=openai/gpt-5.5
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
accounting and truncation/refusal rejection. Core tests also cover draft round trips
and bounded review compaction with unchanged source packets.

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
