"""Coordinate-preserving PDF ingestion; image-only pages remain visibly unresolved."""

import json
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from pdfplumber.table import Table

from .pdf_text import TEXT_REPAIR_VERSION, prepare_page, repair_metadata
from .store import Store


def ingest(path: Path, store: Store, progress=lambda text: None) -> str:
    data = path.read_bytes()
    doc_id = sha256(data).hexdigest()
    config = {
        "parser": "pdfplumber",
        "version": version("pdfplumber"),
        "render": version("pypdfium2"),
        "scale": 1.5,
        "contract": "regions-2",
        "text_repair": TEXT_REPAIR_VERSION,
    }
    parse_id = sha256((doc_id + json.dumps(config, sort_keys=True)).encode()).hexdigest()[:24]
    try:
        if store.get("parses", parse_id)["status"] == "complete":
            return parse_id
    except KeyError:
        pass
    original = store.artifact(data, "application/pdf", path.name)
    store.put("documents", {"id": doc_id, "original_artifact_id": original, "filename": path.name})
    store.put("parses", {"id": parse_id, "document_id": doc_id, "config": config, "status": "running"})
    raster = pdfium.PdfDocument(data)
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages):
                n = i + 1
                progress(f"Reading and rendering page {n}/{len(pdf.pages)}")
                raw_text = page.extract_text(layout=False) or ""
                prepared, text_repair = prepare_page(page)
                text = prepared.extract_text(layout=False) or ""
                page_id = f"{parse_id}:p{n}"
                bitmap = raster[i].render(scale=config["scale"])
                image = bitmap.to_pil()
                buf = BytesIO()
                image.save(buf, format="PNG")
                image_id = store.artifact(buf.getvalue(), "image/png", f"page-{n}.png")
                # pdfplumber coordinates: PDF points, top-left origin, after page rotation.
                common = {
                    "parse_id": parse_id,
                    "page": n,
                    "coordinate_system": "pdf_points_top_left",
                    "image_artifact_id": image_id,
                }
                store.put(
                    "regions",
                    {
                        "id": page_id,
                        **common,
                        "kind": "page",
                        "text": text,
                        "raw_text": raw_text,
                        "text_repair": text_repair,
                        "bbox": [0, 0, page.width, page.height],
                    },
                )
                spans = []
                region_records = []
                raw_lines = page.extract_text_lines()
                for j, line in enumerate(prepared.extract_text_lines()):
                    rid = f"{page_id}:l{j + 1}"
                    bbox = [line[k] for k in ["x0", "top", "x1", "bottom"]]
                    region_records.append(
                        {
                            "id": rid,
                            **common,
                            "kind": "line",
                            "text": line["text"],
                            "raw_text": raw_lines[j]["text"],
                            "bbox": bbox,
                            "text_repair": repair_metadata(text_repair, bbox),
                        }
                    )
                    spans.append(rid)
                table_ids = []
                for j, table in enumerate(page.find_tables()):
                    rid = f"{page_id}:t{j + 1}"
                    raw_rows = table.extract()
                    # The same detected cells and char geometry govern both views.
                    rows = Table(prepared, table.cells).extract()
                    cells = [list(c) if c is not None else None for row in table.rows for c in row.cells]
                    region_records.append(
                        {
                            "id": rid,
                            **common,
                            "kind": "table",
                            "text": json.dumps(rows),
                            "raw_text": json.dumps(raw_rows),
                            "bbox": list(table.bbox),
                            "rows": rows,
                            "raw_rows": raw_rows,
                            "text_repair": repair_metadata(text_repair, list(table.bbox)),
                            "cells": cells,
                            "relationships_status": "headers_and_footnotes_unverified",
                        },
                    )
                    table_ids.append(rid)
                store.put_many("regions", region_records)
                # Preserve the printed footer as a candidate; never confuse it with PDF page index.
                footer_box = (0, page.height * 0.90, page.width, page.height)
                raw_footer = page.crop(footer_box).extract_text() or ""
                footer = prepared.crop(footer_box).extract_text() or ""
                store.put(
                    "pages",
                    {
                        "id": page_id,
                        "parse_id": parse_id,
                        "number": n,
                        "printed_label_candidate": footer,
                        "raw_printed_label_candidate": raw_footer,
                        "width": page.width,
                        "height": page.height,
                        "rotation": page.rotation,
                        "image_width": image.width,
                        "image_height": image.height,
                        "image_artifact_id": image_id,
                        "text": text,
                        "raw_text": raw_text,
                        "text_repair": text_repair,
                        "line_ids": spans,
                        "table_ids": table_ids,
                        "requires_visual_read": len(text.strip()) < 80 or bool(
                            text_repair["unresolved_glyphs"]
                        ),
                        "preparation_status": "requires_visual_verification"
                        if text_repair["unresolved_glyphs"]
                        else "ready",
                    },
                )
                image.close()
                bitmap.close()
            page_count = len(pdf.pages)
        store.put(
            "parses",
            {
                "id": parse_id,
                "document_id": doc_id,
                "config": config,
                "status": "complete",
                "page_count": page_count,
            },
        )
        return parse_id
    except Exception:
        store.put("parses", {"id": parse_id, "document_id": doc_id, "config": config, "status": "failed"})
        raise
    finally:
        raster.close()


def crop_region(store: Store, region_id: str) -> str:
    from PIL import Image

    region = store.get("regions", region_id)
    page = store.get("pages", f"{region['parse_id']}:p{region['page']}")
    with Image.open(BytesIO(store.read_artifact(page["image_artifact_id"]))) as image:
        sx, sy = image.width / page["width"], image.height / page["height"]
        x0, y0, x1, y1 = region["bbox"]
        box = (
            max(0, int(x0 * sx) - 12),
            max(0, int(y0 * sy) - 12),
            min(image.width, int(x1 * sx) + 12),
            min(image.height, int(y1 * sy) + 12),
        )
        out = BytesIO()
        image.crop(box).save(out, format="PNG")
    return store.artifact(out.getvalue(), "image/png", f"crop-{region_id.replace(':', '-')}.png")
