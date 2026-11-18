"""RTL writer: asks an Ollama-served model to write or revise a module.

Mirrors bps_write_mcp.py so results are comparable: same system prompts, the Verilog/VHDL skill files
prepended, thinking off, and the reply normalised to the bare code (first fenced block, <think> blocks
and bare `rtl/x.sv` path lines removed).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

SYSTEM_WRITE = (
    "You are Blue Pearl RTL, an expert FPGA designer. Write complete, synthesisable, lint-clean RTL "
    "that exactly implements the specification. Output ONLY the code in a single fenced block; "
    "no explanation, no file path line."
)
SYSTEM_REVISE = (
    "You are Blue Pearl RTL, an expert FPGA designer. You are given an RTL module and tool output "
    "(compiler error, simulation failure or lint report). Fix the module so the reported problem "
    "is resolved without changing its interface. Output ONLY the complete corrected module in a single "
    "fenced block; no explanation, no file path line."
)

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)
_PATHLINE = re.compile(r"^\s*(rtl|src|hdl)/[\w./-]+\.(sv|v|vhd|vhdl)\s*$", re.M)


@dataclass
class WriterSettings:
    ollama_url: str
    model: str
    skill_dir: Path
    num_ctx: int = 65536
    max_tokens: int = 8192
    temperature: float = 0.2
    timeout_s: int = 900
    log: Path | None = None

    @classmethod
    def from_env(cls, default_log: Path) -> "WriterSettings | None":
        model = os.environ.get("BLUEPEARL_WRITER_MODEL")
        if not model:
            return None  # writer tools are only registered when a model is configured
        return cls(
            ollama_url=os.environ.get("BLUEPEARL_WRITER_URL", "http://127.0.0.1:11434").rstrip("/"),
            model=model,
            skill_dir=Path(os.environ.get("BLUEPEARL_WRITER_SKILL_DIR", "~/AI/ai_bench/skills")).expanduser(),
            num_ctx=int(os.environ.get("BLUEPEARL_WRITER_NUM_CTX", "65536")),
            max_tokens=int(os.environ.get("BLUEPEARL_WRITER_MAX_TOKENS", "8192")),
            temperature=float(os.environ.get("BLUEPEARL_WRITER_TEMP", "0.2")),
            timeout_s=int(os.environ.get("BLUEPEARL_WRITER_TIMEOUT", "900")),
            log=Path(os.environ.get("BLUEPEARL_WRITER_LOG", default_log)).expanduser(),
        )

    def info(self) -> dict:
        lang_files = {lang: [str(p) for p in _skill_files(self.skill_dir, lang) if p.exists()]
                      for lang in ("verilog", "vhdl")}
        return {"model": self.model, "ollama": self.ollama_url, "skill_dir": str(self.skill_dir),
                "skill_files_found": lang_files, "temperature": self.temperature,
                "num_ctx": self.num_ctx, "max_tokens": self.max_tokens}


def _lang(language: str) -> str:
    return "vhdl" if language.lower().startswith("vhd") else "verilog"


def _skill_files(skill_dir: Path, lang: str) -> list[Path]:
    return [skill_dir / lang / f"{lang}_skill.md", skill_dir / lang / f"{lang}_protocols.md"]


def skill_text(ws: WriterSettings, language: str) -> str:
    return "\n\n".join(p.read_text() for p in _skill_files(ws.skill_dir, _lang(language)) if p.exists())


def normalise(text: str) -> str:
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S)
    m = _FENCE.search(text)
    code = m.group(1) if m else text
    code = _PATHLINE.sub("", code)
    return code.strip() + "\n"


def _fence(body: str) -> str:
    """Fence text with more backticks than it contains, so tool output with ``` can't end the block early."""
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}\n{body}\n{f}"


async def _ask(ws: WriterSettings, system: str, user: str) -> tuple[str, dict]:
    body = {
        "model": ws.model, "stream": False, "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": ws.temperature, "num_ctx": ws.num_ctx, "num_predict": ws.max_tokens},
    }
    t0 = time.time()
    async with httpx.AsyncClient(timeout=ws.timeout_s) as client:
        r = await client.post(f"{ws.ollama_url}/api/chat", json=body)
        r.raise_for_status()
        j = r.json()
    meta = {"model": ws.model, "prompt_tokens": j.get("prompt_eval_count"),
            "completion_tokens": j.get("eval_count"), "seconds": round(time.time() - t0, 1)}
    return (j.get("message") or {}).get("content", ""), meta


def _log(ws: WriterSettings, tool: str, meta: dict, extra: dict) -> None:
    if not ws.log:
        return
    try:
        ws.log.parent.mkdir(parents=True, exist_ok=True)
        with ws.log.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, **meta, **extra}) + "\n")
    except OSError:
        pass


def _system(ws: WriterSettings, language: str, base: str) -> str:
    sk = skill_text(ws, language)
    return (sk + "\n\n" if sk else "") + base


async def write_module(ws: WriterSettings, spec: str, language: str, name: str, ports: str, constraints: str) -> str:
    user = (f"Language: {language}\nModule/entity name: {name}\n"
            + (f"Ports / interface:\n{ports}\n" if ports else "")
            + (f"Constraints:\n{constraints}\n" if constraints else "")
            + f"\nSpecification:\n{spec}\n")
    raw, meta = await _ask(ws, _system(ws, language, SYSTEM_WRITE), user)
    code = normalise(raw)
    _log(ws, "bluepearl_write_module", meta, {"name": name, "language": language, "chars": len(code)})
    return code


async def revise_module(ws: WriterSettings, rtl: str, feedback: str, language: str) -> str:
    user = (f"Language: {language}\n\nCurrent module:\n{_fence(rtl)}\n\n"
            f"Tool output to fix:\n{_fence(feedback)}\n")
    raw, meta = await _ask(ws, _system(ws, language, SYSTEM_REVISE), user)
    code = normalise(raw)
    _log(ws, "bluepearl_revise_module", meta,
         {"language": language, "chars": len(code), "feedback_chars": len(feedback)})
    return code
