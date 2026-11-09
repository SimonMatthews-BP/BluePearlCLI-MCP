"""Server configuration, read from environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _find_cli() -> str | None:
    """Locate the BluePearlCLI executable.

    Order: $BLUEPEARL_CLI, then $BLUEPEARL_HOME/{,bin/}BluePearlCLI[.exe], then $PATH.
    """
    explicit = os.environ.get("BLUEPEARL_CLI")
    if explicit:
        return explicit
    home = os.environ.get("BLUEPEARL_HOME")
    if home:
        for sub in ("", "bin"):
            for name in ("BluePearlCLI", "BluePearlCLI.exe"):
                cand = Path(home) / sub / name
                if cand.is_file():
                    return str(cand)
    return shutil.which("BluePearlCLI") or shutil.which("BluePearlCLI.exe")


@dataclass
class Settings:
    cli: str | None
    # Where per-run output directories are created. The User Guide warns that the
    # results directory must be on a local disk, not a network share.
    runs_dir: Path
    # HDL / project files must live under one of these roots.
    allowed_roots: list[Path] = field(default_factory=list)
    default_timeout_s: int = 3600
    max_message_chars: int = 400
    allow_raw_tcl: bool = False
    extra_cli_args: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        roots_env = os.environ.get("BLUEPEARL_ALLOWED_ROOTS")
        roots = (
            [Path(p).expanduser().resolve() for p in roots_env.split(os.pathsep) if p]
            if roots_env
            else [Path.cwd().resolve()]
        )
        runs_dir = Path(
            os.environ.get("BLUEPEARL_RUNS_DIR", Path.cwd() / ".bluepearl_mcp_runs")
        ).expanduser().resolve()
        extra = os.environ.get("BLUEPEARL_EXTRA_ARGS", "").split()
        return cls(
            cli=_find_cli(),
            runs_dir=runs_dir,
            allowed_roots=roots,
            default_timeout_s=int(os.environ.get("BLUEPEARL_TIMEOUT", "3600")),
            max_message_chars=int(os.environ.get("BLUEPEARL_MAX_MESSAGE_CHARS", "400")),
            allow_raw_tcl=_bool_env("BLUEPEARL_ALLOW_RAW_TCL"),
            extra_cli_args=extra,
        )

    def check_path(self, p: str | Path, must_exist: bool = True) -> Path:
        """Resolve a user-supplied path and make sure it sits inside an allowed root."""
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = self.allowed_roots[0] / path
        path = path.resolve()
        roots = [*self.allowed_roots, self.runs_dir]  # the server's own run directories are always readable
        if not any(path == r or r in path.parents for r in roots):
            listed = ", ".join(str(r) for r in self.allowed_roots)
            raise ValueError(f"Path {path} is outside the allowed roots ({listed}).")
        if must_exist and not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        return path
