import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    mongo_uri: str = Field(repr=False)
    database: str = "datasheet_dev"
    work_dir: Path = Path("work")
    provider: str = "codex"
    model: str = "gpt-6-astra"
    reasoning: str = "high"
    llm_timeout: int = 600
    llm_attempts: int = Field(default=3, ge=1, le=5)
    codex_proxy: str | None = Field(default=None, repr=False)
    codex_executable: str | None = None

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(
            mongo_uri=os.getenv("DATASHEET_MONGO_URI", "mongodb://127.0.0.1:27018/?directConnection=true"),
            database=os.getenv("DATASHEET_DATABASE", "datasheet_dev"),
            work_dir=Path(os.getenv("DATASHEET_WORK_DIR", "work")).resolve(),
            provider=os.getenv("DATASHEET_PROVIDER", "codex"),
            model=os.getenv("DATASHEET_MODEL", "gpt-6-astra"),
            reasoning=os.getenv("DATASHEET_REASONING", "high"),
            llm_timeout=int(os.getenv("DATASHEET_LLM_TIMEOUT", "600")),
            llm_attempts=int(os.getenv("DATASHEET_LLM_ATTEMPTS", "3")),
            codex_proxy=os.getenv("DATASHEET_CODEX_PROXY") or None,
            codex_executable=os.getenv("DATASHEET_CODEX_EXECUTABLE") or None,
        )


def resolve_codex_executable(settings: Settings) -> str | None:
    """An explicit project setting works outside the desktop app's augmented PATH."""
    if settings.codex_executable:
        executable = shutil.which(settings.codex_executable)
        if executable is None:
            raise ValueError(
                "DATASHEET_CODEX_EXECUTABLE does not point to an executable. "
                "Update the absolute Codex executable path in .env."
            )
        return executable
    return shutil.which("codex")
