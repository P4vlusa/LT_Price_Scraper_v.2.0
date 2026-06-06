import os
import json
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# Playwright chỉ dùng khi chạy LOCAL
USE_PLAYWRIGHT = os.getenv("CLOUD_MODE") != "1"
if USE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright

# Session cho requests (cloud)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
})

def fetch_price_requests(url, selectors):
    """Cloud mode: dùng requests"""
    try:
        html = session.get(url, timeout=10).text
        soup = BeautifulSoup(html, "lxml")

        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                price = "".join(filter(str.isdigit, el.text))
                if len(price) > 4:
                    return price, sel

        return "N/A", "not-found"

    except:
        return "N/A", "load-error"

def fetch_price_playwright(page, url, selectors):
    """Local mode: dùng Playwright bypass Cloudflare"""
    try:
        page.goto(url, timeout=60000, wait_until="networkidle")

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    text = el.inner_text(timeout=5000)
                    price = "".join(filter(str.isdigit, text))
                    if len(price) > 4:
                        return price, sel
            except:
                pass

        return "N/A", "not-found"

    except:
        return "N/A", "load-error"

def write_to_sheet(rows):
    creds = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )

    service = build("sheets", "v4", credentials=creds)

    body = {"values": rows}

    service.spreadsheets().values().append(
        spreadsheetId=os.getenv("GOOGLE_SHEET_ID"),
        range="Log!A:G",
        valueInputOption="RAW",
        body=body
    ).execute()

def scrape_cloud(tasks):
    """Cloud mode: requests + multi-thread"""
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

            ts = time.time() + 7 * 3600  # UTC → GMT+7
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

            results.append([
                t["model"],
                t["url"],
                price,
                source,
                t["dealer"],
                timestamp,
                "Cloud"
            ])

    return results

def scrape_local(tasks):
    """Local mode: Playwright bypass Cloudflare"""
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        pages = [context.new_page() for _ in range(10)]  # 10 tab song song

        idx = 0
        for t in tasks:
            page = pages[idx % len(pages)]
            idx += 1

            price, source = fetch_price_playwright(page, t["url"], t["selectors"])

            ts = time.time()  # local = GMT+7 sẵn
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

            results.append([
                t["model"],
                t["url"],
                price,
                source,
                t["dealer"],
                timestamp,
                "Local"
            ])

        browser.close()

    return results

def scrape():
    is_cloud = os.getenv("CLOUD_MODE") == "1"

    tasks = []

    # Load toàn bộ nguồn
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

    if is_cloud:
        results = scrape_cloud(tasks)
    else:
        results = scrape_local(tasks)

    write_to_sheet(results)
    print("DONE:", len(results), "rows")

if __name__ == "__main__":
    scrape()
