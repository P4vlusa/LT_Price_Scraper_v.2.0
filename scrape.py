import os
import json
import time
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

BLOCKED_DOMAINS = [
    "thegioididong.com",
    "anphatpc.com.vn",
    "cellphones.com.vn",
    "hoanghamobile.com"
]

def is_blocked(url):
    return any(domain in url for domain in BLOCKED_DOMAINS)

def fetch_price(url, selectors):
    try:
        html = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }).text

        soup = BeautifulSoup(html, "lxml")

        # thử selector
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                price = "".join(filter(str.isdigit, el.text))
                if len(price) > 4:
                    return price, sel

        # fallback: tìm số tiền trong toàn trang
        import re
        match = re.search(r"\d{1,3}(?:\.\d{3}){1,3}", html)
        if match:
            return match.group(0).replace(".", ""), "auto-detect"

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
        range="Log!A:F",
        valueInputOption="RAW",
        body=body
    ).execute()

def scrape():
    is_cloud = os.getenv("CLOUD_MODE") == "1"
    print("MODE:", "CLOUD" if is_cloud else "LOCAL")

    results = []

    for file in os.listdir("./sources"):
        if not file.endswith(".json"):
            continue

        data = json.load(open(f"./sources/{file}", "r", encoding="utf-8"))
        agency = data.get("agency", file.replace(".json", ""))

        print(f"=== {agency} ===")

        for item in data["urls"]:
            url = item["url"]
            selectors = item.get("selector", [])

            if is_cloud and is_blocked(url):
                print("⏭ Bỏ qua (cloud bị chặn):", url)
                continue

            print("Scraping:", url)
            price, source = fetch_price(url, selectors)

            results.append([
                item.get("model", item.get("name", "Unknown")),
                url,
                price,
                source,
                agency,
                time.strftime("%Y-%m-%d %H:%M:%S")
            ])

    write_to_sheet(results)
    print("✔ DONE:", len(results), "rows")

if __name__ == "__main__":
    scrape()
