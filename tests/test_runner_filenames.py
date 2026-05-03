"""Sanity tests for runner helpers (URL → filename, product id extraction)."""
from __future__ import annotations

from sibparser.runner import _extract_product_id, _filename_from_url


def test_extract_product_id() -> None:
    assert _extract_product_id("https://x/ru/shop/catalog/product/400273/") == "400273"
    assert _extract_product_id("https://x/ru/shop/catalog/product/12345") == "12345"
    assert _extract_product_id("https://x/ru/shop/catalog/foo/") is None


def test_filename_from_url_basic() -> None:
    assert (
        _filename_from_url("https://siberianhealth.com/upload/pr_certificates/big/3689.jpg", "x.jpg")
        == "3689.jpg"
    )
    assert _filename_from_url("https://x/", "fallback.txt") == "fallback.txt"
