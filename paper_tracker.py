#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import hmac
import html as html_lib
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "papers.db"
DEFAULT_JOURNALS = ROOT / "journals.csv"
OPENALEX_URL = "https://api.openalex.org/works"
HBR_FEED_URL = "http://feeds.harvardbusiness.org/harvardbusiness"
DEEPL_FREE_API_URL = "https://api-free.deepl.com"
MYMEMORY_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
TAG_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUTH_REALM = "Paper Tracker"


class TranslationConfigError(RuntimeError):
    pass


class PaperNotFoundError(RuntimeError):
    pass


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


def configured_deepl_api_url(api_key: str) -> str:
    value = os.getenv("DEEPL_API_URL", "").strip()
    if value:
        return value.rstrip("/")
    if api_key and not api_key.endswith(":fx"):
        return "https://api.deepl.com"
    return DEEPL_FREE_API_URL


def configured_deepl_target_lang() -> str:
    return os.getenv("DEEPL_TARGET_LANG", "ZH").strip().upper()


def configured_deepl_source_lang() -> str:
    return os.getenv("DEEPL_SOURCE_LANG", "EN").strip().upper()


def configured_deepl_model_label() -> str:
    source = configured_deepl_source_lang() or "auto"
    target = configured_deepl_target_lang()
    return f"deepl:{source.lower()}-{target.lower()}"


def configured_translation_provider() -> str:
    provider = os.getenv("PAPER_TRACKER_TRANSLATION_PROVIDER", "auto").strip().lower()
    if provider in {"auto", "deepl", "mymemory"}:
        return provider
    return "auto"


def configured_mymemory_source_lang() -> str:
    return os.getenv("MYMEMORY_SOURCE_LANG", "en").strip().lower()


def configured_mymemory_target_lang() -> str:
    return os.getenv("MYMEMORY_TARGET_LANG", "zh-CN").strip()


def configured_mymemory_model_label() -> str:
    source = configured_mymemory_source_lang()
    target = configured_mymemory_target_lang()
    return f"mymemory:{source}-{target.lower()}"


def configured_smtp_port() -> int:
    return int(os.getenv("SMTP_PORT", "465") or "465")


def configured_smtp_ssl() -> bool:
    value = os.getenv("SMTP_SSL", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    return configured_smtp_port() == 465


def configured_smtp_starttls() -> bool:
    value = os.getenv("SMTP_STARTTLS", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    return not configured_smtp_ssl()


def configured_email_recipients() -> list[str]:
    value = os.getenv("NOTIFY_EMAIL_TO", "").strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def smtp_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST", "").strip()
        and os.getenv("SMTP_USER", "").strip()
        and os.getenv("SMTP_PASSWORD", "").strip()
    )


def email_notifications_enabled(db_path: Path | None = None) -> bool:
    if not smtp_configured():
        return False
    if configured_email_recipients():
        return True
    return bool(list_subscriber_emails(db_path)) if db_path else False


def normalize_email(value: str) -> str:
    return " ".join(value.strip().lower().split())


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value))


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_translations (
                paper_key TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                title_zh TEXT NOT NULL,
                abstract_zh TEXT,
                model TEXT NOT NULL,
                translated_at TEXT NOT NULL,
                FOREIGN KEY (paper_key) REFERENCES papers(paper_key)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_paper_translations_hash
            ON paper_translations(source_hash)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_subscribers (
                email TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_email_subscribers_active
            ON email_subscribers(is_active)
            """
        )


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


def request_deepl_translation(
    *,
    title: str,
    abstract: str,
) -> dict[str, str]:
    api_key = os.getenv("DEEPL_API_KEY", "").strip()
    if not api_key:
        raise TranslationConfigError("Translation requires DEEPL_API_KEY")
    texts = [title]
    if abstract:
        texts.append(abstract)
    body = {
        "text": texts,
        "target_lang": configured_deepl_target_lang(),
    }
    source_lang = configured_deepl_source_lang()
    if source_lang:
        body["source_lang"] = source_lang
    if abstract:
        body["context"] = abstract[:4000]
    headers = {
        "Accept": "application/json",
        "Authorization": f"DeepL-Auth-Key {api_key}",
        "Content-Type": "application/json",
        "User-Agent": os.getenv("PAPER_TRACKER_USER_AGENT", "paper-tracker/0.1"),
    }
    request = urllib.request.Request(
        f"{configured_deepl_api_url(api_key)}/v2/translate",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=80) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepL HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepL request failed: {exc}") from exc
    translations = payload.get("translations") or []
    if not translations:
        raise RuntimeError("DeepL did not return a title translation")
    if abstract and len(translations) < 2:
        raise RuntimeError("DeepL did not return an abstract translation")
    return {
        "title_zh": str(translations[0].get("text") or "").strip(),
        "abstract_zh": (
            str(translations[1].get("text") or "").strip() if abstract else ""
        ),
    }


def split_translation_chunks(text: str, max_chars: int = 450) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    for part in parts:
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(part), max_chars):
                chunks.append(part[start : start + max_chars])
            continue
        next_value = f"{current} {part}".strip() if current else part
        if len(next_value) > max_chars and current:
            chunks.append(current)
            current = part
        else:
            current = next_value
    if current:
        chunks.append(current)
    return chunks


def request_mymemory_text(text: str) -> str:
    if not text:
        return ""
    params = {
        "q": text,
        "langpair": (
            f"{configured_mymemory_source_lang()}|"
            f"{configured_mymemory_target_lang()}"
        ),
    }
    email = os.getenv("MYMEMORY_EMAIL", "").strip()
    if email:
        params["de"] = email
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{MYMEMORY_TRANSLATE_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": os.getenv(
                "PAPER_TRACKER_USER_AGENT", "paper-tracker/0.1"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MyMemory HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"MyMemory request failed: {exc}") from exc
    response_data = payload.get("responseData") or {}
    translated = str(response_data.get("translatedText") or "").strip()
    status = int(payload.get("responseStatus") or 0)
    if status >= 400 or not translated:
        message = payload.get("responseDetails") or "MyMemory did not return text"
        raise RuntimeError(str(message))
    return translated


def request_mymemory_translation(
    *,
    title: str,
    abstract: str,
) -> dict[str, str]:
    title_zh = request_mymemory_text(title)
    abstract_chunks = split_translation_chunks(abstract)
    abstract_zh = "\n".join(
        request_mymemory_text(chunk) for chunk in abstract_chunks
    )
    return {"title_zh": title_zh, "abstract_zh": abstract_zh}


def translation_backend() -> tuple[str, str]:
    provider = configured_translation_provider()
    has_deepl = bool(os.getenv("DEEPL_API_KEY", "").strip())
    if provider == "deepl":
        if not has_deepl:
            raise TranslationConfigError("Translation requires DEEPL_API_KEY")
        return "deepl", configured_deepl_model_label()
    if provider == "mymemory":
        return "mymemory", configured_mymemory_model_label()
    if has_deepl:
        return "deepl", configured_deepl_model_label()
    return "mymemory", configured_mymemory_model_label()


def request_translation(
    *,
    title: str,
    abstract: str,
    provider: str,
) -> dict[str, str]:
    if provider == "deepl":
        return request_deepl_translation(title=title, abstract=abstract)
    return request_mymemory_translation(title=title, abstract=abstract)


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


def paper_digest_row(paper: dict[str, str | int]) -> dict[str, str]:
    return {
        "paper_key": str(paper.get("paper_key") or ""),
        "title": str(paper.get("title") or "Untitled"),
        "authors": str(paper.get("authors") or ""),
        "journal": str(paper.get("journal") or ""),
        "publication_date": str(paper.get("publication_date") or ""),
        "doi": str(paper.get("doi") or ""),
        "article_url": str(paper.get("article_url") or ""),
    }


def upsert_papers(papers: list[dict[str, str | int]], db_path: Path = DEFAULT_DB) -> dict:
    init_db(db_path)
    inserted = 0
    updated = 0
    new_papers: list[dict[str, str]] = []
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
                new_papers.append(paper_digest_row(paper))
    return {
        "inserted": inserted,
        "updated": updated,
        "seen": len(papers),
        "new_papers": new_papers,
    }


def group_papers_by_journal(papers: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for paper in papers:
        journal = paper.get("journal") or "Unknown journal"
        grouped.setdefault(journal, []).append(paper)
    return grouped


def build_email_digest(papers: list[dict[str, str]]) -> tuple[str, str]:
    max_papers = int(os.getenv("NOTIFY_MAX_PAPERS", "50") or "50")
    visible = papers[:max_papers]
    today_text = today().isoformat()
    prefix = os.getenv("NOTIFY_EMAIL_SUBJECT_PREFIX", "论文追索").strip() or "论文追索"
    subject = f"{prefix}: 新增 {len(papers)} 篇论文 ({today_text})"
    lines = [
        f"本次更新新增 {len(papers)} 篇论文。",
        "",
    ]
    for journal, rows in group_papers_by_journal(visible).items():
        lines.append(journal)
        lines.append("-" * min(len(journal), 60))
        for index, paper in enumerate(rows, start=1):
            title = paper.get("title") or "Untitled"
            authors = paper.get("authors") or "作者未知"
            date = paper.get("publication_date") or "-"
            url = paper.get("article_url") or ""
            doi = paper.get("doi") or ""
            lines.append(f"{index}. {title}")
            lines.append(f"   日期: {date}")
            lines.append(f"   作者: {authors}")
            if doi:
                lines.append(f"   DOI: {doi}")
            if url:
                lines.append(f"   链接: {url}")
            lines.append("")
        lines.append("")
    if len(papers) > len(visible):
        lines.append(f"还有 {len(papers) - len(visible)} 篇未列出，请打开网站查看完整列表。")
        lines.append("")
    lines.append("这封邮件由论文追索自动发送。")
    return subject, "\n".join(lines).strip() + "\n"


def notification_recipients(db_path: Path) -> list[str]:
    seen: set[str] = set()
    recipients: list[str] = []
    for email in [*configured_email_recipients(), *list_subscriber_emails(db_path)]:
        normalized = normalize_email(email)
        if normalized and normalized not in seen:
            seen.add(normalized)
            recipients.append(normalized)
    return recipients


def send_email(subject: str, body: str, recipients: list[str]) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("MAIL_FROM", "").strip() or user
    if not host or not user or not password or not recipients or not sender:
        raise RuntimeError("Email notification SMTP settings are incomplete")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    port = configured_smtp_port()
    if configured_smtp_ssl():
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if configured_smtp_starttls():
            smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)


def notify_new_papers(papers: list[dict[str, str]], db_path: Path) -> dict:
    recipients = notification_recipients(db_path)
    if not papers:
        return {
            "enabled": smtp_configured() and bool(recipients),
            "sent": False,
            "count": 0,
            "recipients": len(recipients),
        }
    if not smtp_configured() or not recipients:
        return {"enabled": False, "sent": False, "count": len(papers)}
    subject, body = build_email_digest(papers)
    try:
        send_email(subject, body, recipients)
    except Exception as exc:
        return {
            "enabled": True,
            "sent": False,
            "count": len(papers),
            "recipients": len(recipients),
            "error": str(exc),
        }
    return {
        "enabled": True,
        "sent": True,
        "count": len(papers),
        "recipients": len(recipients),
    }


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
    totals = {
        "inserted": 0,
        "updated": 0,
        "seen": 0,
        "journals": [],
        "new_papers": [],
    }
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
        totals["new_papers"].extend(result.get("new_papers", []))
        journal_result = dict(result)
        journal_result.pop("new_papers", None)
        totals["journals"].append({"journal": journal["name"], **journal_result})
        print(
            f"{journal['name']}: {result['seen']} seen, "
            f"{result['inserted']} new, {result['updated']} updated",
            flush=True,
        )
    totals["notification"] = notify_new_papers(totals["new_papers"], db_path)
    return totals


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def list_subscribers(db_path: Path = DEFAULT_DB) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT email, created_at
                FROM email_subscribers
                WHERE is_active = 1
                ORDER BY created_at DESC
                """
            )
        )


def list_subscriber_emails(db_path: Path | None = None) -> list[str]:
    if db_path is None:
        return []
    return [row["email"] for row in list_subscribers(db_path)]


def add_subscriber(email: str, db_path: Path = DEFAULT_DB) -> dict:
    normalized = normalize_email(email)
    if not is_valid_email(normalized):
        raise ValueError("请输入有效的邮箱地址")
    created_at = utc_now()
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO email_subscribers (email, created_at, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(email) DO UPDATE SET
                is_active = 1
            """,
            (normalized, created_at),
        )
        row = conn.execute(
            """
            SELECT email, created_at
            FROM email_subscribers
            WHERE email = ?
            """,
            (normalized,),
        )
        rows = rows_to_dicts(row)
    return rows[0]


def remove_subscriber(email: str, db_path: Path = DEFAULT_DB) -> None:
    normalized = normalize_email(email)
    if not is_valid_email(normalized):
        raise ValueError("请输入有效的邮箱地址")
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE email_subscribers
            SET is_active = 0
            WHERE email = ?
            """,
            (normalized,),
        )


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


def paper_source_hash(title: str, abstract: str) -> str:
    source = f"{title}\n\n{abstract}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def get_paper(db_path: Path, paper_key: str) -> dict | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT paper_key, title, abstract, journal, publication_date
            FROM papers
            WHERE paper_key = ?
            """,
            (paper_key,),
        )
        rows = rows_to_dicts(row)
    return rows[0] if rows else None


def cached_translation(
    db_path: Path,
    paper_key: str,
    source_hash: str,
    model: str,
) -> dict | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT paper_key, title_zh, abstract_zh, model, translated_at
                FROM paper_translations
                WHERE paper_key = ? AND source_hash = ? AND model = ?
                """,
                (paper_key, source_hash, model),
            )
        )
    return rows[0] if rows else None


def save_translation(
    *,
    db_path: Path,
    paper_key: str,
    source_hash: str,
    title_zh: str,
    abstract_zh: str,
    model: str,
) -> dict:
    translated_at = utc_now()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_translations (
                paper_key, source_hash, title_zh, abstract_zh, model, translated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_key) DO UPDATE SET
                source_hash = excluded.source_hash,
                title_zh = excluded.title_zh,
                abstract_zh = excluded.abstract_zh,
                model = excluded.model,
                translated_at = excluded.translated_at
            """,
            (paper_key, source_hash, title_zh, abstract_zh, model, translated_at),
        )
    return {
        "paper_key": paper_key,
        "title_zh": title_zh,
        "abstract_zh": abstract_zh,
        "model": model,
        "translated_at": translated_at,
        "cached": False,
    }


def translate_paper(
    *,
    db_path: Path,
    paper_key: str,
    refresh: bool = False,
) -> dict:
    paper = get_paper(db_path, paper_key)
    if not paper:
        raise PaperNotFoundError("Paper not found")
    title = paper.get("title") or ""
    abstract = paper.get("abstract") or ""
    source_hash = paper_source_hash(title, abstract)
    provider, model = translation_backend()
    if not refresh:
        cached = cached_translation(db_path, paper_key, source_hash, model)
        if cached:
            cached["cached"] = True
            return cached
    translated = request_translation(
        title=title,
        abstract=abstract,
        provider=provider,
    )
    return save_translation(
        db_path=db_path,
        paper_key=paper_key,
        source_hash=source_hash,
        title_zh=translated["title_zh"],
        abstract_zh=translated["abstract_zh"],
        model=model,
    )


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
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f1f5f9;
      --line: #dbe2ea;
      --line-strong: #c7d2df;
      --text: #152033;
      --muted: #66758a;
      --soft: #eef3f8;
      --accent: #0f766e;
      --accent-dark: #0b5954;
      --accent-soft: #e3f3f0;
      --blue: #2563eb;
      --blue-soft: #e8f0ff;
      --gold: #a15c07;
      --gold-soft: #fff4db;
      --focus: rgba(37, 99, 235, 0.18);
      --shadow: 0 16px 44px rgba(24, 39, 75, 0.08);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.6;
      text-rendering: optimizeLegibility;
    }
    a { color: inherit; }
    header {
      background: rgba(255, 255, 255, 0.94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(18px);
      padding: 18px 24px 16px;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .wrap {
      max-width: 1320px;
      margin: 0 auto;
    }
    .top {
      align-items: center;
      display: flex;
      gap: 16px;
      justify-content: space-between;
      margin-bottom: 16px;
    }
    .brand { min-width: 0; }
    h1 {
      font-size: 24px;
      font-weight: 800;
      letter-spacing: 0;
      margin: 0;
    }
    .subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }
    .status {
      color: var(--muted);
      font-size: 13px;
      min-height: 20px;
      text-align: right;
      white-space: nowrap;
    }
    .toolbar {
      display: grid;
      gap: 12px;
      grid-template-columns: minmax(280px, 1.4fr) minmax(220px, 0.8fr) 170px;
    }
    .field {
      min-width: 0;
      position: relative;
    }
    .field-label {
      color: var(--muted);
      display: block;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 6px;
    }
    input, select, button {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      font: inherit;
      min-height: 42px;
      outline: none;
      padding: 9px 11px;
      width: 100%;
    }
    input:focus, select:focus, button:focus {
      box-shadow: 0 0 0 4px var(--focus);
      border-color: var(--blue);
    }
    button {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
      cursor: pointer;
      font-weight: 650;
    }
    button:disabled {
      cursor: wait;
      opacity: 0.62;
    }
    main {
      padding: 24px 24px 56px;
    }
    .layout {
      align-items: start;
      display: grid;
      gap: 18px;
      grid-template-columns: 300px minmax(0, 1fr);
    }
    .sidebar {
      display: grid;
      gap: 14px;
      position: sticky;
      top: 122px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .stats {
      display: grid;
      gap: 0;
      grid-template-columns: 1fr;
      overflow: hidden;
    }
    .stat {
      min-width: 0;
      padding: 16px;
    }
    .stat + .stat {
      border-top: 1px solid var(--line);
    }
    .stat-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 3px;
    }
    .stat-value {
      color: var(--text);
      font-size: 22px;
      font-weight: 800;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }
    .journal-panel {
      max-height: calc(100vh - 230px);
      overflow: auto;
      padding: 8px;
    }
    .subscribe-panel {
      display: grid;
      gap: 10px;
      padding: 14px;
    }
    .subscribe-title {
      color: var(--text);
      font-size: 14px;
      font-weight: 800;
    }
    .subscribe-form {
      display: grid;
      gap: 8px;
    }
    .subscriber-list {
      display: grid;
      gap: 6px;
    }
    .subscriber-row {
      align-items: center;
      background: var(--panel-soft);
      border-radius: 7px;
      color: #3d4b60;
      display: grid;
      gap: 8px;
      grid-template-columns: minmax(0, 1fr) auto;
      min-height: 34px;
      padding: 7px 9px;
    }
    .subscriber-email {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .subscriber-remove {
      background: transparent;
      border: 0;
      color: #9f1239;
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
      min-height: 24px;
      padding: 0;
      width: auto;
    }
    .journal-button {
      align-items: center;
      background: transparent;
      border: 0;
      border-radius: 7px;
      color: var(--text);
      cursor: pointer;
      display: grid;
      gap: 8px;
      grid-template-columns: minmax(0, 1fr) auto;
      min-height: 38px;
      padding: 8px 10px;
      text-align: left;
      width: 100%;
    }
    .journal-button:hover {
      background: var(--panel-soft);
    }
    .journal-button.is-active {
      background: var(--accent-soft);
      color: var(--accent-dark);
      font-weight: 750;
    }
    .journal-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .journal-count {
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }
    .journal-button.is-active .journal-count {
      color: var(--accent-dark);
    }
    .paper-list {
      display: grid;
      gap: 14px;
    }
    .results-head {
      align-items: end;
      display: flex;
      gap: 16px;
      justify-content: space-between;
      margin-bottom: 14px;
      min-height: 36px;
    }
    .summary {
      min-width: 0;
    }
    .summary-title {
      color: var(--text);
      font-size: 18px;
      font-weight: 800;
      line-height: 1.25;
      margin: 0;
      overflow-wrap: anywhere;
    }
    .summary-subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-top: 3px;
    }
    .latest {
      color: var(--gold);
      flex: 0 0 auto;
      font-size: 13px;
      font-weight: 700;
      text-align: right;
    }
    .paper {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      display: grid;
      gap: 10px;
      padding: 18px 20px;
      transition: border-color 160ms ease, transform 160ms ease;
    }
    .paper:hover {
      border-color: var(--line-strong);
      transform: translateY(-1px);
    }
    .paper-meta {
      align-items: center;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      font-size: 13px;
      gap: 7px;
      line-height: 1.25;
    }
    .chip {
      align-items: center;
      border-radius: 999px;
      display: inline-flex;
      font-weight: 700;
      gap: 4px;
      max-width: 100%;
      min-height: 24px;
      padding: 4px 9px;
    }
    .journal {
      background: var(--accent-soft);
      color: var(--accent-dark);
    }
    .date {
      background: var(--gold-soft);
      color: var(--gold);
    }
    .doi {
      background: var(--blue-soft);
      color: var(--blue);
      font-weight: 650;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    h2 {
      font-size: 20px;
      letter-spacing: 0;
      line-height: 1.35;
      margin: 0;
      max-width: 50rem;
      overflow-wrap: anywhere;
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
      color: #3d4b60;
      font-size: 14px;
      line-height: 1.45;
      max-width: 65rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .paper-body {
      border-top: 1px solid var(--line);
      display: grid;
      gap: 8px;
      padding-top: 10px;
    }
    .abstract {
      color: #304057;
      margin: 0;
      max-width: 72ch;
      overflow-wrap: anywhere;
    }
    .abstract-preview {
      color: #304057;
      display: -webkit-box;
      margin: 0;
      max-width: 72ch;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
    }
    .abstract-full {
      max-width: 72ch;
    }
    .abstract-block {
      display: grid;
      gap: 8px;
      max-width: 72ch;
    }
    .text-button {
      background: transparent;
      border: 0;
      border-radius: 4px;
      color: var(--blue);
      cursor: pointer;
      display: inline-flex;
      font-size: 13px;
      font-weight: 750;
      min-height: 28px;
      padding: 0;
      text-align: left;
      width: auto;
    }
    .text-button:hover {
      text-decoration: underline;
    }
    .text-button:disabled {
      cursor: wait;
      opacity: 0.62;
      text-decoration: none;
    }
    .paper-actions {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 2px;
    }
    .text-link {
      align-items: center;
      color: var(--blue);
      display: inline-flex;
      font-size: 13px;
      font-weight: 750;
      gap: 5px;
      min-height: 28px;
      text-decoration: none;
    }
    .text-link:hover {
      text-decoration: underline;
    }
    .muted-note {
      color: var(--muted);
      font-size: 13px;
    }
    .translation-card {
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      gap: 8px;
      margin-top: 4px;
      max-width: 72ch;
      padding: 12px 14px;
    }
    .translation-label {
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 800;
    }
    .translation-title {
      color: var(--text);
      font-size: 16px;
      font-weight: 800;
      line-height: 1.45;
      margin: 0;
      overflow-wrap: anywhere;
    }
    .translation-abstract {
      color: #2f3f55;
      margin: 0;
      overflow-wrap: anywhere;
    }
    .translation-error {
      color: #9f1239;
    }
    .empty {
      background: var(--panel);
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 36px 24px;
      text-align: center;
    }
    .is-hidden {
      display: none;
    }
    @media (max-width: 1040px) {
      .layout { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
      }
      .journal-panel {
        display: none;
      }
      .stats {
        grid-template-columns: repeat(3, 1fr);
      }
      .stat + .stat {
        border-left: 1px solid var(--line);
        border-top: 0;
      }
      .toolbar {
        grid-template-columns: minmax(240px, 1fr) minmax(190px, 0.75fr) 150px;
      }
    }
    @media (max-width: 760px) {
      header {
        padding: 16px;
        position: static;
      }
      main {
        padding: 16px 16px 42px;
      }
      .top {
        align-items: flex-start;
        flex-direction: column;
        gap: 8px;
      }
      .status {
        text-align: left;
        white-space: normal;
      }
      .toolbar {
        grid-template-columns: 1fr;
      }
      .stats {
        grid-template-columns: 1fr;
      }
      .stat + .stat {
        border-left: 0;
        border-top: 1px solid var(--line);
      }
      .results-head {
        align-items: flex-start;
        flex-direction: column;
        gap: 6px;
      }
      .latest {
        text-align: left;
      }
      .paper {
        padding: 16px;
      }
      h1 {
        font-size: 22px;
      }
      h2 {
        font-size: 18px;
      }
      .authors {
        white-space: normal;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="top">
        <div class="brand">
          <h1>论文追索</h1>
          <div class="subtitle">把最近论文按期刊、日期和关键词整理成可读清单</div>
        </div>
        <div class="status" id="status"></div>
      </div>
      <div class="toolbar">
        <label class="field">
          <span class="field-label">关键词</span>
          <input id="query" type="search" placeholder="搜索标题、作者、摘要">
        </label>
        <label class="field">
          <span class="field-label">期刊</span>
          <select id="journal"></select>
        </label>
        <label class="field">
          <span class="field-label">时间</span>
          <select id="days">
            <option value="0">全部日期</option>
            <option value="30">最近 30 天</option>
            <option value="90">最近 90 天</option>
            <option value="180">最近 180 天</option>
            <option value="365">最近 365 天</option>
          </select>
        </label>
      </div>
    </div>
  </header>
  <main>
    <div class="wrap">
      <div class="layout">
        <aside class="sidebar" aria-label="期刊概览">
          <section class="panel stats">
            <div class="stat">
              <div class="stat-label">总记录</div>
              <div class="stat-value" id="stat-total">0</div>
            </div>
            <div class="stat">
              <div class="stat-label">已收录期刊</div>
              <div class="stat-value" id="stat-journals">0</div>
            </div>
            <div class="stat">
              <div class="stat-label">当前显示</div>
              <div class="stat-value" id="stat-visible">0</div>
            </div>
          </section>
          <section class="panel subscribe-panel" aria-label="邮件提醒">
            <div>
              <div class="subscribe-title">邮件提醒</div>
              <div class="muted-note" id="subscriber-note">新增论文时发送汇总邮件</div>
            </div>
            <form class="subscribe-form" id="subscriber-form">
              <label class="field">
                <span class="field-label">收件邮箱</span>
                <input id="subscriber-email" type="email" placeholder="you@example.com" autocomplete="email">
              </label>
              <button type="submit">订阅提醒</button>
            </form>
            <div class="subscriber-list" id="subscriber-list"></div>
          </section>
          <nav class="panel journal-panel" id="journal-list"></nav>
        </aside>
        <section class="results" aria-label="论文列表">
          <div class="results-head">
            <div class="summary">
              <h2 class="summary-title" id="count"></h2>
              <div class="summary-subtitle" id="active-filter"></div>
            </div>
            <div class="latest" id="latest"></div>
          </div>
          <div class="paper-list" id="papers"></div>
        </section>
      </div>
    </div>
  </main>
  <script>
    const state = {
      journals: [],
      selectedJournal: '',
      subscribers: [],
      translations: new Map()
    };
    const els = {
      query: document.getElementById('query'),
      journal: document.getElementById('journal'),
      days: document.getElementById('days'),
      papers: document.getElementById('papers'),
      count: document.getElementById('count'),
      activeFilter: document.getElementById('active-filter'),
      latest: document.getElementById('latest'),
      status: document.getElementById('status'),
      journalList: document.getElementById('journal-list'),
      statTotal: document.getElementById('stat-total'),
      statJournals: document.getElementById('stat-journals'),
      statVisible: document.getElementById('stat-visible'),
      subscriberForm: document.getElementById('subscriber-form'),
      subscriberEmail: document.getElementById('subscriber-email'),
      subscriberList: document.getElementById('subscriber-list'),
      subscriberNote: document.getElementById('subscriber-note')
    };

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[ch]);
    }

    function formatNumber(value) {
      return new Intl.NumberFormat('zh-CN').format(Number(value || 0));
    }

    function formatDate(value) {
      if (!value) return '';
      const parsed = new Date(`${value}T00:00:00`);
      if (Number.isNaN(parsed.getTime())) return value;
      return parsed.toLocaleDateString('zh-CN', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
      });
    }

    function splitAuthors(value) {
      return String(value || '')
        .split(';')
        .map(name => name.trim())
        .filter(Boolean);
    }

    function truncateText(value, maxLength) {
      const text = String(value || '').replace(/\s+/g, ' ').trim();
      if (text.length <= maxLength) return text;
      const slice = text.slice(0, maxLength);
      const sentenceEnd = Math.max(
        slice.lastIndexOf('. '),
        slice.lastIndexOf('。'),
        slice.lastIndexOf('? '),
        slice.lastIndexOf('! ')
      );
      const cut = sentenceEnd > 120 ? sentenceEnd + 1 : slice.lastIndexOf(' ');
      return `${slice.slice(0, cut > 80 ? cut : maxLength).trim()}...`;
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
      renderJournalList();
      renderJournalStats();
    }

    async function loadSubscribers() {
      const res = await fetch('/api/subscribers');
      const data = await res.json();
      state.subscribers = data.subscribers || [];
      renderSubscribers(data);
    }

    function renderSubscribers(data = {}) {
      const smtpConfigured = Boolean(data.smtp_configured);
      if (!smtpConfigured) {
        els.subscriberNote.textContent = '邮箱会保存；配置 SMTP 后开始发送提醒';
      } else if (state.subscribers.length) {
        els.subscriberNote.textContent = `已订阅 ${formatNumber(state.subscribers.length)} 个邮箱`;
      } else {
        els.subscriberNote.textContent = '新增论文时发送汇总邮件';
      }
      els.subscriberList.innerHTML = state.subscribers.length ?
        state.subscribers.map(subscriber => `
          <div class="subscriber-row">
            <span class="subscriber-email">${escapeHtml(subscriber.email)}</span>
            <button class="subscriber-remove" type="button" data-remove-subscriber="${escapeHtml(subscriber.email)}">移除</button>
          </div>
        `).join('') :
        '<div class="muted-note">还没有前端订阅邮箱。</div>';
    }

    async function addSubscriber(email) {
      const res = await fetch('/api/subscribers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || '订阅失败');
      state.subscribers = data.subscribers || [];
      renderSubscribers(data);
    }

    async function removeSubscriber(email) {
      const res = await fetch('/api/subscribers', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || '移除失败');
      state.subscribers = data.subscribers || [];
      renderSubscribers(data);
    }

    function renderJournalStats() {
      const total = state.journals.reduce((sum, j) => sum + Number(j.paper_count || 0), 0);
      const active = state.journals.filter(j => Number(j.paper_count || 0) > 0).length;
      els.statTotal.textContent = formatNumber(total);
      els.statJournals.textContent = formatNumber(active);
    }

    function renderJournalList() {
      const rows = [
        { name: '', label: '全部期刊', paper_count: state.journals.reduce((sum, j) => sum + Number(j.paper_count || 0), 0) },
        ...state.journals
      ];
      els.journalList.innerHTML = rows.map(j => {
        const name = j.name || '';
        const label = j.label || j.name;
        const active = name === state.selectedJournal ? ' is-active' : '';
        return `
          <button class="journal-button${active}" type="button" data-journal="${escapeHtml(name)}">
            <span class="journal-name">${escapeHtml(label)}</span>
            <span class="journal-count">${formatNumber(j.paper_count || 0)}</span>
          </button>
        `;
      }).join('');
    }

    function activeFilterLabel(total) {
      const parts = [];
      if (els.journal.value) parts.push(els.journal.value);
      if (els.days.value !== '0') parts.push(els.days.options[els.days.selectedIndex].text);
      if (els.query.value.trim()) parts.push(`包含 "${els.query.value.trim()}"`);
      return parts.length ? parts.join(' / ') : `全部收录记录，共 ${formatNumber(total)} 篇`;
    }

    async function loadPapers() {
      els.status.textContent = '正在整理列表...';
      const res = await fetch(`/api/papers?${params().toString()}`);
      const data = await res.json();
      const papers = data.papers || [];
      const total = Number(data.total || papers.length);
      els.count.textContent = total > papers.length ?
        `显示 ${formatNumber(papers.length)} / ${formatNumber(total)} 篇文章` :
        `${formatNumber(papers.length)} 篇文章`;
      els.activeFilter.textContent = activeFilterLabel(total);
      els.latest.textContent = data.latest ? `最新：${formatDate(data.latest)}` : '';
      els.statVisible.textContent = formatNumber(papers.length);
      els.papers.innerHTML = papers.length ? papers.map(renderPaper).join('') :
        '<div class="empty">暂无记录。可以换个关键词、期刊或时间范围再看。</div>';
      els.status.textContent = data.fetched_at ? `数据更新于 ${data.fetched_at}` : '列表已就绪';
    }

    function renderPaper(paper) {
      const title = escapeHtml(paper.title || 'Untitled');
      const url = escapeHtml(paper.article_url || '#');
      const authors = splitAuthors(paper.authors);
      const authorText = authors.length ? authors.join(', ') : '作者未知';
      const abstractText = String(paper.abstract || '').replace(/\s+/g, ' ').trim();
      const hasLongAbstract = abstractText.length > 320;
      const preview = hasLongAbstract ? truncateText(abstractText, 300) : abstractText;
      const abstract = abstractText ? (
        hasLongAbstract ?
          `<div class="abstract-block" data-expanded="false">
            <p class="abstract-preview">${escapeHtml(preview)}</p>
            <p class="abstract abstract-full is-hidden">${escapeHtml(abstractText)}</p>
            <button class="text-button" type="button" data-toggle-abstract>展开摘要</button>
          </div>` :
          `<p class="abstract">${escapeHtml(abstractText)}</p>`
      ) : '<div class="muted-note">数据源暂无摘要。</div>';
      const doi = paper.doi ? `<span class="chip doi">DOI ${escapeHtml(paper.doi)}</span>` : '';
      return `
        <article class="paper">
          <div class="paper-meta">
            <span class="chip journal">${escapeHtml(paper.journal)}</span>
            ${paper.publication_date ? `<span class="chip date">${escapeHtml(formatDate(paper.publication_date))}</span>` : ''}
            ${doi}
          </div>
          <h2><a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a></h2>
          <div class="authors">${escapeHtml(authorText)}</div>
          <div class="paper-body">
            ${abstract}
            <div class="paper-actions">
              <button class="text-button" type="button" data-translate-paper data-paper-key="${escapeHtml(paper.paper_key)}">翻译</button>
              <a class="text-link" href="${url}" target="_blank" rel="noopener noreferrer">打开原文</a>
            </div>
            <div class="translation-card is-hidden" data-translation-panel></div>
          </div>
        </article>
      `;
    }

    function renderTranslation(translation) {
      const title = escapeHtml(translation.title_zh || '暂无标题翻译');
      const abstract = translation.abstract_zh ?
        `<p class="translation-abstract">${escapeHtml(translation.abstract_zh)}</p>` :
        '<div class="muted-note">暂无摘要翻译。</div>';
      const source = translation.cached ? '缓存翻译' : '新翻译';
      const meta = translation.translated_at ?
        `<div class="muted-note">${source} / ${escapeHtml(translation.model || '')} / ${escapeHtml(translation.translated_at)}</div>` :
        '';
      return `
        <div class="translation-label">中文翻译</div>
        <h3 class="translation-title">${title}</h3>
        ${abstract}
        ${meta}
      `;
    }

    function renderTranslationError(message) {
      return `
        <div class="translation-label translation-error">翻译失败</div>
        <div class="muted-note translation-error">${escapeHtml(message)}</div>
      `;
    }

    async function handleTranslate(button) {
      const article = button.closest('.paper');
      const panel = article.querySelector('[data-translation-panel]');
      const paperKey = button.dataset.paperKey || '';
      if (panel.dataset.loaded === 'true') {
        const shouldHide = !panel.classList.contains('is-hidden');
        panel.classList.toggle('is-hidden', shouldHide);
        button.textContent = shouldHide ? '显示翻译' : '收起翻译';
        return;
      }
      button.disabled = true;
      button.textContent = '翻译中...';
      panel.classList.remove('is-hidden');
      panel.innerHTML = '<div class="muted-note">正在生成中文翻译...</div>';
      try {
        const res = await fetch('/api/translate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paper_key: paperKey })
        });
          const data = await res.json();
          if (!res.ok) {
            const message = data.needs_configuration ?
            '需要先在部署环境配置翻译服务变量，之后再点击翻译。' :
            (data.error || '翻译请求失败');
          throw new Error(message);
        }
        const translation = data.translation || {};
        state.translations.set(paperKey, translation);
        panel.dataset.loaded = 'true';
        panel.innerHTML = renderTranslation(translation);
        button.textContent = '收起翻译';
      } catch (error) {
        panel.innerHTML = renderTranslationError(error.message || '翻译请求失败');
        button.textContent = '重试翻译';
      } finally {
        button.disabled = false;
      }
    }

    els.journalList.addEventListener('click', event => {
      const button = event.target.closest('[data-journal]');
      if (!button) return;
      state.selectedJournal = button.dataset.journal || '';
      els.journal.value = state.selectedJournal;
      renderJournalList();
      loadPapers();
    });

    els.subscriberForm.addEventListener('submit', async event => {
      event.preventDefault();
      const email = els.subscriberEmail.value.trim();
      if (!email) return;
      const button = els.subscriberForm.querySelector('button[type="submit"]');
      button.disabled = true;
      button.textContent = '保存中...';
      try {
        await addSubscriber(email);
        els.subscriberEmail.value = '';
        button.textContent = '已订阅';
        setTimeout(() => { button.textContent = '订阅提醒'; }, 1200);
      } catch (error) {
        els.subscriberNote.textContent = error.message || '订阅失败';
        button.textContent = '重试订阅';
      } finally {
        button.disabled = false;
      }
    });

    els.subscriberList.addEventListener('click', async event => {
      const button = event.target.closest('[data-remove-subscriber]');
      if (!button) return;
      const email = button.dataset.removeSubscriber || '';
      button.disabled = true;
      button.textContent = '移除中...';
      try {
        await removeSubscriber(email);
      } catch (error) {
        els.subscriberNote.textContent = error.message || '移除失败';
        button.disabled = false;
        button.textContent = '移除';
      }
    });

    els.papers.addEventListener('click', event => {
      const translateButton = event.target.closest('[data-translate-paper]');
      if (translateButton) {
        handleTranslate(translateButton);
        return;
      }
      const button = event.target.closest('[data-toggle-abstract]');
      if (!button) return;
      const block = button.closest('.abstract-block');
      const preview = block.querySelector('.abstract-preview');
      const full = block.querySelector('.abstract-full');
      const nextExpanded = block.dataset.expanded !== 'true';
      block.dataset.expanded = String(nextExpanded);
      preview.classList.toggle('is-hidden', nextExpanded);
      full.classList.toggle('is-hidden', !nextExpanded);
      button.textContent = nextExpanded ? '收起摘要' : '展开摘要';
    });

    let timer = null;
    els.query.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(loadPapers, 250);
    });
    els.journal.addEventListener('change', () => {
      state.selectedJournal = els.journal.value;
      renderJournalList();
      loadPapers();
    });
    els.days.addEventListener('change', loadPapers);

    (async function init() {
      await loadJournals();
      await loadSubscribers();
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

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 8192:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

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

    def handle_translate(self) -> None:
        payload = self.read_json_body()
        paper_key = str(payload.get("paper_key") or "").strip()
        refresh = bool(payload.get("refresh"))
        if not paper_key:
            self.send_json({"error": "paper_key is required"}, status=400)
            return
        try:
            translation = translate_paper(
                db_path=self.db_path,
                paper_key=paper_key,
                refresh=refresh,
            )
        except PaperNotFoundError as exc:
            self.send_json({"error": str(exc)}, status=404)
            return
        except TranslationConfigError as exc:
            self.send_json(
                {
                    "error": str(exc),
                    "needs_configuration": True,
                },
                status=503,
            )
            return
        self.send_json({"translation": translation})

    def handle_list_subscribers(self) -> None:
        subscribers = list_subscribers(self.db_path)
        self.send_json(
            {
                "subscribers": subscribers,
                "email_notifications_enabled": email_notifications_enabled(
                    self.db_path
                ),
                "smtp_configured": smtp_configured(),
            }
        )

    def handle_add_subscriber(self) -> None:
        payload = self.read_json_body()
        email = str(payload.get("email") or "")
        try:
            subscriber = add_subscriber(email, self.db_path)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        self.send_json(
            {
                "subscriber": subscriber,
                "subscribers": list_subscribers(self.db_path),
                "email_notifications_enabled": email_notifications_enabled(
                    self.db_path
                ),
                "smtp_configured": smtp_configured(),
            },
            status=201,
        )

    def handle_remove_subscriber(self) -> None:
        payload = self.read_json_body()
        email = str(payload.get("email") or "")
        try:
            remove_subscriber(email, self.db_path)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        self.send_json(
            {
                "ok": True,
                "subscribers": list_subscribers(self.db_path),
                "email_notifications_enabled": email_notifications_enabled(
                    self.db_path
                ),
                "smtp_configured": smtp_configured(),
            }
        )

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
        if parsed.path == "/api/subscribers":
            self.handle_list_subscribers()
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
        if parsed.path == "/api/translate":
            try:
                self.handle_translate()
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body"}, status=400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        if parsed.path == "/api/subscribers":
            try:
                self.handle_add_subscriber()
            except json.JSONDecodeError:
                self.send_json({"error": "Invalid JSON body"}, status=400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
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

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self.is_authorized():
            self.send_auth_required()
            return
        if parsed.path != "/api/subscribers":
            self.send_error(404)
            return
        try:
            self.handle_remove_subscriber()
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON body"}, status=400)
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
