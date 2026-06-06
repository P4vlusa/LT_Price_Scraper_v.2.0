import os
import json
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# Session dùng chung để tăng tốc
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
})

def fetch_price(url, selectors):
    try:
        html = session.get(url, timeout=10).text
        soup = BeautifulSoup(html, "lxml")

        # Chỉ dùng selector, không auto detect
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                price = "".join(filter(str.isdigit, el.text))
                if len(price) > 4:
                    return price, sel

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

def scrape():
    is_cloud = os.getenv("CLOUD_MODE") == "1"

    # Mode label
    mode_label = "Cloud" if is_cloud else "Local"

    print("MODE:", mode_label)

    tasks = []
    results = []

    # Gom toàn bộ job vào 1 list để chạy song song
    for file in os.listdir("./sources"):
        if not file.endswith(".json"):
            continue

        data = json.load(open(f"./sources/{file}", "r", encoding="utf-8"))
        dealer = data.get("agency", file.replace(".json", ""))

        print("Loading dealer:", dealer)

        for item in data["urls"]:
            tasks.append({
                "dealer": dealer,
                "model": item.get("model", item.get("name", "Unknown")),
                "url": item["url"],
                "selectors": item.get("selector", [])
            })

    print("Total tasks:", len(tasks))

    # Tăng luồng
    MAX_WORKERS = 100 if is_cloud else 200

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_price, t["url"], t["selectors"]): t
            for t in tasks
        }

        for future in as_completed(futures):
            t = futures[future]
            price, source = future.result()

            # timestamp
            ts = time.time()
            if is_cloud:
                ts += 7 * 3600  # UTC → GMT+7

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

            results.append([
                t["model"],
                t["url"],
                price,
                source,
                t["dealer"],
                timestamp,
                mode_label
            ])

    write_to_sheet(results)
    print("DONE:", len(results), "rows")

if __name__ == "__main__":
    scrape()
