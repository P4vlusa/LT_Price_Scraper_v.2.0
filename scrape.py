from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests
from bs4 import BeautifulSoup, Tag
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
except ImportError:
    Credentials = None
    build = None

try:
    from playwright.async_api import BrowserContext, async_playwright
except ImportError:
    BrowserContext = Any
    async_playwright = None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

SOURCES_DIR = Path(os.getenv("SOURCES_DIR", "./sources"))
CLOUD_MODE = os.getenv("CLOUD_MODE", "0") == "1"
ENABLE_PLAYWRIGHT = os.getenv("ENABLE_PLAYWRIGHT", "1") == "1" and async_playwright is not None
ENABLE_PAGE_TEXT_FALLBACK = os.getenv("ENABLE_PAGE_TEXT_FALLBACK", "0") == "1"

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
REQUEST_WORKERS = int(os.getenv("REQUEST_WORKERS", "24"))
PLAYWRIGHT_TABS = int(os.getenv("PLAYWRIGHT_TABS", "8"))
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "75000"))
POST_LOAD_WAIT_MS = int(os.getenv("POST_LOAD_WAIT_MS", "1800"))

MIN_PRICE = int(os.getenv("MIN_PRICE", "1000000"))
MAX_PRICE = int(os.getenv("MAX_PRICE", "300000000"))

CLOUDFLARE_DEALERS = {
    name.strip()
    for name in os.getenv(
        "CLOUDFLARE_DEALERS",
        "FRT,Mobile World,Phong Vu,An Phat,Phuc Anh,An Khang",
    ).split(",")
    if name.strip()
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

PRICE_ATTRS = (
    "content",
    "value",
    "data-price",
    "data-product-price",
    "data-price-value",
    "data-final-price",
    "data-sale-price",
)

FALLBACK_SELECTORS = (
    'meta[property="product:price:amount"]',
    'meta[itemprop="price"]',
    '[itemprop="offers"] [itemprop="price"]',
    '[itemprop="price"]',
    '[data-final-price]',
    '[data-sale-price]',
    '[data-product-price]',
    '[data-price-value]',
    '[data-price]',
    '.sale-price',
    '.special-price',
    '.final-price',
    '.price-current',
    '.product-price',
    '.product__price',
    '.product-detail-price',
    '.pro-price',
)

BLOCK_MARKERS = (
    "cf-chl-",
    "cloudflare",
    "captcha",
    "access denied",
    "verify you are human",
    "checking your browser",
)

PRICE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.,\s]\d{3}){1,3}|\d{7,9})(?:\s*(?:₫|đ|vnd|dong))?",
    re.IGNORECASE,
)

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("price-checker")


@dataclass(frozen=True)
class ScrapeTask:
    dealer: str
    model: str
    url: str
    selectors: tuple[str, ...]


@dataclass
class ScrapeResult:
    task: ScrapeTask
    price: str = "N/A"
    source: str = "not-found"
    mode: str = "Unknown"
    error: str = ""

    def to_sheet_row(self) -> list[str]:
        now = time.localtime()
        return [
            self.task.model,
            self.task.url,
            self.price,
            self.source,
            self.task.dealer,
            time.strftime("%Y-%m-%d", now),
            time.strftime("%H:%M:%S", now),
            self.mode,
        ]


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

def create_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.7,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=REQUEST_WORKERS, pool_maxsize=REQUEST_WORKERS)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        }
    )
    return session


# A separate requests.Session is created per worker thread.
_thread_local: dict[int, requests.Session] = {}


def get_thread_session() -> requests.Session:
    import threading

    ident = threading.get_ident()
    if ident not in _thread_local:
        _thread_local[ident] = create_session()
    return _thread_local[ident]


# -----------------------------------------------------------------------------
# Price extraction
# -----------------------------------------------------------------------------

def normalize_selectors(selectors: Any) -> tuple[str, ...]:
    if isinstance(selectors, str):
        selectors = [selectors]
    if not isinstance(selectors, (list, tuple)):
        return ()
    return tuple(s.strip() for s in selectors if isinstance(s, str) and s.strip())


def extract_price_candidates(text: Any) -> list[int]:
    if text is None:
        return []
    value = str(text).replace("\u00a0", " ")
    prices: list[int] = []
    for match in PRICE_PATTERN.finditer(value):
        digits = re.sub(r"\D", "", match.group(1))
        if not digits:
            continue
        number = int(digits)
        if MIN_PRICE <= number <= MAX_PRICE:
            prices.append(number)
    return prices


def choose_price(candidates: Iterable[int]) -> str | None:
    values = list(dict.fromkeys(candidates))
    if not values:
        return None
    # Within a price-specific element the first valid value is usually the
    # current price; choosing min() can accidentally select an instalment value.
    return str(values[0])


def element_price(element: Tag) -> str | None:
    candidates: list[int] = []
    for attr in PRICE_ATTRS:
        candidates.extend(extract_price_candidates(element.get(attr)))
    candidates.extend(extract_price_candidates(element.get_text(" ", strip=True)))
    return choose_price(candidates)


def safe_select(soup: BeautifulSoup, selector: str) -> list[Tag]:
    try:
        return list(soup.select(selector))
    except Exception as exc:
        log.debug("Invalid selector %r: %s", selector, exc)
        return []


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_jsonld_price(soup: BeautifulSoup) -> str | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in walk_json(payload):
            obj_type = obj.get("@type")
            if isinstance(obj_type, list):
                types = {str(x).lower() for x in obj_type}
            else:
                types = {str(obj_type).lower()}
            if types.intersection({"offer", "aggregateoffer"}) or "price" in obj:
                for key in ("price", "lowPrice", "highPrice"):
                    price = choose_price(extract_price_candidates(obj.get(key)))
                    if price:
                        return price
    return None


def find_price(soup: BeautifulSoup, selectors: Sequence[str]) -> tuple[str, str]:
    # 1) Dealer-specific selectors have highest confidence.
    for selector in selectors:
        for element in safe_select(soup, selector):
            price = element_price(element)
            if price:
                return price, f"configured:{selector}"

    # 2) Structured product data.
    price = extract_jsonld_price(soup)
    if price:
        return price, "json-ld"

    # 3) Generic price elements.
    for selector in FALLBACK_SELECTORS:
        for element in safe_select(soup, selector):
            price = element_price(element)
            if price:
                return price, f"fallback:{selector}"

    # 4) Optional low-confidence fallback. Disabled by default because a page
    # may contain instalments, discounts, accessory prices, or old prices.
    if ENABLE_PAGE_TEXT_FALLBACK:
        price = choose_price(extract_price_candidates(soup.get_text(" ", strip=True)))
        if price:
            return price, "page-text-low-confidence"

    return "N/A", "not-found"


def looks_blocked(html: str, status_code: int) -> bool:
    sample = html[:20000].lower()
    return status_code in {401, 403, 429, 503} or any(marker in sample for marker in BLOCK_MARKERS)


# -----------------------------------------------------------------------------
# Requests scraper
# -----------------------------------------------------------------------------

def fetch_with_requests(task: ScrapeTask) -> ScrapeResult:
    session = get_thread_session()
    try:
        response = session.get(task.url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if looks_blocked(response.text, response.status_code):
            return ScrapeResult(task, source=f"blocked-http-{response.status_code}", mode="Cloud" if CLOUD_MODE else "Local")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        price, source = find_price(soup, task.selectors)
        return ScrapeResult(task, price, source, "Cloud" if CLOUD_MODE else "Local")
    except requests.RequestException as exc:
        return ScrapeResult(task, source="request-error", mode="Cloud" if CLOUD_MODE else "Local", error=str(exc))
    except Exception as exc:
        log.exception("Unexpected requests error for %s", task.url)
        return ScrapeResult(task, source="parse-error", mode="Cloud" if CLOUD_MODE else "Local", error=str(exc))


def scrape_requests(tasks: Sequence[ScrapeTask]) -> list[ScrapeResult]:
    if not tasks:
        return []
    results: list[ScrapeResult] = []
    workers = min(REQUEST_WORKERS, len(tasks))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_with_requests, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = ScrapeResult(task, source="worker-error", mode="Cloud" if CLOUD_MODE else "Local", error=str(exc))
            results.append(result)
            log.info("[%s] %s | %s | %s", result.task.dealer, result.task.model, result.price, result.source)
    return results


# -----------------------------------------------------------------------------
# Playwright scraper
# -----------------------------------------------------------------------------

async def fetch_with_playwright(context: BrowserContext, task: ScrapeTask) -> ScrapeResult:
    page = await context.new_page()
    try:
        await page.goto(task.url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(POST_LOAD_WAIT_MS)
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        price, source = find_price(soup, task.selectors)
        return ScrapeResult(task, price, f"playwright:{source}", "Local")
    except Exception as exc:
        return ScrapeResult(task, source="playwright-error", mode="Local", error=str(exc))
    finally:
        await page.close()


async def scrape_playwright_async(tasks: Sequence[ScrapeTask]) -> list[ScrapeResult]:
    if not tasks or not ENABLE_PLAYWRIGHT or async_playwright is None:
        return []

    semaphore = asyncio.Semaphore(max(1, PLAYWRIGHT_TABS))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="vi-VN",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7"},
        )

        async def worker(task: ScrapeTask) -> ScrapeResult:
            async with semaphore:
                result = await fetch_with_playwright(context, task)
                log.info("[%s] %s | %s | %s", result.task.dealer, result.task.model, result.price, result.source)
                return result

        results = await asyncio.gather(*(worker(task) for task in tasks))
        await context.close()
        await browser.close()
        return list(results)


def scrape_playwright(tasks: Sequence[ScrapeTask]) -> list[ScrapeResult]:
    return asyncio.run(scrape_playwright_async(tasks))


# -----------------------------------------------------------------------------
# Sources and output
# -----------------------------------------------------------------------------

def load_tasks(directory: Path = SOURCES_DIR) -> list[ScrapeTask]:
    if not directory.exists():
        raise FileNotFoundError(f"Sources directory not found: {directory}")

    tasks: list[ScrapeTask] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Cannot read %s: %s", path, exc)
            continue

        dealer = str(data.get("agency") or path.stem).strip()
        urls = data.get("urls", [])
        if not isinstance(urls, list):
            log.error("Invalid 'urls' list in %s", path)
            continue

        for item in urls:
            if not isinstance(item, dict) or not item.get("url"):
                log.warning("Skipping invalid item in %s: %r", path, item)
                continue
            model = str(item.get("model") or item.get("name") or "Unknown").strip()
            url = str(item["url"]).strip()
            key = (dealer, url)
            if key in seen:
                log.warning("Skipping duplicate URL for %s: %s", dealer, url)
                continue
            seen.add(key)
            tasks.append(ScrapeTask(dealer, model, url, normalize_selectors(item.get("selector", []))))

    return tasks


def write_to_sheet(rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        log.warning("No rows to write")
        return
    if Credentials is None or build is None:
        log.warning("Google API packages unavailable; skipping Sheets write")
        return

    raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not raw_credentials or not spreadsheet_id:
        log.warning("GOOGLE_SERVICE_ACCOUNT or GOOGLE_SHEET_ID is missing; skipping Sheets write")
        return

    try:
        credentials_info = json.loads(raw_credentials)
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=os.getenv("GOOGLE_SHEET_RANGE", "Log!A:H"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": list(rows)},
        ).execute()
        log.info("Wrote %d rows to Google Sheets", len(rows))
    except Exception:
        log.exception("Google Sheets write failed")


def print_summary(results: Sequence[ScrapeResult]) -> None:
    total = len(results)
    success = sum(result.price != "N/A" for result in results)
    failed = total - success
    log.info("Summary | total=%d success=%d failed=%d", total, success, failed)

    dealer_stats: dict[str, list[int]] = {}
    for result in results:
        stats = dealer_stats.setdefault(result.task.dealer, [0, 0])
        stats[0] += 1
        stats[1] += result.price != "N/A"
    for dealer, (count, ok) in sorted(dealer_stats.items()):
        log.info("Dealer | %-20s total=%d success=%d failed=%d", dealer, count, ok, count - ok)


# -----------------------------------------------------------------------------
# Main orchestration
# -----------------------------------------------------------------------------

def scrape() -> list[ScrapeResult]:
    tasks = load_tasks()
    log.info("Loaded %d tasks from %s", len(tasks), SOURCES_DIR)
    if not tasks:
        return []

    # In local mode, browser-first for known protected dealers. Normal dealers
    # use fast HTTP first. Any failed HTTP task receives one browser fallback.
    if not CLOUD_MODE and ENABLE_PLAYWRIGHT:
        protected = [task for task in tasks if task.dealer in CLOUDFLARE_DEALERS]
        normal = [task for task in tasks if task.dealer not in CLOUDFLARE_DEALERS]

        results = scrape_requests(normal)
        browser_tasks = protected + [result.task for result in results if result.price == "N/A"]
        browser_results = scrape_playwright(browser_tasks)

        browser_by_key = {(result.task.dealer, result.task.url): result for result in browser_results}
        merged: list[ScrapeResult] = []
        for result in results:
            key = (result.task.dealer, result.task.url)
            fallback = browser_by_key.pop(key, None)
            merged.append(fallback if fallback and fallback.price != "N/A" else result)
        merged.extend(browser_by_key.values())
        results = merged
    else:
        # Cloud environments often lack a browser. Do not silently drop protected
        # dealers: attempt every URL with requests and record failures explicitly.
        results = scrape_requests(tasks)

    # Preserve source-file order in the sheet.
    order = {(task.dealer, task.url): index for index, task in enumerate(tasks)}
    results.sort(key=lambda result: order[(result.task.dealer, result.task.url)])

    print_summary(results)
    write_to_sheet([result.to_sheet_row() for result in results])
    return results


if __name__ == "__main__":
    scrape()
