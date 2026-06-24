import re
import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

USER_AGENT = "Mozilla/5.0 (compatible; ChineseTrackerBot/1.0)"
REQUEST_TIMEOUT = 10

SOURCES = [
    {
        "id": "bbc_zhongwen",
        "name": "BBC Chinese (News)",
        "feed_url": "https://www.bbc.com/zhongwen/simp/index.xml",
        "extractor": "fetch_full_page",
        "difficulty_hint": "native",
    },
    {
        "id": "mandarinbean",
        "name": "Mandarin Bean (Graded Readers)",
        "feed_url": "https://mandarinbean.com/feed/",
        "extractor": "mandarinbean_spans",
        "difficulty_hint": "graded",
    },
    {
        "id": "dw_chinese",
        "name": "DW 中文 (Deutsche Welle)",
        "feed_url": "https://rss.dw.com/rdf/rss-chi-all",
        "extractor": "fetch_full_page",
        "difficulty_hint": "native",
    },
    {
        "id": "rfi_chinese",
        "name": "RFI 中文 (Radio France Int'l)",
        "feed_url": "https://www.rfi.fr/cn/rss",
        "extractor": "fetch_full_page",
        "difficulty_hint": "native",
    },
    {
        "id": "rfa_mandarin",
        "name": "RFA 普通话 (Radio Free Asia)",
        "feed_url": "https://www.rfa.org/mandarin/rss2.xml",
        "extractor": "fetch_full_page",
        "difficulty_hint": "native",
    },
    {
        "id": "cna_taiwan",
        "name": "中央社 CNA (Taiwan)",
        "feed_url": "https://feeds.feedburner.com/rsscna/intworld",
        "extractor": "fetch_full_page",
        "difficulty_hint": "native",
    },
]


def _chinese_ratio(text: str) -> float:
    text = text.strip()
    if not text:
        return 0.0
    chinese = sum(1 for ch in text if "一" <= ch <= "鿿")
    non_space = sum(1 for ch in text if not ch.isspace())
    if non_space == 0:
        return 0.0
    return chinese / non_space


def extract_full_page(url: str) -> str:
    """Generic news-article extractor: grab <p> tags, drop short/non-Chinese noise."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    paragraphs = []
    for p in soup.find_all("p"):
        text = p.get_text().strip()
        if len(text) > 15 and _chinese_ratio(text) > 0.4:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_mandarinbean_spans(html: str) -> str:
    """Mandarin Bean wraps each word in <abbr><span class='si'>simplified</span>
    <span class='tr'>traditional</span>pinyin</abbr>. Pull only the simplified
    spans plus any loose punctuation directly in the paragraph, skipping the
    traditional duplicate and inline pinyin text."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for p in soup.find_all("p"):
        if "appeared first on" in p.get_text():
            continue
        for elem in p.descendants:
            if isinstance(elem, Tag) and elem.name == "span" and "si" in (elem.get("class") or []):
                out.append(elem.get_text())
            elif isinstance(elem, NavigableString):
                parent_name = elem.parent.name if elem.parent else None
                if parent_name not in ("abbr", "span"):
                    text = str(elem).strip()
                    if text:
                        out.append(text)
        out.append("\n")
    return "".join(out).strip()


def fetch_candidates(source_id: str, limit: int = 5, exclude_links=None, scan_cap: int = 40):
    """Return up to `limit` NEW candidates from a source.

    Already-imported entries (links in `exclude_links`) are skipped before the
    limit is applied, so reading everything in the first batch doesn't leave the
    fetcher stuck re-offering the same filtered-out articles. We scan up to
    `scan_cap` feed entries to find enough fresh ones, and only scrape full
    content for entries we'll actually keep."""
    source = next((s for s in SOURCES if s["id"] == source_id), None)
    if not source:
        raise ValueError(f"unknown source: {source_id}")

    exclude_links = exclude_links or set()
    feed = feedparser.parse(source["feed_url"])
    candidates = []
    for entry in feed.entries[:scan_cap]:
        if len(candidates) >= limit:
            break
        title = entry.get("title", "").strip()
        link = entry.get("link", "")
        if link and link in exclude_links:
            continue
        try:
            if source["extractor"] == "fetch_full_page":
                content = extract_full_page(link)
            elif source["extractor"] == "mandarinbean_spans":
                raw_html = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else entry.get("summary", "")
                content = extract_mandarinbean_spans(raw_html)
            else:
                content = ""
        except Exception as e:
            content = ""
            title = f"{title} (fetch failed: {e})"

        if not content.strip():
            continue

        candidates.append({
            "title": title,
            "link": link,
            "source_name": source["name"],
            "content": content,
        })
    return candidates
