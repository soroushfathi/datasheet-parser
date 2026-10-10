"""Local input accounting. Estimates are never presented as provider-billed tokens."""

import math
from functools import lru_cache


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except (ImportError, OSError, ValueError):
        # Missing/offline tokenizer data: the byte count is a conservative text upper bound.
        return None


def tokenizer_name():
    return "o200k_base" if _encoding() is not None else "utf8_byte_upper_bound"


@lru_cache(maxsize=4096)
def estimate_text_tokens(text: str) -> int:
    encoding = _encoding()
    return len(encoding.encode(text, disallowed_special=())) if encoding else len(text.encode("utf-8"))


def estimate_image_tokens(width: int, height: int) -> int:
    """Monotonic tile budget proxy; no upscaling or claim of model-specific billing."""
    if width <= 0 or height <= 0:
        return 0
    scale = min(1.0, 2048 / max(width, height))
    width, height = width * scale, height * scale
    return 85 + 170 * math.ceil(width / 512) * math.ceil(height / 512)


def request_input_metrics(prompt, schema, images):
    import json

    from PIL import Image

    prompt_tokens = estimate_text_tokens(prompt)
    schema_tokens = estimate_text_tokens(json.dumps(schema))
    dimensions = []
    for path in images:
        with Image.open(path) as bitmap:
            dimensions.append({"width": bitmap.width, "height": bitmap.height})
    image_tokens = sum(estimate_image_tokens(d["width"], d["height"]) for d in dimensions)
    return {
        "tokenizer": tokenizer_name(),
        "prompt_text_tokens_estimate": prompt_tokens,
        "schema_tokens_estimate": schema_tokens,
        "request_text_tokens_estimate": prompt_tokens + schema_tokens,
        "image_tokens_estimate": image_tokens,
        "input_tokens_estimate": prompt_tokens + schema_tokens + image_tokens,
        "image_estimator": "non_upscaled_tile_budget_proxy_not_provider_billing",
        "image_dimensions": dimensions,
        "image_pixels": sum(d["width"] * d["height"] for d in dimensions),
    }
