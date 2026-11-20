# Installing BluePearlCLI-MCP for OpenCode

## Overview

BluePearlCLI-MCP (`bluepearlcli-mcp`) is a local stdio MCP server. It lets an OpenCode agent run
Blue Pearl Analyze (`BluePearlCLI`), read the lint/CDC messages, view the HDL around each one, and
compare runs to confirm a fix. Each run gets its own directory, so earlier runs stay available for
comparison.

| Tool | What it does |
| --- | --- |
| `bluepearl_status` | Shows the CLI path and version; can run a license test |
| `bluepearl_run` | Runs load/analyze from a `.f` file, HDL file list and/or setup Tcl; returns a summary and a `run_id` |
| `bluepearl_lint_code` | Lints one module passed as text; returns `BPS_CLEAN`, `BPS_WARN`, `BPS_FAIL` or `BPS_TOOL_ERROR` plus messages |
| `bluepearl_import_results` | Loads existing `.sarif`, `.csv`, `.log` or a GUI Results directory |
| `bluepearl_list_runs` | Lists earlier runs |
| `bluepearl_get_summary` | Counts by severity, message id, file and module |
| `bluepearl_get_messages` | Filtered, paginated messages (severity, id, file, module, text, waived) |
| `bluepearl_get_source` | HDL lines around a reported location |
| `bluepearl_help` | Blue Pearl's own explanation of a message id or check |
| `bluepearl_list_checks` | Lists available checks |
| `bluepearl_compare_runs` | Fixed vs. introduced messages between two runs |
| `bluepearl_read_log` | Tails or greps the run log, e.g. for license failures |
| `bluepearl_write_module`, `bluepearl_revise_module`, `bluepearl_writer_info` | Optional RTL writer tools; registered only when `BLUEPEARL_WRITER_MODEL` is set |
| `bluepearl_tcl` | Raw Tcl; registered only when `BLUEPEARL_ALLOW_RAW_TCL=1` |

## Prerequisites

- Python 3.10 or later, with `pip` or `uv`.
- A Blue Pearl Analyze install that provides `BluePearlCLI` (Linux) or `BluePearlCLI.exe` (Windows).
- A working Blue Pearl license, e.g. `LM_LICENSE_FILE=<port>@<license-server>`.
- OpenCode installed, and a model provider configured in it.
- Optional, for the RTL writer tools: an Ollama server hosting the writer model, and a skills directory.

## Installation

Install the package into its own virtual environment, so OpenCode can launch it by absolute path.

1. Get the source and enter the package directory:
   ```bash
   cd BluePearlCLI-MCP/bluepearlcli-mcp
   ```
2. Create a virtual environment and install:
   ```bash
   python3 -m venv ~/venvs/bluepearl-mcp
   ~/venvs/bluepearl-mcp/bin/pip install -e .
   ```
   With uv: `uv venv ~/venvs/bluepearl-mcp && uv pip install --python ~/venvs/bluepearl-mcp -e .`
3. Note the server's path. This is the `command` OpenCode will run:
   ```bash
   ls ~/venvs/bluepearl-mcp/bin/bluepearlcli-mcp
   ```
4. Optional: run the test suite, which uses a fake CLI and needs no license:
   ```bash
   ~/venvs/bluepearl-mcp/bin/pip install -e ".[test]"
   ~/venvs/bluepearl-mcp/bin/pytest
   ```

## Configuring OpenCode

Add a `bluepearl` entry under `mcp` in `opencode.json`. Use the project's `opencode.json` (repo root)
for one design, or `~/.config/opencode/opencode.json` for all projects. The repo's
[`opencode.json.template`](opencode.json.template) is a full starting point, including an Ollama
provider and a `bluepearl-check` agent.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "bluepearl": {
      "type": "local",
      "command": ["/home/<you>/venvs/bluepearl-mcp/bin/bluepearlcli-mcp"],
      "enabled": true,
      "environment": {
        "BLUEPEARL_HOME": "/path/to/BluePearl/install",
        "BLUEPEARL_ALLOWED_ROOTS": "/path/to/designs:/path/to/more/designs",
        "BLUEPEARL_RUNS_DIR": "/local/disk/path/bluepearl_runs",
        "LM_LICENSE_FILE": "<port>@<license-server>"
      }
    }
  }
}
```

- `command` must be an absolute path; OpenCode does not activate your virtual environment.
- `BLUEPEARL_ALLOWED_ROOTS` limits which directories the model can point at. On Linux, separate
  entries with `:`; on Windows, `;`.
- Keep `BLUEPEARL_RUNS_DIR` on a local disk, not NFS.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `BLUEPEARL_CLI` | none | Full path to `BluePearlCLI`; overrides `BLUEPEARL_HOME` |
| `BLUEPEARL_HOME` | none | Install directory; searched if `BLUEPEARL_CLI` is unset, then `$PATH` |
| `BLUEPEARL_ALLOWED_ROOTS` | working directory | Directories the model may use |
| `BLUEPEARL_RUNS_DIR` | `./.bluepearl_mcp_runs` | Where run directories go |
| `BLUEPEARL_TIMEOUT` | 3600 | Per-run timeout, seconds |
| `BLUEPEARL_EXTRA_ARGS` | none | Extra CLI arguments, e.g. `-settingsdir /path` |
| `BLUEPEARL_MAX_MESSAGE_CHARS` | 400 | Truncates long message text |
| `BLUEPEARL_ALLOW_RAW_TCL` | off | Set to `1` to enable `bluepearl_tcl` |
| `BLUEPEARL_WRITER_MODEL` | none | Enables the RTL writer tools |
| `BLUEPEARL_WRITER_URL` | `http://127.0.0.1:11434` | Ollama server for the writer model |
| `BLUEPEARL_WRITER_SKILL_DIR` | `~/AI/ai_bench/skills` | Skill files given to the writer model |

License variables such as `LM_LICENSE_FILE` pass straight through to `BluePearlCLI`.

### Optional: a dedicated agent

The template defines a `bluepearl-check` agent whose prompt tells the model to lint, read errors
first, look at source and help, fix, re-run and compare, stopping after 4 rounds. Copy its `agent`
block into your config, then pick it in OpenCode or call it with `@bluepearl-check`.

### Check the connection

1. Run `opencode mcp list` and confirm `bluepearl` shows as connected.
2. Start `opencode` in your design directory and ask: "Call bluepearl_status with a license test."
3. The reply should show the CLI path, version and a passing license test. If not, see
   [Troubleshooting](#troubleshooting).

## Using it in OpenCode

Ask in plain language; the agent picks the tools. The server keeps its summary short and the model
pulls details on demand, so large designs don't flood the context.

### Typical workflow

1. **Run.** `bluepearl_run` with a `.f` file (e.g. `Results/design.bluepearlgenerated.f` from the
   GUI), setup Tcl (e.g. `design.settings.tcl` with phase `analyze`), or HDL files plus a top module.
2. **Triage.** Read the summary, then `bluepearl_get_messages` filtered by severity, errors (E)
   before warnings (W).
3. **Understand.** `bluepearl_get_source` for the HDL around a message; `bluepearl_help` on the
   message id (e.g. `BPS-0027`).
4. **Fix and re-run.** Edit the RTL, run again, then `bluepearl_compare_runs` to see what was fixed
   and what was introduced.

To reuse results the GUI already produced, ask for `bluepearl_import_results` on the Results
directory. For one module held as text, `bluepearl_lint_code` returns a single verdict line plus
messages.

### Example prompts

- "Run Blue Pearl on `Results/top.bluepearlgenerated.f` and summarise the errors."
- "Show me all W-BPS-0027 messages in `fifo_ctrl.v`, with the source around each."
- "What does BPS-0027 mean? Propose a fix for the first occurrence."
- "Lint this module with Blue Pearl, top is `uart_tx`."
- "I fixed the reset logic. Re-run and compare against the previous run."
- "Import the results in `Results/` and list the CDC messages by module."

Waived messages are hidden unless you ask for them.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `bluepearl` not listed or fails to start | `command` path wrong or not absolute. Run the path in a shell; it should wait silently on stdin. |
| `BluePearlCLI` not found | Set `BLUEPEARL_CLI` to the full binary path, or check `BLUEPEARL_HOME`. |
| License failure | `LM_LICENSE_FILE` missing from `environment`. Run `bluepearl_status` with a license test, or `bluepearl_read_log` on the failed run. |
| "Path ... is outside the allowed roots" | The design is outside `BLUEPEARL_ALLOWED_ROOTS`. Add its directory. |
| Run times out | Raise `BLUEPEARL_TIMEOUT` (seconds). |
| Writer tools missing | `BLUEPEARL_WRITER_MODEL` not set; they are registered only when it is. |
| Counts differ from the GUI | Compare the `.sarif` and `messages.csv` in the run directory; parser keys may need adjusting in `parsers.py`. |

After changing `opencode.json`, restart OpenCode so the server is relaunched with the new environment.
