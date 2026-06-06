require("dotenv").config();
const puppeteer = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");
const { google } = require("googleapis");
const fs = require("fs");
const path = require("path");

puppeteer.use(StealthPlugin());

// Các domain bị Cloudflare chặn khi chạy CLOUD
const BLOCKED_DOMAINS = [
  "thegioididong.com",
  "anphatpc.com.vn",
  "cellphones.com.vn",
  "hoanghamobile.com"
];

function isBlocked(url) {
  return BLOCKED_DOMAINS.some(domain => url.includes(domain));
}

// =========================
// Scrape 1 sản phẩm
// =========================
async function scrapeItem(page, url, selectors) {
  try {
    await page.goto(url, {
      waitUntil: "networkidle2",
      timeout: 90000
    });

    await page.waitForTimeout(3000);

    for (let sel of selectors || []) {
      try {
        await page.waitForSelector(sel, { timeout: 8000 });
        const text = await page.$eval(sel, el => el.innerText);
        const price = text.replace(/[^\d]/g, "");
        if (price.length > 4) return { price, source: sel };
      } catch (e) {}
    }

    const body = await page.content();
    const match = body.match(/(\d{1,3}(\.\d{3}){1,3})/);
    if (match) {
      return { price: match[0].replace(/[^\d]/g, ""), source: "auto-detect" };
    }

    return { price: "N/A", source: "not-found" };

  } catch (e) {
    return { price: "N/A", source: "load-error" };
  }
}

// =========================
// Hàm chính
// =========================
async function scrape() {
  const sourcesDir = "./sources";
  const files = fs.readdirSync(sourcesDir).filter(f => f.endsWith(".json"));

  const isCloud = process.env.CLOUD_MODE === "1";
  console.log("MODE:", isCloud ? "CLOUD" : "LOCAL");

  const browser = await puppeteer.launch({
    headless: isCloud ? true : false,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-blink-features=AutomationControlled"
    ]
  });

  const page = await browser.newPage();

  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
  );

  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => false });
  });

  const results = [];

  for (let file of files) {
    const data = JSON.parse(fs.readFileSync(path.join(sourcesDir, file), "utf8"));
    const agency = data.agency || file.replace(".json", "");

    console.log(`=== Đang xử lý đại lý: ${agency} ===`);

    for (let item of data.urls) {
      const model = item.model || item.name || "Unknown";
      const url = item.url;

      if (!url || !url.startsWith("http")) {
        console.log("❌ URL không hợp lệ, bỏ qua:", item);
        continue;
      }

      if (isCloud && isBlocked(url)) {
        console.log("⏭ Bỏ qua (Cloudflare chặn trên cloud):", url);
        continue;
      }

      console.log("Scraping:", url);

      const { price, source } = await scrapeItem(page, url, item.selector);

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

// =========================
// Ghi Google Sheet
// =========================
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

  console.log("✔ Ghi xong:", rows.length, "dòng");
}

scrape();
