from collections import Counter


def summarize_events(events: list[dict]) -> dict:
    counts = Counter(e["status"] for e in events)
    completed = [e for e in events if e["status"] == "completed" and e.get("kind", "llm") == "llm"]
    totals = Counter()
    elapsed = 0.0
    measured = 0
    for event in completed:
        trace = event.get("telemetry", {})
        elapsed += trace.get("elapsed_seconds", 0)
        usage = trace.get("usage")
        if usage is not None:
            measured += 1
            totals.update({k: v for k, v in usage.items() if isinstance(v, (int, float))})
    latest = {}
    for event in sorted(events, key=lambda e: e["time"]):
        latest[event["stage"]] = event["status"]
    return {
        "events_by_status": dict(counts),
        "latest_stage_status": latest,
        "completed_inference_seconds": elapsed,
        "reported_usage_totals": dict(totals),
        "completed_calls_with_usage": measured,
        "completed_calls": len(completed),
        "billed_cost": None,
        "code_stage_seconds": sum(
            e.get("telemetry", {}).get("elapsed_seconds", 0)
            for e in events
            if e["status"] == "completed" and e.get("kind") == "code"
        ),
        "requests": [
            {
                "stage": e["stage"],
                "status": e["status"],
                "elapsed_seconds": e.get("telemetry", {}).get("elapsed_seconds"),
                "input": e.get("request_metrics") or e.get("telemetry", {}).get("request_metrics"),
                "usage": e.get("telemetry", {}).get("usage"),
                "attempts": e.get("telemetry", {}).get("attempts"),
            }
            for e in events
            if e["status"] in {"completed", "failed"}
        ],
        "note": "Missing usage is unknown, not zero; subscription calls have no fabricated API dollar cost.",
    }
