import asyncio
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FAKE = HERE / "fake_bluepearl" / "BluePearlCLI"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEPEARL_CLI", str(FAKE))
    monkeypatch.setenv("BLUEPEARL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("BLUEPEARL_ALLOWED_ROOTS", os.pathsep.join([str(HERE / "fixtures"), str(tmp_path)]))
    sys.modules.pop("bluepearl_mcp.server", None)
    import bluepearl_mcp.server as s
    return s


class Ctx:
    async def report_progress(self, *a, **k):
        pass


def test_run_query_compare(server, monkeypatch):
    top = str(HERE / "fixtures" / "rtl" / "top.v")
    r1 = asyncio.run(server.bluepearl_run(Ctx(), hdl_files=[top], top="top", packages=["Basic Checks"]))
    assert r1["parsed_from"] == "sarif"
    assert r1["summary"]["by_severity"] == {"Warning": 1, "Error": 1, "Info": 1}
    tcl = (Path(r1["artifacts"]["run_dir"]) / "bpmcp_run.tcl").read_text()
    assert "BPS::set_package_enabled {Basic Checks} -enabled true" in tcl
    assert "BPS::run analyze" in tcl

    errs = server.bluepearl_get_messages(r1["run_id"], severities=["E"])
    assert errs["total_matching"] == 1 and errs["messages"][0]["msg_id"] == "BPS-0100"

    src = server.bluepearl_get_source(top, 2, context=1)
    assert ">     2" in src

    monkeypatch.setenv("FAKE_BP_FIXED", "1")
    r2 = asyncio.run(server.bluepearl_run(Ctx(), hdl_files=[top], top="top"))
    cmp = server.bluepearl_compare_runs(r1["run_id"], r2["run_id"])
    assert cmp["fixed_count"] == 1 and cmp["introduced_count"] == 0

    imp = asyncio.run(server.bluepearl_import_results(r2["artifacts"]["log"]))
    assert imp["parsed_from"] == "log" and imp["summary"]["total"] == 2
    assert len(server.bluepearl_list_runs()) == 3


def test_paths_outside_roots_rejected(server):
    with pytest.raises(Exception, match="outside the allowed roots"):
        asyncio.run(server.bluepearl_run(Ctx(), hdl_files=["/etc/passwd"]))


def test_help_and_status(server):
    assert "BPS-0027" in asyncio.run(server.bluepearl_help("BPS-0027"))
    assert asyncio.run(server.bluepearl_status())["version"] == "2026.4-fake"


CODE = "module counter(input clk, output reg [3:0] q);\n  always @(posedge clk) q <= q + 1;\nendmodule\n"


def test_lint_code(server, monkeypatch):
    out = asyncio.run(server.bluepearl_lint_code(Ctx(), code=CODE, top="counter"))
    head, *lines = out.splitlines()
    assert head.startswith("BPS_FAIL  errors=1 warnings=1 info=1")
    assert lines[0].startswith("E-BPS-0100: counter.sv(12):")  # errors first, base file name only
    assert len(lines) == 2  # info hidden by default
    run_id = head.split("run_id=")[1]
    run_dir = Path(server._get_run(run_id).run_dir)
    tcl = (run_dir / "bpmcp_run.tcl").read_text()
    assert "BPS::add_input_files -language SYS_VERILOG" in tcl
    assert (run_dir / "src" / "counter.sv").read_text() == CODE
    # Source lookup works on the temp file even though it is outside the allowed roots.
    assert "counter" in server.bluepearl_get_source("counter.sv", 1, context=0, run_id=run_id)

    monkeypatch.setenv("FAKE_BP_FIXED", "1")
    out = asyncio.run(server.bluepearl_lint_code(Ctx(), code="entity e is end;", language="vhdl", top="e",
                                                 include_info=True))
    assert out.startswith("BPS_WARN  errors=0 warnings=1 info=1")
    assert "e.vhd(" in out and "I-BPS-0396" in out


def test_lint_code_tool_error(server, monkeypatch):
    monkeypatch.setenv("FAKE_BP_CRASH", "1")
    out = asyncio.run(server.bluepearl_lint_code(Ctx(), code=CODE, top="counter"))
    assert out.startswith("BPS_TOOL_ERROR")
    assert "license" in out


def test_error_reason_reaches_client(tmp_path):
    """Exceptions must arrive with their message, not just 'Error executing tool ...'."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, BLUEPEARL_CLI=str(FAKE), BLUEPEARL_RUNS_DIR=str(tmp_path / "runs"),
               BLUEPEARL_ALLOWED_ROOTS=str(HERE / "fixtures"), PYTHONPATH=str(HERE.parent / "src"))

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "bluepearl_mcp.server"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool("bluepearl_run", {"hdl_files": ["/etc/passwd"]})

    res = asyncio.run(go())
    assert res.is_error if hasattr(res, "is_error") else res.isError
    assert "outside the allowed roots" in res.content[0].text
