"""Site-specific scraping logic for ru.siberianhealth.com.

The module is split into:

* :class:`Browser`  - thin Playwright wrapper that owns a single browser context.
* :class:`Catalog`  - discovers the catalog tree (top categories -> subcategories
  -> leaf category URLs).
* :class:`Product`  - parses a single product card: description, composition,
  reviews, image URLs, document URLs.
* :class:`SiteParser` - orchestrator used by the runner / web server.

Selectors were derived from a saved sample at ``scripts/_explore/sample_product.html``.
The site uses AngularJS; tab content is rendered into the DOM up-front (just
hidden), which means we can extract content directly from the panes without
clicking — except for review pagination which we handle via "Next page".
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import (
    Browser as PWBrowser,
)
from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

HOMEPAGE = "https://ru.siberianhealth.com/ru/"
CATALOG_URL_RE = re.compile(r"^https?://[^/]+/ru/shop/catalog/[^/]+/?$")
PRODUCT_URL_RE = re.compile(r"^https?://[^/]+/ru/shop/catalog/product/(\d+)/?")

# Playwright's default navigation timeout is 30s. The Siberian Wellness CDN
# occasionally serves slow product pages, so we use 90s and retry once on
# Playwright TimeoutError to absorb transient slowness without breaking the run.
PAGE_GOTO_TIMEOUT_MS = 90_000


_WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


def _goto_with_retry(
    page: Page,
    url: str,
    *,
    timeout_ms: int = PAGE_GOTO_TIMEOUT_MS,
    retries: int = 1,
    wait_until: _WaitUntil = "domcontentloaded",
) -> None:
    """``page.goto`` wrapper that retries on Playwright TimeoutError."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except PlaywrightTimeoutError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            page.wait_for_timeout(2000)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryNode:
    """One catalog category. ``children`` are subcategories. Leaf nodes have
    products. ``path`` is the slash-separated breadcrumb used to mirror folders
    on Drive."""

    name: str
    url: str | None  # None for top-level groupings that have no own page
    path: str
    parent_path: str | None
    children: list[CategoryNode] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return not self.children


@dataclass
class ProductCard:
    """Parsed product page contents."""

    url: str
    product_id: str
    name: str
    breadcrumbs: list[str]
    series: str | None
    article: str | None
    volume: str | None  # «Количество в упаковке», e.g. "30 пакетов по 5 капсул"
    price: str | None
    points: str | None  # Bonus points, e.g. "82б"
    short_description: str  # The blurb directly under the H1
    description_html: str
    description_text: str  # Full "О продукте" tab content (works on image-rich pages)
    composition_html: str
    composition_text: str
    reviews: list[dict[str, str]]
    image_urls: list[str]
    document_links: list[dict[str, str]]  # {url, title}


# ---------------------------------------------------------------------------
# Browser wrapper
# ---------------------------------------------------------------------------


class Browser:
    """Owns a single Playwright Chromium context with sane defaults."""

    def __init__(self, headful: bool = True, slow_mo_ms: int = 0) -> None:
        self._headful = headful
        self._slow_mo = slow_mo_ms
        self._pw: Playwright | None = None
        self._browser: PWBrowser | None = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> Browser:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self._headful, slow_mo=self._slow_mo)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1400, "height": 900},
        )
        # Block heavy ad/analytics that we don't need.
        self._context.route(
            re.compile(r"(google-analytics|googletagmanager|doubleclick|yandex\.ru/metrika)"),
            lambda route: route.abort(),
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    @contextmanager
    def page(self) -> Iterator[Page]:
        assert self._context is not None
        page: Page = self._context.new_page()
        try:
            yield page
        finally:
            page.close()


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------


class Catalog:
    """Discover the catalog category tree by visiting the homepage and reading
    the mega-menu, then visiting each category page to find pagination + product
    links."""

    def __init__(self, browser: Browser, request_delay: float = 0.5) -> None:
        self.browser = browser
        self.request_delay = request_delay

    def discover_tree(self) -> list[CategoryNode]:
        """Return top-level categories with their children populated."""
        page: Page
        with self.browser.page() as page:
            _goto_with_retry(page, HOMEPAGE)
            page.wait_for_timeout(2000)
            self._dismiss_overlays(page)
            try:
                page.locator(".navbar-main__btn-catalog").first.click(timeout=5000)
                page.wait_for_timeout(800)
            except Exception:
                pass
            html = page.content()
        return _parse_catalog_menu(html)

    def list_products(self, category_url: str, limit: int | None = None) -> list[str]:
        """Return product URLs found on a category page (handles "show more"
        / pagination by scrolling and clicking)."""
        urls: list[str] = []
        seen: set[str] = set()
        page: Page
        with self.browser.page() as page:
            _goto_with_retry(page, category_url)
            page.wait_for_timeout(2000)
            self._dismiss_overlays(page)

            # Try "show more" pagination loop
            for _ in range(40):  # cap iterations
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(400)
                try:
                    btn = page.locator("button:has-text('Показать ещё'), button:has-text('Показать еще')").first
                    if btn.is_visible(timeout=300):
                        btn.click(timeout=2000)
                        page.wait_for_timeout(800)
                        continue
                except Exception:
                    pass
                break

            anchors = page.eval_on_selector_all(
                "a[href*='/shop/catalog/product/']", "els => els.map(e => e.href)"
            )
            for href in anchors:
                clean = href.split("?")[0].split("#")[0]
                m = PRODUCT_URL_RE.match(clean)
                if m and clean not in seen:
                    seen.add(clean)
                    urls.append(clean)
                    if limit and len(urls) >= limit:
                        break
            time.sleep(self.request_delay)
        return urls

    @staticmethod
    def _dismiss_overlays(page: Page) -> None:
        for selector in [
            ".os-cookie-use__accept",
            "button:has-text('OK')",
            "button:has-text('Да, все верно')",
            ".im21--popup__close",
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=200):
                    el.click(timeout=1000)
                    page.wait_for_timeout(150)
            except Exception:
                continue


def _is_category_url(href: str) -> bool:
    """True for URLs we treat as category landing pages on the shop."""
    if "/product/" in href:
        return False
    return any(p in href for p in ("/shop/catalog/", "/shop/l/", "/shop/s/"))


def _img_alt(a: Tag) -> str:
    img = a.find("img")
    if isinstance(img, Tag):
        alt = img.get("alt")
        if isinstance(alt, str):
            return alt.strip()
    return ""


def _parse_catalog_menu(html: str) -> list[CategoryNode]:
    """Build a tree from the homepage mega-menu HTML.

    The mega-menu is an Angular widget. Each top-level entry is a
    ``.im21--dropdown-navbar__link_tab`` with a ``data-id`` attribute pointing
    to a matching ``.im21--dropdown-navbar__content_tab-content`` pane that
    contains the subcategory links. Top-level entries also have their own
    ``href`` (the parent category page) which we keep on the parent node so
    the user can pick "all of Витамины и добавки" if they wish.
    """
    soup = BeautifulSoup(html, "lxml")

    tab_links: list[Tag] = list(soup.select(".im21--dropdown-navbar__link_tab"))
    pane_by_id: dict[str, Tag] = {}
    for pane in soup.select(".im21--dropdown-navbar__content_tab-content"):
        data_id = pane.get("data-id")
        if isinstance(data_id, str):
            pane_by_id[data_id] = pane

    tree: list[CategoryNode] = []
    seen_top: set[str] = set()

    for tab in tab_links:
        name = tab.get_text(strip=True)
        if not name or name in seen_top:
            continue
        seen_top.add(name)
        data_id_raw = tab.get("data-id")
        data_id = data_id_raw if isinstance(data_id_raw, str) else ""
        href_raw = tab.get("href")
        top_url = urljoin(HOMEPAGE, str(href_raw)) if isinstance(href_raw, str) else None
        # bottom-section-* / product-series tabs have no top-level URL.
        if top_url and not _is_category_url(top_url):
            top_url = None

        children: list[CategoryNode] = []
        seen_child: set[str] = set()
        target_pane = pane_by_id.get(data_id)
        if target_pane is not None:
            for a in target_pane.find_all("a", href=True):
                assert isinstance(a, Tag)
                href = urljoin(HOMEPAGE, str(a["href"]))
                text = a.get_text(strip=True) or _img_alt(a)
                if not text or not _is_category_url(href):
                    continue
                if href in seen_child:
                    continue
                seen_child.add(href)
                children.append(
                    CategoryNode(
                        name=text,
                        url=href,
                        path=f"{name}/{text}",
                        parent_path=name,
                    )
                )

        tree.append(
            CategoryNode(
                name=name,
                url=top_url,
                path=name,
                parent_path=None,
                children=children,
            )
        )

    # Fallback: if for some reason we found nothing, gather any catalog anchors.
    if not tree:
        misc: list[CategoryNode] = []
        seen_misc: set[str] = set()
        for a in soup.find_all("a", href=True):
            assert isinstance(a, Tag)
            href = urljoin(HOMEPAGE, str(a["href"]))
            if not _is_category_url(href) or href in seen_misc:
                continue
            seen_misc.add(href)
            txt = a.get_text(strip=True) or href
            misc.append(
                CategoryNode(name=txt, url=href, path=f"Каталог/{txt}", parent_path="Каталог")
            )
        tree.append(
            CategoryNode(
                name="Каталог",
                url=None,
                path="Каталог",
                parent_path=None,
                children=misc,
            )
        )

    for node in tree:
        node.children.sort(key=lambda n: n.name)
    tree.sort(key=lambda n: n.name)
    return tree


# ---------------------------------------------------------------------------
# Product parsing
# ---------------------------------------------------------------------------


class ProductScraper:
    def __init__(self, browser: Browser, request_delay: float = 0.5) -> None:
        self.browser = browser
        self.request_delay = request_delay

    def fetch(self, url: str, max_review_pages: int = 50) -> ProductCard:
        page: Page
        with self.browser.page() as page:
            _goto_with_retry(page, url)
            page.wait_for_timeout(2000)
            Catalog._dismiss_overlays(page)

            # Click "Читать полное описание" so the full description is in DOM
            try:
                btn = page.locator("button:has-text('Читать полное описание'), a:has-text('Читать полное описание')").first
                if btn.is_visible(timeout=500):
                    btn.click(timeout=2000)
                    page.wait_for_timeout(300)
            except Exception:
                pass

            # Click each tab in turn so review content is hydrated
            for label in ["О ПРОДУКТЕ", "СОСТАВ", "ОТЗЫВЫ"]:
                try:
                    tab = page.locator(f".im21--tabs-nav button:has-text('{label}'), .im21--tabs-nav a:has-text('{label}')").first
                    if tab.count():
                        tab.click(timeout=1500)
                        page.wait_for_timeout(250)
                except Exception:
                    continue

            reviews: list[dict[str, str]] = []
            seen_review_keys: set[str] = set()
            # Collect reviews across pagination
            for _ in range(max_review_pages):
                page.wait_for_timeout(250)
                page_html = page.content()
                page_reviews = _parse_reviews(page_html)
                added = 0
                for r in page_reviews:
                    key = (r.get("author", "") + "|" + r.get("date", "") + "|" + r.get("text", "")[:80])
                    if key in seen_review_keys:
                        continue
                    seen_review_keys.add(key)
                    reviews.append(r)
                    added += 1
                if added == 0:
                    break
                # Try go to next review page
                try:
                    next_btn = page.locator(".im21--product-pagination__btn_next").first
                    if next_btn.count() and next_btn.is_visible(timeout=200) and not next_btn.is_disabled():
                        next_btn.click(timeout=1500)
                        page.wait_for_timeout(400)
                        continue
                except Exception:
                    pass
                break

            html = page.content()
        time.sleep(self.request_delay)
        return _parse_product_html(url, html, reviews=reviews)


def _parse_reviews(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict[str, str]] = []
    for card in soup.select(".im21--product-review, .os-product-review, .product-review"):
        author = _text_of(card.select_one(".im21--product-review__author, .os-product-review__author"))
        date = _text_of(card.select_one(".im21--product-review__date, .os-product-review__date"))
        rating = _text_of(card.select_one(".im21--product-review__rating, .os-product-review__rating"))
        text = _text_of(card.select_one(".im21--product-review__text, .os-product-review__text, .im21--product-review__content"))
        if not text:
            text = card.get_text(" ", strip=True)
        out.append({"author": author, "date": date, "rating": rating, "text": text})
    return out


def _text_of(el: Tag | None) -> str:
    if el is None:
        return ""
    return el.get_text(" ", strip=True)


def _parse_product_html(url: str, html: str, reviews: list[dict[str, str]]) -> ProductCard:
    soup = BeautifulSoup(html, "lxml")

    m = PRODUCT_URL_RE.match(url)
    product_id = m.group(1) if m else url.rstrip("/").split("/")[-1]

    name = _text_of(soup.select_one(".im21--product-main__name, .im21--product__name, h1"))
    if not name:
        name = f"product-{product_id}"

    breadcrumbs = [
        _text_of(li) for li in soup.select(".im21--breadcrumbs__link, .breadcrumbs__link")
    ]
    breadcrumbs = [b for b in breadcrumbs if b and b.lower() != "главная"]

    series_el = soup.find(string=re.compile(r"^Серия:"))
    series = None
    if series_el:
        parent = series_el.parent
        if parent is not None:
            series = parent.get_text(" ", strip=True).removeprefix("Серия:").strip()

    # Article + «Количество в упаковке» live in identical
    # ``.im21--product-options__option`` blocks, distinguished by the
    # ``__title`` text node. Parse them in one pass.
    article: str | None = None
    volume: str | None = None
    for opt in soup.select(".im21--product-options__option"):
        title_el = opt.select_one(".im21--product-options__title")
        value_el = opt.select_one(".im21--product-options__value")
        if title_el is None or value_el is None:
            continue
        title = title_el.get_text(" ", strip=True).rstrip(":").lower()
        value = value_el.get_text(" ", strip=True)
        if not value:
            continue
        if "артикул" in title:
            m2 = re.search(r"\d{4,}", value)
            article = m2.group(0) if m2 else value
        elif "количество" in title or "объ" in title:
            volume = value
    if not article:
        article = product_id

    price = _text_of(
        soup.select_one(".im21--product-info__price, .im21--product-main__price, .im21--product-price__current")
    )
    points = _text_of(soup.select_one(".im21--product-info__points")) or None
    short_description = _text_of(soup.select_one(".im21--product-info__description"))

    # The ``О продукте`` tab pane is the reliable source for the long
    # description — it works for both plain-text pages and the newer
    # image-heavy layouts (where ``__about`` only contains "Документы").
    about_pane = soup.select_one(
        ".im21--product-detail__tab-content, .im21--product-detail__about, .im21--product-about"
    )
    description_html = str(about_pane) if about_pane else ""
    description_text = _text_of(about_pane)

    comp_pane = soup.select_one(".im21--product-detail__composition, .im21--product-composition")
    composition_html = str(comp_pane) if comp_pane else ""
    composition_text = _text_of(comp_pane)
    # The composition pane includes a leading "Состав" heading — strip it so
    # the info file isn't redundant under its own ``СОСТАВ`` section.
    composition_text = re.sub(r"^\s*Состав\s*", "", composition_text, count=1).strip()

    image_urls: list[str] = []
    for img in soup.select(".im21--product-slider__img"):
        src = img.get("src") or img.get("data-src")
        if isinstance(src, str) and "_resize/" not in src:
            image_urls.append(src)
    # Fallback to gallery thumbs (full size by stripping _resize segment)
    if not image_urls:
        for img in soup.select(".im21--product-gallery__img"):
            src = img.get("src") or img.get("data-src")
            if not isinstance(src, str):
                continue
            full = re.sub(r"/_resize/", "/", src)
            full = re.sub(r"_fit_\d+_\d+", "", full)
            image_urls.append(full)
    image_urls = list(dict.fromkeys(image_urls))

    documents: list[dict[str, str]] = []
    for a in soup.select(".product-document a, .product-document__link, a.product-document__link"):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        title = a.get_text(" ", strip=True) or Path(urlparse(href).path).name
        documents.append({"url": urljoin(url, href), "title": title})

    return ProductCard(
        url=url,
        product_id=product_id,
        name=name,
        breadcrumbs=breadcrumbs,
        series=series,
        article=article,
        volume=volume,
        price=price,
        points=points,
        short_description=short_description,
        description_html=description_html,
        description_text=description_text,
        composition_html=composition_html,
        composition_text=composition_text,
        reviews=reviews,
        image_urls=image_urls,
        document_links=documents,
    )


# ---------------------------------------------------------------------------
# Public orchestration helper
# ---------------------------------------------------------------------------


@dataclass
class SiteParser:
    headful: bool = True
    request_delay: float = 0.5

    def discover_tree(self) -> list[CategoryNode]:
        with Browser(headful=self.headful) as br:
            return Catalog(br, request_delay=self.request_delay).discover_tree()

    def list_products(self, category_url: str, limit: int | None = None) -> list[str]:
        with Browser(headful=self.headful) as br:
            return Catalog(br, request_delay=self.request_delay).list_products(category_url, limit=limit)

    def fetch_product(self, url: str) -> ProductCard:
        with Browser(headful=self.headful) as br:
            return ProductScraper(br, request_delay=self.request_delay).fetch(url)


def category_node_to_dict(node: CategoryNode) -> dict[str, Any]:
    """Serialize a tree to plain dicts for JSON / web UI."""
    return {
        "name": node.name,
        "url": node.url,
        "path": node.path,
        "parent_path": node.parent_path,
        "children": [category_node_to_dict(c) for c in node.children],
    }


def product_to_info_text(card: ProductCard) -> str:
    """Render a product into the single-file plain-text body.

    Output convention is one ``<product_name>.txt`` per product, containing
    every field, every review, and **URL lists** of images and documents
    (no actual binaries). The format is optimized for nutrionist consultants
    reading the file as a database — search is easy, links are clickable.
    """
    lines: list[str] = []
    lines.append(f"Название: {card.name}")
    lines.append(f"URL: {card.url}")
    lines.append(f"ID товара: {card.product_id}")
    if card.article:
        lines.append(f"Артикул: {card.article}")
    if card.series:
        lines.append(f"Серия: {card.series}")
    if card.volume:
        lines.append(f"Количество в упаковке: {card.volume}")
    if card.price:
        lines.append(f"Цена: {card.price}")
    if card.points:
        lines.append(f"Баллы: {card.points}")
    if card.breadcrumbs:
        lines.append(f"Категория: {' > '.join(card.breadcrumbs)}")

    if card.short_description:
        lines.append("")
        lines.append("=" * 60)
        lines.append("КРАТКОЕ ОПИСАНИЕ")
        lines.append("=" * 60)
        lines.append(card.short_description)

    lines.append("")
    lines.append("=" * 60)
    lines.append("О ПРОДУКТЕ")
    lines.append("=" * 60)
    lines.append(card.description_text or "—")

    lines.append("")
    lines.append("=" * 60)
    lines.append("СОСТАВ")
    lines.append("=" * 60)
    lines.append(card.composition_text or "—")

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"ОТЗЫВЫ ({len(card.reviews)})")
    lines.append("=" * 60)
    for i, r in enumerate(card.reviews, 1):
        header_parts = [p for p in (r.get("author"), r.get("date"), r.get("rating")) if p]
        lines.append(f"--- Отзыв #{i} {' | '.join(header_parts)} ---")
        lines.append(r.get("text", ""))
        lines.append("")

    if card.image_urls:
        lines.append("=" * 60)
        lines.append(f"ИЗОБРАЖЕНИЯ ({len(card.image_urls)})")
        lines.append("=" * 60)
        for u in card.image_urls:
            lines.append(f"- {u}")
        lines.append("")

    if card.document_links:
        lines.append("=" * 60)
        lines.append("ДОКУМЕНТЫ И МАТЕРИАЛЫ")
        lines.append("=" * 60)
        for d in card.document_links:
            lines.append(f"- {d['title']}: {d['url']}")

    return "\n".join(lines).strip() + "\n"


def safe_folder_name(s: str, max_len: int = 80) -> str:
    """Drive-friendly folder name. Trims slashes, control chars, and length."""
    s = s.strip().replace("/", "-").replace("\\", "-")
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s or "untitled"



