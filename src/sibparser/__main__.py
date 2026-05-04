"""CLI entry point for ``python -m sibparser`` and the ``sibparser`` script.

Subcommands:

* ``serve``    - start the local web UI (default).
* ``discover`` - print the discovered category tree to stdout.
* ``run``      - non-interactive run for a list of category paths or a single URL.
"""
from __future__ import annotations

import json
import logging
import sys
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.tree import Tree

from .config import get_settings
from .drive import open_drive
from .runner import ProgressEvent, Runner, RunRequest
from .site import CategoryNode, category_node_to_dict
from .state import State

app = typer.Typer(help="Siberian Health catalog parser with Google Drive sync.")
console = Console()


def _print_event(event: ProgressEvent) -> None:
    style = {
        "info": "cyan",
        "category": "bold magenta",
        "product": "white",
        "file": "dim",
        "error": "red",
        "done": "bold green",
    }.get(event.kind, "white")
    console.print(f"[{style}]{event.message}[/{style}]")


@app.command()
def serve(
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Открыть UI в браузере"),
) -> None:
    """Start the local web UI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}/"
    console.print(f"[bold green]Запускаю UI на {url}[/bold green]")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    from .server import serve as run_server

    run_server()


@app.command()
def discover(json_out: bool = typer.Option(False, "--json")) -> None:
    """Discover and print the catalog tree (no Drive auth needed)."""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    state = State(settings.state_db)
    runner = Runner(settings=settings, state=state, drive=None, progress=_print_event)
    tree = runner.discover_tree()
    if json_out:
        sys.stdout.write(json.dumps([category_node_to_dict(n) for n in tree], ensure_ascii=False, indent=2))
        return
    rich_tree = Tree("[bold]Каталог[/bold]")
    _add_to_rich_tree(rich_tree, tree)
    console.print(rich_tree)


def _add_to_rich_tree(parent: Tree, nodes: list[CategoryNode]) -> None:
    for node in nodes:
        sub = parent.add(f"[cyan]{node.name}[/cyan] " + (f"[dim]{node.url}[/dim]" if node.url else ""))
        if node.children:
            _add_to_rich_tree(sub, node.children)


@app.command("run")
def run_cmd(
    category: list[str] = typer.Option(
        None,
        "--category",
        "-c",
        help="Путь категории (можно несколько). Например 'Каталог/Чаи и напитки'",
    ),
    product_url: str | None = typer.Option(None, "--product", help="Распарсить один товар по URL"),
    limit: int = typer.Option(0, "--limit", help="Сколько товаров на категорию"),
    upload: bool = typer.Option(True, "--upload/--no-upload", help="Загружать в Google Drive"),
    credentials: Path | None = typer.Option(None, "--credentials"),
) -> None:
    """Run a parse + upload non-interactively."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    settings = get_settings()
    state = State(settings.state_db)
    drive = None
    if upload:
        cred_path = credentials or settings.google_credentials
        try:
            drive = open_drive(
                client_secret_path=cred_path,
                token_path=settings.google_token,
                state=state,
                root_folder_name=settings.drive_root_folder,
            )
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            console.print("[yellow]Совет: запусти с --no-upload чтобы только распарсить локально[/yellow]")
            raise typer.Exit(2) from exc

    runner = Runner(settings=settings, state=state, drive=drive, progress=_print_event)
    runner.run(
        RunRequest(
            selected_category_paths=list(category or []),
            single_product_url=product_url,
            products_per_category_limit=limit,
            upload_to_drive=upload,
        )
    )


if __name__ == "__main__":
    app()
