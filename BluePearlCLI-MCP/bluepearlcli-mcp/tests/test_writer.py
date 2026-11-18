import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bluepearlcli_mcp.writer import _fence, normalise


def test_normalise_matches_bps_write_policy():
    raw = "<think>hmm</think>\nHere:\n```systemverilog\nrtl/top.sv\nmodule top; endmodule\n```\n```\nextra\n```"
    assert normalise(raw) == "module top; endmodule\n"


def test_fence_grows_past_backticks():
    assert _fence("plain") == "```\nplain\n```"
    assert _fence("has ``` inside").startswith("````\n")


@pytest.fixture()
def fake_ollama():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            reply = {"message": {"content": "```verilog\nmodule counter; endmodule\n```"},
                     "prompt_eval_count": 10, "eval_count": 5}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_writer_tools(tmp_path, monkeypatch, fake_ollama):
    url, seen = fake_ollama
    skills = tmp_path / "skills" / "verilog"
    skills.mkdir(parents=True)
    (skills / "verilog_skill.md").write_text("SKILL TEXT")
    monkeypatch.setenv("BLUEPEARL_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("BLUEPEARL_WRITER_MODEL", "bluepearl-rtl-quantized:latest")
    monkeypatch.setenv("BLUEPEARL_WRITER_URL", url)
    monkeypatch.setenv("BLUEPEARL_WRITER_SKILL_DIR", str(tmp_path / "skills"))
    sys.modules.pop("bluepearlcli_mcp.server", None)
    import bluepearlcli_mcp.server as s

    code = asyncio.run(s.bluepearl_write_module("4-bit counter", name="counter", ports="input clk"))
    assert code == "module counter; endmodule\n"
    req = seen[-1]
    assert req["model"] == "bluepearl-rtl-quantized:latest" and req["think"] is False
    assert req["options"] == {"temperature": 0.2, "num_ctx": 65536, "num_predict": 8192}
    assert req["messages"][0]["content"].startswith("SKILL TEXT\n\nYou are Blue Pearl RTL")
    assert "Module/entity name: counter" in req["messages"][1]["content"]

    asyncio.run(s.bluepearl_revise_module("module counter; endmodule", "E-BPS-0100: counter.sv(1): undriven"))
    assert "Tool output to fix:\n```\nE-BPS-0100" in seen[-1]["messages"][1]["content"]

    info = s.bluepearl_writer_info()
    assert info["skill_files_found"]["verilog"] and not info["skill_files_found"]["vhdl"]
    assert (tmp_path / "runs" / "writer.jsonl").exists()


def test_writer_tools_absent_without_model(tmp_path, monkeypatch):
    monkeypatch.delenv("BLUEPEARL_WRITER_MODEL", raising=False)
    monkeypatch.setenv("BLUEPEARL_RUNS_DIR", str(tmp_path / "runs"))
    sys.modules.pop("bluepearlcli_mcp.server", None)
    import bluepearlcli_mcp.server as s
    assert not hasattr(s, "bluepearl_write_module")
