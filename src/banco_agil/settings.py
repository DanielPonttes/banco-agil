"""Local configuration; secrets never belong in source control."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

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
        local_values = dotenv_values(ROOT / ".env")

        def value(name, default=""):
            configured = os.environ.get(name, local_values.get(name))
            return default if configured is None else configured

        data_dir = Path(value("DATA_DIR", "data/runtime"))
        return cls(
            data_dir=data_dir if data_dir.is_absolute() else ROOT / data_dir,
            examples_dir=ROOT / "data/examples",
            gemini_api_key=value("GEMINI_API_KEY", "").strip(),
            gemini_model=value("GEMINI_MODEL", "gemini-3.8-flash").strip(),
            awesomeapi_key=value("AWESOMEAPI_KEY", "").strip(),
            log_level=value("LOG_LEVEL", "INFO").upper(),
        )
