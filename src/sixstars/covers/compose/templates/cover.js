// Cover engine templates (docs/29a sections 6.2-6.5). render.py calls renderCover(data) and screenshots
// #canvas once it resolves. data: {anton, inter, face, game, fillTop, fillBottom, cutout, mark,
// lines: [..], keyWord, boxText, lineY}.

const LINE_WIDTH = 900;
const CAP_MIN = 110, CAP_MAX = 170, FONT_MIN = 96;
const BOX_FONT_MIN = 56, BOX_FONT_MAX = 64;
const SAFE_TOP = 400, SAFE_BOTTOM = 1520;

function loadImage(el, src) {
  return new Promise((resolve) => {
    if (!src) { el.removeAttribute("src"); resolve(); return; }
    el.onload = () => resolve();
    el.onerror = () => resolve();
    el.src = src;
  });
}

function capRatio(family) {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  ctx.font = `100px ${family}`;
  const m = ctx.measureText("H");
  return (m.actualBoundingBoxAscent || 72) / 100;
}

// The largest size where every row fits LINE_WIDTH, capped so the cap height stays 110-170 px and never
// below 96 px.
function fitLine(line) {
  const ratio = capRatio('"CoverAnton"');
  const maxSize = Math.floor(CAP_MAX / ratio);
  let size = maxSize;
  const rows = Array.from(line.querySelectorAll(".row"));
  const fits = () => rows.every((row) => row.scrollWidth <= LINE_WIDTH);
  while (size > FONT_MIN) {
    line.style.fontSize = `${size}px`;
    line.style.setProperty("--stroke", `${2 * Math.min(16, Math.max(12, Math.round(size * 0.075)))}px`);
    if (fits()) break;
    size -= 2;
  }
  line.style.fontSize = `${Math.max(size, FONT_MIN)}px`;
  return { size: Math.max(size, FONT_MIN), capHeight: Math.round(Math.max(size, FONT_MIN) * ratio), fits: fits() };
}

function fitBox(text) {
  let size = BOX_FONT_MAX;
  while (size > BOX_FONT_MIN) {
    text.style.fontSize = `${size}px`;
    const lines = Math.round(text.scrollHeight / (size * 1.15));
    if (lines <= 2) break;
    size -= 2;
  }
  text.style.fontSize = `${size}px`;
  return size;
}

async function renderCover(data) {
  const fonts = [new FontFace("CoverAnton", `url("${data.anton}")`)];
  if (data.inter) fonts.push(new FontFace("CoverInter", `url("${data.inter}")`, { weight: "100 900" }));
  for (const face of fonts) { document.fonts.add(await face.load()); }

  const $ = (id) => document.getElementById(id);
  await Promise.all([
    loadImage($("face"), data.face), loadImage($("game"), data.game),
    loadImage($("fill-top"), data.fillTop), loadImage($("fill-bottom"), data.fillBottom),
    loadImage($("cutout"), data.cutout), loadImage($("mark"), data.boxText ? "" : data.mark),
    loadImage($("box-mark"), data.boxText ? data.mark : ""),
  ]);

  const result = { size: 0, capHeight: 0, fits: true, boxSize: 0 };
  const line = $("line");
  const shade = $("shade");
  if (data.lines && data.lines.length && !data.boxText) {
    const key = (data.keyWord || "").toUpperCase();
    for (const text of data.lines) {
      const row = document.createElement("span");
      row.className = "row";
      text.split(" ").forEach((word, i) => {
        if (i) row.appendChild(document.createTextNode(" "));
        const span = document.createElement("span");
        span.textContent = word;
        if (key && word.replace(/[^A-Z0-9*']/g, "") === key.replace(/[^A-Z0-9*']/g, "")) span.className = "key";
        row.appendChild(span);
      });
      line.appendChild(row);
    }
    Object.assign(result, fitLine(line));
    const height = line.getBoundingClientRect().height;
    let top = Math.round(data.lineY - height / 2);
    if (data.clamp !== false) top = Math.min(Math.max(top, SAFE_TOP + 10), SAFE_BOTTOM - 10 - height);
    line.style.top = `${top}px`;
    shade.style.top = `${top - 90}px`;
    shade.style.height = `${height + 180}px`;
  } else {
    shade.classList.add("off");
  }
  if (data.boxText) {
    const box = $("box");
    box.classList.add("on");
    $("box-text").textContent = data.boxText;
    result.boxSize = fitBox($("box-text"));
    box.style.top = `${Math.round(data.lineY - box.getBoundingClientRect().height / 2)}px`;
  }
  await document.fonts.ready;
  const text = data.boxText ? $("box") : (line.childElementCount ? line : null);
  if (text) {
    const r = text.getBoundingClientRect();
    result.textRect = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  }
  return result;
}
