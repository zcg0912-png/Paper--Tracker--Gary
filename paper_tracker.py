#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hmac
import html as html_lib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "papers.db"
DEFAULT_JOURNALS = ROOT / "journals.csv"
OPENALEX_URL = "https://api.openalex.org/works"
HBR_FEED_URL = "http://feeds.harvardbusiness.org/harvardbusiness"
TAG_RE = re.compile(r"<[^>]+>")
AUTH_REALM = "Paper Tracker"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def today() -> dt.date:
    return dt.date.today()


def configured_db_path() -> Path:
    value = os.getenv("PAPER_TRACKER_DB") or os.getenv("DATABASE_PATH")
    return Path(value) if value else DEFAULT_DB


def configured_port(default: int = 8765) -> int:
    value = os.getenv("PORT") or os.getenv("PAPER_TRACKER_PORT")
    if not value:
        return default
    return int(value)


def load_journals(path: Path = DEFAULT_JOURNALS) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    journals = []
    for row in rows:
        name = (row.get("name") or "").strip()
        issn = (row.get("issn") or "").strip()
        if name and issn:
            journals.append({"name": name, "issn": issn})
    return journals


def init_db(db_path: Path = DEFAULT_DB) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS papers (
                paper_key TEXT PRIMARY KEY,
                openalex_id TEXT,
                doi TEXT,
                title TEXT NOT NULL,
                authors TEXT,
                abstract TEXT,
                journal TEXT NOT NULL,
                journal_issn TEXT NOT NULL,
                publication_date TEXT,
                year INTEGER,
                article_url TEXT,
                source_url TEXT,
                source_updated_at TEXT,
                fetched_at TEXT NOT NULL,
                raw_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_journal ON papers(journal)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_date ON papers(publication_date)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi)")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    value = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.strip()


def doi_url(doi: str | None) -> str:
    normalized = normalize_doi(doi)
    return f"https://doi.org/{normalized}" if normalized else ""


def invert_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return " ".join(word for _, word in sorted(words))


def openalex_params(params: dict[str, str | int]) -> dict[str, str | int]:
    merged = dict(params)
    api_key = os.getenv("OPENALEX_API_KEY")
    mailto = os.getenv("OPENALEX_MAILTO") or os.getenv("CONTACT_EMAIL")
    if api_key:
        merged["api_key"] = api_key
    elif mailto:
        merged["mailto"] = mailto
    return merged


def request_json(url: str, params: dict[str, str | int]) -> dict:
    query = urllib.parse.urlencode(openalex_params(params))
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": os.getenv(
                "PAPER_TRACKER_USER_AGENT", "paper-tracker/0.1"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAlex HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAlex request failed: {exc}") from exc


def request_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/xml, text/xml, */*",
            "User-Agent": os.getenv(
                "PAPER_TRACKER_USER_AGENT", "paper-tracker/0.1"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Feed request failed: {exc}") from exc


def request_remote_fetch(base_url: str, secret: str) -> dict:
    if not base_url:
        raise ValueError("PAPER_TRACKER_PUBLIC_URL is required")
    if not secret:
        raise ValueError("PAPER_TRACKER_CRON_SECRET is required")
    query = urllib.parse.urlencode(
        {
            "days": os.getenv("PAPER_TRACKER_CRON_DAYS", "14"),
            "per_page": os.getenv("PAPER_TRACKER_CRON_PER_PAGE", "50"),
            "pages": os.getenv("PAPER_TRACKER_CRON_PAGES", "2"),
        }
    )
    url = f"{base_url.rstrip('/')}/api/cron/fetch?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": os.getenv(
                "PAPER_TRACKER_USER_AGENT", "paper-tracker/0.1"
            ),
            "X-Cron-Secret": secret,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Remote fetch HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Remote fetch request failed: {exc}") from exc


def clean_html(value: str | None) -> str:
    text = TAG_RE.sub(" ", value or "")
    return " ".join(html_lib.unescape(text).split())


def parse_feed_date(value: str) -> str:
    if not value:
        return ""
    return value[:10]


def parse_openalex_work(work: dict, journal: dict[str, str]) -> dict[str, str | int]:
    doi = normalize_doi(work.get("doi"))
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    article_url = (
        primary_location.get("landing_page_url")
        or primary_location.get("pdf_url")
        or doi_url(doi)
        or work.get("id")
        or ""
    )
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            authors.append(name)
    openalex_id = work.get("id") or ""
    paper_key = f"doi:{doi}" if doi else f"openalex:{openalex_id}"
    return {
        "paper_key": paper_key,
        "openalex_id": openalex_id,
        "doi": doi,
        "title": work.get("title") or "Untitled",
        "authors": "; ".join(authors),
        "abstract": invert_abstract(work.get("abstract_inverted_index")),
        "journal": journal["name"],
        "journal_issn": journal["issn"],
        "publication_date": work.get("publication_date") or "",
        "year": work.get("publication_year") or None,
        "article_url": article_url,
        "source_url": source.get("homepage_url") or "",
        "source_updated_at": work.get("updated_date") or "",
        "fetched_at": utc_now(),
        "raw_json": json.dumps(work, ensure_ascii=False),
    }


def fetch_journal(
    journal: dict[str, str],
    since: str,
    per_page: int,
    pages: int,
    sleep_seconds: float = 0.2,
) -> list[dict[str, str | int]]:
    cursor = "*"
    papers: list[dict[str, str | int]] = []
    for _ in range(pages):
        data = request_json(
            OPENALEX_URL,
            {
                "filter": (
                    f"primary_location.source.issn:{journal['issn']},"
                    f"from_publication_date:{since},type:article"
                ),
                "sort": "publication_date:desc",
                "per-page": per_page,
                "cursor": cursor,
            },
        )
        results = data.get("results") or []
        papers.extend(parse_openalex_work(work, journal) for work in results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not results:
            break
        time.sleep(sleep_seconds)
    return papers


def fetch_hbr_feed(
    journal: dict[str, str],
    since: str,
    limit: int,
) -> list[dict[str, str | int]]:
    feed = request_bytes(HBR_FEED_URL)
    root = ET.fromstring(feed)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers: list[dict[str, str | int]] = []
    for entry in root.findall("atom:entry", ns):
        title = clean_html(entry.findtext("atom:title", default="", namespaces=ns))
        published = entry.findtext("atom:published", default="", namespaces=ns)
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        publication_date = parse_feed_date(published or updated)
        if publication_date and publication_date < since:
            continue
        author_names = [
            clean_html(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        authors = "; ".join(name for name in author_names if name)
        article_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("rel", "alternate") == "alternate":
                article_url = link.attrib.get("href", "")
                break
        if "/sponsored/" in article_url or "/podcast/" in article_url:
            continue
        if not title or not article_url:
            continue
        paper = {
            "paper_key": f"url:{article_url}",
            "openalex_id": "",
            "doi": "",
            "title": title,
            "authors": authors,
            "abstract": clean_html(
                entry.findtext("atom:summary", default="", namespaces=ns)
            ),
            "journal": journal["name"],
            "journal_issn": journal["issn"],
            "publication_date": publication_date,
            "year": int(publication_date[:4]) if publication_date else None,
            "article_url": article_url,
            "source_url": "https://hbr.org/",
            "source_updated_at": parse_feed_date(updated),
            "fetched_at": utc_now(),
            "raw_json": json.dumps(
                {
                    "title": title,
                    "published": published,
                    "updated": updated,
                    "authors": authors,
                    "summary": clean_html(
                        entry.findtext("atom:summary", default="", namespaces=ns)
                    ),
                    "url": article_url,
                    "source": HBR_FEED_URL,
                },
                ensure_ascii=False,
            ),
        }
        papers.append(paper)
        if len(papers) >= limit:
            break
    return papers


def upsert_papers(papers: list[dict[str, str | int]], db_path: Path = DEFAULT_DB) -> dict:
    init_db(db_path)
    inserted = 0
    updated = 0
    with sqlite3.connect(db_path) as conn:
        for paper in papers:
            exists = conn.execute(
                "SELECT 1 FROM papers WHERE paper_key = ?", (paper["paper_key"],)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO papers (
                    paper_key, openalex_id, doi, title, authors, abstract,
                    journal, journal_issn, publication_date, year, article_url,
                    source_url, source_updated_at, fetched_at, raw_json
                ) VALUES (
                    :paper_key, :openalex_id, :doi, :title, :authors, :abstract,
                    :journal, :journal_issn, :publication_date, :year, :article_url,
                    :source_url, :source_updated_at, :fetched_at, :raw_json
                )
                ON CONFLICT(paper_key) DO UPDATE SET
                    openalex_id = excluded.openalex_id,
                    doi = excluded.doi,
                    title = excluded.title,
                    authors = excluded.authors,
                    abstract = CASE
                        WHEN excluded.abstract IS NOT NULL
                             AND trim(excluded.abstract) != ''
                        THEN excluded.abstract
                        ELSE papers.abstract
                    END,
                    journal = excluded.journal,
                    journal_issn = excluded.journal_issn,
                    publication_date = excluded.publication_date,
                    year = excluded.year,
                    article_url = excluded.article_url,
                    source_url = excluded.source_url,
                    source_updated_at = excluded.source_updated_at,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                paper,
            )
            if exists:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "seen": len(papers)}


def fetch_all(
    *,
    days: int,
    per_page: int,
    pages: int,
    db_path: Path = DEFAULT_DB,
    journal_name: str | None = None,
) -> dict:
    journals = load_journals()
    if journal_name:
        wanted = journal_name.casefold()
        journals = [j for j in journals if j["name"].casefold() == wanted]
        if not journals:
            raise ValueError(f"Unknown journal: {journal_name}")

    since = (today() - dt.timedelta(days=days)).isoformat()
    totals = {"inserted": 0, "updated": 0, "seen": 0, "journals": []}
    for journal in journals:
        if journal["name"] == "Harvard Business Review":
            papers = fetch_hbr_feed(journal, since=since, limit=per_page * pages)
        else:
            papers = fetch_journal(
                journal, since=since, per_page=per_page, pages=pages
            )
        result = upsert_papers(papers, db_path=db_path)
        totals["inserted"] += result["inserted"]
        totals["updated"] += result["updated"]
        totals["seen"] += result["seen"]
        totals["journals"].append({"journal": journal["name"], **result})
        print(
            f"{journal['name']}: {result['seen']} seen, "
            f"{result['inserted']} new, {result['updated']} updated",
            flush=True,
        )
    return totals


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def paper_filters(journal: str, q: str, days: int) -> tuple[str, list[str | int]]:
    where = []
    params: list[str | int] = []
    if journal:
        where.append("journal = ?")
        params.append(journal)
    if q:
        like = f"%{q}%"
        where.append("(title LIKE ? OR authors LIKE ? OR abstract LIKE ?)")
        params.extend([like, like, like])
    if days:
        since = (today() - dt.timedelta(days=days)).isoformat()
        where.append("publication_date >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def query_papers(
    *,
    db_path: Path = DEFAULT_DB,
    journal: str = "",
    q: str = "",
    days: int = 0,
    limit: int = 200,
) -> list[dict]:
    init_db(db_path)
    clause, params = paper_filters(journal, q, days)
    sql = f"""
        SELECT paper_key, title, authors, abstract, journal, journal_issn,
               publication_date, year, doi, article_url, fetched_at
        FROM papers
        {clause}
        ORDER BY publication_date DESC, fetched_at DESC
        LIMIT ?
    """
    params.append(limit)
    with sqlite3.connect(db_path) as conn:
        return rows_to_dicts(conn.execute(sql, params))


def count_papers(
    *,
    db_path: Path = DEFAULT_DB,
    journal: str = "",
    q: str = "",
    days: int = 0,
) -> int:
    init_db(db_path)
    clause, params = paper_filters(journal, q, days)
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(f"SELECT COUNT(*) FROM papers {clause}", params).fetchone()[0]
        )


def journal_counts(db_path: Path = DEFAULT_DB) -> list[dict]:
    init_db(db_path)
    journals = load_journals()
    with sqlite3.connect(db_path) as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT journal, COUNT(*) AS paper_count, MAX(publication_date) AS latest
                FROM papers
                GROUP BY journal
                """
            )
        )
    by_name = {row["journal"]: row for row in rows}
    return [
        {
            "name": journal["name"],
            "issn": journal["issn"],
            "paper_count": by_name.get(journal["name"], {}).get("paper_count", 0),
            "latest": by_name.get(journal["name"], {}).get("latest", ""),
        }
        for journal in journals
    ]


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>论文追索</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #17202a;
      --muted: #657386;
      --accent: #0f766e;
      --accent-2: #b45309;
      --focus: rgba(15, 118, 110, 0.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.45;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 18px 24px;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .wrap {
      max-width: 1180px;
      margin: 0 auto;
    }
    .top {
      align-items: center;
      display: flex;
      gap: 16px;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    h1 {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
      margin: 0;
    }
    .status {
      color: var(--muted);
      font-size: 13px;
      min-height: 20px;
      text-align: right;
    }
    .toolbar {
      display: grid;
      gap: 10px;
      grid-template-columns: minmax(220px, 1.2fr) minmax(220px, 1fr) 150px;
    }
    input, select, button {
      border: 1px solid var(--line);
      border-radius: 8px;
      font: inherit;
      min-height: 40px;
      outline: none;
      padding: 8px 10px;
      width: 100%;
    }
    input:focus, select:focus, button:focus {
      box-shadow: 0 0 0 4px var(--focus);
      border-color: var(--accent);
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 650;
    }
    button:disabled {
      cursor: wait;
      opacity: 0.62;
    }
    main {
      padding: 22px 24px 48px;
    }
    .summary {
      align-items: center;
      color: var(--muted);
      display: flex;
      gap: 12px;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .summary strong {
      color: var(--text);
    }
    .paper-list {
      display: grid;
      gap: 12px;
    }
    .paper {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    .paper-meta {
      align-items: center;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      font-size: 13px;
      gap: 8px;
      margin-bottom: 8px;
    }
    .journal {
      color: var(--accent);
      font-weight: 700;
    }
    .date {
      color: var(--accent-2);
      font-weight: 650;
    }
    h2 {
      font-size: 18px;
      letter-spacing: 0;
      line-height: 1.32;
      margin: 0 0 8px;
    }
    h2 a {
      color: var(--text);
      text-decoration: none;
    }
    h2 a:hover {
      color: var(--accent);
      text-decoration: underline;
    }
    .authors {
      color: #39475a;
      font-size: 14px;
      margin-bottom: 10px;
    }
    .abstract {
      color: #2d3746;
      margin: 0;
      max-width: 90ch;
    }
    .empty {
      background: var(--panel);
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 30px;
      text-align: center;
    }
    @media (max-width: 860px) {
      .top { align-items: flex-start; flex-direction: column; }
      .status { text-align: left; }
      .toolbar { grid-template-columns: 1fr; }
      header { position: static; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="top">
        <h1>论文追索</h1>
        <div class="status" id="status"></div>
      </div>
      <div class="toolbar">
        <input id="query" type="search" placeholder="搜索标题、作者、摘要">
        <select id="journal"></select>
        <select id="days">
          <option value="0">全部日期</option>
          <option value="30">最近 30 天</option>
          <option value="90">最近 90 天</option>
          <option value="180">最近 180 天</option>
          <option value="365">最近 365 天</option>
        </select>
      </div>
    </div>
  </header>
  <main>
    <div class="wrap">
      <div class="summary">
        <div id="count"></div>
        <div id="latest"></div>
      </div>
      <div class="paper-list" id="papers"></div>
    </div>
  </main>
  <script>
    const state = { journals: [] };
    const els = {
      query: document.getElementById('query'),
      journal: document.getElementById('journal'),
      days: document.getElementById('days'),
      papers: document.getElementById('papers'),
      count: document.getElementById('count'),
      latest: document.getElementById('latest'),
      status: document.getElementById('status')
    };

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[ch]);
    }

    function params() {
      const p = new URLSearchParams();
      if (els.query.value.trim()) p.set('q', els.query.value.trim());
      if (els.journal.value) p.set('journal', els.journal.value);
      if (els.days.value !== '0') p.set('days', els.days.value);
      p.set('limit', '300');
      return p;
    }

    async function loadJournals() {
      const res = await fetch('/api/journals');
      state.journals = await res.json();
      els.journal.innerHTML = '<option value="">全部期刊</option>' +
        state.journals.map(j => {
          const count = Number(j.paper_count || 0);
          const label = `${j.name}${count ? ` (${count})` : ''}`;
          return `<option value="${escapeHtml(j.name)}">${escapeHtml(label)}</option>`;
        }).join('');
    }

    async function loadPapers() {
      els.status.textContent = '加载中...';
      const res = await fetch(`/api/papers?${params().toString()}`);
      const data = await res.json();
      const papers = data.papers || [];
      const total = Number(data.total || papers.length);
      els.count.innerHTML = total > papers.length ?
        `<strong>${papers.length}</strong> / ${total} 篇文章` :
        `<strong>${papers.length}</strong> 篇文章`;
      els.latest.textContent = data.latest ? `最新：${data.latest}` : '';
      els.papers.innerHTML = papers.length ? papers.map(renderPaper).join('') :
        '<div class="empty">暂无记录。等待每日自动更新完成。</div>';
      els.status.textContent = data.fetched_at ? `更新于：${data.fetched_at}` : '';
    }

    function renderPaper(paper) {
      const title = escapeHtml(paper.title || 'Untitled');
      const url = escapeHtml(paper.article_url || '#');
      const abstract = paper.abstract ?
        `<p class="abstract">${escapeHtml(paper.abstract)}</p>` :
        '<p class="abstract">数据源暂无摘要。</p>';
      return `
        <article class="paper">
          <div class="paper-meta">
            <span class="journal">${escapeHtml(paper.journal)}</span>
            <span class="date">${escapeHtml(paper.publication_date || '')}</span>
            ${paper.doi ? `<span>DOI ${escapeHtml(paper.doi)}</span>` : ''}
          </div>
          <h2><a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a></h2>
          <div class="authors">${escapeHtml(paper.authors || '作者未知')}</div>
          ${abstract}
        </article>
      `;
    }

    let timer = null;
    els.query.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(loadPapers, 250);
    });
    els.journal.addEventListener('change', loadPapers);
    els.days.addEventListener('change', loadPapers);

    (async function init() {
      await loadJournals();
      await loadPapers();
    })();
  </script>
</body>
</html>
"""


class PaperTrackerHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_auth_required(self) -> None:
        body = b"Authentication required"
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"'
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_authorized(self) -> bool:
        password = os.getenv("PAPER_TRACKER_PASSWORD", "")
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        username, sep, supplied_password = decoded.partition(":")
        expected_user = os.getenv("PAPER_TRACKER_USER", "paper")
        return (
            sep == ":"
            and hmac.compare_digest(
                username.encode("utf-8"), expected_user.encode("utf-8")
            )
            and hmac.compare_digest(
                supplied_password.encode("utf-8"), password.encode("utf-8")
            )
        )

    def is_cron_authorized(self, query: dict[str, list[str]]) -> bool:
        expected = os.getenv("PAPER_TRACKER_CRON_SECRET", "")
        if not expected:
            return False
        supplied = self.headers.get("X-Cron-Secret") or (
            query.get("secret") or [""]
        )[0]
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def handle_fetch(self, query: dict[str, list[str]]) -> None:
        days = int((query.get("days") or ["365"])[0] or 365)
        pages = min(int((query.get("pages") or ["2"])[0] or 2), 5)
        per_page = min(int((query.get("per_page") or ["50"])[0] or 50), 100)
        result = fetch_all(
            days=days,
            pages=pages,
            per_page=per_page,
            db_path=self.db_path,
        )
        self.send_json(result)

    def handle_cron_fetch(self, query: dict[str, list[str]]) -> None:
        if not self.is_cron_authorized(query):
            self.send_json({"error": "Cron fetch is not authorized"}, status=403)
            return
        self.handle_fetch(query)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self.send_json({"ok": True, "service": "paper-tracker"})
            return
        if parsed.path == "/api/cron/fetch":
            try:
                self.handle_cron_fetch(query)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        if not self.is_authorized():
            self.send_auth_required()
            return
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/journals":
            self.send_json(journal_counts(self.db_path))
            return
        if parsed.path == "/api/papers":
            query = urllib.parse.parse_qs(parsed.query)
            days = int((query.get("days") or ["0"])[0] or 0)
            limit = min(int((query.get("limit") or ["200"])[0] or 200), 500)
            papers = query_papers(
                db_path=self.db_path,
                journal=(query.get("journal") or [""])[0],
                q=(query.get("q") or [""])[0],
                days=days,
                limit=limit,
            )
            total = count_papers(
                db_path=self.db_path,
                journal=(query.get("journal") or [""])[0],
                q=(query.get("q") or [""])[0],
                days=days,
            )
            latest = max(
                (p.get("publication_date") or "" for p in papers), default=""
            )
            fetched_at = max((p.get("fetched_at") or "" for p in papers), default="")
            self.send_json(
                {
                    "papers": papers,
                    "total": total,
                    "latest": latest,
                    "fetched_at": fetched_at,
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/cron/fetch":
            try:
                self.handle_cron_fetch(query)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        if not self.is_authorized():
            self.send_auth_required()
            return
        if parsed.path != "/api/fetch":
            self.send_error(404)
            return
        if not self.is_cron_authorized(query):
            self.send_json({"error": "Fetch is not authorized"}, status=403)
            return
        try:
            self.handle_fetch(query)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)


def serve(host: str, port: int, db_path: Path) -> None:
    init_db(db_path)
    PaperTrackerHandler.db_path = db_path
    server = ThreadingHTTPServer((host, port), PaperTrackerHandler)
    print(f"Paper Tracker running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track recent journal papers.")
    parser.add_argument(
        "--db", default=str(configured_db_path()), help="SQLite database path"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="Fetch papers from OpenAlex")
    fetch_parser.add_argument("--days", type=int, default=365)
    fetch_parser.add_argument("--per-page", type=int, default=50)
    fetch_parser.add_argument("--pages", type=int, default=2)
    fetch_parser.add_argument("--journal", default="")

    subparsers.add_parser(
        "trigger-remote-fetch",
        help="Trigger a deployed app's protected daily fetch endpoint",
    )

    subparsers.add_parser("journals", help="List tracked journals")

    serve_parser = subparsers.add_parser("serve", help="Start the local web app")
    serve_parser.add_argument(
        "--host", default=os.getenv("PAPER_TRACKER_HOST", "127.0.0.1")
    )
    serve_parser.add_argument("--port", type=int, default=configured_port())

    args = parser.parse_args(argv)
    db_path = Path(args.db)

    if args.command == "fetch":
        result = fetch_all(
            days=args.days,
            per_page=args.per_page,
            pages=args.pages,
            db_path=db_path,
            journal_name=args.journal or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "journals":
        for journal in journal_counts(db_path):
            print(
                f"{journal['name']} | ISSN {journal['issn']} | "
                f"{journal['paper_count']} papers | latest {journal['latest'] or '-'}"
            )
        return 0

    if args.command == "trigger-remote-fetch":
        result = request_remote_fetch(
            os.getenv("PAPER_TRACKER_PUBLIC_URL", ""),
            os.getenv("PAPER_TRACKER_CRON_SECRET", ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        serve(args.host, args.port, db_path)
        return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
