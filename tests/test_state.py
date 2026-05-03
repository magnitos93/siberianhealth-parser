"""Tests for the SQLite state store (file dedup + folder cache)."""
from __future__ import annotations

from pathlib import Path

from sibparser.state import State


def test_categories_and_products(tmp_path: Path) -> None:
    state = State(tmp_path / "s.db")

    state.upsert_category("https://x/cat1/", "Cat 1", None, "Каталог/Cat 1")
    state.upsert_category("https://x/cat1/", "Cat 1 renamed", None, "Каталог/Cat 1")
    cats = state.list_categories()
    assert len(cats) == 1
    assert cats[0]["name"] == "Cat 1 renamed"

    state.upsert_product(
        url="https://x/product/1/",
        product_id="1",
        category_url="https://x/cat1/",
        category_path="Каталог/Cat 1",
        name="Foo",
    )
    assert state.get_product_status("https://x/product/1/") == "pending"
    state.mark_product("https://x/product/1/", "ok", drive_folder_id="drive-1")
    products = state.list_products(status="ok")
    assert len(products) == 1
    assert products[0]["drive_folder_id"] == "drive-1"


def test_file_dedup_by_url_and_hash(tmp_path: Path) -> None:
    state = State(tmp_path / "s.db")
    assert state.lookup_file_by_url("u1") is None

    state.remember_file(
        source_url="u1",
        sha256="abc",
        drive_file_id="d1",
        drive_parent_id="parent",
        name="a.pdf",
        size_bytes=100,
    )
    assert state.lookup_file_by_url("u1")["drive_file_id"] == "d1"
    # Different URL but same content should be discoverable by hash.
    found = state.lookup_file_by_sha256("abc")
    assert found is not None and found["drive_file_id"] == "d1"


def test_folder_cache(tmp_path: Path) -> None:
    state = State(tmp_path / "s.db")
    assert state.get_folder("Каталог/Чаи") is None
    state.remember_folder("Каталог/Чаи", "drive-folder-1")
    assert state.get_folder("Каталог/Чаи") == "drive-folder-1"


def test_run_lifecycle(tmp_path: Path) -> None:
    state = State(tmp_path / "s.db")
    rid = state.start_run("scope=test")
    assert rid > 0
    state.finish_run(rid, "ok", summary="all good")
