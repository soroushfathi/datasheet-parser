import secrets
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import SmokeResult
from .providers import LLMRequest, RecordedProvider, strict_schema


def provider_smoke(provider: RecordedProvider, work_dir: Path) -> dict:
    folder = work_dir / "smoke"
    folder.mkdir(parents=True, exist_ok=True)
    text_code = secrets.token_hex(3).upper()
    image_code = secrets.token_hex(3).upper()
    image = Image.new("RGB", (900, 300), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=60)
    draw.text((40, 35), image_code, fill="black", font=font)
    draw.rectangle((600, 40, 820, 260), fill="blue")
    path = folder / "input.png"
    image.save(path)
    request = LLMRequest(
        task="smoke",
        prompt=(
            "Return only the requested JSON. Do not use tools. "
            f"Set text_code to {text_code}. Read image_code from the attached image. "
            "Set square_color to the lowercase English name of the colored square."
        ),
        schema=strict_schema(SmokeResult),
        images=[path],
    )
    answer = SmokeResult.model_validate(provider.generate(request))
    success = (
        answer.text_code == text_code and answer.image_code == image_code and answer.square_color == "blue"
    )
    return {
        "passed": success,
        "provider": provider.name,
        "requested_model": provider.settings.model,
        "text_correct": answer.text_code == text_code,
        "image_correct": answer.image_code == image_code,
        "color_correct": answer.square_color == "blue",
        "record": provider.last_record,
    }
