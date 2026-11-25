"""Configuration dataclass with defaults and strict validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MB = 1024 * 1024
DEFAULT_USER_AGENT = "Scrapit/1.0 (educational security assignment)"


class ConfigError(ValueError):
    """Raised when a configuration value is out of range or malformed."""


@dataclass
class Config:
    delay: float = 1.0
    timeout: float = 10.0
    max_retries: int = 2
    follow_redirects: bool = True
    max_download_bytes: int = 10 * MB
    respect_robots: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    extra_headers: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    output_dir: Path = field(default_factory=lambda: Path("output"))
    save_headers: bool = True
    download_assets: bool = False
    allow_private: bool = False
    max_paths: int = 500
    warn_paths: int = 200
    max_redirects: int = 10
    max_assets_per_page: int = 50

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir).expanduser()
        if self.extra_headers is None:
            self.extra_headers = {}

    @property
    def request_headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        headers.update(self.extra_headers)
        return headers

    def validate(self) -> None:
        errors: list[str] = []
        try:
            self.delay = float(self.delay)
        except (TypeError, ValueError):
            errors.append("Delay must be a number.")
        else:
            if self.delay < 0.2:
                errors.append("Delay must be at least 0.2 seconds.")
            if self.delay > 300:
                errors.append("Delay must be 300 seconds or less.")

        try:
            self.timeout = float(self.timeout)
        except (TypeError, ValueError):
            errors.append("Timeout must be a number.")
        else:
            if not 1 <= self.timeout <= 300:
                errors.append("Timeout must be between 1 and 300 seconds.")

        try:
            self.max_retries = int(self.max_retries)
        except (TypeError, ValueError):
            errors.append("Max retries must be an integer.")
        else:
            if not 0 <= self.max_retries <= 5:
                errors.append("Max retries must be between 0 and 5.")

        try:
            self.max_download_bytes = int(self.max_download_bytes)
        except (TypeError, ValueError):
            errors.append("Max download size must be an integer number of bytes.")
        else:
            if not 1024 <= self.max_download_bytes <= 1024 * MB:
                errors.append("Max download size must be between 1 KB and 1 GB.")

        if not str(self.user_agent).strip():
            errors.append("User-Agent must not be empty.")
        elif "\n" in self.user_agent or "\r" in self.user_agent:
            errors.append("User-Agent must be a single line.")

        if not isinstance(self.extra_headers, dict):
            errors.append("Extra headers must be a dictionary.")
        else:
            for key, value in self.extra_headers.items():
                if not key or not isinstance(key, str):
                    errors.append("Extra header names must be non-empty strings.")
                if not isinstance(value, str):
                    errors.append(f"Header '{key}' must have a text value.")
                elif "\n" in value or "\r" in value:
                    errors.append(f"Header '{key}' must be a single line.")

        try:
            self.max_paths = int(self.max_paths)
            self.warn_paths = int(self.warn_paths)
        except (TypeError, ValueError):
            errors.append("Path limits must be integers.")
        else:
            if not 1 <= self.max_paths <= 5000:
                errors.append("Max paths per run must be between 1 and 5000.")
            if not 1 <= self.warn_paths <= self.max_paths:
                errors.append("Warning threshold must be between 1 and the max path cap.")

        try:
            self.max_redirects = int(self.max_redirects)
        except (TypeError, ValueError):
            errors.append("Max redirects must be an integer.")
        else:
            if not 0 <= self.max_redirects <= 20:
                errors.append("Max redirects must be between 0 and 20.")

        try:
            self.max_assets_per_page = int(self.max_assets_per_page)
        except (TypeError, ValueError):
            errors.append("Max assets per page must be an integer.")
        else:
            if not 0 <= self.max_assets_per_page <= 200:
                errors.append("Max assets per page must be between 0 and 200.")

        if not str(self.output_dir).strip():
            errors.append("Output folder must not be empty.")

        if errors:
            raise ConfigError("\n".join(errors))

    @property
    def max_download_mb(self) -> float:
        return self.max_download_bytes / MB

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["max_download_mb"] = round(self.max_download_mb, 3)
        return data
