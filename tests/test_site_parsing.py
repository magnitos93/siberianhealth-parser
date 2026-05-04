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
    assert "О ПРОДУКТЕ" in text
    assert "СОСТАВ" in text
    assert "ОТЗЫВЫ (1)" in text
    assert "Отличный продукт" in text
    # New: image links live inline in the same .txt file
    if card.image_urls:
        assert "ИЗОБРАЖЕНИЯ" in text
        assert card.image_urls[0] in text


def test_info_text_includes_new_optional_fields() -> None:
    """Build a synthetic ProductCard to verify the new fields render."""
    from sibparser.site import ProductCard
    card = ProductCard(
        url="https://x/",
        product_id="500572",
        name="3D Bone Vegan Cube",
        breadcrumbs=["Каталог"],
        series="3D Cube",
        article="500572",
        volume="30 пакетов по 5 капсул",
        price="3750 ₽",
        points="82б",
        short_description="Уникальный смарт-комплекс…",
        description_html="",
        description_text="полное описание",
        composition_html="",
        composition_text="состав",
        reviews=[],
        image_urls=["https://cdn/a.jpg"],
        document_links=[{"url": "https://cdn/c.pdf", "title": "Сертификат"}],
    )
    text = product_to_info_text(card)
    assert "Цена: 3750 ₽" in text
    assert "Баллы: 82б" in text
    assert "Количество в упаковке: 30 пакетов по 5 капсул" in text
    assert "Уникальный смарт-комплекс" in text
    assert "КРАТКОЕ ОПИСАНИЕ" in text
    assert "https://cdn/a.jpg" in text
    assert "Сертификат: https://cdn/c.pdf" in text


def test_parse_product_html_picks_up_image_rich_layout_fields() -> None:
    """On image-rich pages (e.g. 500572), price/points/short-desc/options/article
    live in the right rail, and the long description is the
    ``.im21--product-detail__tab-content`` pane (not ``__about``)."""
    html = """
    <html><body>
      <div class="im21--product-detail">
        <h1 class="im21--product-info__headline">3D Bone Vegan Cube</h1>
        <div class="im21--product-info__description">Уникальный смарт-комплекс для прочности костей.</div>
        <div class="im21--product-info__price">3750₽</div>
        <div class="im21--product-info__points">82б</div>
        <div class="im21--product-options">
          <div class="im21--product-options__option">
            <span class="im21--product-options__title">Артикул:</span>
            <span class="im21--product-options__value">#500572</span>
          </div>
          <div class="im21--product-options__option">
            <span class="im21--product-options__title">Количество в упаковке:</span>
            <span class="im21--product-options__value">30 пакетов по 5 капсул</span>
          </div>
        </div>
        <div class="im21--product-detail__tab-content">
          <p>Натуральный состав. Широкий спектр действия. Эффект синергии.</p>
        </div>
        <section class="im21--product-about__section im21--product-about__section_documents">
          <h2 class="im21--product-about__headline">Документы и материалы</h2>
          <div class="im21--product-documents">
            <a class="product-document__link" href="https://cdn/halal.pdf">Сертификат Халяль</a>
          </div>
        </section>
      </div>
      <img class="im21--product-slider__img" src="https://cdn/img1.jpg" />
    </body></html>
    """
    card = _parse_product_html(
        url="https://ru.siberianhealth.com/ru/shop/catalog/product/500572/",
        html=html,
        reviews=[],
    )
    assert card.product_id == "500572"
    assert card.article == "500572"
    assert card.volume == "30 пакетов по 5 капсул"
    assert card.price == "3750₽"
    assert card.points == "82б"
    assert "Уникальный смарт-комплекс" in card.short_description
    # The long description must come from the tab pane, not from the "Документы" section.
    assert "Натуральный состав" in card.description_text
    assert "Документы и материалы" not in card.description_text
    assert card.image_urls == ["https://cdn/img1.jpg"]
    assert card.document_links == [{"url": "https://cdn/halal.pdf", "title": "Сертификат Халяль"}]


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
