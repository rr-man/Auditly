// Records the "Show me around" tour (0.54.0) to a .webm so a shareable clip can be judged before one is
// bundled. Not run by the test suite; needs node and Playwright with Chromium (npm i playwright &&
// npx playwright install chromium). The server must be up in demo mode (python3 auditly_host.py --demo).
// usage: node tests/tour_record.js [base=http://127.0.0.1:8084] [outdir=$TMPDIR/auditly-tour] [--theme light|dark] [--dwell 4000] [--shots]
// Prints one line per stop, the .webm path and size, and every console error; exits 1 on a console error.
const { chromium } = require("playwright");
const fs = require("fs"), os = require("os"), path = require("path");
const a = process.argv.slice(2), opt = (n, d) => { const i = a.indexOf(n); return i < 0 ? d : a[i + 1]; };
const pos = a.filter((x, i) => !x.startsWith("--") && !(i && ["--theme", "--dwell"].includes(a[i - 1])));
const BASE = pos[0] || "http://127.0.0.1:8084", OUT = pos[1] || path.join(os.tmpdir(), "auditly-tour");
const THEME = opt("--theme", "light"), DWELL = +opt("--dwell", 4000), SHOTS = a.includes("--shots"), W = 1280, H = 800;
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, colorScheme: THEME, recordVideo: { dir: OUT, size: { width: W, height: H } } });
  const page = await ctx.newPage(), errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); }); page.on("pageerror", e => errors.push(String(e)));
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.evaluate(t => { try { localStorage.setItem("auditly-guide-seen", "1"); localStorage.setItem("auditly-theme", t); } catch (e) {} }, THEME);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#app:not(.hidden), #loginView:not(.hidden)");
  if (await page.locator("#loginView:not(.hidden)").count()) {           // open access signs in by itself; the demo account otherwise
    await page.fill("#loginEmail", "demo@example.com"); await page.fill("#loginPw", "demo-demo-demo"); await page.click('#loginForm button[type="submit"]');
    await page.waitForSelector("#app:not(.hidden)");
  }
  await page.evaluate(() => document.querySelectorAll(".modalwrap").forEach(w => w.remove()));
  // the sample call must be scored before the Scorecard stop can open it (a fresh --demo boot takes a few seconds)
  await page.waitForFunction(() => fetch("/api/jobs?kind=demo&q=DEMO-0001", { credentials: "same-origin" }).then(r => r.json())
    .then(d => (d.jobs || []).some(j => j.status === "done" && j.scorecard_id)).catch(() => false), null, { timeout: 90000, polling: 1000 });
  await page.waitForTimeout(1200);
  await page.click("#helpBtn"); await page.waitForSelector('.modalwrap [data-x="tour"]'); await page.waitForTimeout(1500);
  await page.click('.modalwrap .foot [data-x="tour"]');
  for (let n = 1; n <= 12; n++) {
    await page.waitForSelector("#tourWrap:not(.hidden):not(.busy)", { timeout: 15000 });
    const s = await page.evaluate(() => ({ title: document.getElementById("tourTitle").textContent, step: document.getElementById("tourStep").textContent,
      spot: !document.getElementById("tourHole").classList.contains("hidden"), last: document.getElementById("tourNext").classList.contains("hidden"),
      hole: document.getElementById("tourHole").getBoundingClientRect().toJSON(), card: document.getElementById("tourCard").getBoundingClientRect().toJSON() }));
    console.log(`stop ${n}: ${s.title} ${s.step} spotlight=${s.spot} hole=${Math.round(s.hole.width)}x${Math.round(s.hole.height)} card@${Math.round(s.card.left)},${Math.round(s.card.top)}`);
    if (SHOTS) await page.screenshot({ path: path.join(OUT, `stop-${n}-${THEME}.png`) });
    await page.waitForTimeout(DWELL);
    if (s.last) break;
    await page.click("#tourNext");
  }
  await page.click("#tourSkip"); await page.waitForTimeout(800);
  const video = page.video(); await ctx.close(); await browser.close();
  const dst = path.join(OUT, `auditly-tour-${THEME}.webm`); fs.renameSync(await video.path(), dst);
  console.log("video:", dst, (fs.statSync(dst).size / 1048576).toFixed(1) + " MB");
  if (errors.length) { console.log("console errors:\n  " + errors.join("\n  ")); process.exit(1); }
})().catch(e => { console.error(e); process.exit(1); });
