"""MCP server exposing Blue Pearl Analyze (BluePearlCLI) to AI models."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Context, MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import Context, FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

import functools
import inspect

from .config import Settings
from .parsers import Message, filter_messages, summarize
from . import writer
from .runner import RunRecord, RunSpec, execute, import_results, new_run_dir, run_cli_capture, tcl_quote

INSTRUCTIONS = """\
Tools for running Blue Pearl Analyze (RTL lint / CDC static checking) and reading its results.

Typical workflow:
1. bluepearl_run with either an existing .f file (e.g. Results/design.bluepearlgenerated.f from the GUI),
   setup Tcl files (e.g. design.settings.tcl), or a list of HDL files plus the top module.
2. Read the summary it returns, then page through details with bluepearl_get_messages, filtering by
   severity (E/W/I), message id (e.g. BPS-0027), file or module. Errors first.
3. Use bluepearl_get_source to see the HDL around a reported line, and bluepearl_help on a message id or
   check name to get Blue Pearl's own explanation before proposing a fix.
4. After edits, run again and use bluepearl_compare_runs to confirm what was fixed or introduced.
Messages look like W-BPS-0027 (severity, then id). Waived messages are hidden unless requested.

For a single module held as text (e.g. just written by another tool), use bluepearl_lint_code instead:
it returns one verdict line (BPS_CLEAN / BPS_WARN / BPS_FAIL / BPS_TOOL_ERROR) followed by the messages.
If bluepearl_write_module / bluepearl_revise_module are available, they author RTL with the Blue Pearl RTL
writer model; pass bluepearl_lint_code output verbatim as the feedback to bluepearl_revise_module.
"""

settings = Settings.from_env()
_server = _Server("bluepearl", instructions=INSTRUCTIONS)


class _Registrar:
    """Registers tools so that any exception reaches the client with its message.

    MCP SDK 2.x shows the client only "Error executing tool <name>" for exceptions other than
    ToolError, hiding reasons such as "path outside the allowed roots" or "BluePearlCLI not found".
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(_server, name)

    def tool(self, *args: Any, **kwargs: Any):
        def decorate(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapper(*a, **k):
                    try:
                        return await fn(*a, **k)
                    except ToolError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        raise ToolError(f"{type(e).__name__}: {e}") from e
            else:
                @functools.wraps(fn)
                def wrapper(*a, **k):
                    try:
                        return fn(*a, **k)
                    except ToolError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        raise ToolError(f"{type(e).__name__}: {e}") from e
            _server.tool(*args, **kwargs)(wrapper)
            return wrapper
        return decorate


mcp = _Registrar()
_runs: dict[str, RunRecord] = {}


def _get_run(run_id: str | None) -> RunRecord:
    if run_id in (None, "", "latest"):
        all_runs = _list_run_dirs()
        if not all_runs:
            raise ValueError("No runs yet. Call bluepearl_run or bluepearl_import_results first.")
        run_id = all_runs[0][0]
    if run_id not in _runs:
        for rid, d in _list_run_dirs():
            if rid == run_id:
                _runs[rid] = RunRecord.load(d)
                break
        else:
            raise ValueError(f"Unknown run_id {run_id!r}. Use bluepearl_list_runs.")
    return _runs[run_id]


def _list_run_dirs() -> list[tuple[str, Path]]:
    if not settings.runs_dir.is_dir():
        return []
    out = []
    for d in settings.runs_dir.iterdir():
        if (d / "run.json").is_file():
            out.append((d.name.split("-", 1)[1], d))
    return sorted(out, key=lambda t: t[1].stat().st_mtime, reverse=True)


def _run_overview(rec: RunRecord) -> dict[str, Any]:
    ov: dict[str, Any] = {
        "run_id": rec.run_id,
        "returncode": rec.returncode,
        "timed_out": rec.timed_out,
        "duration_s": rec.duration_s,
        "parsed_from": rec.parsed_from,
        "summary": summarize(rec.messages),
        "artifacts": {
            k: v
            for k, v in {
                "run_dir": rec.run_dir, "output_dir": rec.output_dir, "log": rec.log_path,
                "sarif": rec.sarif_path, "csv": rec.csv_path,
            }.items()
            if v
        },
    }
    if rec.notes:
        ov["notes"] = rec.notes
    if rec.stdout_tail:
        ov["stdout_tail"] = rec.stdout_tail
    return ov


@mcp.tool()
async def bluepearl_status(
    license_test: Annotated[bool, Field(description="Also run -license_test to report available licenses.")] = False,
) -> dict[str, Any]:
    """Report whether BluePearlCLI is configured, its version, and the server's path settings."""
    info: dict[str, Any] = {
        "cli": settings.cli,
        "runs_dir": str(settings.runs_dir),
        "allowed_roots": [str(r) for r in settings.allowed_roots],
        "raw_tcl_enabled": settings.allow_raw_tcl,
    }
    if not settings.cli:
        info["error"] = "BluePearlCLI not found. Set BLUEPEARL_CLI or BLUEPEARL_HOME."
        return info
    _, out = await run_cli_capture(settings, ["-shortversion"], timeout_s=60)
    info["version"] = out.strip()[-200:]
    if license_test:
        _, out = await run_cli_capture(settings, ["-license_test"], timeout_s=120)
        info["licenses"] = out.strip()[-4000:]
    return info


@mcp.tool()
async def bluepearl_run(
    ctx: Context,
    ffile: Annotated[str | None, Field(description="A .f command file listing HDL files and options.")] = None,
    hdl_files: Annotated[list[str] | None, Field(description="HDL source files, if not using a .f file.")] = None,
    top: Annotated[str | None, Field(description="Top-level module/entity (required for mixed-language designs).")] = None,
    include_dirs: Annotated[list[str] | None, Field(description="Verilog include directories (+incdir+).")] = None,
    defines: Annotated[dict[str, str] | None, Field(description="Verilog macro definitions (+define+).")] = None,
    setup_tcl: Annotated[list[str] | None, Field(description="Tcl files sourced before the run, e.g. design.settings.tcl.")] = None,
    waivers: Annotated[str | None, Field(description="Waivers file to load.")] = None,
    packages: Annotated[list[str] | None, Field(description="Check packages to enable, e.g. ['Basic Checks'].")] = None,
    enable_checks: Annotated[list[str] | None, Field(description="Individual checks to enable, e.g. ['MBA','CONSTANT'].")] = None,
    disable_checks: Annotated[list[str] | None, Field(description="Individual checks to disable.")] = None,
    phase: Annotated[
        Literal["load", "analyze", "none"],
        Field(description="'load' = load checks only; 'analyze' = load then analysis; "
              "'none' = setup Tcl already calls BPS::run."),
    ] = "analyze",
    timeout_s: Annotated[int | None, Field(description="Kill the run after this many seconds.")] = None,
) -> dict[str, Any]:
    """Run Blue Pearl Analyze on a design and return a summary of the resulting messages.

    Provide a .f file, HDL files, or setup Tcl files (or a combination). Returns a run_id to use with
    bluepearl_get_messages, bluepearl_get_summary and bluepearl_compare_runs.
    """
    if not (ffile or hdl_files or setup_tcl):
        raise ValueError("Provide ffile, hdl_files or setup_tcl.")
    spec = RunSpec(
        ffile=settings.check_path(ffile) if ffile else None,
        hdl_files=[settings.check_path(f) for f in hdl_files or []],
        include_dirs=[settings.check_path(d) for d in include_dirs or []],
        defines=defines or {},
        top=top,
        setup_tcl=[settings.check_path(t) for t in setup_tcl or []],
        waivers=settings.check_path(waivers) if waivers else None,
        packages=packages or [],
        enable_checks=enable_checks or [],
        disable_checks=disable_checks or [],
        phase=phase,
        timeout_s=timeout_s,
    )

    async def progress(elapsed: float, msg: str) -> None:
        try:
            await ctx.report_progress(elapsed, None, msg)
        except Exception:  # noqa: BLE001 - progress is best effort
            pass

    rec = await execute(settings, spec, progress)
    _runs[rec.run_id] = rec
    return _run_overview(rec)


_LANG = {"vhdl": ("VHDL_2008", ".vhd"), "verilog": ("SYS_VERILOG", ".sv")}


def _format_lint(rec: RunRecord, include_info: bool, max_messages: int) -> str:
    live = [m for m in rec.messages if not m.waived]
    n = {s: sum(m.severity == s for m in live) for s in "EWI"}
    tool_error = rec.timed_out or rec.parsed_from is None or (rec.returncode not in (0, None) and not live)
    if tool_error:
        verdict = "BPS_TOOL_ERROR"
    elif n["E"]:
        verdict = "BPS_FAIL"
    elif n["W"]:
        verdict = "BPS_WARN"
    else:
        verdict = "BPS_CLEAN"
    head = (f"{verdict}  errors={n['E']} warnings={n['W']} info={n['I']}  "
            f"({rec.duration_s:.0f}s)  run_id={rec.run_id}")
    if tool_error:
        detail = "\n".join(rec.notes + ([rec.stdout_tail] if rec.stdout_tail else []))
        return f"{head}\nBlue Pearl did not produce usable results. Check BluePearlCLI, the license, or the code.\n{detail[-4000:]}"
    shown_sev = "EWI" if include_info else "EW"
    order = {"E": 0, "W": 1, "I": 2}
    msgs = sorted((m for m in live if m.severity in shown_sev),
                  key=lambda m: (order[m.severity], m.line or 0, m.msg_id))
    lines = []
    for m in msgs[:max_messages]:
        loc = f"{Path(m.file).name}({m.line}): " if m.file and m.line else (f"{Path(m.file).name}: " if m.file else "")
        text = m.text if len(m.text) <= settings.max_message_chars else m.text[: settings.max_message_chars] + "…"
        lines.append(f"{m.severity}-{m.msg_id}: {loc}{text}")
    if len(msgs) > max_messages:
        lines.append(f"... {len(msgs) - max_messages} more; use bluepearl_get_messages with run_id={rec.run_id}")
    return head + ("\n" + "\n".join(lines) if lines else "")


@mcp.tool()
async def bluepearl_lint_code(
    ctx: Context,
    code: Annotated[str, Field(description="The complete RTL module as text.")],
    language: Annotated[str, Field(description="'verilog' (treated as SystemVerilog) or 'vhdl' (VHDL-2008).")] = "verilog",
    top: Annotated[str, Field(description="Top module/entity name.")] = "top",
    filename: Annotated[str, Field(description="Optional file name, so messages refer to it.")] = "",
    phase: Annotated[Literal["load", "analyze"], Field(description="'load' is quicker; 'analyze' runs all checks.")] = "analyze",
    packages: Annotated[list[str] | None, Field(description="Check packages to enable, e.g. ['Basic Checks'].")] = None,
    include_info: Annotated[bool, Field(description="Also list Info messages (they are always counted).")] = False,
    max_messages: Annotated[int, Field(ge=1, le=500)] = 40,
) -> str:
    """Check one RTL module, given as text, with Blue Pearl Analyze.

    Returns a first line BPS_CLEAN / BPS_WARN / BPS_FAIL / BPS_TOOL_ERROR with error, warning and info counts
    and a run_id, then one message per line (e.g. 'W-BPS-0027: dut.sv(70): ...'), errors first. Pass this
    output verbatim to a revise tool. Use bluepearl_help on a message id for Blue Pearl's explanation."""
    lang_key = "vhdl" if language.lower().startswith("vhd") else "verilog"
    bp_lang, ext = _LANG[lang_key]
    name = Path(filename).name if filename else f"{top}{ext}"
    run_id, run_dir = new_run_dir(settings, label="lint")
    src_dir = run_dir / "src"
    src_dir.mkdir()
    src = src_dir / name
    src.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    spec = RunSpec(typed_files=[(src, bp_lang)], top=top, packages=packages or [], phase=phase)

    async def progress(elapsed: float, msg: str) -> None:
        try:
            await ctx.report_progress(elapsed, None, msg)
        except Exception:  # noqa: BLE001
            pass

    rec = await execute(settings, spec, progress, run=(run_id, run_dir))
    _runs[rec.run_id] = rec
    return _format_lint(rec, include_info, max_messages)


@mcp.tool()
async def bluepearl_import_results(
    path: Annotated[str, Field(description="A .sarif, .csv or .log file, or a Blue Pearl results directory.")],
    result_id: Annotated[int | None, Field(description="Result run id inside results.db (default: latest).")] = None,
) -> dict[str, Any]:
    """Load results from an earlier Blue Pearl run (e.g. one started from the GUI) without re-running it."""
    rec = await import_results(settings, settings.check_path(path), result_id)
    _runs[rec.run_id] = rec
    return _run_overview(rec)


@mcp.tool()
def bluepearl_list_runs(limit: int = 20) -> list[dict[str, Any]]:
    """List recent runs (newest first) with their message counts."""
    out = []
    for rid, _ in _list_run_dirs()[:limit]:
        rec = _get_run(rid)
        s = summarize(rec.messages, top_n=0)
        out.append({"run_id": rid, "total": s["total"], "by_severity": s["by_severity"],
                    "returncode": rec.returncode, "command": " ".join(rec.command)[:300]})
    return out


@mcp.tool()
def bluepearl_get_summary(
    run_id: Annotated[str, Field(description="Run id, or 'latest'.")] = "latest",
    top_n: int = 50,
) -> dict[str, Any]:
    """Message counts for a run: by severity, by message id (with an example), and top files/modules."""
    rec = _get_run(run_id)
    return {"run_id": rec.run_id, **summarize(rec.messages, top_n=top_n)}


@mcp.tool()
def bluepearl_get_messages(
    run_id: Annotated[str, Field(description="Run id, or 'latest'.")] = "latest",
    severities: Annotated[list[str] | None, Field(description="Any of E, W, I (or Error/Warning/Info).")] = None,
    msg_ids: Annotated[list[str] | None, Field(description="Message ids such as BPS-0027.")] = None,
    file_contains: Annotated[str | None, Field(description="Substring of the source file path.")] = None,
    module: Annotated[str | None, Field(description="Exact module/entity name.")] = None,
    text_contains: Annotated[str | None, Field(description="Substring of the message text.")] = None,
    include_waived: bool = False,
    offset: int = 0,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Return individual messages from a run, filtered and paginated (errors first, then warnings)."""
    rec = _get_run(run_id)
    msgs = filter_messages(rec.messages, severities, msg_ids, file_contains, module, text_contains, include_waived)
    order = {"E": 0, "W": 1, "I": 2}
    msgs = sorted(msgs, key=lambda m: (order.get(m.severity, 3), m.msg_id, m.file or "", m.line or 0))
    page = msgs[offset : offset + limit]
    return {
        "run_id": rec.run_id,
        "total_matching": len(msgs),
        "offset": offset,
        "returned": len(page),
        "next_offset": offset + limit if offset + limit < len(msgs) else None,
        "messages": [m.to_dict(settings.max_message_chars) for m in page],
    }


def _key(m: Message) -> tuple:
    # Line numbers shift when code is edited, so match on id, file and text with digits normalised.
    return (m.msg_id, Path(m.file).name if m.file else None, re.sub(r"\d+", "#", m.text))


@mcp.tool()
def bluepearl_compare_runs(
    base_run_id: Annotated[str, Field(description="The earlier run.")],
    new_run_id: Annotated[str, Field(description="The later run, or 'latest'.")] = "latest",
    limit: int = 100,
) -> dict[str, Any]:
    """Compare two runs: which messages were fixed (gone) and which were introduced."""
    base, new = _get_run(base_run_id), _get_run(new_run_id)
    from collections import Counter

    bc, nc = Counter(map(_key, base.messages)), Counter(map(_key, new.messages))
    first: dict[tuple, Message] = {}
    for m in base.messages + new.messages:
        first.setdefault(_key(m), m)

    def expand(c: Counter) -> list[dict]:
        return [{**first[k].to_dict(settings.max_message_chars), "count": n} for k, n in c.most_common(limit)]

    fixed, introduced = bc - nc, nc - bc
    return {
        "base": {"run_id": base.run_id, "total": len(base.messages)},
        "new": {"run_id": new.run_id, "total": len(new.messages)},
        "fixed_count": sum(fixed.values()),
        "introduced_count": sum(introduced.values()),
        "fixed": expand(fixed),
        "introduced": expand(introduced),
    }


@mcp.tool()
def bluepearl_get_source(
    file: Annotated[str, Field(description="Source file path as reported in a message.")],
    line: Annotated[int, Field(description="Line number to centre on.")],
    context: Annotated[int, Field(ge=0, le=200)] = 15,
    run_id: Annotated[str | None, Field(description="Run whose working directory relative paths resolve against.")] = None,
) -> str:
    """Show HDL source lines around a reported location, with line numbers."""
    p = Path(file)
    if not p.is_absolute():
        bases = []
        if run_id or _list_run_dirs():
            rec = _get_run(run_id)
            cmd = rec.command
            if "-f" in cmd:
                bases.append(Path(cmd[cmd.index("-f") + 1]).parent)
            bases += [Path(rec.run_dir) / "src", Path(rec.run_dir)]
        bases += settings.allowed_roots
        for b in bases:
            if (b / p).is_file():
                p = b / p
                break
    path = settings.check_path(p)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lo, hi = max(1, line - context), min(len(lines), line + context)
    body = "\n".join(f"{'>' if i == line else ' '}{i:6d}  {lines[i - 1]}" for i in range(lo, hi + 1))
    return f"{path} (lines {lo}-{hi} of {len(lines)})\n{body}"


@mcp.tool()
async def bluepearl_help(
    topic: Annotated[str, Field(description="A message id (case sensitive, e.g. BPS-0027), a check name (e.g. MBA) or a help topic.")],
) -> str:
    """Get Blue Pearl's built-in documentation for a message, check or topic (BluePearlCLI -help <topic>)."""
    if not re.match(r"^[A-Za-z0-9_\-:.]+$", topic):
        raise ValueError("Invalid topic.")
    _, out = await run_cli_capture(settings, ["-help", topic], timeout_s=60)
    return out.strip()[-12000:] or "(no output)"


@mcp.tool()
async def bluepearl_list_checks(
    name_filter: Annotated[str, Field(description="Glob filter on check names.")] = "*",
    verbose: bool = True,
) -> str:
    """List available checks and whether they are enabled (BPS::get_checks)."""
    cmd = f"BPS::get_checks {tcl_quote(name_filter)} -verbose {'true' if verbose else 'false'} -report true"
    _, out = await run_cli_capture(settings, ["-e", cmd, "-e", "exit"], timeout_s=120)
    return out.strip()[-30000:] or "(no output)"


@mcp.tool()
def bluepearl_read_log(
    run_id: Annotated[str, Field(description="Run id, or 'latest'.")] = "latest",
    grep: Annotated[str | None, Field(description="Only return lines matching this regular expression.")] = None,
    tail: Annotated[int, Field(ge=1, le=2000)] = 200,
) -> str:
    """Read the Blue Pearl log (or stdout) of a run, e.g. to diagnose load failures or license errors."""
    rec = _get_run(run_id)
    path = Path(rec.log_path) if rec.log_path else Path(rec.run_dir) / "stdout.txt"
    if not path.exists():
        return "No log found for this run."
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if grep:
        rx = re.compile(grep)
        lines = [ln for ln in lines if rx.search(ln)]
    return f"{path} ({len(lines)} lines{' matching' if grep else ''}, last {tail} shown)\n" + "\n".join(lines[-tail:])


writer_settings = writer.WriterSettings.from_env(default_log=settings.runs_dir / "writer.jsonl")

if writer_settings:

    @mcp.tool()
    async def bluepearl_write_module(
        spec: Annotated[str, Field(description="Behavioural specification in plain English.")],
        language: Annotated[str, Field(description="'verilog' (SystemVerilog) or 'vhdl'.")] = "verilog",
        name: Annotated[str, Field(description="Module/entity name.")] = "top",
        ports: Annotated[str, Field(description="Port list / interface text.")] = "",
        constraints: Annotated[str, Field(description="Coding rules, clocking, reset style.")] = "",
    ) -> str:
        """Write a new RTL module with the Blue Pearl RTL writer model. Returns the code only."""
        return await writer.write_module(writer_settings, spec, language, name, ports, constraints)

    @mcp.tool()
    async def bluepearl_revise_module(
        rtl: Annotated[str, Field(description="The full current module.")],
        feedback: Annotated[str, Field(description="Verbatim compiler / simulator / lint output, e.g. from bluepearl_lint_code.")],
        language: Annotated[str, Field(description="'verilog' (SystemVerilog) or 'vhdl'.")] = "verilog",
    ) -> str:
        """Send a module and the exact tool output to the Blue Pearl RTL writer model and get the corrected
        module back (same interface). Returns the code only."""
        return await writer.revise_module(writer_settings, rtl, feedback, language)

    @mcp.tool()
    def bluepearl_writer_info() -> dict[str, Any]:
        """Which writer model, Ollama server and skill files the write/revise tools use."""
        return writer_settings.info()


if settings.allow_raw_tcl:

    @mcp.tool()
    async def bluepearl_tcl(
        commands: Annotated[str, Field(description="Tcl commands to run in BluePearlCLI; 'exit' is appended.")],
        results_dir: Annotated[str | None, Field(description="Results directory to run against (-output).")] = None,
    ) -> str:
        """Run arbitrary Tcl in BluePearlCLI (only available when BLUEPEARL_ALLOW_RAW_TCL=1)."""
        script = settings.runs_dir / "raw_command.tcl"
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        script.write_text(commands + "\nexit\n")
        args = (["-output", str(settings.check_path(results_dir))] if results_dir else []) + ["-tcl", str(script)]
        rc, out = await run_cli_capture(settings, args, timeout_s=settings.default_timeout_s)
        return f"exit code {rc}\n{out[-30000:]}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
