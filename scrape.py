import os
import json
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# ============================
# CONFIG
# ============================

CLOUDFLARE_DEALERS = ["An Phat", "Phong Vu", "Mobile World"]

USE_PLAYWRIGHT = os.getenv("CLOUD_MODE") != "1"
if USE_PLAYWRIGHT:
    from playwright.sync_api import sync_playwright

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
})

# ============================
# REQUESTS SCRAPER
# ============================

def fetch_price_requests(url, selectors):
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

# ============================
# PLAYWRIGHT SCRAPER
# ============================

def fetch_price_playwright(page, url, selectors):
    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")

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

# ============================
# WRITE TO GOOGLE SHEET
# ============================

def write_to_sheet(rows):
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

            ts = time.time() + 7 * 3600
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
# PLAYWRIGHT MODE (40 TABS)
# ============================

def scrape_playwright(tasks):
    results = []
    MAX_TABS = 40  # tăng tốc mạnh

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()

        def worker(task):
            page = context.new_page()
            price, source = fetch_price_playwright(page, task["url"], task["selectors"])
            page.close()

            ts = time.time()
            date = time.strftime("%Y-%m-%d", time.localtime(ts))
            hour = time.strftime("%H:%M:%S", time.localtime(ts))

            return [
                task["model"],
                task["url"],
                price,
                source,
                task["dealer"],
                date,
                hour,
                "Local"
            ]

        with ThreadPoolExecutor(max_workers=MAX_TABS) as executor:
            futures = {executor.submit(worker, t): t for t in tasks}

            for future in as_completed(futures):
                results.append(future.result())

        browser.close()

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

    if cloudflare_tasks and not is_cloud:
        print("Running Playwright for Cloudflare dealers:", len(cloudflare_tasks))
        results += scrape_playwright(cloudflare_tasks)

    write_to_sheet(results)
    print("DONE:", len(results), "rows")

if __name__ == "__main__":
    scrape()
