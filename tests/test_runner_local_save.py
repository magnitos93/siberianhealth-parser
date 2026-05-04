"""Tests for the local-save / dedup / file-manager helpers in ``runner``.

These tests don't touch Playwright or Google Drive — they exercise the
file-handling helpers directly with monkeypatched downloads.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sibparser.config import Settings
from sibparser.runner import (
    LOCAL_SHARED_DIR,
    Runner,
    RunRequest,
    _link_or_copy,
    _safe_filename,
)
from sibparser.site import CategoryNode, ProductCard
from sibparser.state import State


def _make_card(*, image_urls: list[str], doc_urls: list[tuple[str, str]]) -> ProductCard:
    return ProductCard(
        url="https://x/ru/shop/catalog/product/12345/",
        product_id="12345",
        name="Test Product",
        breadcrumbs=["Каталог", "Тест"],
        series=None,
        article=None,
        volume=None,
        price=None,
        description_html="",
        description_text="desc",
        composition_html="",
        composition_text="comp",
        reviews=[],
        image_urls=image_urls,
        document_links=[{"url": u, "title": t} for u, t in doc_urls],
    )


@pytest.fixture()
def runner(tmp_path: Path) -> Iterator[Runner]:
    settings = Settings(
        downloads_dir=tmp_path / "downloads",
        state_db=tmp_path / "state.db",
        headful=False,
    )
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    state = State(settings.state_db)
    yield Runner(settings=settings, state=state, drive=None, progress=lambda e: None)


def test_safe_filename_strips_forbidden_chars() -> None:
    assert _safe_filename("foo/bar.pdf") == "foo_bar.pdf"
    assert _safe_filename("foo:bar?.pdf") == "foo_bar_.pdf"
    # Path separators get collapsed and surrounding dots are stripped.
    assert _safe_filename("../etc/passwd") == "_etc_passwd"
    assert _safe_filename("...") == "_"
    assert _safe_filename("") == "_"


def test_link_or_copy_creates_link_when_possible(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    dst = tmp_path / "sub" / "dst.bin"
    src.write_bytes(b"hello")
    _link_or_copy(src, dst)
    assert dst.read_bytes() == b"hello"
    # Hardlink (or copy) should not raise on a second call — it's a no-op.
    _link_or_copy(src, dst)


def test_local_only_downloads_actual_files(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_handle_product` with save_locally=True must download bytes to disk."""
    fetched: dict[str, bytes] = {
        "https://cdn/img1.jpg": b"image-bytes-1",
        "https://cdn/cert1.pdf": b"pdf-bytes-1",
    }

    def fake_download(url: str, *, timeout: float = 60.0) -> bytes:
        return fetched[url]

    monkeypatch.setattr("sibparser.runner._download_url", fake_download)

    card = _make_card(
        image_urls=["https://cdn/img1.jpg"],
        doc_urls=[("https://cdn/cert1.pdf", "Сертификат Халяль")],
    )
    category = CategoryNode(
        name="Батончики", url=None, path="Питание/Батончики", parent_path="Питание"
    )

    req = RunRequest(save_locally=True, upload_to_drive=False)
    runner._handle_product(card, category, req)

    base = runner.settings.downloads_dir / "Питание/Батончики/Test Product [12345]"
    assert (base / "info.txt").exists()
    assert (base / "info.txt").read_text(encoding="utf-8").startswith("Название: Test Product")
    assert (base / "images" / "img1.jpg").read_bytes() == b"image-bytes-1"
    # Document is hardlinked from the shared certificates folder.
    assert (base / "documents" / "cert1.pdf").read_bytes() == b"pdf-bytes-1"
    shared = runner.settings.downloads_dir / LOCAL_SHARED_DIR / "cert1.pdf"
    assert shared.read_bytes() == b"pdf-bytes-1"


def test_certificate_dedup_only_downloads_once(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two products that reference the same certificate URL share one
    download — second call hardlinks (or copies) from the shared folder."""
    download_calls: list[str] = []

    def fake_download(url: str, *, timeout: float = 60.0) -> bytes:
        download_calls.append(url)
        return b"shared-pdf"

    monkeypatch.setattr("sibparser.runner._download_url", fake_download)

    cert_url = "https://cdn/halal.pdf"
    cat = CategoryNode(name="Y", url=None, path="X/Y", parent_path="X")

    for product_id, image_url in (("11", "https://cdn/a.jpg"), ("22", "https://cdn/b.jpg")):
        card = _make_card(
            image_urls=[image_url],
            doc_urls=[(cert_url, "Halal")],
        )
        card.product_id = product_id
        card.name = f"Prod {product_id}"
        runner._handle_product(card, cat, RunRequest(save_locally=True, upload_to_drive=False))

    cert_downloads = [u for u in download_calls if u == cert_url]
    assert len(cert_downloads) == 1, download_calls

    base = runner.settings.downloads_dir / "X/Y"
    for pid in ("11", "22"):
        assert (base / f"Prod {pid} [{pid}]" / "documents" / "halal.pdf").read_bytes() == b"shared-pdf"


def test_neither_local_nor_drive_writes_url_lists(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_locally=False and no Drive client = legacy URL-list discovery mode."""
    called = False

    def fake_download(url: str, *, timeout: float = 60.0) -> bytes:
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr("sibparser.runner._download_url", fake_download)

    card = _make_card(
        image_urls=["https://cdn/img1.jpg"],
        doc_urls=[("https://cdn/cert.pdf", "Cert")],
    )
    cat = CategoryNode(name="X", url=None, path="X", parent_path=None)
    req = RunRequest(save_locally=False, upload_to_drive=False)
    runner._handle_product(card, cat, req)

    base = runner.settings.downloads_dir / "X" / "Test Product [12345]"
    assert (base / "info.txt").exists()
    assert (base / "images" / "URLS.txt").read_text(encoding="utf-8").strip() == "https://cdn/img1.jpg"
    assert "https://cdn/cert.pdf" in (base / "documents" / "URLS.txt").read_text(encoding="utf-8")
    assert called is False, "no binaries should be downloaded in URL-list mode"


def test_local_dir_override_is_respected(
    runner: Runner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sibparser.runner._download_url", lambda url, *, timeout=60.0: b"x")
    custom = tmp_path / "elsewhere"
    card = _make_card(image_urls=["https://cdn/i.jpg"], doc_urls=[])
    cat = CategoryNode(name="C", url=None, path="C", parent_path=None)
    req = RunRequest(save_locally=True, upload_to_drive=False, local_dir=str(custom))
    runner._handle_product(card, cat, req)
    assert (custom / "C" / "Test Product [12345]" / "images" / "i.jpg").exists()
    # Default downloads_dir should NOT be used.
    assert not (runner.settings.downloads_dir / "C").exists()


def test_existing_file_is_not_redownloaded(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running over a folder that already has the file is a no-op."""
    cat = CategoryNode(name="C", url=None, path="C", parent_path=None)
    base = runner.settings.downloads_dir / "C" / "Test Product [12345]" / "images"
    base.mkdir(parents=True, exist_ok=True)
    (base / "i.jpg").write_bytes(b"already-here")

    def boom(url: str, *, timeout: float = 60.0) -> bytes:
        raise AssertionError("should not be called when file exists")

    monkeypatch.setattr("sibparser.runner._download_url", boom)
    card = _make_card(image_urls=["https://cdn/i.jpg"], doc_urls=[])
    runner._handle_product(card, cat, RunRequest(save_locally=True, upload_to_drive=False))
    assert (base / "i.jpg").read_bytes() == b"already-here"
