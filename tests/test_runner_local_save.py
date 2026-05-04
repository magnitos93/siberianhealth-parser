"""Tests for the simplified single-file output of ``Runner._handle_product``.

Each product becomes one ``<product_name>.txt`` file in its category folder
(no nested images/ or documents/ subfolders, no binary downloads).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sibparser.config import Settings
from sibparser.runner import Runner, RunRequest
from sibparser.site import CategoryNode, ProductCard
from sibparser.state import State


def _make_card(*, name: str = "Test Product", product_id: str = "12345",
                image_urls: list[str] | None = None,
                doc_urls: list[tuple[str, str]] | None = None) -> ProductCard:
    return ProductCard(
        url=f"https://x/ru/shop/catalog/product/{product_id}/",
        product_id=product_id,
        name=name,
        breadcrumbs=["Каталог", "Тест"],
        series=None,
        article=None,
        volume=None,
        price=None,
        points=None,
        short_description="",
        description_html="",
        description_text="full О ПРОДУКТЕ description",
        composition_html="",
        composition_text="composition",
        reviews=[],
        image_urls=image_urls or [],
        document_links=[{"url": u, "title": t} for u, t in (doc_urls or [])],
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


def test_local_save_writes_single_text_file(runner: Runner) -> None:
    card = _make_card(
        image_urls=["https://cdn/img1.jpg", "https://cdn/img2.png"],
        doc_urls=[("https://cdn/cert.pdf", "Сертификат Халяль")],
    )
    cat = CategoryNode(name="Батончики", url=None, path="Питание/Батончики", parent_path="Питание")
    runner._handle_product(card, cat, RunRequest(save_locally=True, upload_to_drive=False))

    base = runner.settings.downloads_dir / "Питание" / "Батончики"
    expected = base / "Test Product.txt"
    assert expected.exists(), list(base.iterdir())
    text = expected.read_text(encoding="utf-8")

    # No subfolders should be created.
    assert not (base / "Test Product").exists()
    assert not (base / "images").exists()
    assert not (base / "documents").exists()

    # All key sections present.
    assert "Название: Test Product" in text
    assert "О ПРОДУКТЕ" in text
    assert "full О ПРОДУКТЕ description" in text
    assert "ИЗОБРАЖЕНИЯ (2)" in text
    assert "https://cdn/img1.jpg" in text
    assert "https://cdn/img2.png" in text
    assert "ДОКУМЕНТЫ И МАТЕРИАЛЫ" in text
    assert "Сертификат Халяль: https://cdn/cert.pdf" in text


def test_local_dir_override_is_respected(runner: Runner, tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    card = _make_card()
    cat = CategoryNode(name="C", url=None, path="C", parent_path=None)
    runner._handle_product(
        card, cat,
        RunRequest(save_locally=True, upload_to_drive=False, local_dir=str(custom)),
    )
    assert (custom / "C" / "Test Product.txt").exists()
    # Default downloads_dir should NOT have been used.
    assert not (runner.settings.downloads_dir / "C").exists()


def test_filenames_with_forbidden_chars_are_sanitized(runner: Runner) -> None:
    card = _make_card(name="3D Cube / Vegan: Bone? Pro|")
    cat = CategoryNode(name="X", url=None, path="X", parent_path=None)
    runner._handle_product(card, cat, RunRequest(save_locally=True, upload_to_drive=False))

    base = runner.settings.downloads_dir / "X"
    files = list(base.iterdir())
    assert len(files) == 1, files
    name = files[0].name
    # No forbidden characters survive.
    for ch in "<>:\"/\\|?*":
        assert ch not in name, name


def test_handle_product_does_not_download_any_binaries(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner used to fetch image/PDF bytes via httpx — that's gone now."""
    import sibparser.runner as runner_mod
    # If httpx ever sneaks back into the runner the test reminds us.
    assert not hasattr(runner_mod, "_download_url"), \
        "runner._download_url was removed; the runner should not download binaries"
    assert not hasattr(runner_mod, "httpx"), \
        "httpx import was removed; the runner should not pull bytes itself"


def test_writing_to_drive_only_does_not_create_local_file(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When upload_to_drive=True, save_locally=False, and a Drive client is set,
    no local file is written."""
    captured: dict[str, object] = {}

    class FakeDrive:
        def ensure_path(self, parts: list[str]) -> str:
            captured["parts"] = list(parts)
            return "fake-folder-id"

        def upload_text(self, name: str, text: str, parent_id: str) -> str:
            captured["uploaded_name"] = name
            captured["uploaded_text"] = text
            captured["uploaded_parent"] = parent_id
            return "fake-file-id"

    runner.drive = FakeDrive()  # type: ignore[assignment]
    card = _make_card()
    cat = CategoryNode(name="Категория", url=None, path="Категория", parent_path=None)
    runner._handle_product(
        card, cat, RunRequest(save_locally=False, upload_to_drive=True),
    )

    assert captured["uploaded_name"] == "Test Product.txt"
    assert "Название: Test Product" in str(captured["uploaded_text"])
    assert captured["parts"] == ["Категория"]
    # Local downloads dir should be untouched.
    assert not list(runner.settings.downloads_dir.iterdir())
