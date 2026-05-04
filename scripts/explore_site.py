"""Probe siberianhealth.com structure: catalog tree and a sample product page.

Run: python scripts/explore_site.py
Outputs:  scripts/_explore/catalog.json, scripts/_explore/sample_product.json
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "_explore"
OUT.mkdir(parents=True, exist_ok=True)
HOME = "https://ru.siberianhealth.com/ru/"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        # Try hovering catalog menu
        try:
            page.locator("a, button", has_text="Каталог").first.hover(timeout=5000)
            page.wait_for_timeout(1000)
        except Exception as exc:
            print(f"hover catalog failed: {exc}")

        anchors = page.eval_on_selector_all(
            "a[href*='/shop/']",
            "els => els.map(e => ({href: e.href, text: (e.innerText||e.textContent||'').trim().slice(0,80)}))",
        )
        # de-dup
        seen: dict[str, dict[str, str]] = {}
        for a in anchors:
            href = a["href"].split("#")[0]
            if href not in seen:
                seen[href] = a

        catalog = sorted(seen.values(), key=lambda x: x["href"])
        (OUT / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
        print(f"got {len(catalog)} catalog/shop links")

        # Find a product URL
        product_urls = [a["href"] for a in catalog if "/shop/catalog/product/" in a["href"]]
        if not product_urls:
            # try a category page
            cat_urls = [a["href"] for a in catalog if re.search(r"/shop/catalog/[^/]+/?$", a["href"])]
            print("category candidates:", cat_urls[:10])
            if cat_urls:
                page.goto(cat_urls[0], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                more = page.eval_on_selector_all(
                    "a[href*='/shop/catalog/product/']",
                    "els => els.map(e => e.href)",
                )
                product_urls = list(dict.fromkeys(more))

        print(f"found {len(product_urls)} product urls; sample: {product_urls[:3]}")
        if product_urls:
            page.goto(product_urls[0], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            html = page.content()
            (OUT / "sample_product.html").write_text(html)
            text = page.evaluate("() => document.body.innerText")
            (OUT / "sample_product.txt").write_text(text)
            # Identify tabs / buttons
            tab_candidates = page.eval_on_selector_all(
                "button, a, [role=tab], [class*=tab i], [class*=Tab]",
                "els => els.map(e => ({tag:e.tagName, text:(e.innerText||'').trim().slice(0,60), classes:e.className||''})).filter(x => x.text)",
            )
            (OUT / "sample_product_tabs.json").write_text(json.dumps(tab_candidates, ensure_ascii=False, indent=2))
            print("saved sample product:", product_urls[0])

        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
