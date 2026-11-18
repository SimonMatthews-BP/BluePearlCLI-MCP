import json
from pathlib import Path

from bluepearlcli_mcp.parsers import filter_messages, parse_csv, parse_log_lines, parse_sarif, summarize


def test_log_line_from_user_guide():
    # Example from the User Guide section 9.1, including the wrapped continuation line.
    msgs = parse_log_lines([
        "W-BPS-0027: source/verilog/case3.v(70): Case expression size\n",
        "         (8 bits) does not match case item expression size (7 bits)\n",
        "I-BPS-0396: Module loaded\n",
        "some unrelated line\n",
    ])
    assert len(msgs) == 2
    m = msgs[0]
    assert (m.severity, m.msg_id, m.file, m.line) == ("W", "BPS-0027", "source/verilog/case3.v", 70)
    assert m.text.endswith("(7 bits)")
    assert msgs[1].file is None and msgs[1].msg_id == "BPS-0396"


def test_csv_header_mapping(tmp_path: Path):
    p = tmp_path / "m.csv"
    p.write_text('Severity,Message ID,File Name,Line Number,Module,Object,Message Text\n'
                 'Warning,BPS-0027,a.v,5,top,sig,"hello, world"\n'
                 ',E-BPS-0100,b.v,7,top,,undriven\n')
    msgs = parse_csv(p)
    assert msgs[0].severity == "W" and msgs[0].line == 5 and msgs[0].text == "hello, world"
    assert msgs[0].object == "sig"
    assert msgs[1].severity == "E" and msgs[1].msg_id == "BPS-0100"


def test_sarif(tmp_path: Path):
    p = tmp_path / "r.sarif"
    p.write_text(json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x", "rules": [
        {"id": "BPS-0001", "defaultConfiguration": {"level": "error"}}]}}, "results": [
        {"ruleId": "BPS-0001", "message": {"text": "bad"},
         "locations": [{"physicalLocation": {"artifactLocation": {"uri": "file:///C:/work/a.v"},
                                             "region": {"startLine": 3}}}]},
        {"ruleId": "BPS-0002", "level": "note", "message": {"text": "fyi"}, "suppressions": [{"kind": "external"}]},
    ]}]}))
    msgs = parse_sarif(p)
    assert msgs[0].severity == "E" and msgs[0].file == "C:/work/a.v" and msgs[0].line == 3
    assert msgs[1].waived and msgs[1].severity == "I"
    assert len(filter_messages(msgs)) == 1
    assert summarize(msgs)["by_severity"] == {"Error": 1, "Info": 1}
