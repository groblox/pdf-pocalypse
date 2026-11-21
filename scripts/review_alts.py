"""Contact sheet of a document's described images + their alt text, for checking by eye.
usage: .venv/bin/python scripts/review_alts.py <batch> "<pdf name>"   (sheet → work/review/)"""
import json, sys, textwrap
from pathlib import Path
import pikepdf, pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "work" / "review"  # contact sheets land here
batch, name = sys.argv[1], sys.argv[2]
stem = Path(name).stem
work = ROOT / "work" / batch
cache = json.loads((work / "01_images" / f"{stem}.json").read_text())
out_pdf = ROOT / "output" / batch / name


# Figures actually in the deliverable
alts = []
with pikepdf.open(out_pdf) as pdf:
    stack, seen = [pdf.Root.StructTreeRoot.get("/K")], set()
    while stack:
        n = stack.pop()
        if isinstance(n, pikepdf.Array):
            stack.extend(reversed(list(n))); continue
        if not isinstance(n, pikepdf.Dictionary): continue
        if n.objgen != (0, 0):
            if n.objgen in seen: continue
            seen.add(n.objgen)
        if n.get("/S") == "/Figure":
            alts.append(str(n.get("/Alt", "(no alt)")))
        stack.append(n.get("/K"))
print(f"{name}: {len(alts)} Figure(s) in deliverable")
units = [(k, v) for k, v in cache.items() if v.get("type") != "minor"]
units.sort(key=lambda kv: (kv[1]["page"], kv[1]["placements"][0][1][1]))
print(f"{len(units)} described unit(s), {sum(1 for v in cache.values() if v.get('type') == 'minor')} minor")
doc = pymupdf.open(work / "01_fonts" / name)
cells = []
for i, (k, v) in enumerate(units, 1):
    page, rect = v["placements"][0]
    clip = pymupdf.Rect(rect) & doc[page].rect
    if clip.is_empty or clip.width < 2 or clip.height < 2:
        continue
    pix = doc[page].get_pixmap(clip=clip, dpi=int(min(110, 300 * 72 / max(clip.width, clip.height))))
    img = Image.frombytes("RGB" if pix.n < 4 else "RGBA", (pix.width, pix.height), pix.samples).convert("RGB")
    img.thumbnail((300, 300))
    cells.append((i, img))
    pages = sorted({p + 1 for p, _ in v["placements"]})
    print(f"[{i}] {k} {v['type']} {v.get('kind')} p{pages[:6]}{'…' if len(pages) > 6 else ''} cov={v.get('coverage')}\n    {v.get('alt') or '(DECORATIVE)'}")
if cells:
    cols = 4
    rows = (len(cells) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 320, rows * 330), "white")
    d = ImageDraw.Draw(sheet)
    for j, (i, img) in enumerate(cells):
        x, y = (j % cols) * 320 + 10, (j // cols) * 330 + 25
        sheet.paste(img, (x, y))
        d.text((x, y - 18), f"[{i}]", fill="red")
    SCRATCH.mkdir(exist_ok=True)
    path = SCRATCH / f"{stem[:40]}.png"
    sheet.save(path)
    print("sheet:", path)
