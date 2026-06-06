import os
import json
import time
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# Session để tăng tốc
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
})

def fetch_price(url, selectors):
    try:
        html = session.get(url, timeout=15).text
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
        range="Log!A:H",
        valueInputOption="RAW",
        body=body
    ).execute()

def scrape():
    is_cloud = os.getenv("CLOUD_MODE") == "1"
    mode_label = "CLOUD" if is_cloud else "LOCAL"

    print("MODE:", mode_label)

    results = []

    for file in os.listdir("./sources"):
        if not file.endswith(".json"):
            continue

        data = json.load(open(f"./sources/{file}", "r", encoding="utf-8"))
        dealer = data.get("agency", file.replace(".json", ""))  # đổi tên cột

        print("Processing:", dealer)

        for item in data["urls"]:
            url = item["url"]
            selectors = item.get("selector", [])

            print("Scraping:", url)
            price, source = fetch_price(url, selectors)

            # timestamp
            ts = time.time()
            if is_cloud:
                ts += 7 * 3600  # UTC → GMT+7

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

            results.append([
                item.get("model", item.get("name", "Unknown")),
                url,
                price,
                source,
                dealer,
                timestamp,
                mode_label,
                file
            ])

    write_to_sheet(results)
    print("DONE:", len(results), "rows")

if __name__ == "__main__":
    scrape()
