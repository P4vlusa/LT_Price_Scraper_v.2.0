const puppeteer = require("puppeteer");
const { google } = require("googleapis");
const fs = require("fs");
const path = require("path");

async function scrape() {
  const sourcesDir = "./sources";
  const files = fs.readdirSync(sourcesDir).filter(f => f.endsWith(".json"));

  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"]
  });

  const page = await browser.newPage();
  const results = [];

  for (let file of files) {
    const data = JSON.parse(fs.readFileSync(path.join(sourcesDir, file), "utf8"));
    const agency = data.agency || file.replace(".json", "");

    console.log("=== Đang xử lý đại lý:", agency, "===");

    for (let item of data.urls) {
      const model = item.model || item.name || "Unknown";

      // FIX LỖI URL BỊ RỖNG → SELECTOR BỊ ĐẨY VÀO
      const url = (item.url && item.url.startsWith("http")) ? item.url : null;

      if (!url) {
        console.log("❌ URL không hợp lệ, bỏ qua:", item);
        continue;
      }

      console.log("Scraping:", url);

      let price = "N/A";
      let source = "N/A";

      try {
        await page.goto(url, {
          waitUntil: "networkidle2",
          timeout: 60000
        });

        for (let sel of item.selector) {
          try {
            await page.waitForSelector(sel, { timeout: 5000 });
            const text = await page.$eval(sel, el => el.innerText);
            price = text.replace(/[^\d]/g, "");
            source = sel;
            break;
          } catch (e) {}
        }
      } catch (e) {
        console.log("Error loading:", url);
      }

      results.push([
        model,
        url,
        price,
        source,
        agency,
        new Date().toISOString()
      ]);
    }
  }

  await browser.close();
  await writeToSheet(results);
}

async function writeToSheet(rows) {
  const auth = new google.auth.GoogleAuth({
    credentials: JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT),
    scopes: ["https://www.googleapis.com/auth/spreadsheets"]
  });

  const sheets = google.sheets({ version: "v4", auth });

  await sheets.spreadsheets.values.append({
    spreadsheetId: process.env.GOOGLE_SHEET_ID,
    range: "Log!A:F",
    valueInputOption: "RAW",
    requestBody: { values: rows }
  });

  console.log("Done:", rows.length, "rows added");
}

scrape();
