"""Print a PDF's structure tree with MCIDs and alt text. usage: .venv/bin/python scripts/struct_tree.py <pdf>"""
import sys, re, pikepdf, pymupdf
path = sys.argv[1]
pdf = pikepdf.open(path)
doc = pymupdf.open(path)
page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
# MCID -> text per page, from pymupdf's marked-content-aware extraction is not available; use content stream
def mcid_text(i):
    page = pdf.pages[i]; out = {}; stack = []; cur = None
    props = page.get('/Resources', {}).get('/Properties', {})
    for ins in pikepdf.parse_content_stream(page):
        op = str(ins.operator)
        if op == 'BDC':
            d = ins.operands[1]; d = props.get(d) if isinstance(d, pikepdf.Name) else d
            stack.append(int(d.MCID) if isinstance(d, pikepdf.Dictionary) and '/MCID' in d else None)
        elif op == 'BMC': stack.append(None)
        elif op == 'EMC' and stack: stack.pop()
        elif op in ('Tj', 'TJ', "'", '"'):
            m = next((x for x in reversed(stack) if x is not None), None)
            if m is None: continue
            ops = ins.operands[-1]
            parts = [ops] if not isinstance(ops, pikepdf.Array) else [x for x in ops if isinstance(x, pikepdf.String)]
            out[m] = out.get(m, 0) + sum(len(bytes(x)) for x in parts)
    return out
cache = {}
def show(e, d, pg):
    if not isinstance(e, pikepdf.Dictionary) or '/S' not in e:
        return
    pg = e.get('/Pg', pg)
    kids = list(e.K) if isinstance(e.get('/K'), pikepdf.Array) else ([e.K] if '/K' in e else [])
    mc = [k for k in kids if isinstance(k, int)]
    info = ''
    if mc and pg is not None:
        i = page_index[pg.objgen]
        cache.setdefault(i, mcid_text(i))
        info = f' mcids={mc[:6]}{"…" if len(mc) > 6 else ""} bytes={sum(cache[i].get(m, 0) for m in mc)}'
    alt = f' alt="{str(e.Alt)[:40]}"' if '/Alt' in e else ''
    print('  ' * d + str(e.S) + info + alt)
    for k in kids:
        show(k, d + 1, pg)
for k in (pdf.Root.StructTreeRoot.K if isinstance(pdf.Root.StructTreeRoot.K, pikepdf.Array) else [pdf.Root.StructTreeRoot.K]):
    show(k, 0, None)
