"""CLI entry point for tagwell."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from tagwell.scan import scan_library
from tagwell.report import generate_report
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


@cli.command()
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--out", "-o", "output", required=True, type=click.Path(dir_okay=False), help="Output Markdown report path.")
def report(jsonl_file: str, output: str) -> None:
    """Generate a metadata quality report from a tagwell JSONL file."""
    jsonl_path = Path(jsonl_file)
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]tagwell report[/bold]  {jsonl_path}")
    md = generate_report(jsonl_path)
    out_path.write_text(md, encoding="utf-8")
    console.print(f"  output → {out_path} ({_human_size(out_path.stat().st_size)})")


@cli.command()
@click.argument("jsonl_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Actually write tags. Without this flag, only a dry-run plan is shown.")
@click.option("--delay", type=float, default=1.0, show_default=True, help="Seconds between MusicBrainz API requests.")
def patch(jsonl_file: str, do_apply: bool, delay: float) -> None:
    """Patch missing release-level tags (script, releasecountry) via MusicBrainz API."""
    jsonl_path = Path(jsonl_file)

    mode = "[bold red]APPLY[/bold red]" if do_apply else "[bold yellow]dry-run[/bold yellow]"
    console.print(f"[bold]tagwell patch[/bold]  {jsonl_path}  ({mode})")
    console.print()

    def on_progress(i: int, total: int, rid: str) -> None:
        console.print(f"  [{i}/{total}] {rid[:12]}…", end="")

    console.print("Querying MusicBrainz API…")
    plan = build_plan(jsonl_path, delay=delay, on_progress=on_progress)
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
        log_path = jsonl_path.parent / "patch-log.txt"
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"tagwell patch --apply  {jsonl_path}\n")
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
