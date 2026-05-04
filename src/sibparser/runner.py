"""Orchestrate scraping + Drive upload with progress callbacks.

The runner is intentionally synchronous and lives on a worker thread so the
FastAPI server can keep serving HTTP/WebSocket requests. Progress updates are
sent through a ``ProgressBus`` (an asyncio queue) so connected clients see live
status.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

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

LOCAL_SHARED_DIR = Path(SHARED_FILES_PATH_SUFFIX) / "certificates"


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
    # Whether to upload to Drive (independent of save_locally).
    upload_to_drive: bool = True
    # Whether to actually download images/PDFs to disk (independent of Drive).
    # When False with upload_to_drive=False, only URL lists are written.
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
        """Save info.txt + images + documents to local disk and/or Google Drive.

        Four supported modes (combinations of ``request.save_locally`` and
        ``self.drive`` being non-None):

        * **Drive only**: upload everything to Drive (legacy behaviour).
        * **Local only**: download images/PDFs to disk; certificates are
          deduped via ``<local_dir>/_shared/certificates/`` + hardlink (with
          copy fallback) so a single physical copy serves all products.
        * **Both**: download bytes once, save locally, then reuse the bytes
          for the Drive upload (no duplicate network round-trip).
        * **Neither**: write ``info.txt`` plus URL lists (``URLS.txt``) so
          discovery is still useful without fetching binaries.
        """
        info_text = product_to_info_text(card)
        product_folder_name = safe_folder_name(f"{card.name} [{card.product_id}]")
        category_parts = [safe_folder_name(p) for p in category.path.split("/") if p]

        # ------------------------- LOCAL paths -----------------------------
        local_root: Path | None = None
        local_product_dir: Path | None = None
        local_images_dir: Path | None = None
        local_documents_dir: Path | None = None
        local_shared_certs: Path | None = None
        # Always write info.txt locally if we save_locally OR there's no Drive
        # — otherwise the user has nothing to look at.
        write_local = request.save_locally or self.drive is None
        if write_local:
            local_root = self._resolve_local_dir(request)
            local_product_dir = local_root / Path(category.path) / product_folder_name
            local_images_dir = local_product_dir / "images"
            local_documents_dir = local_product_dir / "documents"
            local_shared_certs = local_root / LOCAL_SHARED_DIR
            local_images_dir.mkdir(parents=True, exist_ok=True)
            local_documents_dir.mkdir(parents=True, exist_ok=True)
            (local_product_dir / "info.txt").write_text(info_text, encoding="utf-8")

        # ------------------------- DRIVE paths -----------------------------
        drive_images_id: str | None = None
        drive_documents_id: str | None = None
        drive_shared_id: str | None = None
        if self.drive is not None:
            product_parent_id = self.drive.ensure_path([*category_parts, product_folder_name])
            drive_images_id = self.drive.ensure_path(
                [*category_parts, product_folder_name, "images"]
            )
            drive_documents_id = self.drive.ensure_path(
                [*category_parts, product_folder_name, "documents"]
            )
            drive_shared_id = self.drive.ensure_path(
                [SHARED_FILES_PATH_SUFFIX, "certificates"]
            )
            self.drive.upload_text("info.txt", info_text, product_parent_id)

        # ------------------------ no-binary mode ---------------------------
        if not request.save_locally and self.drive is None:
            # Discovery-only: write URL lists so the user can review what was
            # found without paying for downloads.
            assert local_images_dir is not None and local_documents_dir is not None
            (local_images_dir / "URLS.txt").write_text(
                "\n".join(card.image_urls) + "\n", encoding="utf-8"
            )
            (local_documents_dir / "URLS.txt").write_text(
                "\n".join(f"{d['title']}\t{d['url']}" for d in card.document_links) + "\n",
                encoding="utf-8",
            )
            time.sleep(self.settings.request_delay)
            return

        # ------------------------- IMAGES (no dedup) -----------------------
        for img_url in card.image_urls:
            name = _filename_from_url(img_url, default=f"{card.product_id}.jpg")
            try:
                self._save_file(
                    source_url=img_url,
                    target_name=name,
                    local_target_dir=local_images_dir if request.save_locally else None,
                    local_shared_dir=None,
                    drive_parent_id=drive_images_id,
                    drive_shared_id=None,
                )
                self._emit("file", f"    image {name}")
            except _FileError as exc:
                self._emit("error", f"    image failed: {exc}")

        # ------------------------ DOCUMENTS (dedup) ------------------------
        for doc in card.document_links:
            name = _filename_from_url(
                doc["url"],
                default=safe_folder_name(doc.get("title") or "document"),
            )
            try:
                self._save_file(
                    source_url=doc["url"],
                    target_name=name,
                    local_target_dir=local_documents_dir if request.save_locally else None,
                    local_shared_dir=local_shared_certs if request.save_locally else None,
                    drive_parent_id=drive_documents_id,
                    drive_shared_id=drive_shared_id,
                )
                self._emit("file", f"    doc {name}")
            except _FileError as exc:
                self._emit("error", f"    doc failed: {exc}")

        time.sleep(self.settings.request_delay)

    def _save_file(
        self,
        *,
        source_url: str,
        target_name: str,
        local_target_dir: Path | None,
        local_shared_dir: Path | None,
        drive_parent_id: str | None,
        drive_shared_id: str | None,
    ) -> None:
        """Persist ``source_url`` to local disk and/or Google Drive.

        At most one network download per call: when both targets are
        requested, the downloaded bytes are reused for the Drive upload via
        the ``content=`` parameter on :meth:`DriveClient.upload_or_link`.
        """
        content_cache: bytes | None = None

        if local_target_dir is not None:
            local_target_dir.mkdir(parents=True, exist_ok=True)
            local_target = local_target_dir / _safe_filename(target_name)

            if local_target.exists() and local_target.stat().st_size > 0:
                pass  # already saved on a previous run
            elif local_shared_dir is not None:
                # Local dedup: keep one copy in _shared/certificates and
                # hardlink (or copy) it into each product's folder.
                local_shared_dir.mkdir(parents=True, exist_ok=True)
                shared_target = local_shared_dir / _safe_filename(target_name)
                if not shared_target.exists() or shared_target.stat().st_size == 0:
                    content_cache = _download_url(source_url)
                    shared_target.write_bytes(content_cache)
                _link_or_copy(shared_target, local_target)
            else:
                content_cache = _download_url(source_url)
                local_target.write_bytes(content_cache)

        if drive_parent_id is not None:
            assert self.drive is not None
            try:
                self.drive.upload_or_link(
                    source_url=source_url,
                    target_name=target_name,
                    target_parent_id=drive_parent_id,
                    shared_parent_id=drive_shared_id,
                    content=content_cache,
                )
            except UploadError as exc:
                raise _FileError(str(exc)) from exc

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


# ---------------------------------------------------------------------------
# Local file helpers
# ---------------------------------------------------------------------------


# Characters Windows / NTFS reject in filenames.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_filename(name: str) -> str:
    """Sanitize a filename for cross-platform filesystem use."""
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("_", name).strip(" .")
    return cleaned or "_"


class _FileError(RuntimeError):
    """Raised when a per-file operation (download / save) fails."""


def _download_url(url: str, *, timeout: float = 60.0) -> bytes:
    """Fetch the bytes of ``url`` via httpx, raising :class:`_FileError`."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "sibparser/0.1"})
            r.raise_for_status()
            return r.content
    except Exception as exc:
        raise _FileError(f"download {url}: {exc}") from exc


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst`` or fall back to copy on different volumes.

    Hardlinks are O(1), use no extra disk space, and look like normal files
    in Explorer / Finder. They only work if both paths live on the same
    filesystem; we fall back to a regular copy when the OS refuses (Windows
    cross-drive scenario, FAT32 USB sticks, etc.).
    """
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copy2(str(src), str(dst))


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
