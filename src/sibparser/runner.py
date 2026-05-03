"""Orchestrate scraping + Drive upload with progress callbacks.

The runner is intentionally synchronous and lives on a worker thread so the
FastAPI server can keep serving HTTP/WebSocket requests. Progress updates are
sent through a ``ProgressBus`` (an asyncio queue) so connected clients see live
status.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .drive import SHARED_FILES_PATH_SUFFIX, DriveClient, UploadError
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
    # Whether to download to Drive (False = parse only and write to local disk).
    upload_to_drive: bool = True


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
                        product_scraper, request.single_product_url
                    )
                else:
                    self._process_category_selection(
                        catalog, product_scraper, request
                    )

            self.state.finish_run(run_id, "ok")
            self._emit("done", "Готово!")
        except Exception as exc:
            log.exception("run failed")
            self.state.finish_run(run_id, "failed", summary=str(exc))
            self._emit("error", f"Ошибка: {exc}")
            raise

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
                self._process_product(product_scraper, url, leaf)

    def _process_single_product(
        self, product_scraper: ProductScraper, url: str
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
        self._upload_product(card, category)

    def _process_product(
        self,
        product_scraper: ProductScraper,
        url: str,
        category: CategoryNode,
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
            self._upload_product(card, category)
            self.state.mark_product(url, "ok")
        except Exception as exc:
            self.state.mark_product(url, "failed", error=str(exc))
            self._emit("error", f"    upload failed: {exc}", url=url)

    # -- upload ---------------------------------------------------------

    def _upload_product(self, card: ProductCard, category: CategoryNode) -> None:
        info_text = product_to_info_text(card)
        product_folder_name = safe_folder_name(f"{card.name} [{card.product_id}]")

        if not self.drive:
            # Local-only mode: write to settings.downloads_dir/<category>/<product>/
            self._write_local(card, category.path, product_folder_name, info_text)
            return

        category_parts = [safe_folder_name(p) for p in category.path.split("/") if p]
        product_parent_id = self.drive.ensure_path([*category_parts, product_folder_name])
        images_id = self.drive.ensure_path([*category_parts, product_folder_name, "images"])
        documents_id = self.drive.ensure_path([*category_parts, product_folder_name, "documents"])
        shared_certs_id = self.drive.ensure_path([SHARED_FILES_PATH_SUFFIX, "certificates"])

        self.drive.upload_text("info.txt", info_text, product_parent_id)

        for img_url in card.image_urls:
            try:
                name = _filename_from_url(img_url, default=f"{card.product_id}.jpg")
                self.drive.upload_or_link(img_url, name, target_parent_id=images_id)
                self._emit("file", f"    image {name}")
            except UploadError as exc:
                self._emit("error", f"    image failed: {exc}")

        for doc in card.document_links:
            try:
                name = _filename_from_url(doc["url"], default=f"{doc['title']}")
                self.drive.upload_or_link(
                    doc["url"],
                    name,
                    target_parent_id=documents_id,
                    shared_parent_id=shared_certs_id,
                )
                self._emit("file", f"    doc {name}")
            except UploadError as exc:
                self._emit("error", f"    doc failed: {exc}")

        time.sleep(self.settings.request_delay)

    def _write_local(
        self,
        card: ProductCard,
        category_path: str,
        product_folder_name: str,
        info_text: str,
    ) -> None:
        base = self.settings.downloads_dir / Path(category_path) / product_folder_name
        (base / "images").mkdir(parents=True, exist_ok=True)
        (base / "documents").mkdir(parents=True, exist_ok=True)
        (base / "info.txt").write_text(info_text, encoding="utf-8")
        # Save URL lists so the user can manually fetch if needed.
        (base / "images" / "URLS.txt").write_text("\n".join(card.image_urls) + "\n")
        (base / "documents" / "URLS.txt").write_text(
            "\n".join(f"{d['title']}\t{d['url']}" for d in card.document_links) + "\n"
        )

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


def _filename_from_url(url: str, default: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or default
    name = re.sub(r"[\x00-\x1f]", "", name)
    if not name:
        return default
    return name
