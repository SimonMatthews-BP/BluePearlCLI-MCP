# bluepearl-mcp

An MCP server that lets an AI model run **Blue Pearl Analyze** (`BluePearlCLI`), read the resulting
lint/CDC messages, look at the HDL around each one, ask Blue Pearl for its own explanation of a
message, and check whether a fix worked by comparing runs.

## How it drives Blue Pearl

Everything is based on the documented batch flow (User Guide §5.6–5.7, Reference §3.1/§4.1):

```
BluePearlCLI [-f design.f] -output <run>/results -logfile <run>/bluepearl.log -write_sarif -verbosity 2
             [-waivers w.xml] [-root top] [+incdir+...] [+define+...] [hdl files...]
             [-tcl your_setup.tcl ...] -tcl <run>/bpmcp_run.tcl
```

`bpmcp_run.tcl` is generated per run. It enables the requested packages/checks
(`BPS::set_package_enabled`, `BPS::set_check_enabled`), runs `BPS::run load` and, for the analyze
phase, `BPS::run analyze`, exports `BPS::get_result_run_messages -reportcsv`, and exits.

Results are read from the best available source: the SARIF 2.1.0 file (`-write_sarif`), then
the CSV export, then the log (`W-BPS-0027: file(70): text` lines), then stdout.

Each run gets its own directory under `BLUEPEARL_RUNS_DIR`, so runs never overwrite each other's
`results.db`, and old runs can be queried and compared later.

## Tools

| Tool | Purpose |
|---|---|
| `bluepearl_status` | CLI path, version, optional `-license_test` |
| `bluepearl_run` | Run load/analyze from a `.f` file, HDL file list, and/or setup Tcl; returns a summary + `run_id` |
| `bluepearl_import_results` | Load an existing `.sarif`/`.csv`/`.log`, or a results directory (exports from `results.db`) |
| `bluepearl_get_summary` | Counts by severity, message id (with example text), file, module |
| `bluepearl_get_messages` | Filtered, paginated messages (severity, id, file, module, text, waived) |
| `bluepearl_get_source` | HDL lines around a reported location |
| `bluepearl_help` | `BluePearlCLI -help <message id or check>` |
| `bluepearl_list_checks` | `BPS::get_checks` |
| `bluepearl_compare_runs` | Fixed vs. introduced messages between two runs (ignores line-number shifts) |
| `bluepearl_read_log` | Tail/grep the run log, e.g. for load or license failures |
| `bluepearl_tcl` | Arbitrary Tcl; only registered when `BLUEPEARL_ALLOW_RAW_TCL=1` |

The summary is kept small on purpose; the model pulls details on demand, which keeps large
designs (thousands of messages) from flooding its context.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `BLUEPEARL_CLI` | — | Full path to `BluePearlCLI` / `BluePearlCLI.exe` |
| `BLUEPEARL_HOME` | — | Install dir; searched if `BLUEPEARL_CLI` unset, then `$PATH` |
| `BLUEPEARL_ALLOWED_ROOTS` | cwd | `os.pathsep`-separated dirs the model may point at |
| `BLUEPEARL_RUNS_DIR` | `./.bluepearl_mcp_runs` | Where run directories go. **Keep this on a local disk** (User Guide §5.8) |
| `BLUEPEARL_TIMEOUT` | 3600 | Per-run timeout, seconds |
| `BLUEPEARL_EXTRA_ARGS` | — | Extra CLI args for every invocation (e.g. `-settingsdir /path`) |
| `BLUEPEARL_MAX_MESSAGE_CHARS` | 400 | Truncate long message text |
| `BLUEPEARL_ALLOW_RAW_TCL` | off | Enable `bluepearl_tcl` |

License variables (e.g. `LM_LICENSE_FILE` / `BLUEPEARL_LICENSE_FILE`, whatever your site uses)
are inherited from the server's environment.

## Install and connect

```bash
pip install -e .          # or: uv pip install -e .
```

Claude Desktop / any MCP client (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bluepearl": {
      "command": "bluepearl-mcp",
      "env": {
        "BLUEPEARL_HOME": "/opt/BluePearl/2026.4",
        "BLUEPEARL_ALLOWED_ROOTS": "/home/me/projects",
        "BLUEPEARL_RUNS_DIR": "/home/me/.bluepearl_mcp_runs",
        "LM_LICENSE_FILE": "27000@licserver"
      }
    }
  }
}
```

Claude Code: `claude mcp add bluepearl -e BLUEPEARL_HOME=/opt/BluePearl/2026.4 -- bluepearl-mcp`

Works with MCP Python SDK 1.x (`FastMCP`) and 2.x (`MCPServer`).

## Using a GUI project

Run the design once in the GUI, then pass the generated files (User Guide §5.7):
`ffile = Results/design.bluepearlgenerated.f`, or `setup_tcl = [design.settings.tcl]` with
`phase = "analyze"`. Note that a GUI `.f` already sets `-output`/`-waivers`; the server's own
`-output` comes after it on the command line so results land in the run directory.
To reuse results the GUI already produced, call `bluepearl_import_results` on the Results directory.

## Validate against your installation first

The manuals document that SARIF, CSV export and the log exist, but not their exact field names,
CSV columns, or the CLI's exit codes. The parsers are tolerant and the test suite uses a fake
CLI (`tests/fake_bluepearl/BluePearlCLI`), but before relying on this please:

1. Run one real design and compare `bluepearl_get_summary` with the message summary in the GUI/log.
2. Look at the `.sarif` and `messages.csv` in the run directory. If module/object/check names are
   stored under different keys, adjust `parse_sarif` / `_CSV_COLUMNS` in `parsers.py`.
3. Check that `BPS::run load` followed by `BPS::run analyze` is how your version wants it, and that
   `-help BPS-0027` and `BPS::get_checks ... -report true` print what you expect.

## Tests

```bash
pip install -e ".[test]" && pytest
```
