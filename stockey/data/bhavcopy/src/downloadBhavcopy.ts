import { chromium, devices } from 'playwright';

async function downloadBhavcopy() {
  // Connect to existing Chrome in debug mode
  const cdpEndpoint = 'http://localhost:9222';
  const browser = await chromium.connectOverCDP(cdpEndpoint);
  const context = browser.contexts()[0] || await browser.newContext();
  const page = await context.newPage();

  console.log("Navigating to NSE homepage...");
  await page.goto('https://www.nseindia.com');
  await page.waitForTimeout(3000);

  console.log("Navigating to All Reports...");
  await page.goto('https://www.nseindia.com/all-reports');
  await page.waitForTimeout(3000);

  await page.getByRole('tab', { name: 'Archives' }).click();
  await page.waitForTimeout(2000)

  const jsCode = `$("#cr_equity_archives_date").val("06-Jun-2025");`
  await page.evaluate(jsCode);
  await page.waitForTimeout(1000)
  await page.getByRole("checkbox", { name: "Select All Reports" }).click();
  await page.waitForTimeout(1000)

  // Trigger download
  const [ download ] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole("link", { name: "Multiple file Download " }).click()
  ]);

  const path = `bhavcopy_${new Date().toISOString().split('T')[0]}.csv`;
  await download.saveAs(path);
  console.log(`Downloaded to ${path}`);

  await page.close();
  await browser.close();
}

downloadBhavcopy().catch(console.error);
