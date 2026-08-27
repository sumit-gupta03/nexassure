# Copyright 2026 The NexAssure Authors
# SPDX-License-Identifier: Apache-2.0
"""Machine-readable report formats.

JSON for downstream tooling, JUnit XML so CI systems render checks as tests,
Markdown for pull-request comments, and a self-contained HTML page for sharing
with people who do not read terminals.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..core.enums import CheckStatus
from ..core.models import RunResult, TableProfile


def to_json(run: RunResult, indent: int = 2) -> str:
    """Full run as JSON, nothing elided."""
    return json.dumps(run.model_dump(mode="json"), indent=indent, default=str)


def profile_to_json(profile: TableProfile, indent: int = 2) -> str:
    return json.dumps(profile.model_dump(mode="json"), indent=indent, default=str)


def to_junit(run: RunResult) -> str:
    """JUnit XML.

    Every mainstream CI system knows this format, so checks show up in the same
    UI as unit tests with no plugin. ``errored`` maps to ``<error>`` and
    ``failed`` to ``<failure>``, which keeps the two distinguishable in reports.
    """
    s = run.summary
    suite = ET.Element(
        "testsuite",
        {
            "name": run.suite_name,
            "tests": str(s.total),
            "failures": str(s.failed + s.warned),
            "errors": str(s.errored),
            "skipped": str(s.skipped),
            "time": f"{run.duration_ms / 1000:.3f}",
            "timestamp": run.started_at.isoformat(),
            "hostname": run.connection_name,
        },
    )

    properties = ET.SubElement(suite, "properties")
    for key, value in {
        "run_id": run.run_id,
        "connection": run.connection_name,
        "environment": run.environment or "",
        "triggered_by": run.triggered_by,
    }.items():
        ET.SubElement(properties, "property", {"name": key, "value": str(value)})

    for result in run.results:
        classname = result.dataset or run.suite_name
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "name": result.check_name,
                "classname": classname,
                "time": f"{result.duration_ms / 1000:.3f}",
            },
        )
        message = result.message or result.status.value
        detail = _failure_detail(result)

        if result.status is CheckStatus.ERRORED:
            node = ET.SubElement(
                case, "error", {"message": message[:500], "type": result.check_type}
            )
            node.text = detail
        elif result.status in (CheckStatus.FAILED, CheckStatus.WARNED):
            node = ET.SubElement(
                case, "failure", {"message": message[:500], "type": result.check_type}
            )
            node.text = detail
        elif result.status is CheckStatus.SKIPPED:
            ET.SubElement(case, "skipped", {"message": message[:500]})

    ET.indent(suite, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")


def _failure_detail(result: Any) -> str:
    lines = []
    if result.description:
        lines.append(f"Why it matters: {result.description}")
    lines.append(f"Message: {result.message}")
    if result.expected is not None:
        lines.append(f"Expected: {result.expected!r}")
    if result.observed is not None:
        lines.append(f"Observed: {result.observed!r}")
    if result.rows_failed is not None:
        lines.append(f"Failing rows: {result.rows_failed:,} of {result.rows_scanned or 0:,}")
    if result.error:
        lines.append(f"Error: {result.error}")
    if result.query:
        lines.append(f"\nSQL:\n{result.query.strip()}")
    if result.sample_rows:
        lines.append("\nSample failing rows:")
        for row in result.sample_rows[:5]:
            lines.append(f"  {json.dumps(row, default=str)}")
    return "\n".join(lines)


def to_markdown(run: RunResult, include_passed: bool = False) -> str:
    """Markdown summary, sized for a pull-request comment."""
    s = run.summary
    icon = {"passed": "✅", "failed": "❌", "errored": "🚨"}.get(run.status.value, "•")

    lines = [
        f"## {icon} NexAssure - `{run.suite_name}`",
        "",
        f"**{run.status.value.upper()}** on `{run.connection_name}` - "
        f"{s.passed} passed, {s.failed} failed, {s.errored} errored, {s.skipped} skipped "
        f"({run.duration_ms / 1000:.1f}s)",
        "",
    ]

    shown = run.results if include_passed else run.failures()
    if shown:
        lines += [
            "| | Check | Target | Detail |",
            "|---|---|---|---|",
        ]
        status_icon = {
            CheckStatus.PASSED: "✅",
            CheckStatus.FAILED: "❌",
            CheckStatus.WARNED: "⚠️",
            CheckStatus.ERRORED: "🚨",
            CheckStatus.SKIPPED: "⏭️",
        }
        for result in shown[:50]:
            target = result.dataset or "-"
            if result.column:
                target = f"{target}.{result.column}"
            detail = (result.message or "").replace("|", "\\|")[:200]
            lines.append(
                f"| {status_icon.get(result.status, '•')} | `{result.check_name}` "
                f"| `{target}` | {detail} |"
            )
        if len(shown) > 50:
            lines.append(f"| | | | _...and {len(shown) - 50} more_ |")
    else:
        lines.append("All checks passed.")

    lines += ["", f"<sub>run `{run.run_id}`</sub>"]
    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NexAssure - {suite}</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #14161a; --muted: #5b6472; --line: #e3e7ec; --card: #f7f9fb;
    --pass: #12805c; --fail: #c02a3a; --warn: #a8700a; --error: #8a2be2; --skip: #6b7280;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1216; --fg: #e8ecf1; --muted: #9aa4b2; --line: #262c35; --card: #161b21;
      --pass: #35d29a; --fail: #ff6b7d; --warn: #ecc06a; --error: #c58cff; --skip: #8b95a3;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
         font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }}
  .sub {{ color: var(--muted); margin-bottom: 1.75rem; font-size: .9rem; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: .75rem; margin-bottom: 2rem; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
           padding: .85rem 1rem; }}
  .card .n {{ font-size: 1.6rem; font-weight: 650; letter-spacing: -.02em; }}
  .card .l {{ color: var(--muted); font-size: .74rem; text-transform: uppercase;
              letter-spacing: .06em; margin-top: .15rem; }}
  .tablewrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .875rem; min-width: 640px; }}
  th, td {{ text-align: left; padding: .6rem .8rem; border-bottom: 1px solid var(--line);
            vertical-align: top; }}
  th {{ background: var(--card); font-weight: 600; font-size: .76rem;
        text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
  tr:last-child td {{ border-bottom: none; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .86em; }}
  .badge {{ display: inline-block; padding: .1rem .45rem; border-radius: 5px;
            font-size: .72rem; font-weight: 650; letter-spacing: .03em; }}
  .passed {{ color: var(--pass); background: color-mix(in srgb, var(--pass) 14%, transparent); }}
  .failed {{ color: var(--fail); background: color-mix(in srgb, var(--fail) 14%, transparent); }}
  .warned {{ color: var(--warn); background: color-mix(in srgb, var(--warn) 14%, transparent); }}
  .errored {{ color: var(--error); background: color-mix(in srgb, var(--error) 14%, transparent); }}
  .skipped {{ color: var(--skip); background: color-mix(in srgb, var(--skip) 14%, transparent); }}
  .muted {{ color: var(--muted); }}
  details {{ margin-top: .4rem; }}
  summary {{ cursor: pointer; color: var(--muted); font-size: .8rem; }}
  pre {{ background: var(--card); border: 1px solid var(--line); border-radius: 7px;
         padding: .7rem; overflow-x: auto; font-size: .8rem; margin: .5rem 0 0; }}
  footer {{ margin-top: 2.5rem; color: var(--muted); font-size: .8rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{suite} <span class="badge {status_class}">{status}</span></h1>
  <div class="sub">
    Connection <code>{connection}</code> &middot; {started} &middot; {duration:.2f}s
    &middot; run <code>{run_id}</code>{environment}
  </div>
  <div class="cards">{cards}</div>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Status</th><th>Check</th><th>Target</th><th>Detail</th><th>Time</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <footer>Generated by NexAssure &middot; Apache-2.0</footer>
</div>
</body>
</html>
"""


def to_html(run: RunResult, include_passed: bool = True) -> str:
    """Self-contained HTML report - no external assets, works from a file:// URL."""
    s = run.summary
    cards = "".join(
        f'<div class="card"><div class="n {css}">{value}</div><div class="l">{label}</div></div>'
        for label, value, css in [
            ("Total", s.total, ""),
            ("Passed", s.passed, "passed"),
            ("Failed", s.failed, "failed"),
            ("Warned", s.warned, "warned"),
            ("Errored", s.errored, "errored"),
            ("Skipped", s.skipped, "skipped"),
            ("Pass rate", f"{s.pass_rate:.0%}", ""),
        ]
    )

    shown = run.results if include_passed else run.failures()
    rows = []
    for result in shown:
        target = result.dataset or "-"
        if result.column:
            target = f"{target}.{result.column}"

        detail = f"<div>{html.escape(result.message or '')}</div>"
        if result.description:
            detail += f'<div class="muted">{html.escape(result.description)}</div>'
        extras = _html_extras(result)
        if extras:
            detail += f"<details><summary>Evidence</summary>{extras}</details>"

        rows.append(
            f"<tr>"
            f'<td><span class="badge {result.status.value}">{result.status.value}</span></td>'
            f"<td><code>{html.escape(result.check_name)}</code>"
            f'<div class="muted">{html.escape(result.check_type)}</div></td>'
            f"<td><code>{html.escape(target)}</code></td>"
            f"<td>{detail}</td>"
            f'<td class="muted">{result.duration_ms:.0f}ms</td>'
            f"</tr>"
        )

    return _HTML_TEMPLATE.format(
        suite=html.escape(run.suite_name),
        status=run.status.value,
        status_class=run.status.value,
        connection=html.escape(run.connection_name),
        started=run.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        duration=run.duration_ms / 1000,
        run_id=run.run_id[:12],
        environment=f" &middot; {html.escape(run.environment)}" if run.environment else "",
        cards=cards,
        rows="".join(rows) or '<tr><td colspan="5" class="muted">No checks ran.</td></tr>',
    )


def _html_extras(result: Any) -> str:
    parts = []
    if result.expected is not None or result.observed is not None:
        parts.append(
            f"<pre>expected: {html.escape(repr(result.expected))}\n"
            f"observed: {html.escape(repr(result.observed))}</pre>"
        )
    if result.sample_rows:
        body = "\n".join(json.dumps(row, default=str) for row in result.sample_rows[:5])
        parts.append(f"<pre>{html.escape(body)}</pre>")
    if result.query:
        parts.append(f"<pre>{html.escape(result.query.strip())}</pre>")
    return "".join(parts)


#: ``format`` name to renderer, used by the CLI ``--format`` flag.
FORMATTERS = {
    "json": to_json,
    "junit": to_junit,
    "markdown": to_markdown,
    "md": to_markdown,
    "html": to_html,
}


def write_report(run: RunResult, path: str | Path, fmt: str | None = None) -> Path:
    """Render a run to a file, inferring the format from the extension.

    Returns:
        The path written.
    """
    target = Path(path).expanduser()
    if fmt is None:
        fmt = {
            ".json": "json",
            ".xml": "junit",
            ".html": "html",
            ".htm": "html",
            ".md": "markdown",
        }.get(target.suffix.lower(), "json")

    renderer = FORMATTERS.get(fmt.lower())
    if renderer is None:
        raise ValueError(
            f"Unknown report format {fmt!r}. Available: {', '.join(sorted(FORMATTERS))}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(renderer(run), encoding="utf-8")
    return target
