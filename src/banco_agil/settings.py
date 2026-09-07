"""Local configuration; secrets never belong in source control."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    examples_dir: Path
    gemini_api_key: str
    gemini_model: str
    awesomeapi_key: str
    log_level: str = "INFO"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        data_dir = Path(os.getenv("DATA_DIR", "data/runtime"))
        return cls(
            data_dir=data_dir if data_dir.is_absolute() else ROOT / data_dir,
            examples_dir=ROOT / "data/examples",
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
            awesomeapi_key=os.getenv("AWESOMEAPI_KEY", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
