import os
import json
import time
import asyncio
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials
except ImportError:  # pragma: no cover - optional in test environments
    build = None
    Credentials = None

# ============================
# CONFIG
# ============================

CLOUDFLARE_DEALERS = ["FRT","Mobile World","Phong Vu","An Phat","Phuc Anh", "An Khang"]

USE_PLAYWRIGHT = os.getenv("CLOUD_MODE") != "1"
if USE_PLAYWRIGHT:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        USE_PLAYWRIGHT = False

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
})


def extract_price_from_text(text):
    if not text:
        return None

    cleaned = text.replace(".", "").replace(",", "")
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return None

    numeric_value = int(digits)
    if numeric_value < 1000:
        return None

    return str(numeric_value)


def try_extract_price_from_element(element):
    if not element:
        return None

    candidates = []
    for attr in ["content", "value", "data-price", "data-product-price", "data-price-value"]:
        value = element.get(attr)
        if value:
            candidates.append(value)

    for candidate in candidates:
        price = extract_price_from_text(candidate)
        if price:
            return price

    text = " ".join(element.stripped_strings)
    if text:
        price = extract_price_from_text(text)
        if price:
            return price

    return None


def normalize_selectors(selectors):
    if not selectors:
        return []
    if isinstance(selectors, str):
        return [selectors]
    return [sel for sel in selectors if isinstance(sel, str) and sel.strip()]


def find_price_from_selectors(soup, selectors):
    for sel in normalize_selectors(selectors):
        elements = soup.select(sel)
        if not elements:
            continue

        for element in elements:
            price = try_extract_price_from_element(element)
            if price:
                return price, sel

            for child in element.select("*"):
                price = try_extract_price_from_element(child)
                if price:
                    return price, sel

    return "N/A", "not-found"


# ============================
# REQUESTS SCRAPER
# ============================

def fetch_price_requests(url, selectors):
    try:
        html = session.get(url, timeout=10).text
        soup = BeautifulSoup(html, "lxml")
        price, source = find_price_from_selectors(soup, selectors)
        return price, source

    except Exception:
        return "N/A", "load-error"

# ============================
# PLAYWRIGHT ASYNC SCRAPER
# ============================

async def fetch_price_playwright_async(context, task):
    page = await context.new_page()
    try:
        await page.goto(task["url"], timeout=60000, wait_until="domcontentloaded")

        for sel in normalize_selectors(task.get("selectors", [])):
            try:
                elements = page.locator(sel)
                count = await elements.count()
                if count == 0:
                    continue

                for idx in range(count):
                    el = elements.nth(idx)
                    text = await el.evaluate(
                        """(node) => {
                            const attrs = ['content', 'value', 'data-price', 'data-product-price', 'data-price-value'];
                            const values = [];
                            for (const attr of attrs) {
                                const value = node.getAttribute(attr);
                                if (value) values.push(value);
                            }
                            const text = node.innerText || node.textContent || '';
                            if (text) values.push(text);
                            return values.join(' ');
                        }"""
                    )
                    price = extract_price_from_text(text)
                    if price:
                        await page.close()
                        return task, price, sel
            except Exception:
                pass

        await page.close()
        return task, "N/A", "not-found"

    except Exception:
        await page.close()
        return task, "N/A", "load-error"


async def scrape_playwright_async(tasks):
    results = []
    MAX_TABS = 40

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()

        sem = asyncio.Semaphore(MAX_TABS)

        async def worker(task):
            async with sem:
                return await fetch_price_playwright_async(context, task)

        all_tasks = [worker(t) for t in tasks]
        done = await asyncio.gather(*all_tasks)

        for task, price, source in done:
            ts = time.time()
            date = time.strftime("%Y-%m-%d", time.localtime(ts))
            hour = time.strftime("%H:%M:%S", time.localtime(ts))

            results.append([
                task["model"],
                task["url"],
                price,
                source,
                task["dealer"],
                date,
                hour,
                "Local"
            ])

        await browser.close()

    return results


def scrape_playwright(tasks):
    return asyncio.run(scrape_playwright_async(tasks))

# ============================
# WRITE TO GOOGLE SHEET
# ============================

def write_to_sheet(rows):
    if not Credentials or not build:
        print("Google Sheets credentials unavailable; skipping write.")
        return

    creds = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )

    service = build("sheets", "v4", credentials=creds)

    body = {"values": rows}

    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Log!A:H",
        valueInputOption="RAW",
        body=body
    ).execute()

# ============================
# REQUESTS MODE
# ============================

def scrape_requests(tasks):
    results = []
    MAX_WORKERS = 100

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_price_requests, t["url"], t["selectors"]): t
            for t in tasks
        }

        for future in as_completed(futures):
            t = futures[future]
            price, source = future.result()

            ts = time.time() + 0 * 3600
            date = time.strftime("%Y-%m-%d", time.localtime(ts))
            hour = time.strftime("%H:%M:%S", time.localtime(ts))

            results.append([
                t["model"],
                t["url"],
                price,
                source,
                t["dealer"],
                date,
                hour,
                "Cloud"
            ])

    return results

# ============================
# MAIN
# ============================

def scrape():
    is_cloud = os.getenv("CLOUD_MODE") == "1"

    tasks = []

    for file in os.listdir("./sources"):
        if not file.endswith(".json"):
            continue

        data = json.load(open(f"./sources/{file}", "r", encoding="utf-8"))
        dealer = data.get("agency", file.replace(".json", ""))

        for item in data["urls"]:
            tasks.append({
                "dealer": dealer,
                "model": item.get("model", item.get("name", "Unknown")),
                "url": item["url"],
                "selectors": item.get("selector", [])
            })

    print("Total tasks:", len(tasks))

    cloudflare_tasks = [t for t in tasks if t["dealer"] in CLOUDFLARE_DEALERS]
    normal_tasks = [t for t in tasks if t["dealer"] not in CLOUDFLARE_DEALERS]

    results = []

    if normal_tasks:
        print("Running requests for normal dealers:", len(normal_tasks))
        results += scrape_requests(normal_tasks)

    if cloudflare_tasks and not is_cloud and USE_PLAYWRIGHT:
        print("Running Playwright for Cloudflare dealers:", len(cloudflare_tasks))
        results += scrape_playwright(cloudflare_tasks)

    write_to_sheet(results)
    print("DONE:", len(results), "rows")


if __name__ == "__main__":
    scrape()
