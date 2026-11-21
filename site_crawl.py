"""Find where each PDF lives on a website: a polite, bounded crawl (robots.txt, one request
per `delay` seconds, stays under `prefix`) that records every link to a PDF with the page
it is on, its link text and the text around it. Feeds the intent check for document sets
that, unlike a single bid posting, are spread across many pages."""
from __future__ import annotations

import re
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib import robotparser
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import requests

UA = "Mozilla/5.0 (a11y-pipeline; accessibility remediation crawl)"


class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer"}
    BLOCK = {"p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.title, self.links, self.blocks = "", [], [""]
        self._skip, self._in_title, self._href, self._text = 0, False, None, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self._href, self._text = dict(attrs).get("href"), []
        if tag in self.BLOCK:
            self.blocks.append("")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split()), len(self.blocks) - 1))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.blocks[-1] += data
        if self._href is not None:
            self._text.append(data)


def crawl(start: str, prefix: str, max_pages: int = 300, delay: float = 0.5) -> dict:
    """→ {"pages": {url: {"title", "text"}}, "pdfs": {filename_lower: [{page, page_title, link_text, context, href}]}}"""
    robots = robotparser.RobotFileParser(urljoin(start, "/robots.txt"))
    try:
        robots.read()
    except Exception:
        robots = None
    seen, queue = {start}, deque([start])
    pages, pdfs = {}, {}
    session = requests.Session()
    session.headers["User-Agent"] = UA
    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if robots and not robots.can_fetch(UA, url):
            continue
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException:
            continue
        time.sleep(delay)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            continue
        p = _Page()
        p.feed(r.text)
        blocks = [" ".join(b.split()) for b in p.blocks]
        text = "\n".join(b for b in blocks if b)
        pages[r.url] = {"title": " ".join(p.title.split()), "text": text[:20000]}
        for href, link_text, block_i in p.links:
            target, _ = urldefrag(urljoin(r.url, href))
            path = urlparse(target).path
            if path.lower().endswith(".pdf"):
                context = " ".join(b for b in blocks[max(0, block_i - 1): block_i + 2] if b)[:600]
                name = unquote(PurePosixPath(path).name).lower()
                pdfs.setdefault(name, []).append({"page": r.url, "page_title": pages[r.url]["title"],
                                                  "link_text": link_text, "context": context, "href": target})
            elif target.startswith(prefix) and target not in seen and not re.search(
                    r"\.(jpg|jpeg|png|gif|zip|docx?|xlsx?|pptx?|mp4|ics)$", path, re.I):
                seen.add(target)
                queue.append(target)
    for name, refs in pdfs.items():  # one entry per (page, link); links with text first
        unique = {(r["page"], r["href"], r["link_text"]): r for r in refs}
        pdfs[name] = sorted(unique.values(), key=lambda r: (not r["link_text"], -len(r["context"])))
    return {"pages": pages, "pdfs": pdfs, "crawled": len(pages), "start": start}
