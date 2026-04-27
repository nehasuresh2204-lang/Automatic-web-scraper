#!/usr/bin/env python3
"""
Web Scraper Agent — CLI
Usage:
  python cli.py --url "https://example.com" --query "List product names and prices"
  python cli.py --url "https://example.com" --query "..." --format csv --output results.csv
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from agent import run_agent

console = Console()

# ─── Export helpers ───────────────────────────────────────────────────────────

def export_results(data: list[dict], fmt: str, output: str) -> str:
    if not data:
        return ""

    df = pd.DataFrame(data)
    p  = Path(output) if output else None

    if fmt == "json":
        path = str(p or "results.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    elif fmt == "csv":
        path = str(p or "results.csv")
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path

    elif fmt == "excel":
        path = str(p or "results.xlsx")
        df.to_excel(path, index=False)
        return path

    return ""


def print_rich_table(data: list[dict], query: str):
    if not data:
        console.print(Panel("⚠ No data to display.", style="yellow"))
        return

    table = Table(
        title=f"[bold cyan]Results for:[/bold cyan] {query}",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
        expand=True,
    )

    cols = list(data[0].keys())
    for col in cols:
        # make source_url narrower
        if "url" in col.lower():
            table.add_column(col, style="dim blue", no_wrap=False, max_width=40)
        else:
            table.add_column(col, style="white", no_wrap=False, max_width=30)

    for row in data:
        table.add_row(*[str(row.get(c, "")) for c in cols])

    console.print(table)


# ─── Main ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Autonomous Web Scraper Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py \\
    --url "https://www.gsmarena.com/search.php3?sQuickSearch=&chk5G=selected&sPriceMin=0&sPriceMax=10000" \\
    --query "Top 10 highest-rated phones under 10000 with name, price, rating and product link"

  python cli.py \\
    --url "https://tiobe.com/tiobe-index/" \\
    --query "List top 5 programming languages with their ranking" \\
    --format csv --output langs.csv
        """,
    )
    p.add_argument("--url",    required=True, help="Starting URL to scrape")
    p.add_argument("--query",  required=True, help="Natural language extraction query")
    p.add_argument("--format", choices=["table", "json", "csv", "excel"],
                   default="table", help="Output format (default: table)")
    p.add_argument("--output", default="", help="Output file path (optional)")
    p.add_argument("--key",    default="", help="Groq API key (or set GROQ_API_KEY env var)")
    return p.parse_args()


async def _run(args):
    api_key = args.key or os.environ.get("GROQ_API_KEY", "")

    # ── Auto-fix bare URLs like "amazon.in" → "https://amazon.in" ──
    url = args.url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
        console.print(f"  [dim]ℹ  Auto-prefixed URL → {url}[/dim]")
    args.url = url

    if not api_key:
        console.print(Panel(
            "[red]❌ Groq API key not found.[/red]\n\n"
            "Set it via:\n"
            "  • [bold]--key YOUR_KEY[/bold] flag\n"
            "  • [bold]export GROQ_API_KEY=YOUR_KEY[/bold] env variable\n\n"
            "Get a free key at [link=https://console.groq.com]console.groq.com[/link]",
            title="Missing API Key", border_style="red"
        ))
        sys.exit(1)

    console.print(Panel(
        f"[bold]URL:[/bold]   {args.url}\n"
        f"[bold]Query:[/bold] {args.query}",
        title="🕷  Web Scraper Agent", border_style="cyan"
    ))

    steps: list[str] = []

    def progress_cb(msg: str):
        steps.append(msg)
        console.print(f"  {msg}")

    console.print()
    result = await run_agent(args.url, args.query, api_key, progress_cb)
    console.print()

    if result["status"] == "not_found":
        console.print(Panel(
            f"[yellow]No relevant data found.[/yellow]\n\n"
            f"[dim]Reason: {result['reasoning']}[/dim]\n\n"
            f"Pages visited: {', '.join(result['visited'])}",
            title="🔍 Not Found", border_style="yellow"
        ))
        return

    if result["status"] == "error":
        console.print(Panel(
            f"[red]An error occurred.[/red]\n{result['reasoning']}",
            title="❌ Error", border_style="red"
        ))
        return

    data = result["data"]

    # ── Display ──
    if args.format == "table":
        print_rich_table(data, args.query)
    else:
        path = export_results(data, args.format, args.output)
        console.print(Panel(
            f"[green]✅ {len(data)} items exported → [bold]{path}[/bold][/green]",
            border_style="green"
        ))
        # Always also print table for visibility
        print_rich_table(data, args.query)

    # ── Summary ──
    # Build with Text object — avoids Rich markup parser choking on URLs
    # (slashes in https://... get misread as closing tags, causing MarkupError)
    from rich.text import Text
    summary = Text()
    summary.append(f"✅ {len(data)} items extracted\n", style="bold green")
    summary.append(
        f"Pages visited ({len(result['visited'])}): "
        + " → ".join(result['visited']) + "\n",
        style="dim",
    )
    summary.append(result['reasoning'], style="dim italic")
    console.print()
    console.print(Panel(summary, title="Summary", border_style="green"))


def main():
    args = parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
