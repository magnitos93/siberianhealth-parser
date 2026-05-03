"""Smoke tests for the product page parser using the saved sample HTML.

These don't touch the network or Playwright; they exercise the
``_parse_product_html`` helper on a real page snapshot saved under
``scripts/_explore/sample_product.html``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sibparser.site import (
    _parse_catalog_menu,
    _parse_product_html,
    product_to_info_text,
    safe_folder_name,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "scripts" / "_explore"
SAMPLE_PRODUCT = SAMPLE_DIR / "sample_product.html"


@pytest.fixture
def sample_html() -> str:
    if not SAMPLE_PRODUCT.exists():
        pytest.skip("sample_product.html missing - run scripts/explore_site.py first")
    return SAMPLE_PRODUCT.read_text(encoding="utf-8")


def test_parse_product_basic_fields(sample_html: str) -> None:
    card = _parse_product_html(
        url="https://ru.siberianhealth.com/ru/shop/catalog/product/400273/",
        html=sample_html,
        reviews=[],
    )
    assert card.product_id == "400273"
    assert "ЭПАМ" in card.name
    assert "ЭПАМ" in card.description_text
    assert card.composition_text  # composition pane present
    assert card.image_urls, "expected at least one slider image"
    assert all("static.siberianhealth" in u or "siberianhealth.com" in u for u in card.image_urls)


def test_parse_product_documents(sample_html: str) -> None:
    card = _parse_product_html(
        url="https://ru.siberianhealth.com/ru/shop/catalog/product/400273/",
        html=sample_html,
        reviews=[],
    )
    assert card.document_links, "expected documents/materials section"
    titles = [d["title"] for d in card.document_links]
    # the saved snapshot contains a registration certificate + EPAM PDFs
    assert any("регистрации" in t.lower() or "сертификат" in t.lower() or "халяль" in t.lower() or "эпам" in t.lower() for t in titles)
    for d in card.document_links:
        assert d["url"].startswith("http")


def test_info_text_contains_sections(sample_html: str) -> None:
    card = _parse_product_html(
        url="https://ru.siberianhealth.com/ru/shop/catalog/product/400273/",
        html=sample_html,
        reviews=[
            {"author": "Анна", "date": "2025-01-01", "rating": "5", "text": "Отличный продукт"}
        ],
    )
    text = product_to_info_text(card)
    assert "ОПИСАНИЕ" in text
    assert "СОСТАВ" in text
    assert "ОТЗЫВЫ (1)" in text
    assert "Отличный продукт" in text


def test_safe_folder_name() -> None:
    assert safe_folder_name("a / b") == "a - b"
    assert safe_folder_name("  trim   spaces  ") == "trim spaces"
    long = "x" * 200
    out = safe_folder_name(long, max_len=80)
    assert len(out) <= 80
    assert safe_folder_name("") == "untitled"


def test_parse_catalog_menu_handles_empty() -> None:
    # An empty homepage should at least not throw and return something sensible.
    tree = _parse_catalog_menu("<html><body></body></html>")
    assert isinstance(tree, list)
