"""Orchestrate scraping + Drive upload with progress callbacks.

The runner is intentionally synchronous and lives on a worker thread so the
FastAPI server can keep serving HTTP/WebSocket requests. Progress updates are
sent through a ``ProgressBus`` (an asyncio queue) so connected clients see live
status.

Output convention: one plain-text ``<product_name>.txt`` per product, sitting
directly in its category folder. The text file contains all metadata, the full
"О продукте" description, composition, every review, and **URL lists** of the
product's images and documents — no binary files are downloaded. This makes
the resulting tree a flat searchable database for the nutritionist consultant
who reads the files; image and PDF links open straight from Siberian Wellness'
CDN when clicked.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .drive import DriveClient
from .site import (
    Browser,
    Catalog,
    CategoryNode,
    ProductCard,
    ProductScraper,
    product_to_info_text,
    safe_folder_name,
)
from .state import State

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------


@dataclass
class ProgressEvent:
    kind: str  # one of: "info", "category", "product", "file", "error", "done"
    message: str
    data: dict[str, object] = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]


def _noop(event: ProgressEvent) -> None:
    return None


# ---------------------------------------------------------------------------
# Run config
# ---------------------------------------------------------------------------


@dataclass
class RunRequest:
    """What to scrape and where to put it."""

    # If non-empty, only categories whose ``path`` is in this set (or descendants)
    # will be processed.
    selected_category_paths: list[str] = field(default_factory=list)
    # If non-empty, run a single product URL (still mirrored into its category).
    single_product_url: str | None = None
    # Limit number of products per category (for quick smoke-test runs). 0 = no limit.
    products_per_category_limit: int = 0
    # Whether to upload the product .txt file to Google Drive.
    upload_to_drive: bool = True
    # Whether to write the product .txt file to the local download directory.
    save_locally: bool = True
    # Override for the local downloads directory. None = use settings.downloads_dir.
    local_dir: str | None = None
    # Open the local folder in the OS file manager when the run completes.
    open_folder_when_done: bool = False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class Runner:
    def __init__(
        self,
        settings: Settings,
        state: State,
        drive: DriveClient | None,
        progress: ProgressCallback = _noop,
    ) -> None:
        self.settings = settings
        self.state = state
        self.drive = drive
        self.progress = progress
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _emit(self, kind: str, message: str, **data: object) -> None:
        log.info("[%s] %s %s", kind, message, data or "")
        self.progress(ProgressEvent(kind=kind, message=message, data=data))

    # -- discovery ------------------------------------------------------

    def discover_tree(self) -> list[CategoryNode]:
        self._emit("info", "Открываю каталог Siberian Wellness…")
        with Browser(headful=self.settings.headful) as br:
            tree = Catalog(br, request_delay=self.settings.request_delay).discover_tree()
        # Persist categories to state so the UI can show them after restart.
        for top in tree:
            self.state.upsert_category(
                url=top.url or f"virtual://{top.path}",
                name=top.name,
                parent_url=None,
                path=top.path,
            )
            for child in top.children:
                self.state.upsert_category(
                    url=child.url or f"virtual://{child.path}",
                    name=child.name,
                    parent_url=top.url or f"virtual://{top.path}",
                    path=child.path,
                )
        self._emit(
            "info",
            f"Найдено {sum(1 for t in tree for _ in t.children)} подкатегорий в {len(tree)} разделах",
            categories=len(tree),
        )
        return tree

    # -- main run -------------------------------------------------------

    def run(self, request: RunRequest) -> None:
        run_id = self.state.start_run(scope=str(request.__dict__))
        try:
            with Browser(headful=self.settings.headful) as br:
                catalog = Catalog(br, request_delay=self.settings.request_delay)
                product_scraper = ProductScraper(br, request_delay=self.settings.request_delay)

                if request.single_product_url:
                    self._process_single_product(
                        product_scraper, request.single_product_url, request
                    )
                else:
                    self._process_category_selection(
                        catalog, product_scraper, request
                    )

            self.state.finish_run(run_id, "ok")
            self._emit("done", "Готово!")
            if request.open_folder_when_done and request.save_locally:
                local_root = self._resolve_local_dir(request)
                try:
                    _open_in_file_manager(local_root)
                    self._emit("info", f"Открываю {local_root}")
                except Exception as exc:
                    log.warning("open file manager failed: %s", exc)
        except Exception as exc:
            log.exception("run failed")
            self.state.finish_run(run_id, "failed", summary=str(exc))
            self._emit("error", f"Ошибка: {exc}")
            raise

    def _resolve_local_dir(self, request: RunRequest) -> Path:
        if request.local_dir:
            return Path(request.local_dir).expanduser()
        return self.settings.downloads_dir

    def _process_category_selection(
        self,
        catalog: Catalog,
        product_scraper: ProductScraper,
        request: RunRequest,
    ) -> None:
        # Re-discover the tree (cheap and ensures fresh URLs).
        self._emit("info", "Считываю меню каталога…")
        tree = catalog.discover_tree()

        selected = self._filter_tree(tree, request.selected_category_paths)
        leaves: list[CategoryNode] = []
        for top in selected:
            for child in top.children or [top]:
                if child.url:
                    leaves.append(child)

        self._emit(
            "info",
            f"Выбрано {len(leaves)} категорий для обхода",
            leaves=len(leaves),
        )

        for leaf in leaves:
            if self._cancel.is_set():
                self._emit("info", "Отменено пользователем")
                return
            assert leaf.url is not None
            self._emit("category", f"→ {leaf.path}", path=leaf.path, url=leaf.url)
            try:
                limit = request.products_per_category_limit or None
                product_urls = catalog.list_products(leaf.url, limit=limit)
            except Exception as exc:
                self._emit("error", f"Не удалось получить товары для {leaf.path}: {exc}")
                continue
            self._emit(
                "info",
                f"  найдено {len(product_urls)} товаров в «{leaf.name}»",
                count=len(product_urls),
            )
            for url in product_urls:
                if self._cancel.is_set():
                    self._emit("info", "Отменено пользователем")
                    return
                self._process_product(product_scraper, url, leaf, request)

    def _process_single_product(
        self, product_scraper: ProductScraper, url: str, request: RunRequest
    ) -> None:
        try:
            card = product_scraper.fetch(url)
        except Exception as exc:
            self._emit("error", f"Не удалось распарсить {url}: {exc}")
            return
        # Build a synthetic category from the breadcrumbs so folder structure is correct.
        crumb_path = card.breadcrumbs or ["Каталог", "Без категории"]
        category = CategoryNode(
            name=crumb_path[-1],
            url=None,
            path="/".join(safe_folder_name(p) for p in crumb_path),
            parent_path="/".join(safe_folder_name(p) for p in crumb_path[:-1]) or None,
        )
        self._handle_product(card, category, request)

    def _process_product(
        self,
        product_scraper: ProductScraper,
        url: str,
        category: CategoryNode,
        request: RunRequest,
    ) -> None:
        # Persist product so we know it exists, even if scrape fails later.
        product_id = _extract_product_id(url) or url
        self.state.upsert_product(
            url=url,
            product_id=product_id,
            category_url=category.url or "",
            category_path=category.path,
        )
        try:
            card = product_scraper.fetch(url)
        except Exception as exc:
            self.state.mark_product(url, "failed", error=str(exc))
            self._emit("error", f"  {url}: {exc}", url=url)
            return
        self._emit(
            "product",
            f"  · {card.name}",
            name=card.name,
            url=url,
            images=len(card.image_urls),
            documents=len(card.document_links),
            reviews=len(card.reviews),
        )
        try:
            self._handle_product(card, category, request)
            self.state.mark_product(url, "ok")
        except Exception as exc:
            self.state.mark_product(url, "failed", error=str(exc))
            self._emit("error", f"    upload failed: {exc}", url=url)

    # -- save / upload --------------------------------------------------

    def _handle_product(
        self, card: ProductCard, category: CategoryNode, request: RunRequest
    ) -> None:
        """Write the product as a single ``<product_name>.txt`` file.

        Output is always identical text. Up to two destinations:

        * **Local**: ``<local_dir>/<category-path>/<safe-product-name>.txt``
          when ``save_locally=True``.
        * **Drive**: same file uploaded to ``SiberianHealthParser/<category>/``
          when ``upload_to_drive=True`` and a Drive client is configured.

        No subfolders, no image/PDF downloads — image and document URLs live
        inline in the text file (see :func:`product_to_info_text`).
        """
        info_text = product_to_info_text(card)
        file_stem = safe_folder_name(card.name) or f"product-{card.product_id}"
        file_name = f"{file_stem}.txt"
        category_parts = [safe_folder_name(p) for p in category.path.split("/") if p]

        if request.save_locally or self.drive is None:
            local_root = self._resolve_local_dir(request)
            local_dir = local_root / Path(*category_parts) if category_parts else local_root
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / _safe_filename(file_name)).write_text(info_text, encoding="utf-8")
            self._emit("file", f"    txt {file_name}")

        if self.drive is not None and request.upload_to_drive:
            parent_id = self.drive.ensure_path(category_parts) if category_parts else self.drive.ensure_path([])
            self.drive.upload_text(file_name, info_text, parent_id)
            self._emit("file", f"    drive {file_name}")

        time.sleep(self.settings.request_delay)

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _filter_tree(
        tree: list[CategoryNode], selected_paths: Iterable[str]
    ) -> list[CategoryNode]:
        wanted = set(selected_paths)
        if not wanted:
            return tree
        out: list[CategoryNode] = []
        for top in tree:
            if top.path in wanted or any(p.startswith(top.path + "/") for p in wanted):
                kept_children = [
                    c
                    for c in top.children
                    if c.path in wanted or top.path in wanted
                ]
                out.append(
                    CategoryNode(
                        name=top.name,
                        url=top.url,
                        path=top.path,
                        parent_path=top.parent_path,
                        children=kept_children or top.children if top.path in wanted else kept_children,
                    )
                )
        return out


def _extract_product_id(url: str) -> str | None:
    m = re.search(r"/product/(\d+)/?", url)
    return m.group(1) if m else None


# Characters Windows / NTFS reject in filenames.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_filename(name: str) -> str:
    """Sanitize a filename for cross-platform filesystem use."""
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("_", name).strip(" .")
    return cleaned or "_"


def _open_in_file_manager(path: Path) -> None:
    """Open ``path`` in the OS file manager (Explorer / Finder / xdg-open)."""
    if not path.exists():
        return
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(
            ["xdg-open", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
