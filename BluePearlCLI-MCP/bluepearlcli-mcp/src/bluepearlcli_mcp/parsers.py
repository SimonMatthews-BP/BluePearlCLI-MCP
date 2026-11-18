"""Turn Blue Pearl output into a normalized list of messages.

Three sources are supported, tried in this order of preference:

1. SARIF 2.1.0 (``-write_sarif``): a standard JSON format, the most robust.
2. CSV written by ``BPS::get_result_run_messages -reportcsv <file>``.
3. The text log, whose message lines look like
   ``W-BPS-0027: source/verilog/case3.v(70): Case expression size ...``.

The manuals document that these outputs exist but not their exact columns or
SARIF property names, so the parsers are deliberately tolerant. Validate them
against output from your Blue Pearl version (see tests/ and README).
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SEVERITY_NAMES = {"E": "Error", "W": "Warning", "I": "Info"}
_SARIF_LEVEL = {"error": "E", "warning": "W", "note": "I", "none": "I"}


@dataclass
class Message:
    severity: str  # "E", "W" or "I"
    msg_id: str  # e.g. "BPS-0027"
    text: str
    file: str | None = None
    line: int | None = None
    module: str | None = None
    object: str | None = None
    check: str | None = None
    waived: bool = False

    def to_dict(self, max_chars: int | None = None) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v not in (None, False, "")}
        if max_chars and len(self.text) > max_chars:
            d["text"] = self.text[:max_chars] + "…"
        return d


def _norm_sev(value: str | None) -> str:
    if not value:
        return "I"
    v = value.strip().lower()
    if v in _SARIF_LEVEL:
        return _SARIF_LEVEL[v]
    if v.startswith("e") or v.startswith("f"):  # error / fatal
        return "E"
    if v.startswith("w"):
        return "W"
    return "I"


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- SARIF


def _uri_to_path(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    path = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///C:/x -> C:/x
        path = path[1:]
    return path


def parse_sarif(path: Path) -> list[Message]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    out: list[Message] = []
    for run in data.get("runs", []):
        rules = {}
        for r in (run.get("tool", {}).get("driver", {}).get("rules") or []):
            rules[r.get("id")] = r
        for res in run.get("results", []) or []:
            rule_id = res.get("ruleId") or (res.get("rule") or {}).get("id") or "UNKNOWN"
            props = res.get("properties") or {}
            rule = rules.get(rule_id, {})
            level = res.get("level") or (rule.get("defaultConfiguration") or {}).get("level")
            sev = _norm_sev(props.get("severity") or level)
            text = (res.get("message") or {}).get("text") or ""
            file = line = module = obj = None
            locs = res.get("locations") or []
            if locs:
                phys = locs[0].get("physicalLocation") or {}
                file = (phys.get("artifactLocation") or {}).get("uri")
                line = _to_int((phys.get("region") or {}).get("startLine"))
                for ll in locs[0].get("logicalLocations") or []:
                    kind = (ll.get("kind") or "").lower()
                    name = ll.get("fullyQualifiedName") or ll.get("name")
                    if kind in ("module", "entity", "type", "") and module is None:
                        module = name
                    elif obj is None:
                        obj = name
            if file and file.startswith("file:"):
                file = _uri_to_path(file)
            waived = bool(res.get("suppressions"))
            out.append(
                Message(
                    severity=sev,
                    msg_id=rule_id,
                    text=text,
                    file=file,
                    line=line,
                    module=module or props.get("module"),
                    object=obj or props.get("object"),
                    check=props.get("check") or props.get("checks"),
                    waived=waived,
                )
            )
    return out


# --------------------------------------------------------------------------- CSV

# Column-name keywords -> Message field. Matched case-insensitively as substrings.
_CSV_COLUMNS = [
    ("severity", ("severity", "sev", "type")),
    ("msg_id", ("message id", "msgid", "msg id", "id", "code")),
    ("file", ("file",)),
    ("line", ("line",)),
    ("module", ("module", "entity")),
    ("object", ("object", "signal", "instance")),
    ("check", ("check", "property")),
    ("waived", ("waive", "wont", "won't")),
    ("text", ("message", "text", "description")),
]


def _map_csv_header(header: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for fld, keys in _CSV_COLUMNS:
        for col in header:
            if col in used:
                continue
            lc = col.strip().lower()
            if any(k == lc or (len(k) > 2 and k in lc) for k in keys):
                mapping[fld] = col
                used.add(col)
                break
    return mapping


def parse_csv(path: Path) -> list[Message]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        m = _map_csv_header(list(reader.fieldnames))
        out = []
        for row in reader:
            get = lambda f: (row.get(m[f]) or "").strip() if f in m else ""  # noqa: E731
            msg_id, sev = get("msg_id"), get("severity")
            # Some exports combine them as "W-BPS-0027".
            combo = re.match(r"^([EWI])-(.+)$", msg_id)
            if combo:
                sev, msg_id = sev or combo.group(1), combo.group(2)
            out.append(
                Message(
                    severity=_norm_sev(sev),
                    msg_id=msg_id or "UNKNOWN",
                    text=get("text"),
                    file=get("file") or None,
                    line=_to_int(get("line")),
                    module=get("module") or None,
                    object=get("object") or None,
                    check=get("check") or None,
                    waived=get("waived").lower() in {"true", "yes", "1", "wont_fix", "won't fix"},
                )
            )
        return out


# --------------------------------------------------------------------------- log

_LOG_RE = re.compile(
    r"^(?P<sev>[EWI])-(?P<id>[A-Z][A-Z0-9_]*-\d+):\s*"
    r"(?:(?P<file>[^\s()][^()]*?)\((?P<line>\d+)\):\s*)?"
    r"(?P<text>.*)$"
)


def parse_log_lines(lines: Iterable[str]) -> list[Message]:
    out: list[Message] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = _LOG_RE.match(line.strip())
        if m:
            out.append(
                Message(
                    severity=m.group("sev"),
                    msg_id=m.group("id"),
                    text=m.group("text").strip(),
                    file=(m.group("file") or "").strip() or None,
                    line=_to_int(m.group("line")),
                )
            )
        elif out and line[:1] in (" ", "\t") and line.strip():
            # Continuation of a wrapped message.
            out[-1].text += " " + line.strip()
    return out


def parse_log(path: Path) -> list[Message]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        return parse_log_lines(fh)


# --------------------------------------------------------------------------- helpers


def summarize(messages: list[Message], top_n: int = 25) -> dict[str, Any]:
    sev = Counter(SEVERITY_NAMES.get(m.severity, m.severity) for m in messages)
    by_id: dict[str, dict[str, Any]] = {}
    for m in messages:
        e = by_id.setdefault(
            m.msg_id, {"msg_id": m.msg_id, "severity": m.severity, "count": 0, "example": m.text[:200]}
        )
        e["count"] += 1
    ids = sorted(by_id.values(), key=lambda e: ({"E": 0, "W": 1}.get(e["severity"], 2), -e["count"]))
    files = Counter(m.file for m in messages if m.file)
    modules = Counter(m.module for m in messages if m.module)
    return {
        "total": len(messages),
        "waived": sum(m.waived for m in messages),
        "by_severity": dict(sev),
        "by_message_id": ids[:top_n],
        "distinct_message_ids": len(by_id),
        "top_files": [{"file": f, "count": c} for f, c in files.most_common(10)],
        "top_modules": [{"module": f, "count": c} for f, c in modules.most_common(10)],
    }


def filter_messages(
    messages: list[Message],
    severities: list[str] | None = None,
    msg_ids: list[str] | None = None,
    file_contains: str | None = None,
    module: str | None = None,
    text_contains: str | None = None,
    include_waived: bool = False,
) -> list[Message]:
    sevs = {_norm_sev(s) for s in severities} if severities else None
    ids = {i.upper() for i in msg_ids} if msg_ids else None
    out = []
    for m in messages:
        if sevs and m.severity not in sevs:
            continue
        if ids and m.msg_id.upper() not in ids:
            continue
        if file_contains and (not m.file or file_contains.lower() not in m.file.lower()):
            continue
        if module and (not m.module or module.lower() != m.module.lower()):
            continue
        if text_contains and text_contains.lower() not in m.text.lower():
            continue
        if m.waived and not include_waived:
            continue
        out.append(m)
    return out
