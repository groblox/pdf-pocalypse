#!/usr/bin/env python3
"""Files attached to a remediated PDF: companion documents such as a spreadsheet version of a
table, a sheet index, or an author's written description.

They are embedded the way PDF/UA and WTPDF want: a file specification with /F, /UF and a
description (/Desc), declared as an associated file of the document (/AF, relationship
Supplement). Viewers list them in their Attachments panel. An attached PDF also gets a bookmark
that opens it, because bookmarks are what screen-reader users navigate by. Attached files are
not validated themselves, so attach accessible ones (a PDF without tags is flagged).

Attachments are listed per document in the manifest. The files are copied into
attachments/<batch>/<document>/, so a file later deleted from Downloads doesn't break a rerun:

  Drawings.pdf:
    kind: drawing
    attachments:
    - file: attachments/<batch>/Drawings/Sheet index.xlsx
      description: Sheet index as a spreadsheet

Add and remove them interactively (the launcher's "Attach files" option):

    .venv/bin/python attachments.py manifests/<batch>.yaml
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pikepdf
import yaml

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "attachments"
LARGE_MB = 25  # an attachment this big makes the posted PDF noticeably slower to download


def slug(text: str) -> str:  # the batch folder name, as a11y_pipeline.slug
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ---------------------------------------------------------------------------
# What a document should carry
# ---------------------------------------------------------------------------
def _path(p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path


def listed(meta: dict) -> list[dict]:
    """The manifest's `attachments:` as {file, name, description}. An entry is a path, or a
    mapping with `file` and an optional `description` (default: the file name)."""
    out = []
    for item in meta.get("attachments") or []:
        entry = {"file": item} if isinstance(item, str) else dict(item or {})
        if not entry.get("file"):
            continue
        path = _path(str(entry["file"]))
        out.append({"file": path, "name": path.name,
                    "description": str(entry.get("description") or _humane(path.stem))})
    return out


def signature(meta: dict, project: dict) -> str:
    """What the attachments depend on, for the pipeline's fingerprints ("" when none)."""
    items = [(i["name"], i["description"], _sig(i["file"])) for i in listed(meta)]
    return json.dumps(items) if items else ""


def _sig(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{int(st.st_mtime)}"


def _humane(stem: str) -> str:
    return " ".join(re.sub(r"[_]+", " ", stem).split())


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
def is_tagged(data: bytes) -> bool | None:
    """Whether a PDF carries a structure tree (None: not a readable PDF)."""
    try:
        import io
        with pikepdf.open(io.BytesIO(data)) as pdf:
            return "/StructTreeRoot" in pdf.Root and bool(pdf.Root.get("/MarkInfo", {}).get("/Marked", False))
    except Exception:
        return None


def embed(src: Path, dst: Path, items: list[dict]) -> dict:
    """Write src with the items embedded to dst (dst may be src). Returns what was attached:
    {"files": [{name, description, bytes, tagged}], "bookmarks": n, "not_found": [names]}."""
    found = [i for i in items if i["file"].exists()]
    result = {"files": [], "bookmarks": 0, "not_found": [i["name"] for i in items if not i["file"].exists()]}
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        names = {i["name"] for i in found}
        af = [f for f in pdf.Root.get("/AF", []) if str(f.get("/UF", f.get("/F", ""))) not in names]
        for item in found:
            data = item["file"].read_bytes()
            mime = mimetypes.guess_type(item["name"])[0] or "application/octet-stream"
            pdf.attachments[item["name"]] = pikepdf.AttachedFileSpec(
                pdf, data, description=item["description"], filename=item["name"], mime_type=mime,
                relationship=pikepdf.Name.Supplement)
            af.append(pdf.attachments[item["name"]].obj)
            pdf_file = item["name"].lower().endswith(".pdf")
            result["files"].append({"name": item["name"], "description": item["description"], "bytes": len(data),
                                    "tagged": is_tagged(data) if pdf_file else None})
        if af:
            pdf.Root.AF = pikepdf.Array(af)
        # a bookmark per attached PDF, opening it (GoToE); earlier ones for these files are replaced
        pdfs = [f for f in result["files"] if f["name"].lower().endswith(".pdf")]
        with pdf.open_outline() as outline:
            def ours(item) -> bool:
                action = item.action
                return (action is not None and action.get("/S") == "/GoToE"
                        and str((action.get("/T") or {}).get("/N", "")) in names)
            outline.root[:] = [item for item in outline.root if not ours(item)]
            for f in pdfs:
                action = pikepdf.Dictionary(S=pikepdf.Name.GoToE, NewWindow=True, D=pikepdf.Array([0, pikepdf.Name.Fit]),
                                            T=pikepdf.Dictionary(R=pikepdf.Name.C, N=pikepdf.String(f["name"])))
                outline.root.append(pikepdf.OutlineItem(f"Attached: {f['description']} ({f['name']})", action=action))
                result["bookmarks"] += 1
        pdf.save(dst)
    return result


def embedded(pdf: pikepdf.Pdf) -> dict[str, dict]:
    """Attached files in an open PDF: name → {bytes (as stored), description}."""
    out = {}
    for name, spec in pdf.attachments.items():
        stream = spec.obj.get("/EF", {}).get("/F")
        out[name] = {"bytes": int(stream.get("/Length", 0)) if stream is not None else 0,
                     "description": str(spec.obj.get("/Desc", ""))}
    return out


# ---------------------------------------------------------------------------
# Manifest editing (keeps the file's comments and layout)
# ---------------------------------------------------------------------------
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _meaningful(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _entry_key(line: str) -> str | None:
    """The document a "  name:" line starts, or None (a value line, or PyYAML's two-line "? long
    name" form for names over 128 characters, which only the whole-file rewrite can edit)."""
    try:
        parsed = yaml.safe_load(line.strip())
    except yaml.YAMLError:
        return None
    return next(iter(parsed)) if isinstance(parsed, dict) and len(parsed) == 1 and None in parsed.values() else None


def set_entry(manifest: Path, doc: str, updates: dict) -> None:
    """Set (or, with None, remove) keys of one document's entry, editing only those lines. The
    edit is checked by reading the file back; if that fails (an unusual layout), the manifest
    is rewritten whole and the old one kept as <manifest>.bak."""
    text = manifest.read_text()
    data = yaml.safe_load(text)
    entry = data["documents"].get(doc)
    if entry is None:
        raise KeyError(f"{doc} is not in {manifest.name}")
    expected = dict(entry)
    for key, value in updates.items():
        if value is None:
            expected.pop(key, None)
        else:
            expected[key] = value
    lines = text.splitlines()
    new_text = None
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "documents:")
        at = next(i for i in range(start + 1, len(lines)) if _indent(lines[i]) == 2 and _meaningful(lines[i])
                  and _entry_key(lines[i]) == doc)
        end = next((i for i in range(at + 1, len(lines)) if _meaningful(lines[i]) and _indent(lines[i]) <= 2), len(lines))
        # blank lines and comments before the next entry belong to it
        while end > at + 1 and (not lines[end - 1].strip() or (lines[end - 1].lstrip().startswith("#")
                                                                 and _indent(lines[end - 1]) <= 2)):
            end -= 1
        block = lines[at + 1:end]
        for key in updates:  # drop the keys being set
            out, skipping = [], False
            for ln in block:
                if _indent(ln) == 4 and re.match(rf"^ {{4}}{re.escape(key)}:", ln):
                    skipping = True
                    continue
                if skipping and (not ln.strip() or _indent(ln) > 4 or (_indent(ln) == 4 and ln.lstrip().startswith("- "))):
                    continue
                skipping = False
                out.append(ln)
            block = out
        add = [f"    {ln}" for key, value in updates.items() if value is not None
               for ln in yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=106).splitlines()]
        new_lines = lines[:at + 1] + block + add + lines[end:]
        new_text = "\n".join(new_lines) + "\n"
        if yaml.safe_load(new_text) != {**data, "documents": {**data["documents"], doc: expected}}:
            new_text = None
    except (StopIteration, yaml.YAMLError):
        new_text = None
    if new_text is None:
        shutil.copy2(manifest, manifest.with_suffix(manifest.suffix + ".bak"))
        data["documents"][doc] = expected
        new_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)
        print(f"  (rewrote {manifest.name} whole; the previous version is {manifest.name}.bak)")
    manifest.write_text(new_text)


def add(manifest: Path, doc: str, files: list[tuple[Path, str]]) -> list[str]:
    """Copy files into attachments/<batch>/<document>/ and list them on the document (a file
    of the same name replaces the earlier one). Returns the stored paths."""
    data = yaml.safe_load(manifest.read_text())
    folder = STORE / slug(data["project"]["name"]) / Path(doc).stem
    folder.mkdir(parents=True, exist_ok=True)
    current = [{"file": str(e["file"]), "description": e.get("description")}
               for e in (dict(x) if isinstance(x, dict) else {"file": x}
                         for x in data["documents"][doc].get("attachments") or []) if e.get("file")]
    stored = []
    for path, description in files:
        dest = folder / path.name
        if path.resolve() != dest.resolve():
            shutil.copy2(path, dest)
        rel = str(dest.relative_to(ROOT))
        current = [e for e in current if Path(e["file"]).name != path.name]
        current.append({"file": rel, "description": description or _humane(path.stem)})
        stored.append(rel)
    set_entry(manifest, doc, {"attachments": [{k: v for k, v in e.items() if v} for e in current]})
    return stored


def remove(manifest: Path, doc: str, name: str) -> bool:
    data = yaml.safe_load(manifest.read_text())
    items = data["documents"][doc].get("attachments") or []
    kept = [x for x in items if Path(x if isinstance(x, str) else x.get("file", "")).name != name]
    if len(kept) == len(items):
        return False
    set_entry(manifest, doc, {"attachments": kept or None})
    stored = STORE / slug(data["project"]["name"]) / Path(doc).stem / name
    stored.unlink(missing_ok=True)  # the copy add() made; files kept elsewhere are left alone
    return True


# ---------------------------------------------------------------------------
# Interactive
# ---------------------------------------------------------------------------
def _choose_files() -> list[Path]:
    """macOS file chooser (several files allowed); typed paths when it isn't available or the
    input isn't a terminal (a script)."""
    script = ['set fs to choose file with prompt "Choose the file(s) to attach:" with multiple selections allowed',
              'set out to ""', "repeat with f in fs", "set out to out & POSIX path of f & linefeed", "end repeat",
              "return out"]
    if sys.stdin.isatty():
        try:
            r = subprocess.run(["osascript"] + [a for line in script for a in ("-e", line)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return [Path(p) for p in r.stdout.splitlines() if p.strip()]
            if "-128" in r.stderr:  # cancelled
                return []
        except (OSError, subprocess.TimeoutExpired):
            pass
    typed = _ask("  Path(s) of the file(s), separated by spaces (drag them here): ")
    return [Path(p.replace("\\ ", " ")).expanduser() for p in re.findall(r"(?:\\ |[^ ])+", typed)]


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def interactive(manifest: Path) -> set[str]:
    """Menu: pick a PDF, then add files or remove one. Returns the documents whose attachments
    changed."""
    data = yaml.safe_load(manifest.read_text())
    project = data["project"]
    changed: set[str] = set()
    print(f"\nAttach files to PDFs in “{project['name']}”.\n"
          "Attached files travel inside the PDF (the viewer's Attachments panel); an attached PDF also\n"
          "gets a bookmark. Attach accessible files: attachments are not checked themselves.")
    while True:
        docs = yaml.safe_load(manifest.read_text())["documents"]
        names = [n for n in docs if n.lower().endswith(".pdf")]
        print()
        for i, n in enumerate(names, 1):
            extras = []
            if listed(docs[n]):
                extras.append(f"{len(listed(docs[n]))} attached")
            print(f"  {i:>3}  {n}" + (f"   [{', '.join(extras)}]" if extras else ""))
        pick = _ask("Which document? Number, or Return when done: ")
        if not pick:
            return changed
        if not pick.isdigit() or not 1 <= int(pick) <= len(names):
            print("  Type one of the numbers above.")
            continue
        doc = names[int(pick) - 1]
        while True:
            entry = yaml.safe_load(manifest.read_text())["documents"][doc]
            items = listed(entry)
            print(f"\n  {doc}")
            for n, item in enumerate(items, 1):
                missing = "" if item["file"].exists() else "  (file missing!)"
                print(f"    {n}. {item['name']} — {item['description']}{missing}")
            if not items:
                print("    no files attached")
            options = "[a] add files"
            if items:
                options += "   [r] remove one"
            choice = _ask(f"  {options}   [Return] back: ").lower()
            if not choice:
                break
            if choice == "a":
                for path in _choose_files():
                    if not path.is_file():
                        print(f"    not a file: {path}")
                        continue
                    data_bytes = path.read_bytes()
                    if path.suffix.lower() == ".pdf" and is_tagged(data_bytes) is False:
                        print(f"    {path.name} is not tagged, so a screen reader can't read it. Remediate it first\n"
                              "    (put it in a batch folder) and attach the result.")
                        if _ask("    Attach it anyway? [y/N] ").lower() not in ("y", "yes"):
                            continue
                    if len(data_bytes) > LARGE_MB * 1e6:
                        print(f"    note: {path.name} is {len(data_bytes) / 1e6:.0f} MB; the posted PDF grows by that much")
                    default = _humane(path.stem)
                    description = _ask(f"    Description of {path.name} (what it is) [{default}]: ") or default
                    add(manifest, doc, [(path, description)])
                    changed.add(doc)
                    print(f"    attached {path.name}")
            elif choice == "r" and items:
                n = _ask("    Remove which? Number: ")
                if n.isdigit() and 1 <= int(n) <= len(items) and remove(manifest, doc, items[int(n) - 1]["name"]):
                    changed.add(doc)
                    print(f"    removed {items[int(n) - 1]['name']}")
            else:
                print("    Type one of the letters shown.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Attach files to PDFs in a batch (interactive).")
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--changed-out", type=Path, help="write the documents whose attachments changed, one per line")
    args = ap.parse_args()
    try:
        changed = interactive(args.manifest.resolve())
    except KeyboardInterrupt:
        print()
        changed = set()
    if args.changed_out:
        args.changed_out.write_text("".join(f"{n}\n" for n in sorted(changed)))
    if changed:
        print(f"\n{len(changed)} document(s) changed. Rerun them to embed the files"
              + ("." if args.changed_out else ' (launcher: "Process the folder").'))


if __name__ == "__main__":
    sys.exit(main())
