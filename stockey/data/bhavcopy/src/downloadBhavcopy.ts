import { chromium } from 'playwright';
import Redis from 'ioredis';
import { format, subDays } from 'date-fns';

const redis = new Redis(6379, "localhost"); // default localhost:6379

function getRandom(min:number, max:number): number {
  return Math.random() * (max - min) + min
}

async function downloadBhavcopyForDate(formattedDate: string, displayDate: string): Promise<boolean> {
  const cdpEndpoint = 'http://localhost:9222';
  const browser = await chromium.connectOverCDP(cdpEndpoint);
  const context = browser.contexts()[0] || await browser.newContext();
  const page = await context.newPage();

  try {
    await page.waitForTimeout(getRandom(2000, 5000));
    await page.goto('https://www.nseindia.com');
    await page.waitForTimeout(getRandom(1000, 3000));
    await page.goto('https://www.nseindia.com/all-reports');
    await page.waitForTimeout(getRandom(1000, 2000));

    await page.getByRole('tab', { name: 'Archives' }).click();
    await page.waitForTimeout(getRandom(2000, 3000));

    const jsCode = `$("#cr_equity_archives_date").val("${displayDate}");`;
    await page.evaluate(jsCode);
    await page.waitForTimeout(1000);
    await page.getByRole("checkbox", { name: "Select All Reports" }).click();
    await page.waitForTimeout(1000);

    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 10000 }),
      page.getByRole("link", { name: "Multiple file Download " }).click()
    ]);

    const path = `bhavcopy_${formattedDate}.zip`;
    await download.saveAs(path);
    console.log(`Success: ${formattedDate} (${new Date(displayDate).toLocaleDateString('en-US', { weekday: 'long' })})`);
    await redis.sadd("nse:downloaded", formattedDate);
    return true;
  } catch (err) {
    console.log(`Failed: ${formattedDate} (${new Date(displayDate).toLocaleDateString('en-US', { weekday: 'long' })})`);
    return false;
  } finally {
    await page.close();
    await browser.close();
  }
}

async function main() {
  let failures = 0;
  let daysBack = 1;

  while (failures < 7) {
    const date = subDays(new Date(), daysBack);
    const formattedDate = format(date, 'yyyy-MM-dd');
    const displayDate = format(date, 'dd-MMM-yyyy'); // for NSE calendar, like 06-Jun-2025

    const alreadyDownloaded = await redis.sismember("nse:downloaded", formattedDate);
    if (alreadyDownloaded) {
      console.log(`⏩ Already downloaded: ${formattedDate}`);
      daysBack++;
      continue;
    }

    const success = await downloadBhavcopyForDate(formattedDate, displayDate);
    if (!success) {
      failures++;
    } else {
      failures = 0;
    }

    daysBack++;
  }

  console.log("📉 Stopped after 7 consecutive failures.");
  redis.disconnect();
}

main().catch(console.error);

