// Abre a timeline.html num Chrome headless, faz edições pelo teclado e salva.
// Uso: node navegador.mjs <chrome> <url> <pasta_downloads>
// Imprime um JSON com erros de console, estado da página e status do Salvar.
import puppeteer from "puppeteer-core";

const [chrome, url, downloads] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: true,
  args: ["--no-sandbox", "--autoplay-policy=no-user-gesture-required",
         "--allow-file-access-from-files"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 900 });
const erros = [];
page.on("console", (m) => { if (m.type() === "error") erros.push(m.text()); });
page.on("pageerror", (e) => erros.push(String(e)));
page.on("requestfailed", (r) => {
  const motivo = r.failure()?.errorText || "";
  if (!motivo.includes("ERR_ABORTED")) erros.push(`falhou: ${r.url()} ${motivo}`);
});
const cdp = await page.createCDPSession();
await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });

await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() => document.querySelectorAll(".blk").length > 0, { timeout: 15000 });
await page.waitForFunction(() => document.getElementById("cam").readyState >= 2, { timeout: 15000 });

// seleciona o 1º bloco, liga a tela, remove, desfaz a remoção
await page.keyboard.press("ArrowDown");
await page.keyboard.press("t");
await page.keyboard.press("Delete");
await page.keyboard.down("Control"); await page.keyboard.press("z"); await page.keyboard.up("Control");

// toca 2s pulando cortes; depois pula para uma frase da transcrição
await page.keyboard.press(" ");
await sleep(2000);
await page.keyboard.press(" ");
const tocou = await page.evaluate(() => document.getElementById("cam").currentTime);
await page.click('.frase[data-f="2"]');
await sleep(300);

const estado = await page.evaluate(() => ({
  blocos: document.querySelectorAll(".blk").length,
  frases: document.querySelectorAll(".frase").length,
  cortadas: document.querySelectorAll(".frase.cortada").length,
  tempo: document.getElementById("tempo").textContent,
  duracoes: document.getElementById("duracoes").textContent,
  erroVideo: getComputedStyle(document.getElementById("msgVideo")).display !== "none",
  pip: document.getElementById("stage").classList.contains("pip"),
}));

await page.keyboard.down("Control"); await page.keyboard.press("s"); await page.keyboard.up("Control");
await sleep(1500);
const status = await page.$eval("#status", (e) => e.textContent);
console.log(JSON.stringify({ erros, estado, tocou, status }));
await browser.close();
