"""Sanity tests for runner helpers (product id extraction, filename safety)."""
from __future__ import annotations

from sibparser.runner import _extract_product_id, _safe_filename


def test_extract_product_id() -> None:
    assert _extract_product_id("https://x/ru/shop/catalog/product/400273/") == "400273"
    assert _extract_product_id("https://x/ru/shop/catalog/product/12345") == "12345"
    assert _extract_product_id("https://x/ru/shop/catalog/foo/") is None


def test_safe_filename_strips_forbidden_chars() -> None:
    assert _safe_filename("3D Bone Vegan Cube.txt") == "3D Bone Vegan Cube.txt"
    # Drive-friendly: collapse forbidden characters to underscores.
    assert _safe_filename("foo/bar.txt") == "foo_bar.txt"
    assert _safe_filename("a:b?c|d.txt") == "a_b_c_d.txt"
    # Empty / dots-only fall back to a placeholder.
    assert _safe_filename("...") == "_"
    assert _safe_filename("") == "_"
