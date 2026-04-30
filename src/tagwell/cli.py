"""CLI entry point for tagwell."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from tagwell.scan import scan_library
from tagwell.report import generate_report
from tagwell.quality import generate_quality_report
from tagwell.releases_report import generate_releases_report
from tagwell.complete import build_releases_jsonl
from tagwell.patch import build_plan, apply_plan, dry_run_plan

console = Console(stderr=True)


@click.group()
@click.version_option(package_name="tagwell")
def cli() -> None:
    """tagwell — read-only local music metadata scanner."""


@cli.command()
@click.argument("music_root", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", required=True, type=click.Path(dir_okay=False), help="Output JSONL file path.")
@click.option("--pretty", is_flag=True, default=False, help="Pretty-print each JSON record (multi-line).")
@click.option("--follow-symlinks", is_flag=True, default=False, help="Follow symbolic links during scan.")
@click.option("--on-error", type=click.Choice(["continue", "fail"]), default="continue", show_default=True, help="Error handling strategy.")
def scan(
    music_root: str,
    output: str,
    pretty: bool,
    follow_symlinks: bool,
    on_error: str,
) -> None:
    """Recursively scan MUSIC_ROOT and export metadata to JSONL."""
    root = Path(music_root)
    out_path = Path(output)

    # Create output directory if needed
    out_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]tagwell scan[/bold]  {root}")
    console.print(f"  output → {out_path}")
    console.print()

    with open(out_path, "w", encoding="utf-8") as f:
        summary = scan_library(
            root,
            f,
            pretty=pretty,
            follow_symlinks=follow_symlinks,
            on_error=on_error,
        )

    # Console summary
    out_size = out_path.stat().st_size
    _print_summary(summary, out_path, out_size)

    if summary["errors"] > 0:
        sys.exit(1)


@cli.group()
def report() -> None:
    """Generate Markdown reports from tagwell JSONL files."""


def _auto_report_path(report_type: str) -> Path:
    """Return a timestamped report path in the current working directory."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return Path(f"report-{report_type}-{ts}.md")


def _render_report(name: str, jsonl_file: str, output: str | None, autoname: bool, generator) -> None:
    jsonl_path = Path(jsonl_file)
    if autoname or output is None:
        out_path = _auto_report_path(name)
    else:
        out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]tagwell report {name}[/bold]  {jsonl_path}")
    md = generator(jsonl_path)
    out_path.write_text(md, encoding="utf-8")
    console.print(f"  output → {out_path} ({_human_size(out_path.stat().st_size)})")


@report.command("metadata")
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", default=None, type=click.Path(dir_okay=False), help="Output Markdown report path.")
@click.option("-O", "autoname", is_flag=True, default=False, help="Auto-name output as report-metadata-{yyyymmddhhmmss}.md in cwd.")
def report_metadata(jsonl_file: str, output: str | None, autoname: bool) -> None:
    """Tag-quality view of a library JSONL: MBID coverage, parsed fields, library shape."""
    if output is None and not autoname:
        raise click.UsageError("Provide --out PATH or -O for auto-naming.")
    _render_report("metadata", jsonl_file, output, autoname, generate_report)


@report.command("quality")
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", default=None, type=click.Path(dir_okay=False), help="Output Markdown report path.")
@click.option("-O", "autoname", is_flag=True, default=False, help="Auto-name output as report-quality-{yyyymmddhhmmss}.md in cwd.")
def report_quality(jsonl_file: str, output: str | None, autoname: bool) -> None:
    """Audio-source quality view of a library JSONL: codec, bitrate, encoder integrity."""
    if output is None and not autoname:
        raise click.UsageError("Provide --out PATH or -O for auto-naming.")
    _render_report("quality", jsonl_file, output, autoname, generate_quality_report)


@report.command("releases")
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", default=None, type=click.Path(dir_okay=False), help="Output Markdown report path.")
@click.option("-O", "autoname", is_flag=True, default=False, help="Auto-name output as report-releases-{yyyymmddhhmmss}.md in cwd.")
def report_releases(jsonl_file: str, output: str | None, autoname: bool) -> None:
    """Release-level view of a library_releases JSONL: completeness, MB match audit, MB-canonical shape."""
    if output is None and not autoname:
        raise click.UsageError("Provide --out PATH or -O for auto-naming.")
    _render_report("releases", jsonl_file, output, autoname, generate_releases_report)


@cli.command()
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", required=True, type=click.Path(dir_okay=False), help="Output JSONL path (e.g. library_releases.jsonl).")
@click.option("--delay", type=float, default=1.0, show_default=True, help="Seconds between MusicBrainz API requests.")
@click.option("--refresh", is_flag=True, default=False, help="Re-fetch every release, ignoring the existing output file.")
def complete(jsonl_file: str, output: str, delay: float, refresh: bool) -> None:
    """Enrich a library JSONL with release-level MusicBrainz data and per-track local presence."""
    jsonl_path = Path(jsonl_file)
    out_path = Path(output)

    mode = "[bold red]REFRESH[/bold red]" if refresh else "[bold]incremental[/bold]"
    console.print(f"[bold]tagwell complete[/bold]  {jsonl_path}  ({mode})")
    console.print(f"  output → {out_path}")
    console.print()

    def on_progress(i: int, total: int, rid: str) -> None:
        console.print(f"  [{i}/{total}] {rid[:12]}…")

    summary = build_releases_jsonl(
        jsonl_path,
        out_path,
        delay=delay,
        refresh=refresh,
        on_progress=on_progress,
    )

    console.print()
    table = Table(title="Complete Summary", show_header=False, title_style="bold")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Unique release_ids", str(summary.unique_release_ids))
    table.add_row("Fetched (this run)", str(summary.fetched))
    table.add_row("Skipped (cached)", str(summary.skipped_cached))
    table.add_row("Failed", str(summary.failed))
    table.add_row("Orphan files (no release_id)", str(summary.orphan_files))
    table.add_row("Total local files", str(summary.total_local_files))
    table.add_row("Matched local files", str(summary.matched_local_files))
    table.add_row("  by recording_id", str(summary.matched_by_recording_id))
    table.add_row("  by release_track_id", str(summary.matched_by_release_track_id))
    table.add_row("  by position", str(summary.matched_by_position))
    table.add_row("Output size", _human_size(out_path.stat().st_size))
    console.print(table)

    if summary.failed:
        sys.exit(1)


@cli.command()
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option(
    "--mode",
    type=click.Choice(["release-tags", "recording-id", "all"]),
    default="release-tags",
    show_default=True,
    help="Patch scope: release-tags patches script/releasecountry; recording-id patches missing recording MBIDs.",
)
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Actually write tags. Without this flag, only a dry-run plan is shown.")
@click.option("--delay", type=float, default=1.0, show_default=True, help="Seconds between MusicBrainz API requests.")
def patch(jsonl_file: str, mode: str, do_apply: bool, delay: float) -> None:
    """Patch missing MusicBrainz tags via the MusicBrainz API."""
    jsonl_path = Path(jsonl_file)

    apply_mode = "[bold red]APPLY[/bold red]" if do_apply else "[bold yellow]dry-run[/bold yellow]"
    console.print(f"[bold]tagwell patch[/bold]  {jsonl_path}  ({mode}, {apply_mode})")
    console.print()

    def on_progress(i: int, total: int, rid: str) -> None:
        console.print(f"  [{i}/{total}] {rid[:12]}…", end="")

    console.print("Querying MusicBrainz API…")
    plan = build_plan(jsonl_path, mode=mode, delay=delay, on_progress=on_progress)
    console.print()
    console.print(
        f"  ✓ {len(plan.release_cache)} releases fetched"
        + (f", [red]{len(plan.api_failures)} failed[/red]" if plan.api_failures else "")
    )
    console.print()

    if do_apply:
        summary, actions = apply_plan(plan)
    else:
        summary, actions = dry_run_plan(plan)

    # Show first N planned actions
    if actions:
        shown = 0
        for rel_path, fields in actions:
            if shown >= 20:
                console.print(f"  … and {len(actions) - shown} more files")
                break
            console.print(f"  [dim]{rel_path}[/dim]")
            for f in fields:
                console.print(f"    + {f}")
            shown += 1
        console.print()

    table = Table(title="Patch Summary", show_header=False, title_style="bold")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Mode", mode)
    table.add_row("Target files", str(summary.total_targets))
    table.add_row("Unique releases", str(summary.unique_releases))
    table.add_row("API fetched", str(summary.api_fetched))
    table.add_row("API failed", str(summary.api_failed))
    table.add_row("Files patched" if do_apply else "Files to patch", str(summary.files_patched))
    table.add_row("Files skipped", str(summary.files_skipped))
    if do_apply:
        table.add_row("Files failed", str(summary.files_failed))
    table.add_row("Fields written" if do_apply else "Fields to write", str(summary.fields_written))
    console.print(table)

    # Write patch log alongside the JSONL file
    if do_apply and actions:
        log_name = "patch-log.txt" if mode == "release-tags" else f"patch-log-{mode}.txt"
        log_path = jsonl_path.parent / log_name
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"tagwell patch --mode {mode} --apply  {jsonl_path}\n")
            lf.write(f"Files patched: {summary.files_patched}  Fields written: {summary.fields_written}\n\n")
            for rel_path, fields in actions:
                lf.write(f"{rel_path}\n")
                for f in fields:
                    lf.write(f"  + {f}\n")
        console.print(f"\n[dim]Patch log → {log_path}[/dim]")

    if not do_apply and summary.files_patched > 0:
        console.print("\n[dim]Run with --apply to write tags.[/dim]")


def _print_summary(summary: dict, out_path: Path, out_size: int) -> None:
    console.print()
    table = Table(title="Scan Summary", show_header=False, title_style="bold")
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Root", summary["root"])
    table.add_row("Output", str(out_path))
    table.add_row("Audio files", str(summary["audio_files"]))
    table.add_row("Skipped (non-audio)", str(summary["skipped_files"]))
    table.add_row("Errors", str(summary["errors"]))
    table.add_row("Elapsed", f"{summary['elapsed_seconds']:.2f}s")
    table.add_row("Output size", _human_size(out_size))

    console.print(table)


def _human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} TiB"
