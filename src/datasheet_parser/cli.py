"""Terminal entrypoint. Domain logic never prompts or depends on a UI."""

import json
import subprocess
import sys
from functools import wraps
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, resolve_codex_executable
from .engine import Engine
from .evaluation import evaluate
from .ingestion import crop_region
from .ingestion import ingest as ingest_pdf
from .models import ExtractionResult, SectionResult
from .providers import PendingResponse, ProviderError, make_provider, strict_schema
from .requirements import SECTIONS
from .smoke import provider_smoke
from .store import MongoStore, materialize
from .telemetry import summarize_events
from .validation import validate_section

app = typer.Typer(
    no_args_is_help=True, help="Evidence-backed datasheet extraction. All 17 sections, explicit uncertainty."
)
console = Console(stderr=True)


@app.callback()
def prepare_terminal():
    """Keep redirected JSON lossless on Windows, including minus signs and Persian notes."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def guarded(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PendingResponse as error:
            console.print(str(error), style="yellow")
            raise typer.Exit(3)
        except (typer.Exit, typer.Abort):
            raise
        except ProviderError as error:
            console.print(str(error), style="red")
            raise typer.Exit(1)
        except Exception as error:  # noqa: BLE001 -- CLI boundary: redact connection credentials
            # Connection strings and provider errors may contain secrets; keep generic failures terse.
            if isinstance(error, (ValueError, KeyError)):
                console.print(str(error), style="red")
            else:
                console.print(
                    f"{type(error).__name__}: operation failed. Check connection or recorded stage logs.",
                    style="red",
                )
            raise typer.Exit(1)

    return inner


def context(provider=None, model=None):
    settings = Settings.load()
    if provider:
        settings.provider = provider
    if model:
        settings.model = model
    store = MongoStore(settings.mongo_uri, settings.database)
    store.ping()
    store.ensure_indexes()
    return settings, store, Engine(store, make_provider(settings), settings, console.print)


@app.command()
@guarded
def doctor():
    """Check CLI authentication and MongoDB access without model inference."""
    settings = Settings.load()
    console.print(f"Database: {settings.database}; provider: {settings.provider}; model: {settings.model}")
    executable = resolve_codex_executable(settings)
    if executable:
        console.print(f"Codex executable: {executable}")
        result = subprocess.run(
            [executable, "login", "status"], capture_output=True, text=True, timeout=15, check=False
        )
        console.print("Codex login: " + ("available" if result.returncode == 0 else "not authenticated"))
    else:
        console.print("Codex CLI: missing")
    store = MongoStore(settings.mongo_uri, settings.database)
    store.ping()
    store.ensure_indexes()
    console.print("MongoDB: connected; project indexes ready", style="green")


@app.command("smoke")
@guarded
def smoke(provider: str | None = None, model: str | None = None):
    """Make one real text+image+JSON inference request; no database required."""
    settings = Settings.load()
    if provider:
        settings.provider = provider
    if model:
        settings.model = model
    result = provider_smoke(make_provider(settings), settings.work_dir)
    typer.echo(json.dumps(result, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)


@app.command()
@guarded
def ingest(pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]):
    """Persist PDF, page images, text lines and table coordinates to MongoDB/GridFS."""
    _, store, _ = context()
    parse_id = ingest_pdf(pdf.resolve(), store, console.print)
    typer.echo(parse_id)


@app.command()
@guarded
def documents():
    """List prepared documents and parse IDs available in the development database."""
    _, store, _ = context()
    names = {d["id"]: d["filename"] for d in store.find("documents")}
    table = Table("Parse ID", "PDF", "Pages", "Status")
    for row in store.find("parses"):
        table.add_row(
            row["id"], names.get(row["document_id"], "?"), str(row.get("page_count", "?")), row["status"]
        )
    console.print(table)


@app.command()
@guarded
def targets(parse_id: str, refresh: bool = False, provider: str | None = None, model: str | None = None):
    """Discover valid model/package combinations or show the existing catalog."""
    _, store, engine = context(provider, model)
    try:
        catalog = store.get("catalogs", parse_id) if not refresh else None
    except KeyError:
        catalog = None
    if catalog is None:
        catalog = engine.discover(parse_id).model_dump(mode="json")
    table = Table("ID", "Ordering code", "Package", "Grade", "Status")
    for t in catalog["targets"]:
        table.add_row(
            t["id"],
            t["ordering_code"] or t["part_number"],
            f"{t['package_name']} ({t['package_code'] or '?'})",
            t["temperature_grade"] or "—",
            t["status"],
        )
    console.print(table)
    for note in catalog["ambiguities"]:
        console.print(note, style="yellow")


def selected_sections(raw):
    if not raw:
        return None
    values = [int(x.strip()) for x in raw.split(",")]
    if any(v not in SECTIONS for v in values) or len(values) != len(set(values)):
        raise ValueError("Sections must be distinct integers from 1 to 17")
    return values


def choose_target(catalog, target=None):
    for note in catalog["ambiguities"]:
        console.print(note, style="yellow")
    if target is not None:
        return target
    supported = [t for t in catalog["targets"] if t["status"] == "supported"]
    for i, t in enumerate(supported, 1):
        console.print(
            f"{i}: {t['ordering_code'] or t['part_number']} / {t['package_name']} "
            f"({t['package_code']}) / {t['temperature_grade']}"
        )
    if not supported:
        raise ValueError("No supported targets; inspect discovery ambiguities")
    choice = typer.prompt("Select target number", type=int)
    if not 1 <= choice <= len(supported):
        raise ValueError("Invalid selection")
    return supported[choice - 1]["id"]


@app.command("run")
@guarded
def run_document(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination for the full 17-section JSON.")],
    target: str | None = None,
    provider: str | None = None,
    model: str | None = None,
):
    """PDF to JSON: prepare, discover, select a target, extract and review all 17 sections, then export."""
    if output.resolve() == pdf.resolve() or output.is_dir():
        raise ValueError("Output must be a JSON file path distinct from the source PDF")
    _, store, engine = context(provider, model)
    parse_id = ingest_pdf(pdf.resolve(), store, console.print)
    typer.echo(f"parse_id={parse_id}")
    try:
        catalog = store.get("catalogs", parse_id)
    except KeyError:
        catalog = engine.discover(parse_id).model_dump(mode="json")
    run_id = engine.create_run(parse_id, choose_target(catalog, target))
    typer.echo(f"run_id={run_id}")
    console.print(f"Destination: {output.resolve()}")
    console.print(f"If interrupted, resume this run with: datasheet resume {run_id} --output <destination>")
    engine.resume(run_id, review=True)
    write_export(store, engine, run_id, output)


@app.command()
@guarded
def extract(
    parse_id: str,
    target: str | None = None,
    sections: str | None = None,
    review: bool = True,
    provider: str | None = None,
    model: str | None = None,
):
    """Select a target and start a resumable extraction. Default: every section with review."""
    _, store, engine = context(provider, model)
    catalog = store.get("catalogs", parse_id)
    target = choose_target(catalog, target)
    ids = selected_sections(sections)
    run_id = engine.create_run(parse_id, target)
    typer.echo(f"run_id={run_id}")
    engine.resume(run_id, ids, review)


@app.command()
@guarded
def resume(run_id: str, sections: str | None = None, review: bool = True, output: Path | None = None):
    """Resume using the exact provider/model settings recorded when the run was created."""
    settings, store, _ = context()
    run = store.get("runs", run_id)
    settings.provider, settings.model, settings.reasoning = run["provider"], run["model"], run["reasoning"]
    engine = Engine(store, make_provider(settings), settings, console.print)
    typer.echo(json.dumps(engine.resume(run_id, selected_sections(sections), review), indent=2))
    if output is not None:
        write_export(store, engine, run_id, output)


@app.command()
@guarded
def status(run_id: str):
    _, store, _ = context()
    run = store.get("runs", run_id)
    rows = store.find("results", {"run_id": run_id})
    reviews = store.find("reviews", {"run_id": run_id})
    events = store.find("events", {"run_id": run_id})
    typer.echo(
        json.dumps(
            {
                "run": run,
                "sections_extracted": len(rows),
                "sections_reviewed": len(reviews),
                "recent_events": sorted(events, key=lambda e: e["time"])[-5:],
            },
            indent=2,
        )
    )


@app.command()
@guarded
def section(run_id: str, section_id: int, output: Path | None = None):
    """Inspect a saved section during a partial run; this is not a final 17-section export."""
    _, store, _ = context()
    record = store.get("results", f"{run_id}:{section_id}")
    run = store.get("runs", run_id)
    record["validation_at_extraction"] = record["validation"]
    record["validation"] = validate_section(
        SectionResult.model_validate(record["section"]),
        run["target"]["id"],
        {r["id"] for r in store.find("regions", {"parse_id": run["parse_id"]})},
        set(record["supplied_region_ids"]),
    )
    try:
        review_record = store.get("reviews", f"{run_id}:{section_id}")
    except KeyError:
        review_record = None
    data = json.dumps(
        {"partial_section_view": True, "result": record, "review": review_record},
        ensure_ascii=False,
        indent=2,
    )
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(data, encoding="utf-8")
    typer.echo(data)


@app.command()
@guarded
def coverage(run_id: str):
    _, store, _ = context()
    rows = store.find("coverage", {"run_id": run_id})
    table = Table("Requirement", "Candidate regions", "Extraction", "Review")
    for row in sorted(rows, key=lambda r: (int(r["requirement_id"].split(".")[0]), r["requirement_id"])):
        table.add_row(
            row["requirement_id"],
            str(len(row["candidate_region_ids"])),
            row["extraction_status"],
            row["review_status"],
        )
    console.print(table)


@app.command()
@guarded
def trace(run_id: str):
    """Summarize live stage state, elapsed inference time, failures and reported token usage."""
    _, store, _ = context()
    store.get("runs", run_id)
    typer.echo(json.dumps(summarize_events(store.find("events", {"run_id": run_id})), indent=2))


@app.command()
@guarded
def inspect(evidence_id: str, open_image: bool = False):
    """Show exact source text, coordinates, and the rendered region crop."""
    settings, store, _ = context()
    region = store.get("regions", evidence_id)
    image_id = crop_region(store, evidence_id)
    path = materialize(store, image_id, settings.work_dir / "cache", ".png")
    typer.echo(json.dumps({**region, "crop_path": str(path)}, ensure_ascii=False, indent=2))
    if open_image:
        typer.launch(str(path))


@app.command()
@guarded
def review(run_id: str, note: str | None = None, evidence: list[str] | None = None):
    """Inspect machine issues; optionally append a human note without rewriting extracted facts."""
    import uuid

    from .engine import now

    _, store, _ = context()
    run = store.get("runs", run_id)
    if note:
        for rid in evidence or []:
            if store.get("regions", rid)["parse_id"] != run["parse_id"]:
                raise ValueError("Human note evidence belongs to another parse")
        store.put(
            "human_notes",
            {
                "id": uuid.uuid4().hex,
                "run_id": run_id,
                "note": note,
                "evidence_ids": evidence or [],
                "time": now(),
            },
        )
    typer.echo(
        json.dumps(
            {
                "reviews": store.find("reviews", {"run_id": run_id}),
                "human_notes": store.find("human_notes", {"run_id": run_id}),
            },
            indent=2,
        )
    )


@app.command()
@guarded
def export(run_id: str, output: Path):
    """Export all 17 sections; preserve unresolved issues and review status."""
    _, store, engine = context()
    write_export(store, engine, run_id, output)


def write_export(store, engine, run_id, output):
    result = engine.export(run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = result.model_dump_json(indent=2).encode()
    output.write_bytes(data)
    blob = store.artifact(data, "application/json", output.name)
    typer.echo(json.dumps({"path": str(output.resolve()), "artifact_id": blob, "status": result.status}))


@app.command("evaluate")
@guarded
def evaluate_command(prediction: Path, gold: Path, output: Path | None = None):
    """Score exported JSON against a separately human-reviewed reference."""
    p = ExtractionResult.model_validate_json(prediction.read_text(encoding="utf-8"))
    g = ExtractionResult.model_validate_json(gold.read_text(encoding="utf-8"))
    result = json.dumps(evaluate(p.model_dump(mode="json"), g.model_dump(mode="json")), indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result, encoding="utf-8")
    typer.echo(result)


@app.command()
@guarded
def schema(output: Path, section: bool = False):
    """Write the versioned final schema or strict model-facing section schema."""
    output.parent.mkdir(parents=True, exist_ok=True)
    value = strict_schema(SectionResult) if section else ExtractionResult.model_json_schema()
    output.write_text(json.dumps(value, indent=2), encoding="utf-8")
    typer.echo(str(output.resolve()))


if __name__ == "__main__":
    app()
