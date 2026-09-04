"""Small, queue-ready Nykaa product scraper prototype.

The parser is independent from the fetcher so HTTP and browser workers can
share the same extraction and validation path.
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup


@dataclass
class ProductRecord:
    product_url: str
    product_name: str | None = None
    mrp: float | None = None
    selling_price: float | None = None
    size: str | None = None
    rating: float | None = None
    review_count: int | None = None
    scraped_at: str = ""
    error: str | None = None


class NykaaScraper:
    def __init__(self, timeout: int = 20, retries: int = 3, delay: float = 1.0):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-IN,en;q=0.9",
        })

    def fetch_page(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.text
            except requests.RequestException as error:
                last_error = error
                if attempt < self.retries - 1:
                    time.sleep(self.delay * (2 ** attempt))
        return self.fetch_with_browser(url, last_error)

    def fetch_with_browser(self, url: str, http_error: Exception | None) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "HTTP fetch failed. Install playwright and run "
                "'playwright install chromium' for browser fallback."
            ) from error

        parts = urlsplit(url)
        canonical_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(locale="en-IN", ignore_https_errors=True)
                for candidate in dict.fromkeys((url, canonical_url)):
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    except Exception:
                        pass
                    if page.locator("h1").count():
                        break
                page.locator("h1").first.wait_for(timeout=10000)
                html = page.content()
                browser.close()
                return html
        except Exception as error:
            raise RuntimeError(f"Browser fetch failed: {error}; HTTP error: {http_error}") from error

    @staticmethod
    def clean_price(value: Any) -> float | None:
        if value is None:
            return None
        match = re.search(r"\d[\d,.]*", str(value))
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def to_int(value: Any) -> int | None:
        try:
            return int(str(value).replace(",", "")) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def extract_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and "Product" in str(item.get("@type")):
                    return item
        return {}

    def parse_product(self, url: str, html: str) -> ProductRecord:
        soup = BeautifulSoup(html, "html.parser")
        product = self.extract_json_ld(soup)
        aggregate = product.get("aggregateRating") or {}
        offers = product.get("offers") or {}
        page_text = soup.get_text(" ", strip=True)
        mrp_match = re.search(r"MRP\s*[:₹]?\s*₹?\s*([\d,]+)", page_text, re.I)
        size_match = re.search(r"(\d+(?:\.\d+)?\s*(?:ml|g|kg|l|oz))", page_text, re.I)
        title = soup.find("title")
        return ProductRecord(
            product_url=url,
            product_name=product.get("name") or (title.get_text(strip=True) if title else None),
            mrp=self.clean_price(mrp_match.group(1)) if mrp_match else None,
            selling_price=self.clean_price(offers.get("price") if isinstance(offers, dict) else None),
            size=size_match.group(1) if size_match else None,
            rating=self.clean_price(aggregate.get("ratingValue")),
            review_count=self.to_int(aggregate.get("reviewCount")),
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    def scrape_product(self, url: str) -> ProductRecord:
        try:
            return self.parse_product(url, self.fetch_page(url))
        except (RuntimeError, ValueError) as error:
            return ProductRecord(
                product_url=url,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                error=str(error),
            )

    def scrape_many(self, urls: list[str], workers: int = 4) -> list[ProductRecord]:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self.scrape_product, url) for url in urls]
            return [future.result() for future in as_completed(futures)]


def load_urls(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract product fields from Nykaa pages.")
    parser.add_argument("--url", action="append", help="Product URL; repeat for multiple products")
    parser.add_argument("--input", type=Path, help="Text file with one product URL per line")
    parser.add_argument("--output", type=Path, default=Path("nykaa_products.csv"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    urls = list(args.url or [])
    if args.input:
        urls.extend(load_urls(args.input))
    if not urls:
        parser.error("provide --url or --input")
    records = NykaaScraper().scrape_many(list(dict.fromkeys(urls)), args.workers)
    dataframe = pd.DataFrame(asdict(record) for record in records)
    dataframe.to_csv(args.output, index=False)
    print(dataframe.to_string(index=False))
    print(f"\nSaved {len(dataframe)} record(s) to {args.output}")


if __name__ == "__main__":
    main()
