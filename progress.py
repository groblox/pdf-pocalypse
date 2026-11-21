"""Progress, run log and Claude cost for a batch run.

  - before the run: an estimate of the Claude calls the batch needs and their list-price cost,
    from the images, pages and caches of each document and the average cost per call so far;
  - during: a progress bar line per document (documents done, size-weighted ETA, calls and
    cost so far), also shown in the Terminal window's title;
  - after each document: a line with its time, Claude calls and cost, validator result and
    content review, and a JSON line in logs/<batch>.runs.jsonl (kept across runs; work/ can be
    deleted);
  - at the end: validation and content-review totals, the documents that need attention, where
    the files are, and (with A11Y_NOTIFY=1) a macOS notification;
  - logs/claude_usage.json: running totals that make the next estimate better.

Colour is used when stdout is a terminal, or with A11Y_COLOR=1 (the .command pipes through tee
and strips it from the log afterwards); NO_COLOR turns it off.

Costs are list-price equivalents reported by Claude Code. On a Claude subscription nothing is
billed per call, but the calls count against the plan's usage limits.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pymupdf

from haiku import usage_totals

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
USAGE = LOGS / "claude_usage.json"
DEFAULT_COST_PER_CALL = 0.0008   # observed 2026-10-29/30: $0.0004–0.0013 per call (drawings cost more)
MIN_AREA_PT2, MIN_SIDE_PX = 18 * 18, 33
SCAN_PAGE_LIMIT = 300            # count images on at most this many pages, then extrapolate

COLOR = not os.environ.get("NO_COLOR") and (os.environ.get("A11Y_COLOR") == "1" or sys.stdout.isatty())


def _style(code: str):
    return (lambda text: f"\033[{code}m{text}\033[0m") if COLOR else str


bold, dim, green, yellow, red = _style("1"), _style("2"), _style("32"), _style("33"), _style("31")
PAD = " " * 18                   # the summary's value column
REVIEW = {"pass": green("pass"), "review": yellow("needs a look"), "fail": red("fail")}


def cost_per_call() -> float:
    try:
        u = json.loads(USAGE.read_text())
        return u["cost_usd"] / u["calls"] if u.get("calls", 0) >= 50 else DEFAULT_COST_PER_CALL
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return DEFAULT_COST_PER_CALL


def _images(path: Path) -> tuple[int, int]:
    """(distinct describable images, pages) — an upper bound on image calls: tiles that form one
    picture are described once, so real counts are often lower."""
    try:
        with pymupdf.open(path) as doc:
            n = doc.page_count
            step = max(1, -(-n // SCAN_PAGE_LIMIT))
            xrefs = set()
            for i in range(0, n, step):
                for info in doc[i].get_image_info(xrefs=True):
                    r = pymupdf.Rect(info["bbox"])
                    if info.get("xref") and r.width * r.height >= MIN_AREA_PT2 \
                            and min(info["width"], info["height"]) >= MIN_SIDE_PX:
                        xrefs.add(info["xref"])
            return len(xrefs) * step, n
    except Exception:
        return 0, 0


def estimate(docs: dict, source_dir: Path, work: Path, force: bool, stem, wants_companion, intent_fresh,
             stages: set) -> dict:
    """Expected Claude calls per document, skipping work the caches already hold (a content
    review is redone when the remediated file it judged is out of date: intent_fresh(name)) and
    stages this run leaves out."""
    calls, per_doc = 0, {}
    for name, meta in docs.items():
        src = source_dir / name
        if not src.exists():
            continue
        n = 0
        if "remediate" not in stages:  # no new descriptions; at most the content review
            n = int("intent" in stages and src.suffix.lower() != ".docx" and (force or not intent_fresh(name)))
            if n:
                per_doc[name] = n
                calls += n
            continue
        # image and sheet descriptions are cached even with --force (delete the cache to redo them)
        if src.suffix.lower() == ".docx":
            if not (work / "docx" / f"{stem(name)}.images.json").exists():
                import zipfile
                with zipfile.ZipFile(src) as z:
                    n += sum(1 for f in z.namelist() if f.startswith("word/media/"))
            n += 1                                   # final alt-text sanity check
            per_doc[name] = n
            calls += n
            continue
        if wants_companion(src, meta):
            cached = work / "companion" / f"{stem(name)}.json"
            if not cached.exists():
                with pymupdf.open(src) as d:
                    n += d.page_count                # one per sheet
            if not (work / "companion" / f"{stem(name)}.summary.txt").exists():
                n += 1                               # the set summary
        else:
            cached = work / "01_images" / f"{stem(name)}.json"
            if not cached.exists():
                images, _ = _images(src)
                n += images
            if not (work / "01_forms" / f"{stem(name)}.json").exists():
                with pymupdf.open(src) as d:
                    n += sum(1 for p in d if p.first_widget)  # one per form page
        if force or not intent_fresh(name):
            n += 1                                   # content review
        per_doc[name] = n
        calls += n
    return {"calls": calls, "cost_usd": calls * cost_per_call(), "per_doc": per_doc}


CONFIRM_CALLS = 300  # the launcher asks before a run this big: a big batch can use up the plan's 5-hour limit


def confirm(est: dict | None) -> bool:
    """With A11Y_CONFIRM=1 (the launcher), ask before a run that needs many Claude calls. Reads and
    writes the terminal itself, since stdout goes through tee and grep."""
    if os.environ.get("A11Y_CONFIRM") != "1" or not est or est["calls"] < CONFIRM_CALLS:
        return True
    time.sleep(0.5)  # let the piped intro lines reach the window first
    try:  # two handles: a terminal isn't seekable, so it can't be opened "r+" in text mode
        with open("/dev/tty", "w") as out, open("/dev/tty") as answer:
            out.write(f"  This run needs up to ~{est['calls']:,} Claude calls. Start? [Y/n] ")
            out.flush()
            return answer.readline().strip().lower() not in ("n", "no")
    except OSError:
        return True


def _save_usage(calls: int, cost: float) -> None:
    """Add to logs/claude_usage.json, the running totals behind the next estimate."""
    if not calls:
        return
    try:
        u = json.loads(USAGE.read_text()) if USAGE.exists() else {"calls": 0, "cost_usd": 0.0}
    except ValueError:
        u = {"calls": 0, "cost_usd": 0.0}
    u["calls"] += calls
    u["cost_usd"] = round(u["cost_usd"] + cost, 4)
    u["updated"] = datetime.now().isoformat(timespec="seconds")
    USAGE.write_text(json.dumps(u, indent=1))


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _bar(fraction: float, width: int = 24) -> str:
    full = int(fraction * width)
    return "█" * full + dim("░" * (width - full))


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def note(label: str, text: str = "") -> None:
    """A stage's detail line under the document being processed."""
    print(dim(f"  {label}: {text}" if text else f"  {label}"), flush=True)


def warn(text: str) -> None:
    print(yellow(f"  ⚠ {text}"), flush=True)


def error(text: str) -> None:
    print(red(f"  ✗ {text}"), flush=True)


def counts(stats: dict) -> str:
    """{'forms_split': 16, 'th_scoped': 0, 'embedded_Arial': 1, …} → '16 forms split, embedded 1 font
    (Arial)': zeros and non-counts left out, embedded fonts summed."""
    fonts = [k.removeprefix("embedded_").replace("_", " ") for k, v in stats.items()
             if k.startswith("embedded_") and v]
    parts = [f"embedded {_plural(len(fonts), 'font')} ({', '.join(fonts[:4])}{', …' if len(fonts) > 4 else ''})"] \
        if fonts else []
    for k, v in stats.items():
        if k.startswith("embedded_") or isinstance(v, bool) or not isinstance(v, (int, float)) or not v:
            continue
        parts.append(f"{v:,} {k.replace('_', ' ')}")
    return ", ".join(parts) or "nothing to change"


# ---------------------------------------------------------------------------
# Live bar: the document being processed, pinned to the terminal's bottom line
# ---------------------------------------------------------------------------
# Stages of one document and their share of its time; stage() moves the bar to a stage's start,
# and within a stage it creeps on (or follows sub()) so it never sits still.
PLANS = {
    "pdf": [("preparing", 1), ("images", 2), ("tagging", 3), ("structure", 1), ("review", 1),
            ("validating", 2), ("checking", 1), ("report", 1)],
    "set": [("preparing", 1), ("sheets", 6), ("tagging", 1), ("review", 1), ("validating", 2),
            ("checking", 1), ("report", 1)],
    "docx": [("word", 3), ("report", 1)],
}
STAGE_TEXT = {"preparing": "text and fonts", "images": "describing images", "tagging": "tagging",
              "structure": "repairing structure", "review": "content review", "validating": "veraPDF",
              "checking": "checking against the original", "report": "report", "sheets": "describing sheets",
              "word": "Word fixes"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _Live:
    """The bottom terminal line, kept out of the scrolling region and redrawn four times a second
    by a thread. It writes to the terminal itself (/dev/tty), so it works while stdout goes
    through the launcher's tee and grep, whose lines scroll above it."""

    def __init__(self, tty):
        self.tty, self.lock = tty, threading.Lock()
        self.rows = 0
        self.name, self.plan, self.key = "", PLANS["pdf"], None
        self.t_doc = self.t_stage = time.time()
        self.sub_frac: float | None = None
        self.active, self.closed = False, False
        threading.Thread(target=self._loop, daemon=True).start()
        atexit.register(self.close)

    def _write(self, text: str) -> None:
        try:
            self.tty.write(text)
            self.tty.flush()
        except (OSError, ValueError):
            self.closed = True

    def _region(self) -> None:
        """Reserve the bottom line (again, after a resize)."""
        rows = os.get_terminal_size(self.tty.fileno()).lines
        if rows == self.rows or rows < 5:
            return
        if self.rows:  # the old bar line becomes part of the scrolling area
            self._write(f"\0337\033[{self.rows};1H\033[2K\0338")
        self.rows = rows
        # make room (the cursor may be on the last line), then the region; setting it homes the
        # cursor, so save and restore around it
        self._write(f"\n\033[1A\0337\033[1;{rows - 1}r\0338")

    def begin(self, name: str) -> None:
        with self.lock:
            self.name, self.plan, self.key, self.sub_frac = name, PLANS["pdf"], None, None
            self.t_doc = self.t_stage = time.time()
            self.active = True

    def set_plan(self, kind: str) -> None:
        with self.lock:
            self.plan = PLANS.get(kind, PLANS["pdf"])

    def stage(self, key: str) -> None:
        with self.lock:
            if key != self.key:
                self.key, self.t_stage, self.sub_frac = key, time.time(), None

    def sub(self, frac: float) -> None:
        with self.lock:
            self.sub_frac = max(0.0, min(1.0, frac))

    def idle(self) -> None:
        with self.lock:
            self.active = False
        self._write(f"\0337\033[{self.rows};1H\033[2K\0338" if self.rows else "")

    def _fraction(self) -> float:
        total = sum(w for _, w in self.plan)
        keys = [k for k, _ in self.plan]
        if self.key not in keys:
            return 0.9 * (1 - math.exp(-(time.time() - self.t_doc) / 60))
        i = keys.index(self.key)
        start, width = sum(w for _, w in self.plan[:i]) / total, self.plan[i][1] / total
        inner = self.sub_frac if self.sub_frac is not None else \
            0.9 * (1 - math.exp(-(time.time() - self.t_stage) / 25))  # creeps toward the stage's end
        return start + width * inner

    def _loop(self) -> None:
        tick = 0
        while not self.closed:
            time.sleep(0.25)
            tick += 1
            with self.lock:
                if not self.active:
                    continue
                try:
                    self._region()
                    cols = os.get_terminal_size(self.tty.fileno()).columns
                except OSError:
                    continue
                frac = self._fraction()
                what = STAGE_TEXT.get(self.key, "starting")
                tail = f"  {what} · {_fmt(time.time() - self.t_doc)}"
                width = max(10, min(30, cols // 4))
                full = int(frac * width)
                name = self.name
                room = cols - width - len(tail) - 10
                if len(name) > room:
                    name = name[:max(room - 1, 8)] + "…"
                line = (f" {SPIN[tick % len(SPIN)]} \033[32m{'█' * full}\033[2m{'░' * (width - full)}\033[0m "
                        f"{frac:4.0%}  \033[1m{name}\033[0m\033[2m{tail}\033[0m")
                # autowrap off while drawing: a line too long for the window must not spill over
                self._write(f"\0337\033[?7l\033[{self.rows};1H\033[2K{line}\033[?7h\0338")

    def close(self) -> None:
        if self.closed:
            return
        with self.lock:
            self.active = False
            if self.rows:  # clear the bar, give the whole screen back
                self._write(f"\0337\033[{self.rows};1H\033[2K\033[r\0338")
            self.closed = True


_live: _Live | None = None


def plan(kind: str) -> None:
    """The kind of document now being processed ("pdf", "set" for a drawing set, "docx")."""
    if _live:
        _live.set_plan(kind)


def stage(key: str) -> None:
    """The current document has reached this stage (a key of PLANS)."""
    if _live:
        _live.stage(key)


def sub(frac: float) -> None:
    """How far through the current stage it is, when that is known (images described, sheets read)."""
    if _live:
        _live.sub(frac)


class Run:
    def __init__(self, batch: str, docs: list[tuple[str, Path]], estimate: dict | None = None):
        self.batch, self.docs, self.est = batch, docs, estimate or {}
        self.sizes = {n: (p.stat().st_size if p.exists() else 0) for n, p in docs}
        self.total_bytes = sum(self.sizes.values()) or 1
        self.done_bytes, self.done = 0, 0
        self.results: list[dict] = []
        self.t0 = time.time()
        self.calls0, self.cost0 = usage_totals["calls"], usage_totals["cost_usd"]
        LOGS.mkdir(exist_ok=True)
        self.log = LOGS / f"{batch}.runs.jsonl"
        try:
            self.tty = open("/dev/tty", "w")  # the window title, even when stdout is piped to a log
        except OSError:
            self.tty = None
        global _live
        if self.tty and os.isatty(self.tty.fileno()) and os.environ.get("TERM", "dumb") != "dumb" \
                and os.environ.get("A11Y_LIVE", "1") != "0" and _live is None:
            _live = _Live(self.tty)

    def intro(self, title: str, manifest: Path, out: Path, work: Path, backend: str,
              eval_backend: str | None = None) -> None:
        mb = self.total_bytes / 1e6
        who = {"subscription": "Haiku via your Claude subscription", "api": "Haiku via the API key",
               "none": "no AI (images go to a review CSV)"}.get(backend, backend)
        print(f"\n{bold('PDF accessibility')} · {bold(title)}", flush=True)
        print(f"  {_plural(len(self.docs), 'document')} · {mb:,.0f} MB · AI: {who}")
        if eval_backend:
            print(f"  Evals:    {eval_backend}")
        if self.est:
            if self.est["calls"]:
                print(f"  AI calls: up to ~{self.est['calls']:,} calls"
                      + (f" ≈ ${self.est['cost_usd']:.2f} list price " + dim("(subscription estimate)")
                         if "Claude subscription" in who else ""))
            else:
                print("  AI calls: nothing to ask; image descriptions and reviews are cached")
        print(dim(f"  manifest  {manifest}\n  output    {out}/\n  working   {work}/"), flush=True)

    def _title(self, text: str) -> None:
        if self.tty:
            try:
                self.tty.write(f"\033]0;{text}\007")
                self.tty.flush()
            except OSError:
                pass

    def start(self, i: int, name: str) -> None:
        self.doc_t0, self.doc_calls0, self.doc_cost0 = time.time(), usage_totals["calls"], usage_totals["cost_usd"]
        n = len(self.docs)
        frac = (i - 1) / n                                # the bar counts documents …
        by_size = self.done_bytes / self.total_bytes      # … the ETA goes by size, which tracks time better
        elapsed = time.time() - self.t0
        eta = f" · about {_fmt(elapsed / by_size * (1 - by_size))} left" if by_size > 0.02 and elapsed > 30 else ""
        calls = usage_totals["calls"] - self.calls0
        so_far = f" · AI {_plural(calls, 'call')} so far" if calls else ""
        est = self.est.get("per_doc", {}).get(name)
        print(f"\n{_bar(frac)} {frac:4.0%}  " + dim(f"[{i:>{len(str(n))}}/{n}] {_fmt(elapsed)} elapsed{eta}{so_far}"),
              flush=True)
        print(bold(name) + (dim(f"  (up to {_plural(est, 'AI call')})") if est else ""), flush=True)
        self._title(f"PDF accessibility {i}/{n} — {name}")
        if _live:
            _live.begin(f"[{i}/{n}] {name}")

    def end(self, name: str, row: dict) -> None:
        if _live:
            _live.idle()
        secs = time.time() - self.doc_t0
        calls = usage_totals["calls"] - self.doc_calls0
        cost = usage_totals["cost_usd"] - self.doc_cost0
        self.done += 1
        self.done_bytes += self.sizes.get(name, 0)
        compat = row.get("compat") or {}
        checks = compat.get("checks", {})
        ua = checks.get("verapdf_ua1", {}).get("ok")
        wt = checks.get("verapdf_wtpdf", {}).get("ok")
        ok = row.get("status") == "ok" and compat.get("passed")
        review = (row.get("intent") or {}).get("verdict")
        mark = (yellow("⚠") if review == "fail" else green("✓")) if ok else red("✗")
        parts = [_fmt(secs)]
        if row.get("status") != "ok":
            parts.append(red(row.get("status", "?")))
        elif not compat.get("passed"):
            failing = [k for k, c in checks.items() if not c["ok"]]
            parts.append(red(f"held: {', '.join(failing)}" if failing else "not validated"))
        if calls:
            parts.append(f"{_plural(calls, 'AI call')}" + (f" " + dim(f"≈ ${cost:.3f}") if cost else ""))
        elif row.get("cached"):
            parts.append(dim("cached"))
        if "verapdf_ua1" in checks:
            parts.append(f"PDF/UA {green('✓') if ua else red('✗')} WTPDF {green('✓') if wt else red('✗')}")
        if review:
            parts.append(f"review: {REVIEW.get(review, review)}")
        print(f"  {mark} " + " · ".join(parts), flush=True)
        self.results.append({"name": name, "ok": bool(ok), "status": row.get("status"), "review": review,
                             "failing": [k for k, c in checks.items() if not c["ok"]]})
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "batch": self.batch, "name": name,
                 "status": row.get("status"), "passed": bool(ok), "seconds": round(secs, 1),
                 "claude_calls": calls, "cost_usd": round(cost, 4), "review": review}
        with self.log.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _save_usage(calls, cost)  # per document, so a stopped run still counts

    def finish(self, out: Path, findings: dict[str, int] | None = None) -> None:
        if _live:
            _live.close()
        secs = time.time() - self.t0
        calls = usage_totals["calls"] - self.calls0
        cost = usage_totals["cost_usd"] - self.cost0
        res = self.results
        missing = len(self.docs) - self.done
        passed = sum(r["ok"] for r in res)
        reviews = {v: [r["name"] for r in res if r["review"] == v] for v in ("pass", "review", "fail")}
        claude = (f"AI: {_plural(calls, 'call')}" + (f" ≈ ${cost:.2f} list price" if cost else "") if calls
                  else "AI: no calls" + (" (all cached)" if self.est else ""))
        print(f"\n{_bar(1.0)} 100%  " + dim(f"{_plural(self.done, 'document')} in {_fmt(secs)} · {claude}"))
        print(f"\n{bold('Done')} · {self.batch}")
        line = f"{passed} of {len(res)} pass PDF/UA-1 and WTPDF 1.0"
        print(f"  Validation      {green(line) if passed == len(res) else yellow(line)}")
        for r in res:
            if not r["ok"]:
                why = r["status"] if r["status"] != "ok" else \
                    "held in “needs review”: " + ", ".join(r["failing"]) if r["failing"] else "not checked yet"
                print(red(f"{PAD}✗ {r['name']}") + dim(f" — {why}"))
        if missing:
            print(red(f"{PAD}{missing} document(s) missing from the source folder"))
        if any(reviews.values()):
            print("  Content review  " + " · ".join(
                f"{len(reviews[v])} {REVIEW[v]}" for v in ("pass", "review", "fail") if reviews[v]))
            for n in reviews["fail"]:
                print(red(f"{PAD}⚠ {n}"))
        if findings and any(findings.values()):
            print("  Findings        " + " · ".join(f"{n} {k}" for k, n in findings.items() if n)
                  + dim("  (summary.pdf)"))
        print(dim(f"\n  Deliverables    {out}/"))
        if os.environ.get("A11Y_LOG"):
            print(dim(f"  Full log        {ROOT / os.environ['A11Y_LOG']}"))
        print(dim(f"  Run history     {self.log}"), flush=True)
        self._title(f"PDF accessibility — done: {self.batch}")
        if os.environ.get("A11Y_NOTIFY") == "1":
            text = f"{passed}/{len(res)} pass" + (f" · {len(reviews['fail'])} review fail" if reviews["fail"] else "") \
                + f" · {_fmt(secs)}"
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                            "-e", "end run", text, f"PDF accessibility: {self.batch}"], capture_output=True)
