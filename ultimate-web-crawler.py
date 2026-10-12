#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webbdammsugare Pro (v7.1)
Skapad av Fredrik Eriksson

Asynkron webbcrawler med GUI (CustomTkinter) och CLI-/serverläge.

Funktioner:
  - Hybrid-motor: aiohttp + Playwright-fallback för JavaScript-tunga sidor
  - Inkrementell crawl: conditional GET (ETag / If-Modified-Since), sparade länkar
    spelas upp vid 304, borttagna sidor (404/410) raderas, changes.jsonl + crawl_report.json
  - Riktig CookieJar med stöd för SAML/SSO-inloggning via Playwright (+ cookie_file för serverläge)
  - Per-domän rate limiter med adaptiv throttling, prioritetskö med boostning/penalty
  - Sitemap-parser (XML, gzip, sitemap-index) med storleks- och entitetsskydd
  - robots.txt-stöd med Crawl-Delay (Protego om installerat)
  - Nätverkssäkerhet: blockerar privata IP-adresser för publika sajter (SSRF), storleksgränser,
    ärlig User-Agent, certifikatfel ignoreras bara på begäran
  - Login-detektor (URL-redirect, HTTP-status, innehållsheuristik) och bot-/soft-404-detektion
  - Dokumentnedladdning (PDF, Word, Excel, PPTX, ZIP m.fl.) med .part-filer och filtypskontroll
  - Dokument → Markdown (även äldre format via LibreOffice) med källinformation i brödtexten
  - Dokument-manifest (manifest_<domän>.json) som kopplar filer till ursprungssida
  - CMS-boilerplate-rensning (Sitevision m.fl.) i alla utdataformat
  - PII-tvätt (e-post, personnummer, telefon, IP) av allt sparat innehåll
  - Semantisk chunkning med URL, rubrikväg och kontext per chunk
  - Flaggning av möjlig prompt-injektion i crawlat innehåll
  - Serverläge utan tkinter med exit-koder, --output och webhook
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import ipaddress
import io
import json
import logging
import math
import os
import posixpath
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, Comment
from crawler_runtime import ConversionProcessRunner, PublicationWorkspace, remove_tree, blocking_complete

from crawler_text import (LogLevel, slugify, metadata_line, file_sha256, markdown_table,
                          downgrade_body_h1, atomic_write_text, detect_prompt_injection,
                          clean_pii_text, ROLE_MAILBOX_LOCALPARTS, _INJECTION_RE,
                          _is_table_row, _is_table_sep, _split_long_text)
from document_conversion import (DocumentConverter, convert_document_result, HAS_PYMUPDF,
                                 HAS_DOCX, HAS_OPENPYXL, HAS_PPTX)
import document_conversion
try:
    if HAS_PYMUPDF:
        pymupdf = document_conversion.pymupdf
except ImportError:
    HAS_PYMUPDF = False
try:
    if HAS_DOCX:
        DocxDocument = document_conversion.DocxDocument
except ImportError:
    HAS_DOCX = False
try:
    if HAS_OPENPYXL:
        xl_load_workbook = document_conversion.xl_load_workbook
except ImportError:
    HAS_OPENPYXL = False
try:
    if HAS_PPTX:
        PptxPresentation = document_conversion.PptxPresentation
except ImportError:
    HAS_PPTX = False

VERSION = "7.1"
DEFAULT_USER_AGENT = ("UltimateWebCrawler/7.1 "
                      "(+https://github.com/elementarpartikel/ultimate-web-crawler)")
ROBOTS_TOKEN = "UltimateWebCrawler"

# GUI-beroenden är frivilliga så att serverläget (--config) fungerar på
# headless-servrar utan tkinter/display.
try:
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk
    import customtkinter as ctk
    HAS_GUI = True
except Exception:  # ImportError, TclError m.fl.
    HAS_GUI = False

# ─────────────────────────────────────────────────────────────
#  Frivilliga integrationer
# ─────────────────────────────────────────────────────────────
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import aiohttp
    from aiohttp import CookieJar
    from yarl import URL as YarlURL
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

try:
    import uvloop
    HAS_UVLOOP = True
except ImportError:
    HAS_UVLOOP = False

try:
    import brotli  # noqa: F401  -- aiohttp upptäcker den automatiskt
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False





try:
    import defusedxml.ElementTree as SafeET
    HAS_DEFUSEDXML = True
except ImportError:
    HAS_DEFUSEDXML = False

try:
    from protego import Protego
    HAS_PROTEGO = True
except ImportError:
    HAS_PROTEGO = False


if HAS_GUI:
    ctk.set_appearance_mode("Light")
    ctk.set_default_color_theme("blue")

# ─────────────────────────────────────────────────────────────
#  ENUMS OCH DATACLASSES
# ─────────────────────────────────────────────────────────────


class CrawlPriority(Enum):
    CRITICAL = 1
    SITEMAP = 5
    HIGH = 10
    MEDIUM = 15
    LOW = 20


class CrawlerState(Enum):
    IDLE = 0
    RUNNING = 1
    PAUSED = 2
    STOPPED = 3


@dataclass
class CrawlStats:
    pages_visited: int = 0
    pages_unchanged: int = 0
    pages_not_modified_304: int = 0   # Räknar 304-träffar separat
    pages_skipped_lastmod: int = 0    # Hoppade över p.g.a. sitemapens lastmod
    pages_failed: int = 0
    playwright_fallbacks: int = 0
    documents_downloaded: int = 0
    bytes_downloaded: int = 0
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None

    @property
    def duration(self) -> timedelta:
        end = self.end_time or datetime.now()
        return end - self.start_time

    @property
    def pages_per_second(self) -> float:
        secs = self.duration.total_seconds()
        return self.pages_visited / secs if secs > 0 else 0.0


# ─────────────────────────────────────────────────────────────
#  HJÄLPFUNKTIONER & CHUNKING
# ─────────────────────────────────────────────────────────────


# Standard-parametrar som aldrig påverkar sidans innehåll och därför ska strippas
# före URL-deduplicering. Lägg till nya här när du upptäcker dubletter i utmappen.
DEFAULT_IGNORE_QUERY_PARAMS = (
    # Generisk kampanj-/spårningsspårning
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'msclkid', 'mc_cid', 'mc_eid',
    'ref', 'source', 'igshid', 'mibextid',
    # Session-/auth-tokens som varierar mellan besök men inte ändrar innehåll
    'sessionid', 'jsessionid', 'phpsessid',
    # Sitevision (svenska kommuners CMS — Tyresö m.fl.).
    # `state` (addBookmark/removeBookmark) och `logout=true` är de vanligaste
    # som sett till fyra dubletter av samma sida.
    'state', 'logout', 'printerfriendly',
)

# Prefix-matchade parametrar — alla nycklar som börjar med dessa strippas.
# Detta täcker hela Sitevision-familjen (sv.url, sv.target, sv.viewportname,
# sv.scrollTo, sv.13.svid12_*, etc.) utan att vi behöver räkna upp varje variant.
DEFAULT_IGNORE_QUERY_PREFIXES = (
    'sv.',          # Sitevision — alla interna parametrar
    '_hsenc',       # HubSpot tracking
    '_hsmi',        # HubSpot tracking
)


def normalize_url(url: str,
                  ignore_query_params: Optional[List[str]] = None,
                  ignore_query_prefixes: Optional[List[str]] = None) -> str:
    """Normaliserar URL för stabil deduplicering.

    Tar bort:
      - tracking-/session-parametrar via exakt matchning (utm_*, fbclid, ...)
      - CMS-interna parametrar via prefix-matchning (sv.* för Sitevision, ...)
      - /index.html → /
      - trailing slash (för konsekvens)
      - URL-fragment (#...)
    """
    if ignore_query_params is None:
        ignore_query_params = DEFAULT_IGNORE_QUERY_PARAMS
    if ignore_query_prefixes is None:
        ignore_query_prefixes = DEFAULT_IGNORE_QUERY_PREFIXES

    # Sänk till lowercase en gång för effektivitet
    ignore_set = frozenset(p.lower() for p in ignore_query_params)
    ignore_prefixes = tuple(p.lower() for p in ignore_query_prefixes)

    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or 'http'
        netloc = parsed.netloc.lower()
        path = parsed.path or '/'
        path = re.sub(r'/index\.(html|htm|php)$', '/', path, flags=re.IGNORECASE)
        if path != '/' and path.endswith('/'):
            path = path.rstrip('/')
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {
            k: sorted(v) for k, v in query_params.items()
            if k.lower() not in ignore_set
            and not k.lower().startswith(ignore_prefixes)
        }
        query_string = urlencode(sorted(filtered.items()), doseq=True) if filtered else ""
        normalized = f"{scheme}://{netloc}{path}"
        if query_string:
            normalized += f"?{query_string}"
        return normalized.split('#')[0]
    except Exception:
        return url.strip()


def request_url(url: str) -> str:
    """Behåll serverns sökväg och query; fragment skickas aldrig till servern."""
    return urlparse(url.strip())._replace(fragment='').geturl()


def get_clean_hash(text: str) -> str:
    """Stabil hash av text efter normalisering av whitespace."""
    clean_text = re.sub(r'\s+', ' ', text).strip().lower()
    return hashlib.sha256(clean_text.encode('utf-8')).hexdigest()






def stable_filename(url: str, save_format: str) -> str:
    """Filnamn som är stabilt mellan körningar (baserat på URL, inte content)."""
    parsed = urlparse(url)
    path_slug = slugify(parsed.path)[:60] or "root"
    url_digest = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
    return f"{path_slug}_{url_digest}{save_format}"








def _merge_small_sections(sections: List[Dict], min_words: int, max_words: int) -> List[Dict]:
    """Slå bara ihop intilliggande syskon inom samma dokumentdel och sida/bild."""
    groups = []

    def key(sec):
        path = sec.get('path') or sec.get('heading', 'Huvudinnehåll')
        return (path.rpartition(' > ')[0], sec.get('page_number'),
                sec.get('slide_number'), sec.get('section_group'))

    def size(group):
        return sum(len(sec['text'].split()) + (len(sec.get('heading', '').split())
                                              if len(group) > 1 else 0) for sec in group)

    for original in sections:
        if not original.get('text', '').strip():
            continue
        sec = dict(original)
        if (groups and key(groups[-1][-1]) == key(sec)
                and (size(groups[-1]) < min_words or len(sec['text'].split()) < min_words)
                and size(groups[-1] + [sec]) <= max_words):
            groups[-1].append(sec)
        else:
            groups.append([sec])
    merged = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        paths = list(dict.fromkeys(sec.get('path') or sec.get('heading', 'Huvudinnehåll') for sec in group))
        parent = key(group[0])[0]
        headings = list(dict.fromkeys(sec.get('heading', 'Huvudinnehåll') for sec in group))
        merged.append({**group[0], 'heading': ' / '.join(headings),
                       'path': parent or ' / '.join(headings), 'heading_paths': paths,
                       'text': '\n\n'.join(f"**{sec.get('heading', 'Huvudinnehåll')}**\n\n{sec['text'].strip()}"
                                           for sec in group)})
    return merged


def semantic_chunk_text(sections: List[Dict], max_words: int = 400,
                        overlap_words: int = 50,
                        source_url: Optional[str] = None,
                        title: str = "", min_words: int = 80) -> List[Dict]:
    """Chunkar strukturerade sektioner till {heading, content, url}-objekt.

    `source_url` injiceras i varje chunk under nyckeln `url`. Det är harmlöst
    om RAG-systemet ignorerar fältet, men oerhört nyttigt när modellen får
    flera relaterade chunks i kontexten samtidigt — då kan källan plockas
    direkt från chunken istället för att gissas från rotnivå-metadata.

    Sektioner kan ha nyckeln `path` (rubrikväg, t.ex. "Skola > Förskola").
    Den bevaras som `heading_path` och `context` ("Titel > Rubrikväg") i varje
    chunk så att en ensam chunk går att förstå utan resten av sidan.
    Dubbleringar ("Avgifter > Avgifter") tvättas automatiskt.
    För dokument bevaras även `page_number` och käll-URL berikas med #page=X.
    """
    if not sections:
        return []

    chunks = []
    for sec in _merge_small_sections(sections, min(min_words, max_words), max_words):
        heading = sec.get("heading", "Huvudinnehåll")
        path = sec.get("path") or heading
        text = sec.get("text", "").strip()
        page_num = sec.get("page_number")
        if not text:
            continue

        pieces = (_split_long_text(text, max_words, overlap_words)
                  if len(text.split()) > max_words else [text])
        for part_num, piece in enumerate(pieces, start=1):
            chunk_heading = heading if part_num == 1 else f"{heading} (del {part_num})"
            chunk = {"heading": chunk_heading, "content": piece,
                     "heading_path": path}
            if page_num is not None:
                chunk["page_number"] = page_num
            if sec.get('slide_number') is not None:
                chunk['slide_number'] = sec['slide_number']
            if sec.get('heading_paths'):
                chunk['heading_paths'] = sec['heading_paths']

            # Undvik stutter som "Avgifter > Avgifter" eller "Avgifter > Avgifter > Förskola"
            if title and path:
                clean_title = title.strip()
                clean_path = path.strip()
                if clean_path == clean_title:
                    chunk["context"] = clean_title
                elif clean_path.startswith(f"{clean_title} > "):
                    chunk["context"] = clean_path
                else:
                    chunk["context"] = f"{clean_title} > {clean_path}"
            else:
                chunk["context"] = (title or path or "Huvudinnehåll").strip()

            chunks.append(chunk)

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i + 1
        chunk["total_chunks"] = total
        if source_url:
            chunk["url"] = source_url
            p_num = chunk.get("page_number")
            if p_num is not None:
                chunk["page_url"] = f"{source_url}#page={p_num}" if "#page=" not in source_url else source_url
    return chunks


def markdown_to_plain(text: str) -> str:
    """Enkel Markdown → ren text för .txt-utdata."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]*)\]\(([^)]+)\)',
                  lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) else m.group(2),
                  text)
    text = text.replace('**', '').replace('__', '')
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def markdown_file_to_chunks(text: str, is_document: bool = False) -> Tuple[Dict, List[Dict]]:
    """Läser en av crawlerns .md-filer och returnerar (metadata, chunks)."""
    parts = text.split('\n---\n')
    header = parts[0]

    def field(label: str) -> str:
        # Brödtexten får inte kunna fylla i eller förfalska dokumentets metadata.
        m = re.search(r'^\*\*' + re.escape(label) + r':\*\*[ \t]*(.+)$', header, re.MULTILINE)
        return m.group(1).strip() if m else ""

    title_match = re.match(r'#\s+(.+)', text)
    title = title_match.group(1).strip() if title_match else ""
    source = field("Källa")
    doc_url = field("Dokument-URL")
    url = doc_url if (is_document and doc_url) else source

    raw_flags = field("Flaggor")
    flags = [f.strip() for f in raw_flags.split(",") if f.strip()] if raw_flags else []

    # Brödtexten ligger mellan första och sista "---"-raden
    body = "\n---\n".join(parts[1:-1]) if len(parts) >= 3 else text
    if is_document:
        repeated = {f'**Dokument:** {title}', f'**Källa:** {source or url}',
                    f'**Dokument-URL:** {url}'}
        for label in ('Hämtad', 'Dokumentets ändringsdatum', 'Serverns ändringsdatum', 'Skapad'):
            if field(label):
                repeated.add(f'**{label}:** {field(label)}')
        body = '\n'.join(line for line in body.splitlines() if line not in repeated)

    sections: List[Dict] = []
    stack: List[Tuple[int, str]] = []
    for raw in re.split(r'(?=^#{1,6}\s)', body, flags=re.MULTILINE):
        raw = raw.strip()
        if not raw:
            continue
        m = re.match(r'^(#{1,6})\s+(.+)', raw)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip()
            content = raw[m.end():].strip()
            stack = [h for h in stack if h[0] < level]
            stack.append((level, heading))
        else:
            heading, content = "Huvudinnehåll", raw
        if content:
            page_num, slide_num = None, None
            if is_document:
                for heading_level, h_text in reversed(stack):
                    if heading_level != 2:
                        continue
                    page_m = ((re.search(r'\s[–—-]\sSida\s+(\d+)\s*$', h_text, re.IGNORECASE)
                               or re.match(r'^Sida\s+(\d+)\b', h_text, re.IGNORECASE))
                              if not field('Filtyp') or 'PDF' in field('Filtyp') else None)
                    if page_m:
                        page_num = int(page_m.group(1))
                        break
                    slide_m = ((re.search(r'\s[–—-]\sBild\s+(\d+)\s*$', h_text, re.IGNORECASE)
                                or re.match(r'^Bild\s+(\d+)\b', h_text, re.IGNORECASE))
                               if not field('Filtyp') or 'PowerPoint' in field('Filtyp') else None)
                    if slide_m:
                        slide_num = int(slide_m.group(1))
                        break
            sec_dict = {"heading": heading, "text": content,
                        "path": " > ".join(h[1] for h in stack) or heading}
            if page_num is not None:
                sec_dict["page_number"] = page_num
            if slide_num is not None:
                sec_dict['slide_number'] = slide_num
            if is_document and 'Excel' in field('Filtyp') and stack:
                sec_dict['section_group'] = stack[0][1]
            sections.append(sec_dict)

    meta = {
        "url": url,
        "title": title,
        "source_type": "document" if is_document else "page",
        "referer_url": source if is_document else "",
        "language": field("Språk"),
        "modified_date": field("Senast ändrad"),
        "crawled_at": field("Hämtad"),
        "flags": flags,
    }
    if is_document:
        meta.update(document_modified_at=field('Dokumentets ändringsdatum'),
                    server_modified_at=field('Serverns ändringsdatum'),
                    created_at=field('Skapad'), fetched_at=field('Hämtad'))
    return meta, semantic_chunk_text(sections, source_url=url, title=title)


def plain_file_to_chunks(text: str) -> Tuple[Dict, List[Dict]]:
    """Läser textformatets käll- och kvalitetsmetadata utan att tappa brödtexten."""
    parts = text.split('\n---\n')
    header = parts[0].strip().splitlines()
    title = header[0] if header else ''
    fields = {}
    for line in header[1:]:
        key, separator, value = line.partition(':')
        if separator:
            fields[key.strip()] = value.strip()
    body = '\n---\n'.join(parts[1:-1]) if len(parts) >= 3 else text
    flags = [flag.strip() for flag in fields.get('Flaggor', '').split(',') if flag.strip()]
    meta = {'url': fields.get('Källa', ''), 'title': title, 'source_type': 'page',
            'referer_url': '', 'language': fields.get('Språk', ''),
            'modified_date': fields.get('Senast ändrad', fields.get('Publicerad', '')),
            'crawled_at': fields.get('Hämtad', ''), 'flags': flags}
    return meta, semantic_chunk_text([{'heading': 'Huvudinnehåll', 'text': body}],
                                     source_url=meta['url'], title=title)


def csv_safe(value) -> str:
    """Skydd mot CSV/formel-injektion när index.csv öppnas i Excel."""
    v = "" if value is None else str(value)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v




# Funktionsbrevlådor är inte personuppgifter och är ofta exakt det en användare
# av Svea letar efter. Bevaras bara om `keep_role_emails` är påslaget.


def absolutize_markdown_links(text: str, base_url: str) -> str:
    """Gör relativa markdown-länkar [text](href) absoluta."""
    def _fix_md_link(m):
        href = m.group(2)
        if href and not href.startswith(('http://', 'https://', 'mailto:', '#')):
            href = urljoin(base_url, href)
        return f"[{m.group(1)}]({href})"
    return re.sub(r'\[([^\]]*)\]\(([^)]+)\)', _fix_md_link, text)


# ── Boilerplate/CMS-chrome som ska rensas från extraherad brödtext ──
# Sitevision (Tyresö m.fl.) injicerar feedback-widget, kontaktfooter och
# "Sidan publicerad av"-blocket i <main> — trafilatura/BS4 kan inte skilja
# detta från riktigt innehåll.
_CMS_BOILERPLATE_RE = re.compile(
    r'(?:'
    # Sitevision feedback-widget
    r'Tack för din medverkan!'
    r'|Du har hjälpt oss att förbättra webbplatsen'
    r'|Någonting gick fel\.?\s*Prova igen senare\.?'
    # Publiceringsinfo ("Sidan publicerad av:" + e-post/namn + datum)
    r'|\*{0,2}Sidan publicerad av:\*{0,2}.*'
    r'|\*{0,2}Senast uppdaterad:\*{0,2}.*'
    # Generisk "Var informationen till nytta?"-widget
    r'|Var informationen till nytta\??'
    r'|Skriv inte personuppgifter här'
    r'|Fältet är obligatoriskt'
    r')'
    r'\s*$',
    re.MULTILINE | re.IGNORECASE
)


def strip_cms_boilerplate(text: str) -> str:
    """Ta bort CMS-chrome/boilerplate (Sitevision m.fl.) från extraherad text.

    Rensningen sker radvis med regex. Rader som matchar kända boilerplate-
    mönster ersätts med tomrader, och sedan städas överflödiga tomrader.
    """
    cleaned = _CMS_BOILERPLATE_RE.sub('', text)
    # Kolla om rader med bara e-postlänk i slutet troligen är del av
    # "Sidan publicerad av"-blocket
    cleaned = re.sub(
        r'\n\[?[\w.+-]+@[\w.-]+\]?\(mailto:[^)]+\)\s*$',
        '', cleaned
    )
    # Städa upp: max två tomrader i rad
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()




# ─────────────────────────────────────────────────────────────
#  DATABAS  (utökad med ETag / Last-Modified för conditional GET)
# ─────────────────────────────────────────────────────────────
class AsyncCrawlDatabase:
    """Cache med stöd för ETag och Last-Modified för 304-respons.

    Batched commits: ändringar samlas och commitas var COMMIT_BATCH_SIZE skrivning
    eller var COMMIT_BATCH_SECONDS sekund — vilket som kommer först. Detta
    eliminerar en fsync per sida.
    """

    COMMIT_BATCH_SIZE = 25
    COMMIT_BATCH_SECONDS = 5.0

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None
        self._pending_writes = 0
        self._last_commit = time.monotonic()
        self._commit_lock = asyncio.Lock()

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")  # snabbare, fortfarande crash-säkert i WAL
        await self._init_db()

    async def _init_db(self):
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS page_cache (
                url TEXT PRIMARY KEY,
                content_hash TEXT,
                title TEXT,
                crawled_at TEXT,
                content_length INTEGER,
                etag TEXT,
                last_modified TEXT
            )
        ''')
        # Migration från äldre schema (lägger bara till om de saknas)
        async with self.conn.execute("PRAGMA table_info(page_cache)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if 'etag' not in cols:
            await self.conn.execute("ALTER TABLE page_cache ADD COLUMN etag TEXT")
        if 'last_modified' not in cols:
            await self.conn.execute("ALTER TABLE page_cache ADD COLUMN last_modified TEXT")
        # Utgående länkar sparas så att en 304-sida ändå kan "spelas upp" och
        # dess undersidor besökas (annars stannar inkrementell crawl på startsidan).
        if 'links_json' not in cols:
            await self.conn.execute("ALTER TABLE page_cache ADD COLUMN links_json TEXT")
        if 'filename' not in cols:
            await self.conn.execute("ALTER TABLE page_cache ADD COLUMN filename TEXT")
        # Äldre poster saknar säker tid för en verklig HTTP-kontroll: kontrollera
        # dem igen i stället för att ärva crawled_at från ett sitemap-hopp.
        for name in ('fetched_at', 'checked_at', 'fetch_url', 'source_url', 'processing_profile', 'output_hash'):
            if name not in cols:
                await self.conn.execute(f"ALTER TABLE page_cache ADD COLUMN {name} TEXT")
        if 'rendered' not in cols:
            await self.conn.execute('ALTER TABLE page_cache ADD COLUMN rendered INTEGER DEFAULT 0')
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_page_cache_hash ON page_cache(content_hash)")
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS doc_cache (
                url TEXT PRIMARY KEY,
                filename TEXT,
                size_bytes INTEGER,
                etag TEXT,
                last_modified TEXT,
                referer_url TEXT,
                referer_title TEXT,
                link_text TEXT,
                downloaded_at TEXT
            )
        ''')
        async with self.conn.execute("PRAGMA table_info(doc_cache)") as cur:
            doc_cols = {row[1] for row in await cur.fetchall()}
        for name in ('content_hash', 'converted_hash', 'md_filename', 'conversion_profile',
                     'fetched_at', 'conversion_context'):
            if name not in doc_cols:
                await self.conn.execute(f"ALTER TABLE doc_cache ADD COLUMN {name} TEXT")
        await self.conn.commit()

    async def get_cache(self, url: str) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT content_hash, title, crawled_at, content_length, etag, "
            "last_modified, links_json, filename, fetched_at, checked_at, "
            "fetch_url, processing_profile, output_hash, source_url, rendered "
            "FROM page_cache WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    links = json.loads(row[6]) if row[6] else None
                except ValueError:
                    links = None
                return {
                    'hash': row[0],
                    'title': row[1],
                    'crawled_at': row[2],
                    'content_length': row[3],
                    'etag': row[4],
                    'last_modified': row[5],
                    'links': links,
                    'filename': row[7],
                    'fetched_at': row[8],
                    'checked_at': row[9],
                    'fetch_url': row[10],
                    'processing_profile': row[11],
                    'output_hash': row[12],
                    'source_url': row[13],
                    'rendered': bool(row[14]),
                }
        return None

    async def save_cache(self, url: str, content_hash: str, title: str,
                         length: int, etag: Optional[str] = None,
                         last_modified: Optional[str] = None,
                         links: Optional[List[Dict]] = None,
                         filename: Optional[str] = None, fetch_url: Optional[str] = None,
                         processing_profile: Optional[str] = None, output_hash: Optional[str] = None,
                         source_url: Optional[str] = None, rendered: bool = False):
        await self.conn.execute(
            'INSERT OR REPLACE INTO page_cache '
            '(url, content_hash, title, crawled_at, content_length, etag, '
            'last_modified, links_json, filename, fetched_at, checked_at, '
            'fetch_url, processing_profile, output_hash, source_url, rendered) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (url, content_hash, title, datetime.now().isoformat(),
             length, etag, last_modified,
             json.dumps(links, ensure_ascii=False) if links is not None else None,
             filename, datetime.now().isoformat(), datetime.now().isoformat(),
             fetch_url, processing_profile, output_hash, source_url, int(rendered))
        )
        await self._maybe_commit()

    async def find_original_by_hash(self, content_hash: str, exclude_url: str) -> Optional[str]:
        """URL till en annan sida med samma innehåll som faktiskt har en sparad fil."""
        async with self.conn.execute(
            "SELECT url FROM page_cache WHERE content_hash = ? AND url != ? "
            "AND filename IS NOT NULL LIMIT 1", (content_hash, exclude_url)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def mark_page_unsaved(self, url: str):
        await self.conn.execute(
            "UPDATE page_cache SET filename=NULL, etag=NULL, last_modified=NULL WHERE url=?", (url,))
        await self._maybe_commit()

    async def get_page_files(self):
        async with self.conn.execute(
                "SELECT url, filename, processing_profile FROM page_cache ORDER BY url") as cursor:
            return await cursor.fetchall()

    async def delete_page(self, url: str):
        await self.conn.execute("DELETE FROM page_cache WHERE url = ?", (url,))
        await self._maybe_commit()

    async def get_unseen_since(self, iso_ts: str) -> List[Tuple[str, str]]:
        """Sidor i cachen som inte besökts sedan `iso_ts` (kandidater för borttagna sidor)."""
        async with self.conn.execute(
            "SELECT url, title FROM page_cache WHERE crawled_at < ? ORDER BY url",
            (iso_ts,)
        ) as cursor:
            return await cursor.fetchall()

    async def get_doc(self, url: str) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT filename, size_bytes, etag, last_modified, referer_url, "
            "referer_title, link_text, content_hash, converted_hash, md_filename, "
            "conversion_profile, fetched_at, conversion_context FROM doc_cache WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {'filename': row[0], 'size_bytes': row[1], 'etag': row[2],
                'last_modified': row[3], 'referer_url': row[4] or '',
                'referer_title': row[5] or '', 'link_text': row[6] or '',
                'content_hash': row[7], 'converted_hash': row[8],
                'md_filename': row[9], 'conversion_profile': row[10],
                'fetched_at': row[11], 'conversion_context': row[12]}

    async def save_doc(self, url: str, filename: str, size_bytes: int,
                       etag: Optional[str], last_modified: Optional[str],
                       referer_url: str = "", referer_title: str = "",
                       link_text: str = "", content_hash: Optional[str] = None,
                       fetched_at: Optional[str] = None):
        prior = await self.get_doc(url)
        same_version = bool(prior and content_hash and prior['content_hash'] == content_hash)
        await self.conn.execute(
            'INSERT OR REPLACE INTO doc_cache (url, filename, size_bytes, etag, '
            'last_modified, referer_url, referer_title, link_text, downloaded_at, '
            'content_hash, converted_hash, md_filename, conversion_profile, fetched_at, conversion_context) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (url, filename, size_bytes, etag, last_modified, referer_url,
             referer_title, link_text, datetime.now().isoformat(), content_hash,
             prior['converted_hash'] if same_version else None,
             prior['md_filename'] if same_version else None,
             prior['conversion_profile'] if same_version else None,
             datetime.now().isoformat(timespec='seconds') if fetched_at is None else fetched_at,
             prior['conversion_context'] if same_version else None))
        await self._maybe_commit()

    async def mark_doc_converted(self, url: str, content_hash: str,
                                 md_filename: str, profile: str, context_hash: Optional[str] = None):
        await self.conn.execute(
            "UPDATE doc_cache SET converted_hash=?, md_filename=?, conversion_profile=?, conversion_context=? "
            "WHERE url=? AND content_hash=?",
            (content_hash, md_filename, profile, context_hash, url, content_hash))
        await self._maybe_commit()

    async def invalidate_doc_text(self, url: str):
        await self.conn.execute(
            "UPDATE doc_cache SET converted_hash=NULL, md_filename=NULL, "
            "conversion_profile=NULL, conversion_context=NULL WHERE url=?", (url,))
        await self._maybe_commit()

    async def delete_doc(self, url: str):
        await self.conn.execute("DELETE FROM doc_cache WHERE url=?", (url,))
        await self._maybe_commit()

    async def get_all_docs(self) -> List[Dict]:
        keys = ('url', 'filename', 'size_bytes', 'etag', 'last_modified', 'referer_url',
                'referer_title', 'link_text', 'content_hash', 'converted_hash',
                'md_filename', 'conversion_profile', 'fetched_at', 'conversion_context')
        async with self.conn.execute(
                f"SELECT {', '.join(keys)} FROM doc_cache ORDER BY url") as cur:
            return [dict(zip(keys, row)) for row in await cur.fetchall()]

    async def touch_cache(self, url: str, etag: Optional[str] = None,
                          last_modified: Optional[str] = None, checked: bool = True):
        """Sedd tid uppdateras alltid; kontrolltid bara vid ett verkligt HTTP-svar."""
        params = [datetime.now().isoformat()]
        sql = "UPDATE page_cache SET crawled_at=?"
        if checked:
            sql += ", checked_at=?"
            params.append(datetime.now().isoformat())
        if etag is not None:
            sql += ", etag=?"
            params.append(etag)
        if last_modified is not None:
            sql += ", last_modified=?"
            params.append(last_modified)
        sql += " WHERE url=?"
        params.append(url)
        await self.conn.execute(sql, params)
        await self._maybe_commit()

    async def _maybe_commit(self):
        async with self._commit_lock:
            self._pending_writes += 1
            elapsed = time.monotonic() - self._last_commit
            if (self._pending_writes >= self.COMMIT_BATCH_SIZE
                    or elapsed >= self.COMMIT_BATCH_SECONDS):
                await self.conn.commit()
                self._pending_writes = 0
                self._last_commit = time.monotonic()

    async def flush(self):
        async with self._commit_lock:
            if self._pending_writes > 0:
                await self.conn.commit()
                self._pending_writes = 0
                self._last_commit = time.monotonic()

    async def get_all_records(self):
        async with self.conn.execute(
            "SELECT url, title, crawled_at, content_hash, filename FROM page_cache "
            "ORDER BY crawled_at DESC"
        ) as cursor:
            return await cursor.fetchall()

    async def close(self):
        if self.conn:
            await self.flush()
            await self.conn.close()
            self.conn = None


# ─────────────────────────────────────────────────────────────
#  RATE LIMITER & QUEUE
# ─────────────────────────────────────────────────────────────
class PerDomainRateLimiter:
    """Async-vänlig per-domän rate limiter."""

    MAX_DELAY = 10.0

    def __init__(self, requests_per_second: float):
        self.delay = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self.base_delay = self.delay
        self.domain_minimums: Dict[str, float] = {}
        self.domain_delays: Dict[str, float] = {}
        self.last_requests: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def async_wait(self, domain: str):
        async with self._lock:
            now = datetime.now()
            last_req = self.last_requests.get(domain, datetime.min)
            elapsed = (now - last_req).total_seconds()
            delay = max(self.domain_delays.get(domain, self.delay), self.domain_minimums.get(domain, 0))
            if elapsed < delay:
                sleep_time = delay - elapsed
                self.last_requests[domain] = now + timedelta(seconds=sleep_time)
            else:
                sleep_time = 0
                self.last_requests[domain] = now
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    def slow_down(self, factor: float = 1.5, domain: Optional[str] = None):
        """Adaptiv throttling: anropas vid 429/503 så att vi blir långsammare."""
        delay = self.domain_delays.get(domain, self.base_delay) if domain else self.delay
        delay = min(max(delay, 0.5) * factor, max(self.MAX_DELAY, self.base_delay))
        if domain:
            self.domain_delays[domain] = delay
        else:
            self.delay = delay

    def succeeded(self, domain: Optional[str] = None):
        """Återgå gradvis till inställd takt; Crawl-delay är ett separat golv."""
        delay = self.domain_delays.get(domain, self.base_delay) if domain else self.delay
        delay = max(self.base_delay, delay * 0.9)
        if delay - self.base_delay < 0.01:
            delay = self.base_delay
        if domain:
            self.domain_delays[domain] = delay
        else:
            self.delay = delay

    def set_minimum(self, domain: str, delay: float):
        self.domain_minimums[domain] = max(0, delay)


_YEAR_IN_URL_RE = re.compile(r'(?<!\d)20[0-3]\d(?!\d)')


class PriorityURLQueue:
    """Prioritetskö som rangordnar sidor efter "intressanthet"."""

    DEFAULT_BOOST_WORDS = ("policy", "om-oss", "kontakt", "regler", "guide")
    DEFAULT_PENALTY_WORDS = ("nyheter", "arkiv", "blogg", "kalender")

    def __init__(self, ignore_query_params: Optional[List[str]] = None,
                 boost_words: Optional[List[str]] = None,
                 penalty_words: Optional[List[str]] = None):
        self.queue: queue.PriorityQueue = queue.PriorityQueue()
        self.seen_urls: Set[str] = set()
        self._queued: Dict[str, Tuple[int, int]] = {}
        self._next_token = 0
        self._lock = threading.Lock()
        self.ignore_query_params = ignore_query_params

        self.boost_words = [w.lower() for w in
                            (boost_words if boost_words is not None
                             else self.DEFAULT_BOOST_WORDS)]
        self.penalty_words = [w.lower() for w in
                              (penalty_words if penalty_words is not None
                               else self.DEFAULT_PENALTY_WORDS)]

    def mark_seen(self, url: str):
        with self._lock:
            key = normalize_url(url, self.ignore_query_params)
            self.seen_urls.add(key)
            self._queued.pop(key, None)

    def add_url(self, url: str, depth: int = 0,
                base_priority: int = CrawlPriority.MEDIUM.value) -> bool:
        normalized = normalize_url(url, self.ignore_query_params)
        with self._lock:
            score = base_priority
            lower_url = normalized.lower()
            if any(w in lower_url for w in self.boost_words):
                score -= 3
            # Årtal i sökvägen (arkiv, protokoll) nedprioriteras — men bara som
            # fristående årtal, inte vilken siffersekvens som helst.
            if (any(w in lower_url for w in self.penalty_words)
                    or _YEAR_IN_URL_RE.search(urlparse(lower_url).path)):
                score += 5
            score += depth
            if normalized in self.seen_urls:
                queued = self._queued.get(normalized)
                if queued is None or score >= queued[0]:
                    return False
            # Aktuella länkar får ersätta gamla cache-URL:er som ännu inte hämtats.
            # En token gör den äldre köposten ogiltig utan att sortera om hela kön.
            self._next_token += 1
            token = self._next_token
            self._queued[normalized] = (score, token)
            self.queue.put((score, time.time(), depth, request_url(url), normalized, token))
            self.seen_urls.add(normalized)
            return True

    def get_next(self) -> Optional[Tuple[int, str]]:
        with self._lock:
            while True:
                try:
                    item = self.queue.get_nowait()
                except queue.Empty:
                    return None
                queued = self._queued.get(item[4])
                if queued and queued[1] == item[5]:
                    self._queued.pop(item[4])
                    return (item[2], item[3])

    def size(self) -> int:
        with self._lock:
            return len(self._queued)


# ─────────────────────────────────────────────────────────────
#  LOGIN-DETEKTOR
# ─────────────────────────────────────────────────────────────
class LoginDetector:
    """Avgör om en respons faktiskt representerar en utgången session.

    Tre lager av signaler, från starkast till svagast:
      1. URL-redirect: slut-URL:en innehåller en känd login-sökväg
         (t.ex. /login, /cas/login, /adfs/ls, ?SAMLRequest=, /saml2/sso)
      2. HTTP-status: 401 / 403
      3. Innehåll: små HTML-sidor som ser ut som login-formulär
         (få forms, exakt ett password-fält, eller känd login-text)

    SAML-matchning görs på URL-mönster (/saml/, /saml2/) istället för
    substrängar i brödtext — "saml" förekommer i svenska ord som *samla,
    samling, samlade* och ger annars falska positiva. Password-fält kräver
    att sidan är liten (<15 kB) med max 2 forms för att undvika att sidor
    med login-widget i headern flaggas felaktigt.
    """

    DEFAULT_URL_PATTERNS = (
        '/login', '/signin', '/sign-in', '/log-in',
        '/cas/login', '/adfs/ls', '/oauth/authorize',
        '/saml/', '/saml2/', '/sso/',
        'samlrequest=', 'returnurl=', 'redirect_uri=',
        'logon.aspx', '/auth/realms/',
    )

    STRONG_CONTENT_SIGNALS = (
        'id="loginform"', "id='loginform'",
        'class="login-form"', 'class="loginform"',
        'inloggning krävs', 'du måste logga in',
        'din session har gått ut', 'session expired',
        'please sign in to continue', 'sign in to continue',
        'authentication required',
    )

    def __init__(self, extra_url_patterns: Optional[List[str]] = None,
                 max_login_html_size: int = 30000):
        self.url_patterns = list(self.DEFAULT_URL_PATTERNS)
        if extra_url_patterns:
            self.url_patterns.extend(p.lower() for p in extra_url_patterns)
        self.max_login_html_size = max_login_html_size

    def is_login_redirect(self, original_url: str, final_url: Optional[str]) -> bool:
        """Stark signal: sidan redirecterade till en login-URL."""
        if not final_url:
            return False
        # Bara intressant om vi faktiskt redirectades NÅGONSTANS, eller om slut-URL:en
        # tydligt är en login-sida även utan redirect.
        final_lower = final_url.lower()
        return any(p in final_lower for p in self.url_patterns)

    def is_login_status(self, http_status: Optional[int]) -> bool:
        return http_status in (401, 407)

    def is_login_content(self, html: Optional[str]) -> bool:
        """Innehållsbaserad heuristik. Endast små HTML-sidor undersöks
        för att undvika falska positiva på vanliga innehållssidor."""
        if not html:
            return False
        if len(html) > self.max_login_html_size:
            return False  # Innehållssidor är typiskt 50+ kB; login-sidor är små
        html_lower = html.lower()

        # Stark signal: explicit login-text eller login-form-id
        if any(s in html_lower for s in self.STRONG_CONTENT_SIGNALS):
            return True

        # Svagare: ett ENDA password-fält i en liten sida med få forms.
        # Vi kräver att det är en kort sida (<15 kB) och har max 2 forms.
        if 'type="password"' in html_lower or "type='password'" in html_lower:
            if len(html) < 15000:
                form_count = html_lower.count('<form')
                pw_count = (html_lower.count('type="password"')
                            + html_lower.count("type='password'"))
                if form_count <= 2 and pw_count == 1:
                    return True
        return False

    def detect(self, original_url: str, final_url: Optional[str],
               http_status: Optional[int], html: Optional[str]) -> bool:
        if self.is_login_redirect(original_url, final_url):
            return True
        if self.is_login_status(http_status):
            return True
        if self.is_login_content(html):
            return True
        return False


# ─────────────────────────────────────────────────────────────
#  DOKUMENT-MANIFEST
# ─────────────────────────────────────────────────────────────
class DocumentManifest:
    """Spårar nedladdade dokument och var de länkades från.

    Producerar en `manifest.json` i utmappen som låter ett RAG-system koppla
    en PDF-text tillbaka till sin ursprungssida på intranätet. Utan detta
    blir varje PDF en "isolerad ö" — modellen kan citera innehåll men inte
    säga vilken intranätsida som beskriver dokumentet.

    Strukturen för varje dokument:
      filename       — det faktiska filnamnet på disk
      download_url   — direktlänken som crawlern hämtade
      referer_url    — sidan på intranätet som länkade till dokumentet
      referer_title  — titel på den länkande sidan
      link_text      — den klickbara textens innehåll (ofta beskrivande)
      size_bytes     — filstorlek
      downloaded_at  — ISO-tidsstämpel
      additional_referers — om PDFen länkas från flera sidor
    """

    def __init__(self):
        # download_url → list[{referer_url, referer_title, link_text, found_at}]
        self._referers: Dict[str, List[Dict[str, str]]] = {}
        # download_url → {filename, size_bytes, downloaded_at}
        self._downloads: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()

    async def record_link(self, doc_url: str, referer_url: str,
                          referer_title: str, link_text: str):
        """Anropas i process_page när en länk till ett dokument hittas."""
        entry = {
            "referer_url": referer_url,
            "referer_title": referer_title or "",
            "link_text": (link_text or "").strip(),
            "found_at": datetime.now().isoformat(),
        }
        async with self._lock:
            referer_list = self._referers.setdefault(doc_url, [])
            # Dedupa: samma referer_url ska inte registreras två gånger
            if not any(r["referer_url"] == referer_url for r in referer_list):
                referer_list.append(entry)

    async def record_download(self, doc_url: str, filename: str,
                              size_bytes: int):
        """Anropas i download_document efter framgångsrik nedladdning."""
        async with self._lock:
            self._downloads[doc_url] = {
                "filename": filename,
                "size_bytes": size_bytes,
                "downloaded_at": datetime.now().isoformat(),
            }

    def build(self, domain: str) -> Dict:
        """Producerar slutlig manifest-struktur. Kallas en gång vid crawl-slut."""
        documents = []
        # Sortera på filnamn för stabil output mellan körningar
        for doc_url in sorted(self._downloads.keys(),
                              key=lambda u: self._downloads[u]["filename"]):
            dl_info = self._downloads[doc_url]
            referers = self._referers.get(doc_url, [])

            entry = {
                "filename": dl_info["filename"],
                "download_url": doc_url,
                "size_bytes": dl_info["size_bytes"],
                "downloaded_at": dl_info["downloaded_at"],
            }

            if referers:
                primary = referers[0]
                entry["referer_url"] = primary["referer_url"]
                entry["referer_title"] = primary["referer_title"]
                entry["link_text"] = primary["link_text"]
                if len(referers) > 1:
                    entry["additional_referers"] = referers[1:]
            documents.append(entry)

        # Inkludera även dokument som länkades men inte laddades ner
        # (kan hända vid avbruten crawl) — bra för felsökning
        orphan_links = []
        for doc_url, refs in self._referers.items():
            if doc_url not in self._downloads:
                orphan_links.append({
                    "download_url": doc_url,
                    "referers": refs,
                })

        manifest = {
            "generated_at": datetime.now().isoformat(),
            "domain": domain,
            "document_count": len(documents),
            "documents": documents,
        }
        if orphan_links:
            manifest["orphan_links"] = orphan_links
            manifest["orphan_count"] = len(orphan_links)
        return manifest


# ─────────────────────────────────────────────────────────────
#  DOKUMENT → MARKDOWN KONVERTERARE
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  ASYNC WEBB CRAWLER CORE
# ─────────────────────────────────────────────────────────────
# Returneras av nätverkslagret för konsekvent hantering uppströms
@dataclass
class FetchResult:
    body: Optional[bytes] = None         # Råa bytes (eller None om body inte hämtades)
    text: Optional[str] = None           # Avkodad text (om relevant content-type)
    content_type: str = ""
    final_url: str = ""                  # URL efter ev. redirects
    status: int = 0
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False           # True om servern svarade 304


# Filextensioner som indikerar binära dokument — för dessa gör vi HEAD först
DOCUMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.odt', '.ods', '.odp', '.rtf', '.csv', '.zip',
}
# Filextensioner vi aldrig hämtar (bilder, fonts, video, etc.)
# .zip ligger MEDVETET inte här — den finns i DOCUMENT_EXTENSIONS, så
# zip-arkiv kan laddas ner som dokument om download_docs är på.
SKIP_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.bmp', '.tiff',
    '.rar', '.exe', '.mp4', '.mp3', '.avi', '.mov', '.css', '.js',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
}


class RobotsDeniedError(Exception):
    """En hämtning eller redirect som robots.txt inte tillåter."""


class BlockedHostError(Exception):
    """Mål-adressen är inte tillåten (t.ex. privat IP när startsidan är publik)."""


def is_non_public_host(host: Optional[str]) -> bool:
    """True om `host` är en IP-literal eller localhost som inte är publik."""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False      # vanligt värdnamn — kontrolleras av SafeResolver vid DNS-uppslag


if HAS_AIOHTTP:
    class SafeResolver(aiohttp.abc.AbstractResolver):
        """DNS-resolver som vägrar privata/loopback/link-local-adresser.

        Skyddar mot SSRF via redirects, sitemaps och länkar när en publik sajt
        crawlas. Intranät (privat startadress) eller `allow_private_hosts`
        stänger av skyddet.
        """

        def __init__(self, allow_private: bool):
            self.allow_private = allow_private
            self._inner = aiohttp.ThreadedResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            infos = await self._inner.resolve(host, port, family)
            if not self.allow_private:
                for info in infos:
                    try:
                        if not ipaddress.ip_address(info["host"]).is_global:
                            raise OSError(f"Blockerad icke-publik adress "
                                          f"{info['host']} för {host}")
                    except ValueError:
                        continue
            return infos

        async def close(self):
            await self._inner.close()


class _ProxySocketReader:
    """SOCKS-ström utan Proactor-transportens felkänsliga avslut på Windows."""

    def __init__(self, sock: socket.socket):
        self.sock = sock

    async def read(self, size: int = 65536) -> bytes:
        return await asyncio.get_running_loop().sock_recv(self.sock, size)

    async def readexactly(self, size: int) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            block = await self.read(size - len(parts))
            if not block:
                raise asyncio.IncompleteReadError(bytes(parts), size)
            parts.extend(block)
        return bytes(parts)


class _ProxySocketWriter:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        if self.buffer:
            data = bytes(self.buffer)
            self.buffer.clear()
            await asyncio.get_running_loop().sock_sendall(self.sock, data)

    def close(self):
        if self.sock.fileno() == -1:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            self.sock.close()

    async def wait_closed(self):
        # close() stänger råsocketen synkront; ingen transportcallback väntas in.
        return


class BrowserEgressProxy:
    """Lokal SOCKS5-proxy: varje TCP-mål DNS-kontrolleras och ansluts via samma IP.

    Chromium skickar även redirects, popup-fönster och nedladdningar genom proxyn.
    Till skillnad från page.route finns inget glapp mellan DNS-kontroll och
    webbläsarens eget uppslag. TLS går oförändrat mellan Chromium och målservern.
    """

    def __init__(self, log_fn=None):
        self.resolver = SafeResolver(allow_private=False)
        self.server = None
        self._accept_task = None
        self.tasks: Set[asyncio.Task] = set()
        self.writers: Set[_ProxySocketWriter] = set()
        self._closing = False
        self._log = log_fn
        self.url = ""

    async def start(self) -> str:
        self._closing = False
        # Python/Windows kan kasta vid transportens shutdown och aldrig frigöra
        # Server.wait_closed. Råa async-sockets undviker både häng och läckta handles.
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.server.setblocking(False)
            self.server.bind(('127.0.0.1', 0))
            self.server.listen(socket.SOMAXCONN)
            self.url = f"socks5://127.0.0.1:{self.server.getsockname()[1]}"
            self._accept_task = asyncio.create_task(self._accept_clients())
        except BaseException:
            self.server.close()
            raise
        return self.url

    async def _accept_clients(self):
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                client, _ = await loop.sock_accept(self.server)
            except OSError:
                if self._closing:
                    return
                raise
            client.setblocking(False)
            self._accept(_ProxySocketReader(client), _ProxySocketWriter(client))

    def _accept(self, reader, writer):
        # Registrera synkront: close() kan annars missa en accepterad klient
        # vars coroutine ännu inte börjat köras och sedan vänta på den för alltid.
        if self._closing:
            writer.close()
            return
        self.writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)

    async def _connect(self, host: str, port: int):
        if not 0 < port <= 65535 or is_non_public_host(host):
            raise BlockedHostError("Webbläsarens mål är inte publikt")
        infos = await self.resolver.resolve(host, port, family=socket.AF_UNSPEC)
        if not infos or any(not ipaddress.ip_address(i['host']).is_global for i in infos):
            raise BlockedHostError("Webbläsarens DNS-mål är inte publikt")
        last_error = None
        for info in infos:
            sock = socket.socket(socket.AF_INET6 if ':' in info['host'] else socket.AF_INET,
                                 socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                # Anslut till den granskade IP-adressen, aldrig till värdnamnet igen.
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(
                    sock, (info['host'], port)), timeout=10)
                return _ProxySocketReader(sock), _ProxySocketWriter(sock)
            except asyncio.CancelledError:
                sock.close()
                raise
            except (OSError, asyncio.TimeoutError) as e:
                sock.close()
                last_error = e
            except BaseException:
                sock.close()
                raise
        raise OSError("Webbläsarens mål kunde inte nås") from last_error

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote_writer = None
        pumps = []
        try:
            async with asyncio.timeout(15):
                version, n_methods = await reader.readexactly(2)
                methods = await reader.readexactly(n_methods)
                if version != 5 or 0 not in methods:
                    writer.write(b'\x05\xff')
                    await writer.drain()
                    return
                writer.write(b'\x05\x00')
                await writer.drain()
                version, command, reserved, address_type = await reader.readexactly(4)
                if version != 5 or command != 1 or reserved != 0:
                    raise BlockedHostError("Otillåtet proxykommando")
                if address_type == 1:
                    host = str(ipaddress.ip_address(await reader.readexactly(4)))
                elif address_type == 4:
                    host = str(ipaddress.ip_address(await reader.readexactly(16)))
                elif address_type == 3:
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode('ascii')
                else:
                    raise BlockedHostError("Otillåten proxyadress")
                port = int.from_bytes(await reader.readexactly(2), 'big')
                remote_reader, remote_writer = await self._connect(host, port)
                writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6)
                await writer.drain()

            async def pump(src, dst):
                while data := await asyncio.wait_for(src.read(65536), timeout=120):
                    dst.write(data)
                    await dst.drain()

            pumps = [asyncio.create_task(pump(reader, remote_writer)),
                     asyncio.create_task(pump(remote_reader, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError,
                asyncio.TimeoutError, BlockedHostError):
            # Fel och nekade mål leder aldrig till direktanslutning som reservväg.
            if remote_writer is None:
                if self._log:
                    self._log("  ⛔ Webbläsarens nätverksanslutning nekades eller misslyckades",
                              LogLevel.DEBUG)
                try:
                    writer.write(b'\x05\x02\x00\x01' + b'\x00' * 6)
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for pump_task in pumps:
                pump_task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (remote_writer, writer):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), timeout=3)
                    except (OSError, asyncio.TimeoutError):
                        pass
            self.tasks.discard(task)
            self.writers.discard(writer)

    async def close(self):
        self._closing = True
        if self._accept_task:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
            self._accept_task = None
        if self.server:
            self.server.close()
        for writer in list(self.writers):
            writer.close()
        # Accepterade klienter är registrerade innan accept-loopen kan avbrytas.
        await asyncio.sleep(0)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)
        self.writers.clear()
        await self.resolver.close()


# Alla crawlerinstanser delar låset; PyMuPDF får inte köras parallellt i trådar.




class RobotsRules:
    """Tunn adapter: Protego (stöder * och $) om installerat, annars stdlib."""

    def __init__(self, text: str, token: str = ROBOTS_TOKEN):
        self.token = token
        self._protego = None
        self._std = None
        if HAS_PROTEGO:
            try:
                self._protego = Protego.parse(text)
            except Exception:
                self._protego = None
        if self._protego is None:
            self._std = RobotFileParser()
            self._std.parse(text.splitlines())

    def can_fetch(self, url: str) -> bool:
        if self._protego is not None:
            return bool(self._protego.can_fetch(url, self.token))
        return self._std.can_fetch(self.token, url)

    def crawl_delay(self) -> Optional[float]:
        try:
            if self._protego is not None:
                d = self._protego.crawl_delay(self.token)
            else:
                d = self._std.crawl_delay(self.token)
            return float(d) if d else None
        except Exception:
            return None


_BLOCK_PAGE_SIGNALS = (
    'just a moment...', 'cf-browser-verification', '_cf_chl_opt',
    'attention required! | cloudflare', 'checking your browser before accessing',
    'verify you are human', 'unusual traffic from your computer',
    'enable javascript and cookies to continue', 'ddos protection by',
    'du har blockerats', 'din förfrågan har blockerats',
)
_SOFT_404_TITLE_RE = re.compile(
    r'\b404\b|hittades inte|kunde inte hittas|page not found|sidan saknas|'
    r'finns inte|not found$', re.IGNORECASE)
_HIDDEN_STYLE_RE = re.compile(
    r'(?:display\s*:\s*none|'
    r'visibility\s*:\s*hidden|'
    r'font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|pt|%)?|'
    r'opacity\s*:\s*0(?:\.0+)?|'
    r'text-indent\s*:\s*-[0-9]{4,}px|'
    r'left\s*:\s*-[0-9]{4,}px)'
    r'(?:\s*!important)?\s*(?:;|\b|$)',
    re.IGNORECASE
)
_NOISE_CLASS_RE = re.compile(
    r'^(cookie|cookies|banner|menu|nav|navbar|navigation|sidebar|footer|share|social)'
    r'([-_].*)?$', re.IGNORECASE)
_SAFE_DOC_EXT_RE = re.compile(r'^\.[a-z0-9]{1,6}$')
_CRAWL_TRAP_RE = re.compile(r'(/[^/]+/[^/]+)\1\1|(/[^/]+)\2\2')

KNOWN_CONFIG_KEYS = {
    "name", "start_url", "output_dir", "delay", "max_pages", "max_depth",
    "concurrency", "playwright_concurrency", "save_format", "headless_mode",
    "find_sitemap", "respect_robots", "use_hybrid", "use_trafilatura",
    "download_docs", "convert_docs_to_md", "strict_domain", "allowed_domains",
    "include_subdomains", "exclude_keywords", "require_keywords",
    "remove_email", "remove_phone", "remove_pnr", "remove_ip",
    "keep_role_emails", "incremental", "cookie_file", "user_agent",
    "ignore_https_errors", "allow_private_hosts", "max_page_mb",
    "max_download_mb", "max_path_segments", "max_query_params",
    "keep_query_params", "ignore_query_params", "boost_words", "penalty_words",
    "respect_canonical", "dedupe_content", "use_sitemap_lastmod",
    "sitemap_lastmod_max_age_days", "languages", "export_jsonl",
    "ocr", "ocr_languages", "ocr_max_pages",
    "login_browser_headless",      # för automatiska tester av inloggningsflödet
    "quarantine_flagged_chunks",   # karantänsätt chunks med säkerhetsflaggor (standard True)
    "session_check_url",          # resurs som kräver inloggning och som kontot har rätt att läsa
    'conversion_workers', 'conversion_queue_size', 'conversion_timeout',
    'generation_keep', 'generation_min_age_hours',
}

_FLOAT_CONFIG_RULES = {
    'delay': (0.5, 0), 'max_page_mb': (10, 0.000001),
    'max_download_mb': (100, 0.000001), 'sitemap_lastmod_max_age_days': (14, 0),
    'conversion_timeout': (180, 0.1),
    'generation_min_age_hours': (24, 0.01),
}
_INT_CONFIG_RULES = {
    'max_pages': (0, 0), 'max_depth': (0, 0), 'concurrency': (10, 1),
    'playwright_concurrency': (2, 1), 'max_path_segments': (15, 1),
    'max_query_params': (5, 0), 'ocr_max_pages': (50, 0),
    'conversion_workers': (2, 1), 'conversion_queue_size': (8, 1),
    'generation_keep': (0, 0),
}
_LIST_CONFIG_KEYS = {
    'allowed_domains', 'exclude_keywords', 'require_keywords', 'keep_query_params',
    'ignore_query_params', 'boost_words', 'penalty_words',
}
_BOOL_CONFIG_KEYS = {
    'find_sitemap', 'respect_robots', 'use_hybrid', 'use_trafilatura', 'download_docs',
    'convert_docs_to_md', 'strict_domain', 'include_subdomains', 'remove_email',
    'remove_phone', 'remove_pnr', 'remove_ip', 'keep_role_emails', 'incremental',
    'ignore_https_errors', 'allow_private_hosts', 'respect_canonical', 'dedupe_content',
    'use_sitemap_lastmod', 'export_jsonl', 'ocr', 'login_browser_headless',
    'quarantine_flagged_chunks',
}


def normalize_site_config(site: dict) -> Dict:
    """Gemensamma typer och standardvärden för GUI och CLI efter validering."""
    result = dict(site)
    result.setdefault('incremental', True)
    for key, (default, _) in _FLOAT_CONFIG_RULES.items():
        result[key] = float(site.get(key, default))
    for key, (default, _) in _INT_CONFIG_RULES.items():
        result[key] = int(float(site.get(key, default)))
    for key in _LIST_CONFIG_KEYS:
        if key in site:
            result[key] = [value.strip() for value in site[key] if value.strip()]
    for key in ('exclude_keywords', 'require_keywords'):
        result[key] = [value.lower() for value in result.get(key, [])]
    return result


def validate_site_config(site: dict) -> Tuple[List[str], List[str]]:
    """Returnerar (fel, varningar) för en sajt-post i sites.json."""
    errors, warnings = [], []
    if not isinstance(site, dict):
        return ["posten är inte ett JSON-objekt"], []
    url = str(site.get("start_url", "")).strip()
    def valid_http_url(value):
        try:
            parsed = urlparse(value)
            return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                    and parsed.port != 0 and not parsed.username and not parsed.password)
        except ValueError:
            return False
    if not valid_http_url(url):
        errors.append('start_url saknas eller är ogiltig (kräver http(s), värd och giltig port utan inloggningsuppgifter)')
    if site.get('session_check_url') and not valid_http_url(str(site['session_check_url'])):
        errors.append('session_check_url måste vara en http(s)-adress utan inloggningsuppgifter')
    for key in site:
        if key not in KNOWN_CONFIG_KEYS:
            warnings.append(f"okänd nyckel ignoreras: {key!r}")
    for key, (_, minimum) in {**_FLOAT_CONFIG_RULES, **_INT_CONFIG_RULES}.items():
        if key in site:
            try:
                value = float(site[key])
                if isinstance(site[key], bool) or not math.isfinite(value) or value < minimum:
                    raise ValueError
                if key in _INT_CONFIG_RULES and value != int(value):
                    raise ValueError
                if key == 'sitemap_lastmod_max_age_days' and value >= 999999999:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                kind = 'heltal' if key in _INT_CONFIG_RULES else 'tal'
                errors.append(f"{key} måste vara ett ändligt {kind} >= {minimum}, fick {site[key]!r}")
    for key in _LIST_CONFIG_KEYS:
        if key in site and (not isinstance(site[key], list)
                            or any(not isinstance(value, str) for value in site[key])):
            errors.append(f'{key} måste vara en lista med textvärden')
    for key in _BOOL_CONFIG_KEYS:
        if key in site and not isinstance(site[key], bool):
            errors.append(f'{key} måste vara true eller false')
    if 'headless_mode' in site and site['headless_mode'] not in ('headless', 'visible', 'login_then_headless'):
        errors.append('headless_mode måste vara headless, visible eller login_then_headless')
    if 'languages' in site and not (isinstance(site['languages'], str)
            or isinstance(site['languages'], list) and all(isinstance(v, str) for v in site['languages'])):
        errors.append('languages måste vara text eller en lista med språk')
    if 'save_format' in site and site['save_format'] not in ('.md', '.json', '.txt', 'Ingen text', 'No text'):
        errors.append('save_format måste vara .md, .json, .txt eller Ingen text')
    return errors, warnings


def clean_page_title(raw: str, domain: str) -> str:
    """Tar bort avslutande sajtnamn ("Avgifter - Tyresö kommun" → "Avgifter")."""
    raw = metadata_line(raw)
    parts = re.split(r'\s+[-–—|·»]\s+', raw)
    if len(parts) >= 2:
        last = parts[-1]
        tokens = [t for t in slugify(last).split('-') if len(t) >= 4]
        dom = domain.lower().replace('-', '')
        if len(last) <= 50 and any(t.replace('-', '') in dom for t in tokens):
            return " - ".join(parts[:-1]).strip() or raw
    return raw




def safe_gunzip(data: bytes, limit: int) -> bytes:
    """gunzip med övre gräns (skydd mot dekompressionsbomber)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError("Dekomprimerad sitemap överskrider storleksgränsen")
    return out


def magic_ok(ext: str, head: bytes) -> bool:
    """Kontrollerar att filens första byte stämmer med förväntad filtyp
    (stoppar t.ex. en HTML-inloggningssida som sparas som .pdf)."""
    ext = ext.lower()
    if ext == '.pdf':
        return b'%PDF' in head[:1024]
    if ext in ('.docx', '.dotx', '.xlsx', '.pptx', '.zip', '.odt', '.ods', '.odp'):
        return head[:2] == b'PK'
    if ext in ('.doc', '.xls', '.ppt'):
        return head[:8] == bytes.fromhex('D0CF11E0A1B11AE1')
    return True




class AsyncWebCrawler:
    LOGIN_EXPIRED_LIMIT = 5      # antal sessionsfel i rad innan crawlen avbryts

    def __init__(self, config: dict, msg_queue: Optional[queue.Queue] = None):
        if not HAS_AIOHTTP or not HAS_AIOSQLITE:
            missing = [n for n, ok in (("aiohttp", HAS_AIOHTTP),
                                       ("aiosqlite", HAS_AIOSQLITE)) if not ok]
            raise RuntimeError(f"Saknade beroenden: {', '.join(missing)} "
                               f"(kör: pip install -r requirements.txt)")
        config = dict(config)
        if 'start_url' in config and '://' not in str(config['start_url']):
            config['start_url'] = 'https://' + str(config['start_url']).strip()
        errors, _ = validate_site_config(config)
        if errors:
            raise ValueError('; '.join(errors))
        config = normalize_site_config(config)
        self.config = config
        self.msg_queue = msg_queue
        self.state = CrawlerState.RUNNING
        self.stats = CrawlStats()
        self.active_tasks = 0
        self.fatal_error: Optional[str] = None
        self._output_root = os.path.abspath(config['output_dir'])
        self._publication = None
        self._export_ready = False
        self._conversion_queue = None
        self._conversion_tasks = []
        self._conversion_pending = 0
        self._crawl_loop = None
        self._url_tasks = set()
        self._conversion_runner = ConversionProcessRunner(
            os.path.join(self._output_root, '.conversion_jobs'),
            workers=config['conversion_workers'], timeout=config['conversion_timeout'])

        # URL-normalisering: parametrar som ska strippas kan justeras per sajt
        keep = {k.lower() for k in config.get("keep_query_params", [])}
        self.ignore_query_params = (
            [p for p in DEFAULT_IGNORE_QUERY_PARAMS if p not in keep]
            + [p.lower() for p in config.get("ignore_query_params", [])])

        start_url = str(config["start_url"]).strip()
        if "://" not in start_url:
            start_url = "https://" + start_url
        self.start_url = request_url(start_url)
        self.output_dir = config["output_dir"]
        self.delay = config["delay"]
        self.max_pages = config["max_pages"]
        self.max_depth = config.get("max_depth", 0)
        self.save_format = config.get("save_format", ".md")
        self.use_hybrid = config.get("use_hybrid", True)
        self.use_trafilatura = config.get("use_trafilatura", HAS_TRAFILATURA)

        # Samtidighet: justerbar via config
        self.concurrency = max(1, int(config.get("concurrency", 10)))
        self.playwright_concurrency = max(1, int(config.get("playwright_concurrency", 2)))

        # Säkerhets- och resursgränser
        self.max_page_bytes = int(float(config.get("max_page_mb", 10)) * 1024 * 1024)
        self.max_download_bytes = int(float(config.get("max_download_mb", 100)) * 1024 * 1024)
        self.max_sitemap_bytes = 50 * 1024 * 1024
        self.max_path_segments = int(config.get("max_path_segments", 15))
        self.max_query_params = int(config.get("max_query_params", 5))
        self.user_agent = config.get("user_agent") or DEFAULT_USER_AGENT
        ua_token = self.user_agent.split("/")[0].split()[0] if self.user_agent else ROBOTS_TOKEN
        self.robots_token = ua_token or ROBOTS_TOKEN
        self._allow_private = bool(config.get("allow_private_hosts", False))

        self.find_sitemap = config.get("find_sitemap", True)
        self.robot_parser: Optional[RobotsRules] = None
        self._robots_rules: Dict[str, RobotsRules] = {}
        self._robots_text: Dict[str, str] = {}
        self._robots_locks: Dict[str, asyncio.Lock] = {}
        self._robots_unreachable: Set[str] = set()
        self._robots_denied: Set[str] = set()

        # Innehållskvalitet
        self.respect_canonical = bool(config.get("respect_canonical", True))
        self.dedupe_content = bool(config.get("dedupe_content", True))
        self.use_sitemap_lastmod = bool(config.get("use_sitemap_lastmod", True))
        self.lastmod_max_age = timedelta(days=float(config.get("sitemap_lastmod_max_age_days", 14)))
        langs = config.get("languages") or []
        if isinstance(langs, str):
            langs = [x for x in re.split(r'[,\s]+', langs) if x]
        self.languages = {str(x).lower().split('-')[0] for x in langs}
        self.sitemap_lastmod: Dict[str, str] = {}
        self._canonical_of: Dict[str, str] = {}
        self._canonical_pending: Dict[str, str] = {}
        self._initial_page_files: Set[str] = set()

        parsed_start = urlparse(self.start_url)
        self.domain = parsed_start.netloc.lower()
        self.base_url = f"{parsed_start.scheme}://{parsed_start.netloc}"
        self.allowed_domains = {d.strip().lower().removeprefix("www.")
                                for d in config.get("allowed_domains", []) if d.strip()}
        self.include_subdomains = bool(config.get("include_subdomains", False))

        os.makedirs(self.output_dir, exist_ok=True)
        self.db = AsyncCrawlDatabase(
            os.path.join(self.output_dir, f"{slugify(self.domain)}_cache.db")
        )
        self.url_queue = PriorityURLQueue(
            ignore_query_params=self.ignore_query_params,
            boost_words=config.get("boost_words"),
            penalty_words=config.get("penalty_words"),
        )
        self.url_queue.add_url(self.start_url, depth=0,
                               base_priority=CrawlPriority.CRITICAL.value)

        self.rate_limiter = PerDomainRateLimiter(
            requests_per_second=1.0 / max(self.delay, 0.05)
        )
        self.downloaded_files: Set[str] = set()
        self.manifest = DocumentManifest()
        self.convert_docs = config.get("convert_docs_to_md", False)
        self.converter: Optional[DocumentConverter] = None
        if self.convert_docs:
            self.converter = DocumentConverter(
                self.output_dir,
                log_fn=self._log,
                pii_cleaner=self.clean_pii,
                ocr=bool(config.get("ocr", True)),
                ocr_languages=str(config.get("ocr_languages", "swe+eng")),
                ocr_max_pages=int(config.get("ocr_max_pages", 50)),
            )
        self.visited_sitemaps: Set[str] = set()
        self.login_event = threading.Event()
        self.saved_cookies: List[Dict] = []
        self._login_expired_streak = 0
        self._session_probe_cache = None
        self._session_probe_lock = asyncio.Lock()
        self._file_hash_cache = {}

        self.async_download_lock = asyncio.Lock()
        self.async_pw_lock = asyncio.Lock()
        self.async_stats_lock = asyncio.Lock()
        self.async_sitemap_lock = asyncio.Lock()
        self._dns_allowed_cache: Dict[str, bool] = {}

        self.semaphore = asyncio.Semaphore(self.concurrency)
        self.playwright_semaphore = asyncio.Semaphore(self.playwright_concurrency)

        # Login-detektor — bara aktiv när vi använder login-läge
        self.login_detector = LoginDetector()
        self._login_detection_enabled = (
            config.get("headless_mode") == "login_then_headless"
        )

        self.req_session: Optional[aiohttp.ClientSession] = None
        self._pw = None
        self._browser = None
        self._context = None
        self._browser_proxy: Optional[BrowserEgressProxy] = None
        self._export_files: Optional[Set[str]] = None

        # Ändringslogg och rapport (skrivs som changes.jsonl / crawl_report.json)
        self.changes: List[Dict] = []
        self.counts = {"added": 0, "updated": 0, "removed": 0, "docs_added": 0,
                       "docs_updated": 0, "docs_removed": 0}
        self.report: Dict[str, List] = {
            "short_pages": [], "blocked_pages": [], "soft_404": [],
            "gone_pages": [], "off_domain_redirects": [],
            "conversion_failures": [], "download_failures": [],
            "possible_prompt_injection": [], "session_expired": [],
            "duplicates": [], "canonical_skipped": [], "wrong_language": [],
            "ocr_used": [], "gone_documents": [], "retired_text_files": [],
            "document_version_mismatch": [],
            "canonical_unresolved": [], "access_denied": [], "session_unverified": [],
            "robots_unreachable": [], "robots_denied": [], "export_failures": [],
            'publication_blocked': [],
            'generation_cleanup': [], 'generation_cleanup_failures': [],
        }
        self._completed_naturally = False
        self._successful_documents = 0

        self.crawl_session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + '_' + uuid.uuid4().hex[:8]
        self.logger = logging.getLogger(f'Crawler_{id(self)}')
        self.logger.setLevel(logging.DEBUG)
        log_dir = os.path.join(self.output_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        self._file_handler = RotatingFileHandler(
            os.path.join(log_dir, f'crawl_{self.crawl_session_id}.log'),
            maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8'
        )
        self._file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'
        ))
        self.logger.addHandler(self._file_handler)

        self._log(f"🚀 Initierar ASYNC crawl för {self.domain} (v{VERSION})")
        if self.convert_docs:
            libs = []
            if HAS_PYMUPDF: libs.append('PDF')
            if HAS_DOCX: libs.append('Word')
            if HAS_OPENPYXL: libs.append('Excel')
            if HAS_PPTX: libs.append('PPTX')
            if self.converter and self.converter._soffice: libs.append('äldre format via LibreOffice')
            self._log(f"✓ Dokument→Markdown aktiv ({', '.join(libs) or 'inga bibliotek!'})")
            if not (HAS_PYMUPDF or HAS_DOCX or HAS_OPENPYXL or HAS_PPTX):
                self._log("⚠ Dokumentkonvertering är påslagen men inga konverterare är "
                          "installerade (pip install PyMuPDF python-docx openpyxl python-pptx)",
                          LogLevel.WARNING)
        if HAS_BROTLI:
            self._log("✓ Brotli-stöd aktivt", LogLevel.DEBUG)
        if not HAS_PROTEGO:
            self._log("ℹ Protego saknas — robots.txt-regler med * och $ tolkas ofullständigt "
                      "(pip install protego)", LogLevel.DEBUG)

    # ─── Logging & GUI ──────────────────────────────────────
    def _log(self, msg: str, level=LogLevel.INFO):
        if level == LogLevel.DEBUG:
            self.logger.debug(msg)
        elif level == LogLevel.INFO:
            self.logger.info(msg)
        elif level == LogLevel.WARNING:
            self.logger.warning(msg)
        elif level == LogLevel.ERROR:
            self.logger.error(msg)
        if self.msg_queue:
            # DEBUG hålls utanför GUI:t — annars svämmar loggkön över vid snabba körningar
            if level != LogLevel.DEBUG:
                self.msg_queue.put(("log", f"[{level.name}] {msg}"))
        else:
            line = f"[{level.name}] {msg}"
            try:
                print(line)
            except UnicodeEncodeError:       # t.ex. cp1252-konsol utan emoji-stöd
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                print(line.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass                          # ingen stdout (pythonw) — loggfilen räcker

    def _close_log_handlers(self):
        try:
            self.logger.removeHandler(self._file_handler)
            self._file_handler.close()
        except Exception:
            pass

    def _record_change(self, event: str, url: str, **extra):
        self.changes.append({"ts": datetime.now().isoformat(timespec="seconds"),
                             "event": event, "url": url, **extra})

    def _gui_update(self, url: str, status: str, title: str):
        nya_eller_sparade = self.stats.pages_visited - self.stats.pages_unchanged
        queue_size = self.url_queue.size() + self.active_tasks + self._conversion_pending
        pages_done = self.stats.pages_visited

        eta_str = "Beräknar..."
        if pages_done > 2:
            avg_time = self.stats.duration.total_seconds() / pages_done
            remaining = queue_size
            if self.max_pages > 0:
                remaining = min(self.url_queue.size() + self.active_tasks,
                                max(0, self.max_pages - pages_done)) + self._conversion_pending
            eta_sec = avg_time * remaining
            if remaining <= 0:
                eta_str = "Klar snart"
            elif eta_sec > 3600:
                eta_str = f"~{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
            elif eta_sec > 60:
                eta_str = f"~{int(eta_sec // 60)}m {int(eta_sec % 60)}s"
            else:
                eta_str = f"~{int(eta_sec)}s"

        if self.msg_queue:
            safe_title = title.replace('\x00', '') if title else "Ingen titel"
            self.msg_queue.put(("table", (url, status, safe_title)))
            self.msg_queue.put(("stats_data", (
                self.stats.pages_visited,
                nya_eller_sparade,
                self.stats.documents_downloaded,
                (self.stats.pages_unchanged + self.stats.pages_not_modified_304
                 + self.stats.pages_skipped_lastmod),
                queue_size,
                self.stats.pages_failed,
                eta_str,
            )))

    # ─── URL-validering ─────────────────────────────────────
    def _domain_allowed(self, netloc: str) -> bool:
        host = (urlparse('//' + netloc).hostname or '').lower().rstrip('.').removeprefix('www.')
        core = (urlparse(self.start_url).hostname or '').lower().rstrip('.').removeprefix('www.')
        if host == core or host in self.allowed_domains:
            return True
        return self.include_subdomains and host.endswith("." + core)

    def is_valid_url(self, url: str, for_export: bool = False) -> bool:
        if len(url) > 2000:
            return False
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return False

            if not self._allow_private and is_non_public_host(parsed.hostname):
                return False

            parsed_path = parsed.path.lower()
            ext = posixpath.splitext(parsed_path)[1]
            if ext in SKIP_EXTENSIONS:
                return False

            if any(p in parsed_path for p in ('/images/', '/media/', '/assets/')):
                if any(img in parsed_path
                       for img in ('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    return False

            # Skydd mot crawl-fällor: extremt djupa sökvägar, upprepade segment
            # (/a/b/a/b/a/b), och URL:er med många query-parametrar (facetter).
            segments = [s for s in parsed.path.split('/') if s]
            if len(segments) > self.max_path_segments:
                return False
            if _CRAWL_TRAP_RE.search(parsed.path):
                return False
            if parsed.query and len(parse_qs(parsed.query)) > self.max_query_params:
                return False

            if self.config.get("strict_domain", True) and not self._domain_allowed(parsed.netloc):
                return False

            lower_url = url.lower()
            for kw in self.config.get("exclude_keywords", []):
                if kw and kw in lower_url:
                    return False
            req_kws = self.config.get("require_keywords", [])
            if req_kws and not any(kw in lower_url for kw in req_kws):
                return False

            rules = self._robots_rules.get(self._origin(url))
            if (self.config.get('respect_robots', True) and rules
                    and not (for_export and self._origin(url) in self._robots_unreachable)
                    and not rules.can_fetch(url)):
                return False
            return True
        except Exception:
            return False

    # ─── Nätverkslager ──────────────────────────────────────
    async def _resolve_private_policy(self):
        """Tillåt privata adresser bara om startadressen själv är privat (intranät)."""
        if self._allow_private:
            return
        host = urlparse(self.start_url).hostname
        if not host:
            return
        if is_non_public_host(host):
            self._allow_private = True
        else:
            try:
                loop = asyncio.get_running_loop()
                infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                if infos and all(not ipaddress.ip_address(i[4][0]).is_global
                                 for i in infos):
                    self._allow_private = True
            except Exception:
                pass
        if self._allow_private:
            self._log("ℹ Startadressen är ett privat nätverk/intranät — "
                      "privata adresser tillåts", LogLevel.DEBUG)

    async def _on_redirect(self, session, ctx, params):
        """Stoppar redirects till privata IP-literaler och icke-http(s)-scheman."""
        loc = params.response.headers.get('Location', '')
        target = urlparse(urljoin(str(params.url), loc))
        if target.scheme not in ('http', 'https'):
            raise BlockedHostError(f"Redirect till otillåtet schema: {target.scheme}")
        if not self._allow_private and is_non_public_host(target.hostname):
            raise BlockedHostError(f"Redirect till icke-publik adress: {target.hostname}")
        # Robots-hämtningen själv måste kunna följa redirects utan rekursion.
        obey = (getattr(ctx, 'trace_request_ctx', None) or {}).get('obey_robots', True)
        if obey and not await self._robots_allowed(target.geturl()):
            raise RobotsDeniedError("Redirect stoppad av robots.txt")
        await self.rate_limiter.async_wait(target.netloc.lower())

    def _check_http_target(self, url: str):
        """IP-literaler passerar inte aiohttp-resolvern och måste kontrolleras här."""
        target = urlparse(url)
        if target.scheme not in ('http', 'https') or not target.hostname:
            raise BlockedHostError("Otillåten nätverksadress")
        if not self._allow_private and is_non_public_host(target.hostname):
            raise BlockedHostError("Nätverksadressen är inte publik")

    async def _create_session(self) -> aiohttp.ClientSession:
        """Skapar aiohttp-session med riktig CookieJar och Brotli om tillgängligt.

        Cookies från Playwright-inloggningen filtreras till samma toppdomän
        innan de injiceras i CookieJar — undviker att tappa secure/httponly-flaggor.
        """
        connector = aiohttp.TCPConnector(
            limit=max(20, self.concurrency * 2),
            limit_per_host=self.concurrency,
            ttl_dns_cache=300,
            resolver=SafeResolver(self._allow_private),
        )
        accept_encoding = "gzip, deflate"
        if HAS_BROTLI:
            accept_encoding = "br, " + accept_encoding

        jar = CookieJar(unsafe=True)  # tillåt även IP-baserade cookies
        headers = {
            'User-Agent': self.user_agent,
            'Accept-Encoding': accept_encoding,
            'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                       'image/avif,image/webp,*/*;q=0.8'),
            'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.8',
        }

        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(self._on_redirect)

        session = aiohttp.ClientSession(
            headers=headers,
            connector=connector,
            cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            trace_configs=[trace],
        )

        # Injicera cookies från Playwright (om vi har sådana från login-läget)
        if self.saved_cookies:
            self._inject_playwright_cookies(jar, self.saved_cookies)
        return session

    def _inject_playwright_cookies(self, jar: CookieJar, cookies: List[Dict]):
        """Konverterar Playwright-cookies till format som aiohttp's CookieJar förstår."""
        added = 0
        for c in cookies:
            try:
                domain = (c.get('domain') or '').lstrip('.')
                if not domain:
                    domain = self.domain
                # Bygg en URL för update_cookies så att domain/path bevaras
                scheme = 'https' if c.get('secure') else 'http'
                cookie_url = f"{scheme}://{domain}{c.get('path', '/')}"

                sc = SimpleCookie()
                sc[c['name']] = c['value']
                m = sc[c['name']]
                if c.get('path'):
                    m['path'] = c['path']
                if c.get('expires') and c['expires'] > 0:
                    # Konvertera epoch till GMT-sträng
                    try:
                        expires_dt = datetime.fromtimestamp(c['expires'], tz=timezone.utc)
                        m['expires'] = expires_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
                    except Exception:
                        pass
                if c.get('secure'):
                    m['secure'] = True
                if c.get('httpOnly'):
                    m['httponly'] = True

                jar.update_cookies(sc, response_url=YarlURL(cookie_url))
                added += 1
            except Exception as e:
                self._log(f"Kunde inte överföra cookie {c.get('name')}: {e}",
                          LogLevel.DEBUG)
        self._log(f"✓ Överförde {added} cookies från login-sessionen")

    @staticmethod
    def _decode_body(body: bytes, charset: Optional[str]) -> str:
        """Avkodar svarskroppen. Saknas charset testas UTF-8 strikt, sen cp1252."""
        enc = charset
        if not enc:
            m = re.search(rb'<meta[^>]+charset=["\']?\s*([\w-]+)', body[:4096], re.I)
            if not m:
                m = re.search(rb'<\?xml[^>]+encoding=["\']([\w-]+)', body[:200], re.I)
            if m:
                enc = m.group(1).decode('ascii', 'ignore')
        if enc:
            try:
                return body.decode(enc, errors='replace')
            except LookupError:
                pass
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError:
            return body.decode('cp1252', errors='replace')

    async def _read_limited(self, resp, limit: int) -> Optional[bytes]:
        """Läser svarskroppen upp till `limit` byte. None om den är för stor."""
        if resp.content_length is not None and resp.content_length > limit:
            return None
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            buf += chunk
            if len(buf) > limit:
                return None
            if self.state == CrawlerState.STOPPED:
                return None
        return bytes(buf)

    async def fetch(self, url: str, method: str = 'GET',
                    cached: Optional[Dict] = None,
                    max_retries: int = 3,
                    decode_text: bool = True,
                    max_bytes: Optional[int] = None,
                    obey_robots: bool = True) -> Optional[FetchResult]:
        """Enhetlig nätverkshämtning med retries, conditional GET och slut-URL.

        - method='HEAD' → bara content-type
        - cached + ETag/Last-Modified → conditional GET → 304 returneras som
          FetchResult(not_modified=True)
        - decode_text=True → text i .text; icke-textuella svar (PDF, bilder)
          läses INTE in utan returneras bara med content_type
        - decode_text=False → råa bytes i .body (t.ex. gzip-sitemaps)
        - 404/410 returneras som FetchResult(status=404/410) så att anroparen
          kan skilja "sidan finns inte" från nätverksfel (None)
        """
        base_delay = 1.0
        limit = max_bytes or self.max_page_bytes
        try:
            self._check_http_target(url)
        except BlockedHostError:
            return None
        if obey_robots and not await self._robots_allowed(url):
            return None

        # Bygg conditional-headers från cache
        extra_headers: Dict[str, str] = {}
        if method == 'GET' and cached:
            if cached.get('etag'):
                extra_headers['If-None-Match'] = cached['etag']
            if cached.get('last_modified'):
                extra_headers['If-Modified-Since'] = cached['last_modified']

        async def _backoff(resp, attempt: int):
            wait = base_delay * (2 ** attempt) + random.random() * 0.5
            retry_after = resp.headers.get('Retry-After', '')
            if retry_after.strip().isdigit():
                wait = min(int(retry_after), 60)
            if resp.status in (429, 503):
                self.rate_limiter.slow_down(domain=urlparse(str(resp.url)).netloc.lower())
            await asyncio.sleep(wait)

        for attempt in range(max_retries + 1):
            # Respektera pause/stop mellan försök
            while self.state == CrawlerState.PAUSED:
                await asyncio.sleep(0.3)
            if self.state == CrawlerState.STOPPED:
                return None
            await self.rate_limiter.async_wait(urlparse(url).netloc.lower())
            if self.state == CrawlerState.STOPPED:
                return None

            try:
                if method == 'HEAD':
                    async with self.req_session.head(
                        url, allow_redirects=True,
                        trace_request_ctx={'obey_robots': obey_robots},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status in (403, 405):
                            # Servern stödjer inte HEAD — låt anroparen falla tillbaka till GET
                            return FetchResult(
                                content_type=resp.headers.get('Content-Type', '').lower(),
                                final_url=str(resp.url), status=resp.status,
                            )
                        if resp.status in (429, 500, 502, 503, 504):
                            if attempt == max_retries:
                                return FetchResult(final_url=str(resp.url), status=resp.status)
                            await _backoff(resp, attempt)
                            continue
                        if resp.status >= 400:
                            return FetchResult(final_url=str(resp.url), status=resp.status)
                        self.rate_limiter.succeeded(urlparse(str(resp.url)).netloc.lower())
                        return FetchResult(
                            content_type=resp.headers.get('Content-Type', '').lower(),
                            final_url=str(resp.url),
                            status=resp.status,
                        )

                # GET
                async with self.req_session.get(
                    url, headers=extra_headers,
                    trace_request_ctx={'obey_robots': obey_robots},
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 304:
                        self.rate_limiter.succeeded(urlparse(str(resp.url)).netloc.lower())
                        return FetchResult(
                            final_url=str(resp.url), status=304,
                            not_modified=True,
                            etag=resp.headers.get('ETag'),
                            last_modified=resp.headers.get('Last-Modified'),
                        )
                    if resp.status in (404, 410):
                        return FetchResult(final_url=str(resp.url), status=resp.status)
                    if resp.status in (429, 500, 502, 503, 504):
                        if attempt == max_retries:
                            if resp.status in (429, 503):
                                self.rate_limiter.slow_down(domain=urlparse(str(resp.url)).netloc.lower())
                            return FetchResult(final_url=str(resp.url), status=resp.status)
                        await _backoff(resp, attempt)
                        continue
                    if resp.status >= 400 and resp.status not in (401, 403):
                        return FetchResult(final_url=str(resp.url), status=resp.status)
                    if 200 <= resp.status < 300:
                        self.rate_limiter.succeeded(urlparse(str(resp.url)).netloc.lower())

                    content_type = resp.headers.get('Content-Type', '').lower()
                    final_url = str(resp.url)
                    etag = resp.headers.get('ETag')
                    last_mod = resp.headers.get('Last-Modified')

                    is_textual = (
                        'text' in content_type
                        or 'html' in content_type
                        or 'json' in content_type
                        or 'xml' in content_type
                        or content_type == ''
                    )
                    if not decode_text:
                        body = await self._read_limited(resp, limit)
                        if body is None:
                            self._log(f"  ✗ Svaret är för stort (> {limit // 1024 // 1024} MB): {url}",
                                      LogLevel.WARNING)
                            return None
                        return FetchResult(
                            body=body, content_type=content_type,
                            final_url=final_url, status=resp.status,
                            etag=etag, last_modified=last_mod,
                        )
                    if is_textual:
                        body = await self._read_limited(resp, limit)
                        if body is None:
                            if self.state != CrawlerState.STOPPED:
                                self._log(f"  ✗ Sidan är för stor (> {limit // 1024 // 1024} MB): {url}",
                                          LogLevel.WARNING)
                            return None
                        return FetchResult(
                            text=self._decode_body(body, resp.charset),
                            content_type=content_type,
                            final_url=final_url, status=resp.status,
                            etag=etag, last_modified=last_mod,
                        )
                    # Binärt svar (PDF, bild …): läs inte in kroppen
                    return FetchResult(
                        content_type=content_type, final_url=final_url,
                        status=resp.status, etag=etag, last_modified=last_mod,
                    )
            except asyncio.CancelledError:
                raise
            except RobotsDeniedError:
                return None
            except BlockedHostError as e:
                self._log(f"  ⛔ Blockerad: {url} ({e})", LogLevel.WARNING)
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == max_retries:
                    self._log(f"  ✗ Nätverksfel: {url} ({e})", LogLevel.DEBUG)
                    return None
                await asyncio.sleep(base_delay * (2 ** attempt) + random.random() * 0.5)
            except Exception as e:
                if isinstance(e.__cause__, BlockedHostError):
                    self._log(f"  ⛔ Blockerad: {url} ({e.__cause__})", LogLevel.WARNING)
                else:
                    self._log(f"  ✗ Oväntat hämtningsfel: {url} ({e})", LogLevel.DEBUG)
                return None
        return None

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        # Scheme och port är del av robots-reglernas tillämpningsområde.
        return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'

    async def _ensure_robots(self, url: str) -> RobotsRules:
        origin = self._origin(url)
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._robots_rules:
                return self._robots_rules[origin]
            try:
                result = await self.fetch(origin + '/robots.txt', max_retries=2,
                                          obey_robots=False)
            except Exception:
                result = None
            if result and 200 <= result.status < 300:
                text = result.text or ''
            elif result and 400 <= result.status < 500:
                # RFC 9309: 4xx = unavailable; 5xx/nätverksfel = unreachable.
                text = 'User-agent: *\nAllow: /\n'
            else:
                text = 'User-agent: *\nDisallow: /\n'
                self._robots_unreachable.add(origin)
                if self.config.get('respect_robots', True):
                    self.report['robots_unreachable'].append({'origin': origin})
                    self._log(f'⚠ robots.txt kan inte nås; avstår från hämtning på {origin}',
                              LogLevel.WARNING)
            rules = RobotsRules(text, self.robots_token)
            self._robots_rules[origin] = rules
            self._robots_text[origin] = text
            delay = rules.crawl_delay()
            if delay and self.config.get('respect_robots', True):
                self.rate_limiter.set_minimum(urlparse(origin).netloc, delay)
            if origin == self._origin(self.start_url):
                self.robot_parser = rules
            return rules

    async def _robots_allowed(self, url: str) -> bool:
        if not self.config.get('respect_robots', True):
            return True
        rules = await self._ensure_robots(url)
        if urlparse(url).path == '/robots.txt' or rules.can_fetch(url):
            return True
        if url not in self._robots_denied:
            self._robots_denied.add(url)
            self.report['robots_denied'].append({'url': url})
            self._log(f'ℹ Hoppar över enligt robots.txt: {url}', LogLevel.DEBUG)
        return False

    async def _load_robots_txt(self):
        # Sitemap-inläsning är oberoende av om reglerna ska respekteras.
        await self._ensure_robots(self.start_url)
        if not self.find_sitemap:
            return
        if (self.config.get('respect_robots', True)
                and self._origin(self.start_url) in self._robots_unreachable):
            return
        text = self._robots_text.get(self._origin(self.start_url), '')
        sitemaps = re.findall(r'(?im)^\s*sitemap:\s*(\S+)', text)
        for sm in sitemaps or [self.base_url + '/sitemap.xml']:
            await self._parse_sitemap(urljoin(self.base_url + '/', sm))

    async def _parse_sitemap(self, url: str):
        async with self.async_sitemap_lock:
            if url in self.visited_sitemaps or len(self.visited_sitemaps) >= 500:
                return
            # Sitemaps på andra domäner följs bara om de uttryckligen är tillåtna
            if self.config.get("strict_domain", True) and not self._domain_allowed(
                    urlparse(url).netloc):
                self._log(f"  ⛔ Hoppar över sitemap utanför domänen: {url}", LogLevel.DEBUG)
                return
            self.visited_sitemaps.add(url)

        self._log(f"🗺️ Letar i sitemap: {url}")
        try:
            r = await self.fetch(url, max_retries=2, decode_text=False,
                                 max_bytes=self.max_sitemap_bytes)
            if not r or not r.body:
                return
            content = r.body
            if url.lower().endswith('.gz') or content[:2] == b'\x1f\x8b':
                content = safe_gunzip(content, self.max_sitemap_bytes)

            # XML-entiteter i en sitemap behövs aldrig — och är ett klassiskt
            # angrepp (billion laughs / XXE). Avvisa hellre än att riskera det.
            if b'<!ENTITY' in content:
                self._log(f"  ⛔ Sitemap med XML-entiteter avvisad: {url}", LogLevel.WARNING)
                return

            sitemap_urls: List[str] = []
            url_strs: List[str] = []
            try:
                soup = BeautifulSoup(content, 'lxml-xml')
                sitemap_urls = [
                    loc.text.strip() for sm in soup.find_all('sitemap')
                    if (loc := sm.find('loc'))
                ]
                for node in soup.find_all('url'):
                    loc = node.find('loc')
                    if not loc:
                        continue
                    loc_text = loc.text.strip()
                    url_strs.append(loc_text)
                    lastmod = node.find('lastmod')
                    if lastmod and lastmod.text.strip():
                        self.sitemap_lastmod[normalize_url(
                            loc_text, self.ignore_query_params)] = lastmod.text.strip()
            except Exception:
                if HAS_DEFUSEDXML:
                    try:
                        root = SafeET.fromstring(content)
                        for elem in root.iter():
                            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                            if tag in ('sitemap', 'url'):
                                for child in elem:
                                    ctag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                                    if ctag == 'loc' and child.text:
                                        (sitemap_urls if tag == 'sitemap' else url_strs
                                         ).append(child.text.strip())
                    except Exception:
                        pass
                else:
                    self._log("ℹ Sitemapen kunde inte tolkas (installera defusedxml för "
                              "säker reservtolkning)", LogLevel.DEBUG)

            if sitemap_urls:
                await asyncio.gather(
                    *[self._parse_sitemap(s) for s in sitemap_urls],
                    return_exceptions=True
                )

            count = 0
            for s in url_strs:
                if self.is_valid_url(s):
                    # Spåra sitemap-funna dokument i manifestet — så att
                    # PDFer som bara hittas via sitemap (och inte via en
                    # webbsida) ändå syns där, om än utan riktig referer.
                    if (self.config.get("download_docs", False)
                            and self._looks_like_document_url(s)):
                        await self.manifest.record_link(
                            doc_url=normalize_url(s, self.ignore_query_params),
                            referer_url="",          # ingen riktig sida som länkar
                            referer_title="",        # → konvertern faller till metadata/filnamn
                            link_text="(via sitemap)",
                        )
                    if self.url_queue.add_url(s, depth=0,
                                              base_priority=CrawlPriority.SITEMAP.value):
                        count += 1
            if count > 0:
                self._log(f"✓ Hittade {count} (godkända) URLs i sitemap/index")
        except Exception as e:
            self._log(f"⚠ Fel vid sitemap-läsning: {e}", LogLevel.DEBUG)

    # ─── Playwright (lazy initialization) ──────────────────
    async def get_playwright_context(self):
        if not HAS_PLAYWRIGHT:
            return None
        if self._context is None:
            async with self.async_pw_lock:
                if self._context is None:
                    self._pw = await async_playwright().start()
                    is_headless = self.config.get("headless_mode", "headless") != "visible"
                    self._browser = await self._pw.chromium.launch(
                        headless=is_headless, **await self._browser_network_options())
                    # HTTPS-fel ignoreras bara om det uttryckligen begärts i config
                    # (t.ex. intranät med egen CA) — annars riskerar inloggningscookies.
                    self._context = await self._browser.new_context(
                        ignore_https_errors=bool(self.config.get("ignore_https_errors", False)),
                        user_agent=self.user_agent,
                        service_workers='block',
                        accept_downloads=False,
                    )
                    if self.saved_cookies:
                        try:
                            await self._context.add_cookies(self.saved_cookies)
                        except Exception as e:
                            self._log(f"Kunde inte sätta Playwright-cookies: {e}",
                                      LogLevel.DEBUG)
        return self._context

    async def _browser_network_options(self) -> Dict:
        """Publika körningar måste använda den kontrollerade utgående proxyn."""
        if self._allow_private:
            return {}
        if self._browser_proxy is None:
            self._browser_proxy = BrowserEgressProxy(self._log)
            await self._browser_proxy.start()
        return {
            'proxy': {'server': self._browser_proxy.url, 'bypass': '<-loopback>'},
            'args': ['--disable-quic',
                     '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
        }

    async def _check_playwright_url_allowed(self, target_url: str) -> bool:
        """Kontrollerar om en URL är tillåten i Playwright (skydd mot SSRF)."""
        if self._allow_private:
            return True
        host = urlparse(target_url).hostname
        if not host:
            return False
        if is_non_public_host(host):
            return False
        allowed = self._dns_allowed_cache.get(host)
        if allowed is None:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                allowed = all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
            except Exception:
                allowed = False
            self._dns_allowed_cache[host] = allowed
        return bool(allowed)

    async def _render_with_playwright(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Returnerar (html, final_url). Avbryter snabbt vid stop."""
        result = await self._render_response_with_playwright(url)
        return result.text, result.final_url or None

    async def _install_browser_guard(self, page, filepath: Optional[str] = None) -> Dict:
        """Kontrollera varje redirect före sändning och strömma dokument efter headers.

        CDP:s response-stream hindrar Chromium från att först spara hela bilagan.
        Varken context.request eller route.fetch får användas för dokumentkroppen.
        """
        cdp = await page.context.new_cdp_session(page)
        guard = {'cdp': cdp, 'tasks': set(), 'done': asyncio.Event(),
                 'started': False, 'success': False, 'reason': '', 'closed': False}

        async def handle(event):
            request_id = event['requestId']
            target = event['request']['url']
            response_stage = 'responseStatusCode' in event or 'responseErrorReason' in event
            try:
                if not response_stage:
                    self._check_http_target(target)
                    if self.state == CrawlerState.STOPPED:
                        raise RobotsDeniedError('Körningen stoppad')
                    if event['resourceType'] in ('Image', 'Stylesheet', 'Font', 'Media'):
                        await cdp.send('Fetch.failRequest', {'requestId': request_id,
                                                           'errorReason': 'Aborted'})
                        return
                    if event['resourceType'] == 'Document':
                        if not await self._robots_allowed(target):
                            raise RobotsDeniedError('Robots.txt nekar navigation')
                        await self.rate_limiter.async_wait(urlparse(target).netloc.lower())
                    await cdp.send('Fetch.continueRequest', {'requestId': request_id})
                    return
                status = event.get('responseStatusCode', 0)
                headers = {h['name'].lower(): h['value'] for h in event.get('responseHeaders', [])}
                content_type = headers.get('content-type', '').lower()
                is_document = ('attachment' in headers.get('content-disposition', '').lower()
                               or any(ct in content_type for ct in (
                                   'application/pdf', 'application/vnd', 'application/msword',
                                   'application/octet-stream', 'application/zip', 'text/csv',
                                   'application/rtf', 'application/x-download')))
                if filepath and is_document and status == 200:
                    guard['started'] = True
                    guard['final_url'] = target
                    try:
                        if (self.config.get('strict_domain', True)
                                and not self._domain_allowed(urlparse(target).netloc)):
                            guard['reason'] = 'omdirigerad utanför tillåtna domäner'
                        else:
                            guard['success'] = await self._stream_browser_document(
                                cdp, request_id, headers, filepath, guard)
                    finally:
                        # En övertagen stream kan inte fortsätta som vanlig navigation.
                        await cdp.send('Fetch.failRequest', {'requestId': request_id,
                                                           'errorReason': 'Aborted'})
                        guard['done'].set()
                    return
                await cdp.send('Fetch.continueRequest', {'requestId': request_id})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                guard['reason'] = guard['reason'] or str(e)[:160]
                try:
                    await cdp.send('Fetch.failRequest', {'requestId': request_id,
                                                       'errorReason': 'Aborted'})
                except Exception:
                    pass
                if filepath and (guard['started'] or event['resourceType'] == 'Document'):
                    guard['done'].set()

        def paused(event):
            if guard['closed']:
                return
            task = asyncio.create_task(handle(event))
            guard['tasks'].add(task)
            task.add_done_callback(guard['tasks'].discard)

        guard['callback'] = paused
        cdp.on('Fetch.requestPaused', paused)
        patterns = [{'urlPattern': '*', 'requestStage': 'Request'}]
        if filepath:
            patterns.append({'urlPattern': '*', 'resourceType': 'Document',
                             'requestStage': 'Response'})
        await cdp.send('Fetch.enable', {'patterns': patterns})
        return guard

    async def _stream_browser_document(self, cdp, request_id: str, headers: Dict,
                                       filepath: str, guard: Dict) -> bool:
        """Läs högst max_download_bytes + en byte och kasta temporärt browser-lager."""
        size_header = headers.get('content-length', '')
        if size_header.isdigit() and int(size_header) > self.max_download_bytes:
            guard['reason'] = 'för stort'
            return False
        stream = None
        written = 0
        success = False
        try:
            result = await cdp.send('Fetch.takeResponseBodyAsStream', {'requestId': request_id})
            stream = result['stream']
            async with asyncio.timeout(120):
                with open(filepath, 'wb') as output:
                    while self.state != CrawlerState.STOPPED:
                        block = await cdp.send('IO.read', {
                            'handle': stream, 'size': min(65536, self.max_download_bytes - written + 1)})
                        data = (base64.b64decode(block['data']) if block.get('base64Encoded')
                                else block['data'].encode('utf-8'))
                        written += len(data)
                        if written > self.max_download_bytes:
                            guard['reason'] = 'för stort'
                            return False
                        output.write(data)
                        if block.get('eof'):
                            success = written > 0
                            if success:
                                self.rate_limiter.succeeded(urlparse(guard.get("final_url") or self.start_url).netloc.lower())
                            return success
                guard['reason'] = 'körningen stoppad'
                return False
        finally:
            if stream:
                try:
                    await cdp.send('IO.close', {'handle': stream})
                except Exception:
                    pass
            if not success and os.path.exists(filepath):
                os.remove(filepath)

    async def _close_browser_guard(self, guard: Dict):
        guard['closed'] = True
        cdp = guard['cdp']
        cdp.remove_listener('Fetch.requestPaused', guard['callback'])
        tasks = list(guard['tasks'])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await cdp.detach()
        except Exception:
            pass

    async def _render_response_with_playwright(self, url: str) -> FetchResult:
        """Bevara HTTP-status även när Chromium renderar svaret."""
        async with self.playwright_semaphore:
            ctx = await self.get_playwright_context()
            if ctx is None:
                return FetchResult()
            page = await ctx.new_page()

            guard = None
            try:
                guard = await self._install_browser_guard(page)
                # Kort timeout — om sidan inte är klar inom 12s tar vi vad vi har
                goto_ok = False
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    goto_ok = True
                except Exception as e:
                    self._log(f"  ⚠ Playwright kunde inte ladda {url}: {str(e)[:80]}",
                              LogLevel.DEBUG)
                # Avbryt om goto misslyckades eller crawl stoppats
                if not goto_ok or self.state == CrawlerState.STOPPED:
                    return FetchResult()
                html = await page.content()
                final_url = page.url
                if response and 200 <= response.status < 300:
                    self.rate_limiter.succeeded(urlparse(final_url).netloc.lower())
                return FetchResult(text=html, final_url=final_url,
                                   status=response.status if response else 200)
            finally:
                if guard:
                    await self._close_browser_guard(guard)
                try:
                    await page.close()
                except Exception:
                    pass

    # ─── PII-tvätt ──────────────────────────────────────────
    def clean_pii(self, text: str) -> str:
        return clean_pii_text(self.config, text)

    # ─── Innehållsextraktion ────────────────────────────────
    def extract_structured_data(self, html: str, url: str) -> Dict:
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        base_tag = soup.find('base', href=True)
        base_url = urljoin(url, base_tag['href'].strip()) if base_tag else url
        if urlparse(base_url).scheme not in ('http', 'https'):
            base_url = url

        # Dold text och kommentarer kan innehålla instruktioner riktade mot en
        # LLM (indirekt prompt-injektion). Vi undersöker dolda element och kommentarer:
        # Om ett dolt element innehåller ett injektionsmönster raderas det så att det
        # varken når Trafilatura eller reservvägen.
        # Normala dolda element (accordions, FAQ-paneler, flikar, liten text) bevaras!
        hidden_elements = []
        for el in soup.find_all(True, style=_HIDDEN_STYLE_RE):
            hidden_elements.append(el)
        for el in soup.find_all(attrs={"hidden": True}):
            hidden_elements.append(el)
        for el in soup.find_all(attrs={"aria-hidden": "true"}):
            hidden_elements.append(el)

        hidden_texts: List[str] = []
        for el in hidden_elements:
            if el.decomposed if hasattr(el, "decomposed") else False:
                continue
            t = el.get_text(' ', strip=True)
            if t:
                hidden_texts.append(t)
                if detect_prompt_injection(t):
                    el.decompose()

        for c in list(soup.find_all(string=lambda t: isinstance(t, Comment))):
            c_str = str(c).strip()
            if c_str:
                hidden_texts.append(c_str)
            c.extract()

        page_links = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag.get('href', '').strip()
            if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                full_url = urljoin(base_url, href)
                a_tag['href'] = full_url
                # Länktexten kan innehålla namn/e-post och ska tvättas som all annan text
                link_text = self.clean_pii(a_tag.get_text(separator=' ', strip=True)[:200])
                page_links.append({"url": full_url, "text": link_text})

        raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
        og_title_tag = soup.find('meta', attrs={'property': 'og:title'})
        og_title = (og_title_tag.get('content', '').strip()
                    if og_title_tag and og_title_tag.get('content') else "")
        h1_tag = soup.find('h1')
        h1_text = h1_tag.get_text(' ', strip=True) if h1_tag else ""
        title = clean_page_title(raw_title or og_title, self.domain)
        if not title and 3 <= len(h1_text) <= 200:
            title = h1_text
        title = metadata_line(self.clean_pii(title or "Okänd"))

        keywords = []
        meta_kw = soup.find('meta', attrs={'name': re.compile(r'keywords', re.I)})
        if meta_kw and meta_kw.get('content'):
            keywords = [self.clean_pii(k.strip()) for k in meta_kw['content'].split(',')]

        description = ""
        meta_desc = soup.find('meta', attrs={'name': re.compile(r'description', re.I)})
        if meta_desc and meta_desc.get('content'):
            description = self.clean_pii(meta_desc['content'].strip())

        author_tag = soup.find('meta', attrs={'name': ['author', 'DC.creator']})
        author = self.clean_pii(author_tag['content']) if author_tag and author_tag.get('content') else ""

        pub_date = ""
        mod_date = ""

        meta_pub = soup.find('meta', attrs={'property': re.compile(
            r'article:published_time|og:pubdate', re.I)}) or \
            soup.find('meta', attrs={'name': re.compile(r'pubdate|date', re.I)})
        if meta_pub and meta_pub.get('content'):
            pub_date = meta_pub['content'].strip()

        meta_mod = soup.find('meta', attrs={'property': re.compile(
            r'article:modified_time|og:updated_time', re.I)}) or \
            soup.find('meta', attrs={'name': re.compile(r'last-modified|revised', re.I)}) or \
            soup.find('meta', attrs={'itemprop': 'dateModified'})
        if meta_mod and meta_mod.get('content'):
            mod_date = meta_mod['content'].strip()

        if not mod_date:
            time_tag = soup.find('time', attrs={'itemprop': 'dateModified'}) or \
                soup.find('time', class_=re.compile(r'update|modify', re.I))
            if time_tag:
                mod_date = time_tag.get('datetime', time_tag.get_text(strip=True))

        if not mod_date or not pub_date:
            for script in soup.find_all('script', type='application/ld+json'):
                if script.string:
                    if not mod_date:
                        m = re.search(r'"dateModified"\s*:\s*"([^"]+)"', script.string)
                        if m:
                            mod_date = m.group(1)
                    if not pub_date:
                        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string)
                        if m:
                            pub_date = m.group(1)

        if not mod_date or not pub_date:
            text_content = soup.get_text(separator=' ', strip=True)
            date_pattern = (
                r'(?i)(?:senast\s+uppdaterad|uppdaterad|publicerad|ändrad)'
                r'[\s\:\*]*(?P<date>\d{1,2}\s+(?:januari|februari|mars|april|maj|juni|'
                r'juli|augusti|september|oktober|november|december)\s+\d{4}|\d{4}-\d{2}-\d{2})'
            )
            matches = list(re.finditer(date_pattern, text_content))
            if matches:
                extracted = matches[-1].group('date')
                if not mod_date and "uppdaterad" in matches[-1].group(0).lower():
                    mod_date = extracted
                if not pub_date and "publicerad" in matches[-1].group(0).lower():
                    pub_date = extracted
                if not mod_date and not pub_date:
                    mod_date = extracted

        lang_tag = soup.find('html')
        lang = lang_tag.get('lang', '') if lang_tag else ""
        canonical_tag = soup.find('link', rel='canonical')
        canonical = (canonical_tag.get('href', '').strip()
                     if canonical_tag and canonical_tag.get('href') else "")

        og_type_tag = soup.find('meta', attrs={'property': 'og:type'})
        og_type = og_type_tag['content'] if og_type_tag and og_type_tag.get('content') else ""

        full_text = ""
        structured_sections: List[Dict] = []

        cleaned_html = str(soup)

        if self.use_trafilatura and HAS_TRAFILATURA:
            try:
                extracted = trafilatura.extract(
                    cleaned_html, include_links=True, include_images=False,
                    include_tables=True, include_formatting=True,
                    output_format="markdown", url=url,
                )
                if extracted:
                    extracted = self.clean_pii(extracted)
                    extracted = absolutize_markdown_links(extracted, base_url)
                    raw_sections = re.split(r'(?=^#{1,6}\s)', extracted, flags=re.MULTILINE)
                    heading_stack: List[Tuple[int, str]] = []
                    for raw in raw_sections:
                        raw = raw.strip()
                        if not raw:
                            continue
                        heading_match = re.match(r'^(#{1,6})\s+(.+)', raw)
                        if heading_match:
                            level = len(heading_match.group(1))
                            heading = heading_match.group(2).strip()
                            content = raw[heading_match.end():].strip()
                            heading_stack = [h for h in heading_stack if h[0] < level]
                            heading_stack.append((level, heading))
                        else:
                            heading = "Huvudinnehåll"
                            content = raw
                        path = " > ".join(h[1] for h in heading_stack) or heading
                        if content:
                            structured_sections.append(
                                {"heading": heading, "text": content, "path": path})
                    full_text = extracted
            except Exception as e:
                self._log(f"  ⚠ Trafilatura misslyckades ({e})", LogLevel.DEBUG)

        if not full_text:
            for tag in ('script', 'style', 'nav', 'footer', 'aside',
                        'iframe', 'svg', 'button', 'form'):
                for el in soup.find_all(tag):
                    el.decompose()
            # Brus-rensning på klass/id. Matchar hela klasstoken från början
            # ("nav-item", "menu") men aldrig t.ex. <body class="has-nav">, och
            # rör aldrig html/body/main/article.
            for el in soup.find_all(True):
                if el.decomposed if hasattr(el, "decomposed") else False:
                    continue
                if el.name in ('html', 'body', 'main', 'article', 'head'):
                    continue
                classes = el.get('class') or []
                el_id = el.get('id') or ""
                if any(_NOISE_CLASS_RE.match(c) for c in classes) or \
                        (el_id and _NOISE_CLASS_RE.match(el_id)):
                    el.decompose()

            sections: List[Dict] = []
            current_heading = "Huvudinnehåll"
            current_path = current_heading
            heading_stack = []
            current_text: List[str] = []

            for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                     'p', 'ul', 'ol', 'table', 'pre', 'blockquote']):
                if el.decomposed if hasattr(el, "decomposed") else False:
                    continue
                if el.name.startswith('h') and len(el.name) == 2:
                    if current_text:
                        text_block = "\n".join(current_text).strip()
                        if text_block:
                            sections.append({"heading": current_heading,
                                             "text": text_block, "path": current_path})
                    current_heading = el.get_text(separator=' ', strip=True)
                    level = int(el.name[1])
                    heading_stack = [h for h in heading_stack if h[0] < level]
                    heading_stack.append((level, current_heading))
                    current_path = " > ".join(h[1] for h in heading_stack)
                    current_text = []
                elif el.name in ('ul', 'ol'):
                    # Bara direkta li-barn — nästlade listor hanteras av sina egna ul/ol
                    for li in el.find_all('li', recursive=False):
                        parts = []
                        for child in li.children:
                            if hasattr(child, 'name') and child.name in ('ul', 'ol'):
                                continue
                            if hasattr(child, 'name') and child.name == 'a' and child.get('href'):
                                link_text = child.get_text(strip=True)
                                parts.append(f"[{link_text}]({child['href']})")
                            else:
                                t = child.get_text(strip=True) if hasattr(child, 'get_text') else str(child).strip()
                                if t:
                                    parts.append(t)
                        txt = " ".join(parts)
                        if txt:
                            current_text.append(f"• {txt}")
                elif el.name == 'table':
                    rows = el.find_all('tr')
                    for i, row in enumerate(rows):
                        cols = [c.get_text(separator=' ', strip=True)
                                for c in row.find_all(['td', 'th'])]
                        if cols:
                            current_text.append("| " + " | ".join(cols) + " |")
                            if i == 0:
                                current_text.append("|" + "|".join(["---"] * len(cols)) + "|")
                else:
                    # Stycken inuti tabeller/listor har redan tagits med av sin förälder
                    if el.find_parent(['table', 'li']):
                        continue
                    parts = []
                    for child in el.children:
                        if hasattr(child, 'name') and child.name == 'a' and child.get('href'):
                            link_text = child.get_text(strip=True)
                            parts.append(f"[{link_text}]({child['href']})")
                        else:
                            t = child.get_text(strip=True) if hasattr(child, 'get_text') else str(child).strip()
                            if t:
                                parts.append(t)
                    txt = " ".join(parts)
                    if len(txt) > 5:
                        current_text.append(txt)

            if current_text:
                text_block = "\n".join(current_text).strip()
                if text_block:
                    sections.append({"heading": current_heading,
                                     "text": text_block, "path": current_path})

            for s in sections:
                s['heading'] = self.clean_pii(s['heading'])
                s['path'] = self.clean_pii(s['path'])
                s['text'] = self.clean_pii(s['text'])

            structured_sections = sections
            full_text = "\n\n".join([f"## {s['heading']}\n{s['text']}" for s in sections])

        # CMS-boilerplate (feedback-widget, "Sidan publicerad av" …) rensas i ALLA
        # utdataformat så att även JSON-chunkarna blir rena.
        cleaned_sections = []
        for sec in structured_sections:
            sec_text = strip_cms_boilerplate(sec["text"])
            if sec_text:
                sec["text"] = sec_text
                cleaned_sections.append(sec)
        structured_sections = cleaned_sections
        full_text = strip_cms_boilerplate(full_text)

        flags = []
        if detect_prompt_injection(full_text, *hidden_texts):
            flags.append("possible_prompt_injection")

        return {
            "title": title,
            "site_title": self.clean_pii(raw_title),
            "url": url,
            "crawled_at": datetime.now().isoformat(),
            "author": author,
            "published_date": metadata_line(self.clean_pii(pub_date)),
            "modified_date": metadata_line(self.clean_pii(mod_date)),
            "language": metadata_line(self.clean_pii(lang)),
            "canonical": urljoin(base_url, canonical) if canonical else "",
            "og_type": og_type,
            "description": description,
            "keywords": keywords,
            "flags": flags,
            "plain_text": full_text,
            "chunks": semantic_chunk_text(structured_sections, source_url=url, title=title),
            "page_links": page_links,
        }

    def _looks_like_document_url(self, url: str) -> bool:
        """Heuristik: är URL:en sannolikt ett binärt dokument?"""
        ext = posixpath.splitext(urlparse(url).path.lower())[1]
        return ext in DOCUMENT_EXTENSIONS

    def _needs_javascript(self, html: str) -> bool:
        """Avgör om sidan behöver Playwright-rendering.

        Kräver tydliga signaler. Tomma sidor triggar bara fallback om de
        uttryckligen ber om JavaScript eller innehållet är trivialt litet
        OCH det finns en SPA-rotnod.
        """
        if not html or len(html) < 500:
            return True
        html_lower = html.lower()
        if 'enable javascript' in html_lower or 'please enable javascript' in html_lower:
            return True
        # SPA-mönster: liten initial HTML + tom rotnod
        if len(html) < 2000:
            for pattern in ('id="root"', 'id="app"', 'id="__next"',
                            'ng-app', 'data-reactroot'):
                if pattern in html_lower:
                    return True
        return False

    @staticmethod
    def _detect_block_page(html: str) -> str:
        """Känner igen bot-skydd/blockeringssidor som annars sparas som "innehåll"."""
        if not html or len(html) > 60000:
            return ""
        low = html.lower()
        for sig in _BLOCK_PAGE_SIGNALS:
            if sig in low:
                return sig
        return ""

    # ─── Process page ───────────────────────────────────────
    def _text_output_path(self, url: str, is_quarantined: bool = False) -> str:
        folder = "texter_karantan" if is_quarantined else "texter"
        return os.path.join(self.output_dir, folder, stable_filename(url, self.save_format))

    def _saves_text(self) -> bool:
        return self.save_format not in ("Ingen text", "No text")

    async def _enqueue_links(self, url: str, page_title: str, links: List[Dict], depth: int):
        """Lägger sidans länkar i kön och registrerar dokumentlänkar i manifestet."""
        if not (self.max_depth == 0 or depth < self.max_depth):
            return
        for link_info in links:
            full_url = link_info.get("url")
            if not full_url or not self.is_valid_url(full_url):
                continue

            # Om länken pekar på ett dokument: registrera referer-info
            # i manifestet INNAN länken hamnar i kön.
            if (self.config.get("download_docs", False)
                    and self._looks_like_document_url(full_url)):
                await self.manifest.record_link(
                    doc_url=normalize_url(full_url, self.ignore_query_params),
                    referer_url=url,
                    referer_title=page_title,
                    link_text=link_info.get("text", ""),
                )

            self.url_queue.add_url(full_url, depth=depth + 1)

    def _text_file_present(self, cached: Optional[Dict]) -> bool:
        """True om den sparade textfilen för en cachad sida finns (eller inte ska finnas)."""
        if not self._saves_text() or not cached or not cached.get('filename'):
            return True
        fn = os.path.basename(cached['filename'])
        return (os.path.exists(os.path.join(self.output_dir, "texter", fn)) or
                os.path.exists(os.path.join(self.output_dir, "texter_karantan", fn)))

    def _page_profile(self) -> str:
        # Urval och hämtmotor ändrar inte hur redan sparad text har tvättats.
        # De prövas separat, också för källor som inte hinns med denna körning.
        keys = ('remove_email', 'remove_phone', 'remove_pnr', 'remove_ip', 'keep_role_emails',
                'quarantine_flagged_chunks')
        defaults = {'quarantine_flagged_chunks': True}
        options = {key: bool(self.config.get(key, defaults.get(key, False))) for key in keys}
        options.update(format=self.save_format,
                       trafilatura=bool(self.use_trafilatura and HAS_TRAFILATURA),
                       extractor_version=4)
        return hashlib.sha256(json.dumps(options, sort_keys=True).encode('utf-8')).hexdigest()

    @staticmethod
    def _page_output_hash(data: Dict) -> str:
        # Hämtningstid ska inte ensam skapa en ny innehållsversion. Övriga
        # exporterade fält (inklusive titel, språk och ändringsdatum) ska göra det.
        payload = {key: value for key, value in data.items() if key != 'crawled_at'}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    async def _save_page_cache(self, url: str, data: Dict, content_hash: str,
                               etag: Optional[str], last_modified: Optional[str],
                               links: List[Dict], filename: Optional[str], fetch_url: str,
                               rendered: bool = False):
        await self.db.save_cache(url, content_hash, data['title'], len(data['plain_text']),
                                 etag=etag, last_modified=last_modified, links=links, filename=filename,
                                 fetch_url=fetch_url, source_url=data['url'],
                                 processing_profile=self._page_profile(), output_hash=self._page_output_hash(data),
                                 rendered=rendered)

    def _exportable_page(self, cached: Optional[Dict]) -> bool:
        return bool(cached and cached.get('filename') and self._saves_text()
                    and cached.get('processing_profile') == self._page_profile()
                    and os.path.isfile(os.path.join(self.output_dir, 'texter',
                                                    os.path.basename(cached['filename']))))

    async def _replay_cached(self, url: str, cached: Optional[Dict], depth: int,
                             status_text: str, etag: Optional[str] = None,
                             last_mod: Optional[str] = None,
                             from_sitemap: bool = False) -> bool:
        """Behandlar en oförändrad sida utan att hämta den: de sparade länkarna läggs i kön
        så att undersidorna ändå besöks. Returnerar False om uppspelning inte är möjlig
        (inga sparade länkar eller saknad textfil) — då måste sidan hämtas på riktigt."""
        links = cached.get('links') if cached else None
        if (links is None or not self._text_file_present(cached)
                or cached.get('processing_profile') != self._page_profile()):
            return False
        async with self.async_stats_lock:
            if from_sitemap:
                self.stats.pages_skipped_lastmod += 1
            else:
                self.stats.pages_not_modified_304 += 1
            self.stats.pages_visited += 1
        self._login_expired_streak = 0
        self._gui_update(url, status_text, cached.get('title', ''))
        await self.db.touch_cache(url, etag=etag, last_modified=last_mod,
                                  checked=not from_sitemap)
        await self._enqueue_links(cached.get('source_url') or cached.get('fetch_url') or url,
                                  cached.get('title', ''), links, depth)
        return True

    @staticmethod
    def _parse_lastmod(value: str) -> Optional[datetime]:
        """W3C-datum från sitemap → lokal naiv tid. Bara datum räknas som slutet av dagen."""
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if len(value.strip()) <= 10:
            dt = dt + timedelta(days=1)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def _sitemap_says_unchanged(self, url: str, cached: Dict) -> bool:
        """Sitemapens lastmod är äldre än vår senaste hämtning → hoppa över requesten.

        Säkerhetsventil: sajter med felaktig lastmod upptäcks via att sidor äldre än
        `sitemap_lastmod_max_age_days` alltid kontrolleras igen med villkorlig GET."""
        if not self.use_sitemap_lastmod or not self.sitemap_lastmod or cached.get('rendered'):
            return False
        lastmod = self.sitemap_lastmod.get(normalize_url(url, self.ignore_query_params))
        if (not lastmod or not cached.get('checked_at')
                or cached.get('processing_profile') != self._page_profile()):
            return False
        modified = self._parse_lastmod(lastmod)
        try:
            crawled = datetime.fromisoformat(cached['checked_at'])
        except ValueError:
            return False
        if modified is None or modified > crawled:
            return False
        if datetime.now() - crawled > self.lastmod_max_age:
            return False
        return bool(cached.get('filename')) or not self._saves_text()

    def _canonical_duplicate(self, url: str, canonical: Optional[str]) -> Optional[str]:
        """Returnerar canonical-URL:en om den här sidan bör ses som en kopia av en annan."""
        if not self.respect_canonical or not canonical:
            return None
        me = normalize_url(url, self.ignore_query_params)
        canon = normalize_url(canonical, self.ignore_query_params)
        if canon == me or not self.is_valid_url(canon):
            return None
        # Felkonfigurerade sajter pekar ofta alla sidor på startsidan — lita inte på det
        if urlparse(canon).path in ("", "/") and urlparse(me).path not in ("", "/"):
            return None
        # Två sidor som pekar på varandra: spara båda hellre än ingen
        if self._canonical_of.get(canon) == me:
            return None
        return request_url(canonical)

    async def _drop_saved_page(self, url: str, cached: Optional[Dict], reason: str):
        """Sidan sparas inte längre (t.ex. blev en dubblett) — ta bort en äldre sparad fil."""
        if cached and cached.get('filename'):
            fn = os.path.basename(cached['filename'])
            for folder in ("texter", "texter_karantan"):
                try:
                    os.remove(os.path.join(self.output_dir, folder, fn))
                except OSError:
                    pass
            self.counts["removed"] += 1
            self._record_change("removed", url, reason=reason, file=cached['filename'])

    async def _handle_gone(self, url: str, cached: Optional[Dict], status: int, confirmed: bool = True):
        """404/410: sidan finns inte längre. Ta bort den sparade texten och cachen."""
        async with self.async_stats_lock:
            self.stats.pages_failed += 1
        self.report["gone_pages"].append({"url": url, "status": status, 'confirmed': confirmed})
        if cached is not None:
            fn = cached.get('filename')
            if fn:
                for folder in ("texter", "texter_karantan"):
                    try:
                        os.remove(os.path.join(self.output_dir, folder, os.path.basename(fn)))
                    except OSError:
                        pass
            await self.db.delete_page(url)
            self.counts["removed"] += 1
            self._record_change("removed", url, reason=f"HTTP {status}" if confirmed else 'soft 404',
                                 file=fn)
            self._gui_update(url, f"Borttagen ({status})", cached.get('title', ''))
        else:
            self._gui_update(url, f"Hittades inte ({status})", "")

    async def process_page(self, url: str, depth: int) -> bool:
        if self.state == CrawlerState.STOPPED:
            return False
        while self.state == CrawlerState.PAUSED:
            await asyncio.sleep(0.3)

        fetch_url = request_url(url)
        url = normalize_url(url, self.ignore_query_params)
        if not await self._robots_allowed(fetch_url):
            return False

        if self.state == CrawlerState.STOPPED:
            return False

        # Filregistret behövs för städning också vid full omcrawl. Bara
        # validatorer och innehållsåteranvändning styrs av incremental.
        cached = await self.db.get_cache(url)
        conditional_cache = (cached if self.config.get("incremental") and cached
                             and cached.get('processing_profile') == self._page_profile() else None)
        html, source, final_url = "", "Standard", fetch_url
        etag, last_mod = None, None
        render_status = 0

        try:
            # ─── Dokument-URL: hoppa över HTML-flödet helt ───
            # URL-extensionen är en starkare signal än Content-Type. Sitevision
            # och andra CMS:er returnerar ofta text/html för PDF-URL:er när
            # auth-cookies finns (de serverar en förhandsvyssida).
            if self._looks_like_document_url(fetch_url):
                if self.config.get("download_docs", False):
                    await self.download_document(fetch_url)
                return True

            # ─── Sitemapens lastmod säger att sidan inte ändrats sedan vi sist hämtade den ───
            if conditional_cache and self._sitemap_says_unchanged(url, cached):
                if await self._replay_cached(url, cached, depth, "Ej ändrad (sitemap)",
                                             from_sitemap=True):
                    return True

            # ─── GET (med conditional headers) ───
            if self.use_hybrid:
                result = await self.fetch(fetch_url, cached=conditional_cache)

                # 304 Not Modified: sidan är oförändrad. Vi "spelar upp" de sparade
                # länkarna så att undersidorna ändå besöks (annars stannar en
                # inkrementell crawl på startsidan). Saknas sparade länkar, eller
                # saknas den sparade textfilen, hämtas sidan om utan villkor.
                if result is not None and result.not_modified:
                    if await self._replay_cached(url, cached, depth, "Ej ändrad (304)",
                                                 result.etag, result.last_modified):
                        return True
                    result = await self.fetch(fetch_url, cached=None)

                if result is None:
                    async with self.async_stats_lock:
                        self.stats.pages_failed += 1
                    self._gui_update(url, "Fel", "")
                    return False

                if result.status in (404, 410):
                    await self._handle_gone(url, cached, result.status)
                    if url == normalize_url(self.start_url, self.ignore_query_params) and not cached:
                        self.stats.pages_failed += 1
                    return False

                # 401/403 → behandla som login-utgång eller räkna som fel
                if result.status in (401, 403):
                    if self._login_detection_enabled:
                        await self._handle_login_expired(url, status=result.status)
                    else:
                        self._log(f"  ✗ HTTP {result.status}: {url}", LogLevel.DEBUG)
                        async with self.async_stats_lock:
                            self.stats.pages_failed += 1
                        self._gui_update(url, f"HTTP {result.status}", "")
                    if result.status == 403 and self._login_detection_enabled:
                        await self._drop_saved_page(url, cached, 'HTTP 403')
                        await self.db.mark_page_unsaved(url)
                    return False

                if result.status >= 400:
                    self.stats.pages_failed += 1
                    return False

                # Content-typ-hantering
                final_url = result.final_url or fetch_url
                etag = result.etag
                last_mod = result.last_modified
                content_type = result.content_type

                if any(t in content_type for t in ('image/', 'video/', 'audio/', 'font/')):
                    await self._drop_saved_page(url, cached, 'ändrad filtyp')
                    await self.db.mark_page_unsaved(url)
                    return False
                if any(dt in content_type for dt in ('application/pdf', 'application/vnd',
                                                     'application/msword')):
                    if self.config.get("download_docs", False):
                        await self.download_document(fetch_url)
                    await self._drop_saved_page(url, cached, 'sidan är nu ett dokument')
                    await self.db.mark_page_unsaved(url)
                    return True

                html = result.text or ""
                if not html:
                    self.stats.pages_failed += 1
                    return False

                # JS-fallback
                if self._needs_javascript(html):
                    if self.state == CrawlerState.STOPPED:
                        return False
                    rendered = await self._render_response_with_playwright(final_url)
                    rendered_html, rendered_url = rendered.text, rendered.final_url
                    render_status = rendered.status
                    if rendered_html:
                        html = rendered_html
                        final_url = rendered_url or final_url
                        source = "Webbläsare"
                        # ETag/Last-Modified från aiohttp gäller JS-skalet,
                        # inte det renderade innehållet — nollställ så att
                        # nästa crawl inte missar ändringar via felaktig 304.
                        etag = None
                        last_mod = None
                        async with self.async_stats_lock:
                            self.stats.playwright_fallbacks += 1
            else:
                # Endast Playwright
                if self.state == CrawlerState.STOPPED:
                    return False
                rendered = await self._render_response_with_playwright(fetch_url)
                rendered_html, rendered_url = rendered.text, rendered.final_url
                render_status = rendered.status
                if rendered_html:
                    html = rendered_html
                    final_url = rendered_url or fetch_url
                    source = "Webbläsare"

            if render_status in (404, 410):
                await self._handle_gone(url, cached, render_status)
                if url == normalize_url(self.start_url, self.ignore_query_params) and not cached:
                    self.stats.pages_failed += 1
                return False
            if render_status in (401, 403):
                if self._login_detection_enabled:
                    await self._handle_login_expired(url, status=render_status)
                else:
                    async with self.async_stats_lock:
                        self.stats.pages_failed += 1
                if render_status == 403 and self._login_detection_enabled:
                    await self._drop_saved_page(url, cached, 'HTTP 403')
                    await self.db.mark_page_unsaved(url)
                return False
            if render_status >= 400 or not html:
                async with self.async_stats_lock:
                    self.stats.pages_failed += 1
                return False

            if self.state == CrawlerState.STOPPED:
                return False

            # ─── Login-detektion ───
            if self._login_detection_enabled and html:
                # 'final_url' kan vara samma som 'url' — då är det ingen redirect.
                # Vi kollar bara om sidan redirectades till login-URL ELLER om
                # innehållet otvetydigt är ett login-formulär.
                redirected_away = (normalize_url(final_url, self.ignore_query_params)
                                   != normalize_url(url, self.ignore_query_params))
                triggered = False
                if redirected_away and self.login_detector.is_login_redirect(url, final_url):
                    triggered = True
                elif self.login_detector.is_login_content(html):
                    triggered = True

                if triggered:
                    await self._handle_login_expired(url)
                    await self._drop_saved_page(url, cached, 'inloggning krävs')
                    await self.db.mark_page_unsaved(url)
                    return False

            # ─── Redirect utanför domänen / bot-skydd ───
            if (self.config.get("strict_domain", True)
                    and not self._domain_allowed(urlparse(final_url).netloc)):
                self.report["off_domain_redirects"].append({"url": url, "final_url": final_url})
                self._gui_update(url, "Omdirigerad utanför domän", "")
                await self._drop_saved_page(url, cached, 'omdirigerad utanför tillåtna domäner')
                await self.db.mark_page_unsaved(url)
                return False
            if final_url != url:
                # Slut-URL:en är redan besökt — undvik att hämta samma innehåll två gånger
                self.url_queue.mark_seen(final_url)

            block_reason = self._detect_block_page(html)
            if block_reason:
                self._log(f"  ⛔ Blockeringssida ({block_reason}): {url}", LogLevel.WARNING)
                self.report["blocked_pages"].append({"url": url, "signal": block_reason})
                async with self.async_stats_lock:
                    self.stats.pages_failed += 1
                self._gui_update(url, "Blockerad av sajten", "")
                return False

            data = await asyncio.to_thread(self.extract_structured_data, html, final_url)
            content_hash = get_clean_hash(data["plain_text"])
            text_length = len(data["plain_text"])

            if _SOFT_404_TITLE_RE.search(data["title"]) and text_length < 1500:
                self.report["soft_404"].append(url)
                await self._handle_gone(url, cached, 404, confirmed=False)
                return False

            if "possible_prompt_injection" in data.get("flags", []):
                self._log(f"  ⚠ Möjlig prompt-injektion i sidans innehåll: {url}",
                          LogLevel.WARNING)
                self.report["possible_prompt_injection"].append(url)

            page_links = data.get("page_links", [])

            # ─── Språkfilter ───
            if self.languages:
                page_lang = (data.get("language") or "").lower().replace("_", "-").split("-")[0]
                if page_lang and page_lang not in self.languages:
                    self.report["wrong_language"].append({"url": url, "language": page_lang})
                    self._gui_update(url, f"Hoppar över språk ({page_lang})", data["title"])
                    await self._drop_saved_page(url, cached, "fel språk")
                    await self._save_page_cache(url, data, content_hash, etag, last_mod, [], None, fetch_url,
                                                rendered=source == 'Webbläsare')
                    async with self.async_stats_lock:
                        self.stats.pages_visited += 1
                    return True

            # ─── Canonical: sidan är en kopia av en annan URL ───
            canon = self._canonical_duplicate(url, data.get("canonical"))
            if canon:
                canon_key = normalize_url(canon, self.ignore_query_params)
                self._canonical_of[url] = canon_key
                self._canonical_pending[url] = canon_key
                self.url_queue.add_url(canon, depth=depth, base_priority=CrawlPriority.HIGH.value)

            # Dubblettbeslut tas först efter alla sidkontroller. Ett tidigare
            # original kan ändras eller försvinna senare i samma körning.

            is_quarantined = bool(data.get("flags") and self.config.get("quarantine_flagged_chunks", True))
            out_path = self._text_output_path(url, is_quarantined=is_quarantined)
            fn = os.path.basename(out_path) if self._saves_text() else None
            clean_path = self._text_output_path(url, is_quarantined=False)
            quarantine_path = self._text_output_path(url, is_quarantined=True)

            if is_quarantined and os.path.exists(clean_path):
                try:
                    os.remove(clean_path)
                except OSError:
                    pass
            elif not is_quarantined and os.path.exists(quarantine_path):
                try:
                    os.remove(quarantine_path)
                except OSError:
                    pass

            unchanged = bool(
                self.config.get("incremental") and cached
                and cached.get('processing_profile') == self._page_profile()
                and cached.get('output_hash') == self._page_output_hash(data)
                and (not fn or os.path.exists(out_path))
            )

            if unchanged:
                async with self.async_stats_lock:
                    self.stats.pages_unchanged += 1
                self._gui_update(url, "Oförändrad", data["title"])
            else:
                self._gui_update(url, f"Hämtad ({source})", data["title"])

                if text_length > 50 and self._saves_text():
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    await blocking_complete(self._write_page_file, out_path, final_url, data)
                    event = "updated" if cached else "added"
                    self.counts[event] += 1
                    self._record_change(event, url, file=fn, title=data["title"])
                elif self._saves_text():
                    # För lite text att spara — notera det så att luckor syns i rapporten
                    fn = None
                    await self._drop_saved_page(url, cached, "för lite text")
                    self.report["short_pages"].append({"url": url, "chars": text_length})
                    self._log(f"  ⚠ För lite text ({text_length} tecken), sparas inte: {url}",
                              LogLevel.DEBUG)

            await self._save_page_cache(url, data, content_hash, etag, last_mod,
                                        data.get("page_links", []), fn, fetch_url,
                                        rendered=source == 'Webbläsare')
            self._login_expired_streak = 0

            # ─── Länkdetektering ───
            await self._enqueue_links(final_url, data.get("title", ""),
                                      data.get("page_links", []), depth)

            async with self.async_stats_lock:
                self.stats.pages_visited += 1
            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f"  ✗ Fel vid besök ({url}): {str(e)[:80]}", LogLevel.ERROR)
            async with self.async_stats_lock:
                self.stats.pages_failed += 1
            self._gui_update(url, "Fel", str(e)[:30])
            return False

    def _write_page_file(self, out_path: str, url: str, data: Dict):
        """Skriver sidans utdatafil atomärt (körs i tråd)."""
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if self.save_format == ".json":
            atomic_write_text(out_path, json.dumps(data, ensure_ascii=False, indent=2))
            return
        body = downgrade_body_h1(data['plain_text'])
        if self.save_format == ".txt":
            meta = [data['title'], '', f'Källa: {url}', f"Hämtad: {data['crawled_at'][:10]}"]
            for key, label in (('modified_date', 'Senast ändrad'), ('published_date', 'Publicerad'),
                               ('language', 'Språk')):
                if data.get(key):
                    meta.append(f'{label}: {data[key]}')
            if data.get('flags'):
                meta.append(f"Flaggor: {', '.join(data['flags'])}")
            atomic_write_text(
                out_path,
                '\n'.join(meta) + f'\n\n---\n\n{markdown_to_plain(body)}\n\n---\n\nKälla: {url}\n')
            return
        # Markdown: bädda in URL i BRÖDTEXTEN (inte bara i header) eftersom RAG-
        # pipelines ofta kapar de första raderna före retrieval. Upprepa källan
        # sist så att även avslutande chunks har källinformation. Datum och språk
        # ligger också i brödtexten så att "hur aktuell är sidan?" går att svara på.
        meta = [f"# {data['title']}", "", f"**Källa:** {url}"]
        if data.get('modified_date'):
            meta.append(f"**Senast ändrad:** {data['modified_date']}")
        elif data.get('published_date'):
            meta.append(f"**Publicerad:** {data['published_date']}")
        meta.append(f"**Hämtad:** {data['crawled_at'][:10]}")
        if data.get('language'):
            meta.append(f"**Språk:** {data['language']}")
        if data.get('flags'):
            meta.append(f"**Flaggor:** {', '.join(data['flags'])}")
        atomic_write_text(
            out_path,
            "\n".join(meta) + f"\n\n---\n\n{body}\n\n---\n\n**Källa:** {url}\n")

    async def _handle_login_expired(self, url: str, status: Optional[int] = None):
        verified = await self._verify_login()
        async with self.async_stats_lock:
            self.stats.pages_failed += 1
        if verified is not False:
            self._login_expired_streak = 0
            entry = {'url': url, 'status': status}
            if verified is None:
                self.report['session_unverified'].append(entry)
            else:
                self.report['access_denied'].append(entry)
            self._gui_update(url, f'HTTP {status}' if status else 'Inloggning krävs', '')
            return
        self._log(f"⚠ Session utgången — bekräftad vid sessionskontroll för: {url}",
                  LogLevel.WARNING)
        self.report["session_expired"].append(url)
        self._gui_update(url, "Session utgången", "")
        self._login_expired_streak += 1
        if (self._login_expired_streak >= self.LOGIN_EXPIRED_LIMIT
                and self.state != CrawlerState.STOPPED):
            self.fatal_error = ("Sessionen har gått ut (upprepade inloggningssidor). "
                                "Crawlen avbröts — logga in på nytt och kör igen.")
            self._log(f"⛔ {self.fatal_error}", LogLevel.ERROR)
            self.stop()

    # ─── Dokument ───────────────────────────────────────────
    def _conversion_profile(self) -> str:
        """Inställningar som avgör om en tidigare dokumenttext kan återanvändas."""
        keys = ('remove_email', 'remove_phone', 'remove_pnr', 'remove_ip', 'keep_role_emails')
        options = {key: bool(self.config.get(key, False)) for key in keys}
        options.update(ocr=bool(self.config.get('ocr', True)),
                       ocr_languages=str(self.config.get('ocr_languages', 'swe+eng')),
                       ocr_max_pages=int(self.config.get('ocr_max_pages', 50)),
                       quarantine=bool(self.config.get('quarantine_flagged_chunks', True)),
                       converter_version=5)
        return hashlib.sha256(json.dumps(options, sort_keys=True).encode('utf-8')).hexdigest()

    def _conversion_context(self, ref: Dict, last_modified: Optional[str]) -> str:
        values = {key: metadata_line(self.clean_pii(ref.get(key, '') or ''))
                  for key in ('referer_url', 'referer_title', 'link_text')}
        values['server_modified'] = DocumentConverter._parse_date(last_modified, http=True)
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode('utf-8')).hexdigest()

    async def _invalidate_document_text(self, url: str, prior: Optional[Dict]):
        """En gammal konvertering får aldrig leva vidare som text för en ny version."""
        names = set()
        if prior:
            if prior.get('md_filename'):
                names.add(os.path.basename(prior['md_filename']))
            if prior.get('filename'):
                names.add(DocumentConverter.md_filename(url, prior['filename']))
        await self.db.invalidate_doc_text(url)
        # Spara invalidationen före filbyte/konvertering också vid avbruten körning.
        await self.db.flush()
        for name in names:
            for folder in ('texter', 'texter_karantan'):
                path = os.path.join(self.output_dir, folder, name)
                if os.path.isfile(path):
                    os.remove(path)
                    self._record_change('document_text_removed', url, file=name)

    async def _handle_gone_document(self, url: str, prior: Optional[Dict], status: int):
        await self._invalidate_document_text(url, prior)
        await self.db.delete_doc(url)
        if prior and prior.get('filename'):
            path = os.path.join(self.output_dir, 'dokument', os.path.basename(prior['filename']))
            if os.path.isfile(path):
                os.remove(path)
            self.counts['docs_removed'] += 1
            self._record_change('document_removed', url, file=prior['filename'],
                                reason=f'HTTP {status}')
        self.manifest._downloads.pop(url, None)
        self.report['gone_documents'].append({'url': url, 'status': status})

    async def _convert_document(self, url: str, filepath: str, filename: str,
                                ref: Dict) -> Optional[str]:
        if not self.converter:
            return None
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in DocumentConverter.SUPPORTED and ext not in DocumentConverter.LEGACY_TARGETS:
            return None
        keys = ('remove_email', 'remove_phone', 'remove_pnr', 'remove_ip', 'keep_role_emails',
                'ocr', 'ocr_languages', 'ocr_max_pages')
        options = {key: self.config[key] for key in keys if key in self.config}
        md_name = DocumentConverter.md_filename(url, filename)
        result = await self._conversion_runner.run(
            filepath, url, ref, options, md_name, self.max_download_bytes,
            expected_hash=ref.get('_content_hash'))
        md_path = None
        if result.get('text'):
            md_path = os.path.join(self.output_dir, 'texter', md_name)
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            await blocking_complete(atomic_write_text, md_path, result['text'])
        if md_path:
            with open(md_path, encoding='utf-8') as f:
                meta, _ = markdown_file_to_chunks(f.read(), is_document=True)
            if 'possible_prompt_injection' in meta['flags']:
                self.report['possible_prompt_injection'].append(url)
                if self.config.get('quarantine_flagged_chunks', True):
                    folder = os.path.join(self.output_dir, 'texter_karantan')
                    os.makedirs(folder, exist_ok=True)
                    target = os.path.join(folder, os.path.basename(md_path))
                    os.replace(md_path, target)
                    md_path = target
            self._log(f"  📄 Konverterad till .md: {os.path.basename(md_path)}",
                      LogLevel.DEBUG)
            if result['ocr']:
                self.report["ocr_used"].append({"url": url, "file": filename})
        else:
            reason = result['error'] or "okänd orsak"
            self.report["conversion_failures"].append(
                {"url": url, "file": filename, "reason": reason})
        return md_path

    def _start_conversion_queue(self):
        if self._conversion_queue is not None or not self.converter:
            return
        self._conversion_queue = asyncio.Queue(maxsize=self.config['conversion_queue_size'])

        async def worker():
            while True:
                job = await self._conversion_queue.get()
                try:
                    if job is None:
                        return
                    while self.state == CrawlerState.PAUSED:
                        await asyncio.sleep(0.3)
                    if self.state != CrawlerState.STOPPED:
                        await self._complete_conversion(job)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.report['conversion_failures'].append({
                        'url': job['url'], 'file': job['filename'], 'reason': str(error)[:200]})
                    self._log(f'⚠ Konverteringsjobb misslyckades: {error}', LogLevel.ERROR)
                finally:
                    if job is not None:
                        self._conversion_pending -= 1
                    self._conversion_queue.task_done()

        self._conversion_tasks = [asyncio.create_task(worker())
                                  for _ in range(self.config['conversion_workers'])]

    async def _complete_conversion(self, job: Dict):
        ref = {**job['ref'], '_content_hash': job['content_hash']}
        md_path = await self._convert_document(job['url'], job['filepath'], job['filename'], ref)
        if not md_path:
            return
        current = await self.db.get_doc(job['url'])
        if (not current or current['content_hash'] != job['content_hash']
                or self._conversion_context(current, current['last_modified']) != job['context_hash']):
            os.remove(md_path)
            self.report['conversion_failures'].append({'url': job['url'], 'file': job['filename'],
                                                       'reason': 'Originalversion eller metadata ändrades under konverteringen'})
            return
        await self.db.mark_doc_converted(job['url'], job['content_hash'], os.path.basename(md_path),
                                         job['profile'], job['context_hash'])
        self._record_change('document_converted', job['url'], file=os.path.basename(md_path),
                            content_hash=job['content_hash'])

    async def _queue_conversion(self, job: Dict):
        ext = os.path.splitext(job['filepath'])[1].lower()
        if ext not in DocumentConverter.SUPPORTED and ext not in DocumentConverter.LEGACY_TARGETS:
            return
        if self._conversion_queue is None:
            await self._complete_conversion(job)
            return
        self._conversion_pending += 1
        try:
            await self._conversion_queue.put(job)
        except BaseException:
            self._conversion_pending -= 1
            raise

    async def _close_conversion_queue(self, cancel: bool = False):
        if not self._conversion_tasks:
            return
        if cancel:
            for task in self._conversion_tasks:
                if not task.cancelling():
                    task.cancel()
        else:
            await self._conversion_queue.join()
            for _ in self._conversion_tasks:
                await self._conversion_queue.put(None)
        await asyncio.gather(*self._conversion_tasks, return_exceptions=True)
        self._conversion_tasks = []
        if cancel:
            while not self._conversion_queue.empty():
                job = self._conversion_queue.get_nowait()
                if job is not None:
                    self._conversion_pending -= 1
                self._conversion_queue.task_done()

    async def _download_via_aiohttp(self, url: str, docs_dir: str, default_name: str,
                                    url_ext: str, slug_base: str, url_hash: str,
                                    headers: Dict[str, str],
                                    prior_path: Optional[str], prior_hash: Optional[str] = None) -> Dict:
        """Strömmar ett dokument till <fil>.part och byter atomärt till slutnamnet.

        Returnerar dict med "status": ok | not_modified | same | too_large |
        aborted | failed (+ filename/etag/last_modified vid ok/same).
        """
        part_path = None
        try:
            self._check_http_target(url)
            async with self.req_session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status in (429, 503):
                    self.rate_limiter.slow_down(domain=urlparse(str(resp.url)).netloc.lower())
                elif resp.status in (200, 304):
                    self.rate_limiter.succeeded(urlparse(str(resp.url)).netloc.lower())
                if resp.status == 304:
                    return {"status": "not_modified", 'etag': resp.headers.get('ETag'),
                            'last_modified': resp.headers.get('Last-Modified')}
                if resp.status in (404, 410):
                    return {"status": "gone", "http_status": resp.status}
                if resp.status != 200:
                    self._log(f"  ⚠ aiohttp HTTP {resp.status} för dokument: {url}",
                              LogLevel.DEBUG)
                    return {"status": "failed", "browser_retry": resp.status in (401, 403)}

                if (self.config.get('strict_domain', True)
                        and not self._domain_allowed(resp.url.raw_authority)):
                    self.report['off_domain_redirects'].append(
                        {'url': url, 'final_url': str(resp.url)})
                    return {"status": "blocked"}

                content_type = resp.headers.get('Content-Type', '').lower()
                cd_raw = resp.headers.get('Content-Disposition', '')
                is_attachment = 'attachment' in cd_raw.lower()
                doc_content_types = (
                    'application/pdf', 'application/vnd',
                    'application/msword', 'application/octet-stream',
                    'application/x-download', 'application/force-download',
                    'application/zip', 'application/x-zip',
                    'application/x-rar', 'application/rtf', 'text/csv',
                )
                looks_like_doc = (
                    is_attachment or any(dt in content_type for dt in doc_content_types)
                )
                if not looks_like_doc and 'text/html' in content_type:
                    self._log(f"  ⚠ Servern svarar HTML istället för dokument: {url}",
                              LogLevel.DEBUG)
                    return {"status": "failed", "browser_retry": True}

                if (resp.content_length is not None
                        and resp.content_length > self.max_download_bytes):
                    self._log(f"  ⚠ Dokumentet är för stort "
                              f"({resp.content_length // 1024 // 1024} MB > "
                              f"{self.max_download_bytes // 1024 // 1024} MB): {url}",
                              LogLevel.WARNING)
                    return {"status": "too_large"}

                etag = resp.headers.get('ETag')
                last_mod = resp.headers.get('Last-Modified')

                # Filnamn: ett tidigare namn behålls (stabilt mellan körningar)
                if prior_path:
                    filename = os.path.basename(prior_path)
                else:
                    ext = url_ext
                    filename = default_name
                    if ext == ".bin":
                        ct_ext_map = {
                            'application/pdf': '.pdf',
                            'application/msword': '.doc',
                            'application/vnd.openxmlformats-officedocument.wordprocessingml': '.docx',
                            'application/vnd.openxmlformats-officedocument.spreadsheetml': '.xlsx',
                            'application/vnd.openxmlformats-officedocument.presentationml': '.pptx',
                            'application/vnd.ms-excel': '.xls',
                            'application/vnd.ms-powerpoint': '.ppt',
                            'application/zip': '.zip',
                            'application/x-zip': '.zip',
                        }
                        for ct_prefix, ct_extension in ct_ext_map.items():
                            if ct_prefix in content_type:
                                ext = ct_extension
                                filename = f"{slug_base}_{url_hash}{ext}"
                                break
                    if cd_raw:
                        match = re.search(
                            r"filename\*?=(?:UTF-8'')?[\"']?([^\"';]+)[\"']?",
                            cd_raw, flags=re.IGNORECASE)
                        if match:
                            cd_name = unquote(match.group(1)).strip()
                            if cd_name:
                                cd_ext = self._safe_ext(posixpath.splitext(cd_name)[1]) or ext
                                filename = (f"{slugify(cd_name.split('.')[0])[:50] or slug_base}_"
                                            f"{url_hash}{cd_ext}")

                final_ext = os.path.splitext(filename)[1]
                final_path = os.path.join(docs_dir, filename)

                part_path = final_path + ".part"
                written = 0
                first = True
                digest = hashlib.sha256()
                with open(part_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        if self.state == CrawlerState.STOPPED:
                            return {"status": "aborted"}
                        if first:
                            first = False
                            if not magic_ok(final_ext, chunk):
                                self._log(f"  ⚠ Innehållet matchar inte filtypen "
                                          f"{final_ext} (troligen en inloggnings-/felsida): {url}",
                                          LogLevel.WARNING)
                                return {"status": "failed"}
                        written += len(chunk)
                        if written > self.max_download_bytes:
                            self._log(f"  ⚠ Dokumentet överskred storleksgränsen: {url}",
                                      LogLevel.WARNING)
                            return {"status": "too_large"}
                        f.write(chunk)
                        digest.update(chunk)
                if written == 0:
                    return {"status": "failed"}
                content_hash = digest.hexdigest()
                if prior_path and prior_hash == content_hash:
                    return {"status": "same", "filename": filename, "content_hash": content_hash,
                            "etag": etag, "last_modified": last_mod}
                if prior_path:
                    cache_url = normalize_url(url, self.ignore_query_params)
                    await self._invalidate_document_text(cache_url, await self.db.get_doc(cache_url))
                os.replace(part_path, final_path)
                part_path = None
                return {"status": "ok", "filename": filename,
                        "etag": etag, "last_modified": last_mod, "content_hash": content_hash}
        except BlockedHostError:
            return {"status": "blocked"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._log(f"  ⚠ aiohttp-fel vid dokumentnedladdning: {e}", LogLevel.DEBUG)
            return {"status": "failed"}
        finally:
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    @staticmethod
    def _safe_ext(ext: str) -> str:
        ext = (ext or "").lower()
        return ext if _SAFE_DOC_EXT_RE.match(ext) else ""

    async def download_document(self, url: str):
        fetch_url = request_url(url)
        url = normalize_url(url, self.ignore_query_params)
        if self.state == CrawlerState.STOPPED:
            return
        # Ett blockerat nätverksmål ska inte felrapporteras som ett robots-fel.
        try:
            self._check_http_target(fetch_url)
        except BlockedHostError:
            self.report['download_failures'].append({'url': url, 'reason': 'blocked'})
            return
        if not await self._robots_allowed(fetch_url):
            return
        while self.state == CrawlerState.PAUSED:
            await asyncio.sleep(0.3)

        async with self.async_download_lock:
            if url in self.downloaded_files:
                return
            self.downloaded_files.add(url)

        # Rate limit även dokumentnedladdningar
        domain = urlparse(url).netloc
        await self.rate_limiter.async_wait(domain)

        try:
            docs_dir = os.path.join(self.output_dir, "dokument")
            os.makedirs(docs_dir, exist_ok=True)

            prior = await self.db.get_doc(url)
            prior_path = None
            if prior and prior['filename']:
                cand = os.path.join(docs_dir, os.path.basename(prior['filename']))
                if os.path.isfile(cand):
                    prior_path = cand
            prior_hash = await self._original_hash(prior_path) if prior_path else None

            # Stabilt namn från URL:en (används om inget tidigare namn finns)
            url_basename = os.path.basename(unquote(urlparse(url).path)) or ""
            url_ext = self._safe_ext(posixpath.splitext(url_basename)[1]) or ".bin"
            slug_base = slugify((url_basename.split('.')[0] if url_basename else 'dokument'))[:50] or "dokument"
            url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:6]
            default_name = f"{slug_base}_{url_hash}{url_ext}"

            # Villkorlig GET: uppdaterade dokument hämtas om, oförändrade hoppas över
            headers: Dict[str, str] = {}
            if (prior_path and (not prior.get('content_hash')
                                or prior['content_hash'] == prior_hash)):
                if prior['etag']:
                    headers['If-None-Match'] = prior['etag']
                if prior['last_modified']:
                    headers['If-Modified-Since'] = prior['last_modified']

            outcome = await self._download_via_aiohttp(
                fetch_url, docs_dir, default_name, url_ext, slug_base, url_hash,
                headers, prior_path, prior_hash)
            status = outcome["status"]

            if status == 'gone':
                await self._handle_gone_document(url, prior, outcome['http_status'])
                return

            if status == "aborted":
                async with self.async_download_lock:
                    self.downloaded_files.discard(url)
                return

            # ─── Playwright-fallback (SAML-skyddade dokument) ───
            if (status == "failed" and outcome.get('browser_retry') and HAS_PLAYWRIGHT
                    and self._login_detection_enabled):
                self._log(f"  🔄 Försöker Playwright-fallback för: {url}", LogLevel.DEBUG)
                filename = os.path.basename(prior_path) if prior_path else default_name
                final_path = os.path.join(docs_dir, filename)
                part_path = final_path + ".part"
                ok = await self._download_via_playwright(fetch_url, part_path)
                if ok and os.path.exists(part_path):
                    with open(part_path, 'rb') as fh:
                        head = fh.read(1024)
                    size = os.path.getsize(part_path)
                    if (magic_ok(os.path.splitext(filename)[1], head)
                            and 0 < size <= self.max_download_bytes):
                        incoming_hash = await asyncio.to_thread(file_sha256, part_path)
                        if prior_path and incoming_hash != prior_hash:
                            await self._invalidate_document_text(url, prior)
                        os.replace(part_path, final_path)
                        outcome = {"status": "same" if prior_hash == incoming_hash else "ok", "filename": filename,
                                   "etag": None, "last_modified": None,
                                   "content_hash": incoming_hash}
                        status = outcome['status']
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass

            referers = self.manifest._referers.get(url, [])
            ref = dict(referers[0]) if referers else {}
            if not ref and prior:
                ref = {'referer_url': prior['referer_url'],
                       'referer_title': prior['referer_title'],
                       'link_text': prior['link_text']}

            if status in ("ok", "not_modified", "same"):
                self._successful_documents += 1
                filename = outcome.get("filename") or os.path.basename(prior_path or default_name)
                filepath = os.path.join(docs_dir, filename)
                size = os.path.getsize(filepath)
                content_hash = outcome.get('content_hash') or prior_hash
                if not content_hash:
                    content_hash = await asyncio.to_thread(file_sha256, filepath)
                profile = self._conversion_profile()
                lm = outcome.get('last_modified') or (prior or {}).get('last_modified')
                context_hash = self._conversion_context(ref, lm)
                if status == 'not_modified':
                    fetched_at = (prior or {}).get('fetched_at') or ''
                elif status == 'same' and (prior or {}).get('fetched_at'):
                    fetched_at = prior['fetched_at']
                else:
                    fetched_at = datetime.now().isoformat(timespec='seconds')
                ref.update(fetched_at=fetched_at, server_last_modified=lm or '')
                conversion_valid = bool(
                    prior and prior.get('converted_hash') == content_hash
                    and prior.get('conversion_profile') == profile
                    and prior.get('conversion_context') == context_hash
                    and prior.get('md_filename')
                    and any(os.path.isfile(os.path.join(self.output_dir, folder,
                                                       os.path.basename(prior['md_filename'])))
                            for folder in ('texter', 'texter_karantan'))
                )
                if not conversion_valid:
                    await self._invalidate_document_text(url, prior)

                if status == "ok":
                    async with self.async_stats_lock:
                        self.stats.documents_downloaded += 1
                        self.stats.bytes_downloaded += size
                    kind = "docs_updated" if prior_path else "docs_added"
                    self.counts[kind] += 1
                    self._record_change("document_updated" if prior_path else "document_added",
                                        url, file=filename)
                    self._log(f"  ⬇ Dokument sparat: {filename}")
                    etag, lm = outcome.get("etag"), outcome.get("last_modified")
                else:
                    etag = outcome.get("etag") or (prior or {}).get('etag')
                    lm = outcome.get("last_modified") or (prior or {}).get('last_modified')
                    self._log(f"  ↩ Oförändrat dokument: {filename}", LogLevel.DEBUG)

                await self.manifest.record_download(doc_url=url, filename=filename,
                                                    size_bytes=size)
                await self.db.save_doc(url, filename, size, etag, lm,
                                       ref.get('referer_url', ''),
                                       ref.get('referer_title', ''),
                                       ref.get('link_text', ''), content_hash=content_hash,
                                       fetched_at=fetched_at)

                # Även 304 måste försöka igen efter ett tidigare konverteringsfel.
                if self.converter and not conversion_valid:
                    await self._queue_conversion({'url': url, 'filepath': filepath, 'filename': filename,
                                                 'ref': dict(ref), 'content_hash': content_hash,
                                                 'profile': profile, 'context_hash': context_hash})
                return

            # Misslyckades. Finns en tidigare version behålls den och registreras.
            reason = {"too_large": "för stort", "failed": "kunde inte hämtas"}.get(status, status)
            self._log(f"  ✗ Kunde inte ladda ner dokument ({reason}): {url}",
                      LogLevel.WARNING)
            self.report["download_failures"].append({"url": url, "reason": reason})
            async with self.async_download_lock:
                self.downloaded_files.discard(url)
            if prior_path:
                await self.manifest.record_download(
                    doc_url=url, filename=os.path.basename(prior_path),
                    size_bytes=os.path.getsize(prior_path))
        except Exception as e:
            self._log(f"  ✗ Filnedladdning misslyckades: {url}, {e}", LogLevel.ERROR)
            self.report['download_failures'].append({'url': url, 'reason': str(e)[:160]})
            async with self.async_download_lock:
                self.downloaded_files.discard(url)

    async def _download_via_playwright(self, url: str, filepath: str) -> bool:
        """Fallback-nedladdning via Playwright för SAML-skyddade dokument.

        Browser-sessionen följer SAML-flödet. Bilagor och inline-dokument
        avlyssnas efter headers och strömmas med en strikt storleksgräns.
        """
        async with self.playwright_semaphore:
            ctx = await self.get_playwright_context()
            if ctx is None:
                return False

            page = await ctx.new_page()
            guard = None
            try:
                guard = await self._install_browser_guard(page, filepath)
                try:
                    await page.goto(url, wait_until='commit', timeout=15000)
                except Exception:
                    # Navigationen avbryts avsiktligt när dokumentstreamen tas över.
                    pass
                try:
                    await asyncio.wait_for(guard['done'].wait(), 120 if guard['started'] else 15)
                except asyncio.TimeoutError:
                    guard['reason'] = 'tidsgränsen överskreds'
                if not guard['success']:
                    self._log(f"  ⚠ Browser-nedladdning misslyckades: {guard['reason'] or 'inget dokument'}",
                              LogLevel.DEBUG)
                return guard['success']
            finally:
                if guard:
                    await self._close_browser_guard(guard)
                try:
                    await page.close()
                except Exception:
                    pass

    # ─── Filer som skrivs vid körningens slut ───────────────
    async def _drop_duplicate_page(self, url: str, cached: Optional[Dict], reason: str):
        if url in self._initial_page_files:
            await self._drop_saved_page(url, cached, reason)
        elif cached and cached.get('filename'):
            # En ny provisorisk kopia publicerades aldrig i korpusen. Räkna
            # därför inte dess borttagning som en borttagen publicerad källa.
            path = os.path.join(self.output_dir, 'texter', os.path.basename(cached['filename']))
            if os.path.isfile(path):
                os.remove(path)
            provisional = [change for change in self.changes
                           if change['url'] == url and change['event'] in ('added', 'updated')]
            for change in provisional:
                self.counts[change['event']] -= 1
                self.changes.remove(change)
        await self.db.mark_page_unsaved(url)

    async def _finalize_page_duplicates(self):
        """Canonical får ersätta källan först när målet bekräftats och sparats."""
        for source, target in self._canonical_pending.items():
            visited = {source}
            while target in self._canonical_pending and target not in visited:
                visited.add(target)
                target = self._canonical_pending[target]
            if target in visited:
                target = min(visited)  # behåll en deterministisk kopia vid en cykel
            if source == target:
                continue
            target_cache = await self.db.get_cache(target)
            confirmed = bool(target_cache and target_cache.get('crawled_at', '')
                             >= self.stats.start_time.isoformat())
            if confirmed and self.is_valid_url(target) and self._exportable_page(target_cache):
                source_cache = await self.db.get_cache(source)
                await self._drop_duplicate_page(source, source_cache, f'canonical → {target}')
                self.report['canonical_skipped'].append({'url': source, 'canonical': target})
                self._gui_update(source, 'Duplikat (canonical)', (source_cache or {}).get('title', ''))
            else:
                self.report['canonical_unresolved'].append({'url': source, 'canonical': target})

        if not self.dedupe_content:
            return
        owners = {}
        unresolved = {entry['url'] for entry in self.report['canonical_unresolved']}
        records = sorted(await self.db.get_all_records(),
                         key=lambda row: (row[0] not in self._initial_page_files, row[0]))
        for url, _, _, content_hash, _ in records:
            cached = await self.db.get_cache(url)
            if (not self.is_valid_url(url) or not self._exportable_page(cached)
                    or (cached.get('content_length') or 0) < 200):
                continue
            if url in unresolved:
                owners.setdefault(content_hash, url)
                continue
            if content_hash in owners:
                await self._drop_duplicate_page(url, cached, f'duplicate_of {owners[content_hash]}')
                self.report['duplicates'].append({'url': url, 'duplicate_of': owners[content_hash]})
            else:
                owners[content_hash] = url

    async def _enqueue_cached_urls(self):
        """Återkontrollera kända URL:er sist, även när de försvunnit ur navigeringen.

        Djupbegränsade körningar saknar säker djupinformation för gamla URL:er.
        De återkontrolleras först i nästa körning utan djupbegränsning.
        """
        if not self.config.get('incremental') or self.max_depth != 0:
            return
        for url, *_ in await self.db.get_all_records():
            if self.is_valid_url(url):
                cached = await self.db.get_cache(url)
                self.url_queue.add_url(cached.get('fetch_url') or url, depth=0,
                                       base_priority=CrawlPriority.LOW.value)
        if self.config.get('download_docs', False):
            for doc in await self.db.get_all_docs():
                if self.is_valid_url(doc['url']):
                    self.url_queue.add_url(doc['url'], depth=0,
                                           base_priority=CrawlPriority.LOW.value)

    async def _original_hash(self, path: str) -> str:
        """Verifiera originalet minst en gång per körning, återanvänd inom körningen.

        Storlek/filmetadata ensamma får inte intyga en tidigare körnings version.
        Atomiska filbyten och ändrad metadata ogiltigförklarar den lokala cachen.
        """
        stat = os.stat(path)
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        key = os.path.abspath(path)
        cached = self._file_hash_cache.get(key)
        if cached and cached[0] == identity:
            return cached[1]
        digest = await asyncio.to_thread(file_sha256, path)
        after = os.stat(path)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError('Dokumentoriginalet ändrades under hashkontrollen')
        self._file_hash_cache[key] = (identity, digest)
        return digest

    async def _prepare_export(self):
        """Exportera bara registrerade texter, och bara rätt version av dokumenttext.

        Äldre filer utan aktiv registrering flyttas ut ur Sveas textmappar till
        texter_utgangna. De raderas inte eftersom de kan komma från äldre körningar.
        """
        allowed = set()
        self._intentional_export_exclusions = set()
        # Äldre generationer saknar profilbeskrivning. Då ska ett begränsat
        # migrationsförsök inte tyst ersätta dem med nästan tomma utdata.
        known_previous_profile = False
        known_previous_doc_profile = False
        if self._publication and self._publication.previous_directory:
            old_report = os.path.join(self._publication.previous_directory, 'crawl_report.json')
            if os.path.isfile(old_report):
                with open(old_report, encoding='utf-8') as source:
                    previous_report = json.load(source)
                    known_previous_profile = bool(previous_report.get('page_processing_profile'))
                    known_previous_doc_profile = bool(previous_report.get('document_processing_profile'))
        for url, filename, profile in await self.db.get_page_files():
            if not filename:
                continue
            name = os.path.basename(filename)
            if (not self._saves_text() or not self.is_valid_url(url, for_export=True)
                    or profile != self._page_profile()):
                if (not self._saves_text() or not self.is_valid_url(url, for_export=True)
                        or known_previous_profile):
                    self._intentional_export_exclusions.add(name)
                continue
            if self.languages:
                path = next((os.path.join(self.output_dir, folder, name)
                             for folder in ('texter', 'texter_karantan')
                             if os.path.isfile(os.path.join(self.output_dir, folder, name))), None)
                if path:
                    with open(path, encoding='utf-8') as source:
                        raw = source.read()
                    if name.endswith('.json'):
                        language = json.loads(raw).get('language', '')
                    elif name.endswith('.txt'):
                        language = plain_file_to_chunks(raw)[0].get('language', '')
                    else:
                        language = markdown_file_to_chunks(raw)[0].get('language', '')
                    language = language.lower().replace('_', '-').split('-')[0]
                    if language and language not in self.languages:
                        self._intentional_export_exclusions.add(name)
                        self.report['wrong_language'].append({'url': url, 'language': language})
                        continue
            allowed.add(name)
        for doc in await self.db.get_all_docs():
            if not doc['filename'] or not self.is_valid_url(doc['url'], for_export=True):
                if doc.get('md_filename'):
                    self._intentional_export_exclusions.add(os.path.basename(doc['md_filename']))
                continue
            original = os.path.join(self.output_dir, 'dokument', os.path.basename(doc['filename']))
            if os.path.isfile(original):
                await self.manifest.record_download(doc['url'], doc['filename'],
                                                    os.path.getsize(original))
                await self.manifest.record_link(doc['url'], doc.get('referer_url') or '',
                                               doc.get('referer_title') or '',
                                               doc.get('link_text') or '')
            if not (self.convert_docs and doc['md_filename'] and doc['content_hash']
                    and doc['converted_hash'] == doc['content_hash']
                    and doc['conversion_profile'] == self._conversion_profile()
                    and os.path.isfile(original)):
                if doc.get('md_filename') and (not self.convert_docs
                        or known_previous_doc_profile and doc['conversion_profile'] != self._conversion_profile()):
                    self._intentional_export_exclusions.add(os.path.basename(doc['md_filename']))
                continue
            if (doc.get('conversion_context') is not None
                    and doc['conversion_context'] != self._conversion_context(doc, doc.get('last_modified'))):
                continue
            actual_hash = await self._original_hash(original)
            if actual_hash == doc['content_hash']:
                allowed.add(os.path.basename(doc['md_filename']))
                quarantine_file = os.path.join(self.output_dir, 'texter_karantan',
                                               os.path.basename(doc['md_filename']))
                if (os.path.isfile(quarantine_file)
                        and doc['url'] not in self.report['possible_prompt_injection']):
                    self.report['possible_prompt_injection'].append(doc['url'])
            else:
                self.report['document_version_mismatch'].append(
                    {'url': doc['url'], 'file': doc['filename']})
        self._export_files = allowed
        retired = await blocking_complete(self._retire_unregistered_texts, allowed)
        self.report['retired_text_files'].extend(retired)

    def _check_publication_loss(self):
        """Stoppa stort oförklarat bortfall, även när några hämtningar lyckats."""
        previous = self._publication.previous_directory if self._publication else None
        if not previous:
            return
        directory = os.path.join(previous, 'texter')
        if not os.path.isdir(directory):
            return
        before = {name for name in os.listdir(directory) if name.endswith(('.md', '.json', '.txt'))}
        now = {name for name in (self._export_files or set())
               if os.path.isfile(os.path.join(self.output_dir, 'texter', name))}
        explained = set(self._intentional_export_exclusions)
        # Stabil URL-hash identifierar samma källa efter format-/filnamnsbyte,
        # också när en HTML-resurs nu levereras som konverterat dokument.
        def source_identity(name):
            match = re.search(r'_([0-9a-f]{8})(?:_doc)?\.(?:md|json|txt)$', name)
            return match.group(1) if match else name
        identities = {source_identity(name) for name in now}
        for name in before:
            if source_identity(name) in identities or (not self._saves_text() and not name.endswith('_doc.md')):
                explained.add(name)
        for change in self.changes:
            if change.get('reason') in ('HTTP 404', 'HTTP 410', 'fel språk', 'HTTP 403'):
                if change.get('file'):
                    explained.add(os.path.basename(change['file']))
        # Bekräftade canonical-/dubblettbeslut och karantän är avsiktliga.
        for name in before:
            if os.path.isfile(os.path.join(self.output_dir, 'texter_karantan', name)):
                explained.add(name)
        for entry in self.report['duplicates'] + self.report['canonical_skipped']:
            url = entry.get('url') if isinstance(entry, dict) else entry
            if url:
                explained.add(stable_filename(url, self.save_format))
        lost = before - now - explained
        if lost and len(lost) > len(before) * 0.5:
            self.report['publication_blocked'].append(
                {'reason': 'oförklarat korpusbortfall', 'before': len(before),
                 'after': len(now), 'unexplained': len(lost), 'files': sorted(lost)[:200]})
            self.fatal_error = self.fatal_error or (
                f'Publicering stoppad: {len(lost)} av {len(before)} källfiler saknas utan belagd borttagning')
            self._log(self.fatal_error, LogLevel.ERROR)

    def _retire_unregistered_texts(self, allowed: Set[str]) -> List[Dict]:
        retired = []
        for folder in ('texter', 'texter_karantan'):
            directory = os.path.join(self.output_dir, folder)
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if (name in allowed or not os.path.isfile(path)
                        or not name.endswith(('.md', '.json', '.txt'))):
                    continue
                archive = os.path.join(self.output_dir, 'texter_utgangna', folder)
                os.makedirs(archive, exist_ok=True)
                # Unikt arkivnamn så att tidigare versioner också kan återställas.
                target = os.path.join(archive, f'{time.time_ns()}_{name}')
                os.replace(path, target)
                retired.append({'file': name, 'from': folder,
                                'archive': os.path.relpath(target, self.output_dir)})
        return retired

    async def _generate_index(self):
        self._log("📊 Skapar index-fil (index.csv)...")
        try:
            records = await self.db.get_all_records()
            texts_dir = os.path.join(self.output_dir, "texter")
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(['URL', 'Titel', 'Hämtad_Datum', 'Filnamn'])
            for url, title, date, content_hash, filename in records:
                fn = filename if (filename and os.path.exists(
                    os.path.join(texts_dir, os.path.basename(filename)))) else ""
                writer.writerow([csv_safe(url), csv_safe(title), date, fn])
            atomic_write_text(os.path.join(self.output_dir, 'index.csv'), '\ufeff' + output.getvalue())
        except Exception as e:
            self._export_failed('index.csv', e)
            self._log(f"⚠ Fel vid skapande av index.csv: {e}", LogLevel.ERROR)

    async def _generate_manifest(self):
        """Skriver dokument-manifest.json till utmappen.

        Filen länkar varje nedladdat dokument till den sida som hade
        länken — RAG-systemet kan slå upp ett filnamn där och få
        tillbaka rätt intranät-URL att citera som källa.
        """
        try:
            manifest_data = self.manifest.build(domain=self.domain)
            count = manifest_data.get("document_count", 0)
            # Även ett tomt manifest måste ersätta föregående körnings dokumentlista.
            manifest_filename = f"manifest_{slugify(self.domain)}.json"
            manifest_path = os.path.join(self.output_dir, manifest_filename)
            atomic_write_text(manifest_path,
                              json.dumps(manifest_data, ensure_ascii=False, indent=2))
            for name in os.listdir(self.output_dir):
                if name.startswith('manifest_') and name.endswith('.json') and name != manifest_filename:
                    os.remove(os.path.join(self.output_dir, name))
            orphans = manifest_data.get("orphan_count", 0)
            msg = f"📋 Manifest skapad: {count} dokument"
            if orphans:
                msg += f" ({orphans} länkade men ej nedladdade)"
            self._log(msg)
        except Exception as e:
            self._export_failed('manifest', e)
            self._log(f"⚠ Fel vid skapande av manifest-fil: {e}", LogLevel.ERROR)

    def _export_failed(self, artifact: str, error: Exception):
        self.report['export_failures'].append({'artifact': artifact, 'reason': str(error)[:160]})
        message = (f'JSONL-export misslyckades: {error}' if artifact == 'chunks.jsonl'
                   else f'Export av {artifact} misslyckades: {error}')
        self.fatal_error = self.fatal_error or message

    def delivery_status(self) -> str:
        """Skilj körfel från normala borttagningar och avsiktliga urvalsgränser."""
        if self.fatal_error:
            return 'failed'
        # 404/410 är belagda borttagningar, inte driftfel. Startsidan utan
        # tidigare cache räknas redan separat som fel i process_page.
        errors = (max(0, self.stats.pages_failed - len(self.report['gone_pages']))
                  + len(self.report['download_failures'])
                  + len(self.report['conversion_failures'])
                  + len(self.report['robots_unreachable'])
                  + len(self.report['document_version_mismatch'])
                  + len(self.report['export_failures']))
        if self.state == CrawlerState.STOPPED or errors:
            return 'partial' if self.stats.pages_visited or self._successful_documents else 'failed'
        return 'success'

    def _write_chunks_jsonl(self) -> int:
        """Bygger chunks.jsonl (en chunk per rad) av ALLA sparade sidor och dokument.

        Läser från disk i stället för från minnet, så att filen blir komplett även vid
        inkrementella körningar där oförändrade sidor inte bearbetas om. Färdig att läsas
        in i en vektordatabas: varje rad har id, url, titel, rubrikväg, kontext och text.
        Flaggade sidor (t.ex. möjlig prompt-injektion) karantänsätts i chunks_quarantine.jsonl."""
        texts_dir = os.path.join(self.output_dir, "texter")
        quarantine_dir = os.path.join(self.output_dir, "texter_karantan")
        out_path = os.path.join(self.output_dir, "chunks.jsonl")
        quarantine_path = os.path.join(self.output_dir, "chunks_quarantine.jsonl")
        tmp_path = out_path + ".tmp"
        tmp_quarantine = quarantine_path + ".tmp"
        written = 0
        quarantine_written = 0
        quarantine_enabled = bool(self.config.get("quarantine_flagged_chunks", True))

        sources: List[Tuple[str, str, bool]] = []
        if os.path.isdir(texts_dir):
            for name in sorted(os.listdir(texts_dir)):
                if self._export_files is None or name in self._export_files:
                    sources.append((texts_dir, name, False))
        if os.path.isdir(quarantine_dir):
            for name in sorted(os.listdir(quarantine_dir)):
                if self._export_files is None or name in self._export_files:
                    sources.append((quarantine_dir, name, True))

        with open(tmp_path, 'w', encoding='utf-8') as out, \
             open(tmp_quarantine, 'w', encoding='utf-8') as q_out:
            for sdir, name, force_quarantine in sources:
                path = os.path.join(sdir, name)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    if name.endswith('.json'):
                        data = json.loads(raw)
                        flags = data.get("flags") or []
                        meta = {"url": data.get("url", ""), "title": data.get("title", ""),
                                "source_type": "page", "referer_url": "",
                                "language": data.get("language", ""),
                                "modified_date": data.get("modified_date", ""),
                                "crawled_at": data.get("crawled_at", ""),
                                "flags": flags}
                        chunks = data.get("chunks", [])
                    elif name.endswith('.md'):
                        meta, chunks = markdown_file_to_chunks(raw, is_document=name.endswith('_doc.md'))
                        flags = meta.get("flags") or []
                    elif name.endswith('.txt'):
                        meta, chunks = plain_file_to_chunks(raw)
                        flags = meta.get('flags') or []
                    else:
                        continue
                except Exception as e:
                    if self._export_files is not None:
                        raise RuntimeError(f'Registrerad källfil kunde inte exporteras: {name}') from e
                    self._log(f"  ⚠ Hoppar över {name} i chunks.jsonl: {e}", LogLevel.DEBUG)
                    continue

                if force_quarantine and not flags:
                    flags = ["quarantined"]
                    meta["flags"] = flags

                key = hashlib.md5((meta.get("url") or name).encode('utf-8')).hexdigest()[:10]
                is_quarantined = bool((flags or force_quarantine) and quarantine_enabled)
                target_out = q_out if is_quarantined else out

                for ch in chunks:
                    chunk_url = ch.get("url") or meta.get("url", "")
                    record = {
                        "id": f"{key}-{ch.get('chunk_index', 0)}",
                        **meta,
                        "url": chunk_url,
                        "heading": ch.get("heading", ""),
                        "heading_path": ch.get("heading_path", ""),
                        "context": ch.get("context", ""),
                        "content": ch.get("content", ""),
                        "chunk_index": ch.get("chunk_index", 0),
                        "total_chunks": ch.get("total_chunks", 0),
                        "flags": flags,
                    }
                    if "page_number" in ch and ch["page_number"] is not None:
                        record["page_number"] = ch["page_number"]
                        record["page_url"] = ch.get("page_url", f"{record['url']}#page={ch['page_number']}")
                    for field_name in ('heading_paths', 'slide_number'):
                        if field_name in ch:
                            record[field_name] = ch[field_name]

                    target_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    if is_quarantined:
                        quarantine_written += 1
                    else:
                        written += 1

        os.replace(tmp_path, out_path)
        if quarantine_written > 0:
            os.replace(tmp_quarantine, quarantine_path)
            self._log(f"🛡️ {quarantine_written} chunks sattes i karantän i chunks_quarantine.jsonl "
                      f"(flaggade för säkerhetsrisker)", LogLevel.WARNING)
        else:
            if os.path.exists(tmp_quarantine):
                try:
                    os.remove(tmp_quarantine)
                except OSError:
                    pass
            if os.path.exists(quarantine_path):
                try:
                    os.remove(quarantine_path)
                except OSError:
                    pass

        return written

    async def _generate_report(self):
        """Skriver changes.jsonl (append) och crawl_report.json (överskrivs)."""
        if self.changes:
            try:
                path = os.path.join(self.output_dir, 'changes.jsonl')
                with open(path + '.tmp', 'wb') as output:
                    if os.path.exists(path):
                        with open(path, 'rb') as previous:
                            shutil.copyfileobj(previous, output)
                    for change in self.changes:
                        output.write((json.dumps({**change, 'run': self.crawl_session_id},
                                                 ensure_ascii=False) + '\n').encode('utf-8'))
                os.replace(path + '.tmp', path)
            except Exception as e:
                self._export_failed('changes.jsonl', e)
        try:
            not_seen: List[Dict] = []
            if self._completed_naturally:
                # Sidor som fanns i cachen men inte nåddes den här gången. Bara
                # meningsfullt när hela sajten faktiskt genomsöktes.
                unrestricted = (self.max_pages == 0 and self.max_depth == 0
                                and not self.config.get("require_keywords"))
                if unrestricted:
                    rows = await self.db.get_unseen_since(self.stats.start_time.isoformat())
                    not_seen = [{"url": u, "title": t} for u, t in rows]

            report = {
                "run": self.crawl_session_id,
                "domain": self.domain,
                "version": VERSION,
                'page_processing_profile': self._page_profile(),
                'document_processing_profile': self._conversion_profile(),
                "started_at": self.stats.start_time.isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "completed_naturally": self._completed_naturally,
                "status": self.delivery_status(),
                "stopped": self.state == CrawlerState.STOPPED,
                "fatal_error": self.fatal_error,
                'attempt_generation': self._publication.generation_id if self._publication else None,
                "stats": {
                    "pages_visited": self.stats.pages_visited,
                    "pages_unchanged": self.stats.pages_unchanged,
                    "pages_not_modified_304": self.stats.pages_not_modified_304,
                    "pages_failed": self.stats.pages_failed,
                    "playwright_fallbacks": self.stats.playwright_fallbacks,
                    "documents_downloaded": self.stats.documents_downloaded,
                    "bytes_downloaded": self.stats.bytes_downloaded,
                },
                "changes": self.counts,
                "not_seen_since_last_run_count": len(not_seen),
                "not_seen_since_last_run": not_seen[:500],
            }
            for key, items in self.report.items():
                report[key + "_count"] = len(items)
                report[key] = items[:200]
            report_path = os.path.join(self.output_dir, 'crawl_report.json')
            if self._publication is None and self.fatal_error:
                # En nekad parallell körning får inte skriva över den aktiva körningens rapport.
                reports = os.path.join(self._output_root, 'reports')
                os.makedirs(reports, exist_ok=True)
                report_path = os.path.join(reports, self.crawl_session_id + '.json')
            atomic_write_text(report_path,
                              json.dumps(report, ensure_ascii=False, indent=2))
        except Exception as e:
            self._export_failed('crawl_report.json', e)
            self._log(f"⚠ Fel vid skapande av crawl_report.json: {e}", LogLevel.ERROR)

    def summary_text(self) -> str:
        s = self.stats
        parts = [f"{s.pages_visited} sidor besökta",
                 f"{self.counts['added']} nya", f"{self.counts['updated']} ändrade",
                 f"{self.counts['removed']} borttagna",
                 f"{s.documents_downloaded} dokument", f"{s.pages_failed} sidfel"]
        label = {'success': 'Lyckad körning', 'partial': 'Partiell körning',
                 'failed': 'Misslyckad körning'}[self.delivery_status()]
        text = label + ': ' + ", ".join(parts) + "."
        warn = []
        if self.report['download_failures']:
            warn.append(f"{len(self.report['download_failures'])} dokument kunde inte hämtas")
        if self.report['robots_unreachable']:
            warn.append(f"robots.txt kunde inte nås på {len(self.report['robots_unreachable'])} värdar")
        if self.report["conversion_failures"]:
            warn.append(f"{len(self.report['conversion_failures'])} dokument gick inte att konvertera")
        if self.report["short_pages"]:
            warn.append(f"{len(self.report['short_pages'])} sidor hade för lite text")
        if self.report["possible_prompt_injection"]:
            warn.append(f"{len(self.report['possible_prompt_injection'])} sidor kan innehålla prompt-injektion")
        if self.fatal_error:
            warn.append(f"FEL: {self.fatal_error}")
        if warn:
            text += " Observera: " + "; ".join(warn) + ". Se crawl_report.json."
        return text

    def pause(self):
        if self.state == CrawlerState.RUNNING:
            self.state = CrawlerState.PAUSED
            return True
        elif self.state == CrawlerState.PAUSED:
            self.state = CrawlerState.RUNNING
            return False
        return False

    def stop(self):
        self.state = CrawlerState.STOPPED
        self._log("🛑 Avbryter crawl (väntar på aktiva processer)...")
        self.login_event.set()
        if self._crawl_loop and self._crawl_loop.is_running():
            self._crawl_loop.call_soon_threadsafe(self._cancel_current_work)

    def _cancel_current_work(self):
        for task in list(self._url_tasks) + self._conversion_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    # ─── Inloggning ─────────────────────────────────────────
    def _load_cookie_file(self) -> bool:
        path = self.config.get("cookie_file")
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and cookies:
                self.saved_cookies = cookies
                self._log(f"✓ Läste {len(cookies)} sparade cookies från {path}")
                return True
        except Exception as e:
            self._log(f"⚠ Kunde inte läsa cookie-filen {path}: {e}", LogLevel.WARNING)
        return False

    def _save_cookie_file(self):
        path = self.config.get("cookie_file")
        if not path or not self.saved_cookies:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.saved_cookies, f)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._log(f"✓ Sparade sessionscookies i {path} (innehåller inloggningsuppgifter "
                      f"— skydda filen!)", LogLevel.WARNING)
        except Exception as e:
            self._log(f"⚠ Kunde inte spara cookie-filen: {e}", LogLevel.WARNING)

    async def _interactive_login(self) -> bool:
        """Öppnar en synlig webbläsare för manuell inloggning. Kräver GUI."""
        if not HAS_PLAYWRIGHT:
            self._log("⚠ Playwright saknas, kan inte utföra manuell inloggning!",
                      LogLevel.ERROR)
            self.fatal_error = "Playwright saknas"
            return False
        if self.msg_queue is None:
            self.fatal_error = ("Interaktiv inloggning går inte i serverläge. Ange "
                                "'cookie_file' i konfigurationen (skapa filen genom att "
                                "köra en inloggad crawl i GUI:t med samma cookie_file).")
            self._log(f"⛔ {self.fatal_error}", LogLevel.ERROR)
            return False

        self.login_event.clear()
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=bool(self.config.get("login_browser_headless", False)),
                **await self._browser_network_options())
            context = await browser.new_context(
                ignore_https_errors=bool(self.config.get("ignore_https_errors", False)),
                service_workers='block', user_agent=self.user_agent)
            page = await context.new_page()

            self._log("👤 Navigerar till start-URL för manuell inloggning...")
            await page.goto(self.start_url)

            self._log("⏳ VÄNTAR PÅ MANUELL INLOGGNING...")
            self.msg_queue.put(("login_wait", None))
            await asyncio.to_thread(self.login_event.wait)

            if self.state == CrawlerState.STOPPED:
                await browser.close()
                return False

            self._log("🔄 Sparar cookies och byter till osynligt läge...")
            self.saved_cookies = await context.cookies()
            await browser.close()
        self._save_cookie_file()
        return True

    async def _verify_login(self) -> Optional[bool]:
        # Samtidiga behörighetsfel ska bara göra en skyddad sessionskontroll.
        async with self._session_probe_lock:
            now = time.monotonic()
            if self._session_probe_cache and now - self._session_probe_cache[0] < 60:
                return self._session_probe_cache[1]
            result = await self._verify_login_uncached()
            self._session_probe_cache = (time.monotonic(), result)
            return result

    async def _verify_login_uncached(self) -> Optional[bool]:
        """True=åtkomlig resurs, False=inloggning krävs, None=kontroll kunde inte göras.

        Ange session_check_url till en skyddad resurs om startsidan är publik.
        Nätverksfel får aldrig användas som bevis för en fungerande session.
        """
        probe = self.config.get('session_check_url') or self.start_url
        if not self._domain_allowed(urlparse(probe).netloc):
            return None
        r = await self.fetch(probe, max_retries=1)
        if r is None:
            return None
        if r.status in (401, 403):
            return False
        if r.status != 200:
            return None
        return not self.login_detector.detect(probe, r.final_url, r.status, r.text)

    # ─── Huvudloopen ────────────────────────────────────────
    async def _begin_publication(self):
        publication = PublicationWorkspace(self._output_root, os.path.basename(self.db.db_path),
                                             self.crawl_session_id)
        task = asyncio.create_task(asyncio.to_thread(publication.begin))
        try:
            working = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            await asyncio.to_thread(publication.abort)
            raise
        self._publication = publication
        self.output_dir = working
        self.db = AsyncCrawlDatabase(os.path.join(working, publication.cache_name))
        if self.converter:
            self.converter.texts_dir = os.path.join(working, 'texter')
            os.makedirs(self.converter.texts_dir, exist_ok=True)
        self._conversion_runner.work_root = os.path.join(working, '.conversion_jobs')

    async def _finish_publication(self):
        publication = self._publication
        if not publication:
            return
        latest_report = {'run': self.crawl_session_id, 'domain': self.domain,
                         'started_at': self.stats.start_time.isoformat(),
                         'finished_at': datetime.now().isoformat(), 'attempt_generation': publication.generation_id,
                         'export_failures': self.report['export_failures']}
        try:
            await self.db.close()
            report_path = os.path.join(self.output_dir, 'crawl_report.json')
            if os.path.isfile(report_path):
                with open(report_path, encoding='utf-8') as report_file:
                    candidate_report = json.load(report_file)
                if candidate_report.get('run') == self.crawl_session_id:
                    latest_report = candidate_report
            if (self._export_ready and self.delivery_status() != 'failed'
                    and self.state != CrawlerState.STOPPED):
                task = asyncio.create_task(asyncio.to_thread(publication.publish, self.delivery_status()))
                # Publiceringens enda commit måste avslutas även om GUI:t avbryter väntan.
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                if publication.mirror_errors:
                    self._export_failed('kompatibilitetskopior', RuntimeError(publication.mirror_errors[0]))
                try:
                    removed = await blocking_complete(publication.prune, self.config['generation_keep'],
                                                     self.config['generation_min_age_hours'])
                    self.report['generation_cleanup'].extend(removed)
                except Exception as error:
                    self.report['generation_cleanup_failures'].append({'reason': str(error)[:160]})
                    self._log(f'⚠ Generationsstädning misslyckades: {error}', LogLevel.WARNING)
            current = publication.current()
            latest_report.update(status=self.delivery_status(), fatal_error=self.fatal_error,
                                 published_generation=current['generation'] if current else None,
                                 publication_committed=publication.published,
                                 generation_cleanup=self.report['generation_cleanup'],
                                 generation_cleanup_count=len(self.report['generation_cleanup']),
                                 generation_cleanup_failures=self.report['generation_cleanup_failures'],
                                 generation_cleanup_failures_count=len(self.report['generation_cleanup_failures']),
                                 export_failures=self.report['export_failures'][:200],
                                 export_failures_count=len(self.report['export_failures']))
            atomic_write_text(os.path.join(self._output_root, 'crawl_report.json'),
                              json.dumps(latest_report, ensure_ascii=False, indent=2))
        except Exception as error:
            self._export_failed('publicering', error)
            if latest_report:
                latest_report.update(status='failed', fatal_error=self.fatal_error,
                                     publication_committed=publication.published,
                                     export_failures=self.report['export_failures'][:200],
                                     export_failures_count=len(self.report['export_failures']))
                atomic_write_text(os.path.join(self._output_root, 'crawl_report.json'),
                                  json.dumps(latest_report, ensure_ascii=False, indent=2))
        finally:
            await blocking_complete(publication.abort)
            self.output_dir = self._output_root
            self.db.db_path = os.path.join(self._output_root, publication.cache_name)
            if self.converter:
                self.converter.texts_dir = os.path.join(self._output_root, 'texter')

    async def crawl(self):
        """Kör hela crawlen. Fångar alla fel så att GUI/CLI alltid får ett slutmeddelande."""
        try:
            self._crawl_loop = asyncio.get_running_loop()
            await self._begin_publication()
            await self._crawl_inner()
        except asyncio.CancelledError:
            self.state = CrawlerState.STOPPED
            raise
        except Exception as e:
            self.fatal_error = f"{type(e).__name__}: {e}"
            self._log(f"💥 Crawlen avbröts av ett oväntat fel: {self.fatal_error}",
                      LogLevel.ERROR)
        finally:
            self.stats.end_time = datetime.now()
            try:
                await self._generate_report()
                await self._finish_publication()
            finally:
                await self._shutdown()
            rate = self.stats.pages_per_second
            self._log(f"Färdig! Total tid: {self.stats.duration} "
                      f"({rate:.1f} sidor/sek, "
                      f"{self.stats.pages_not_modified_304} via 304-cache)")
            summary = self.summary_text()
            self._log(summary)
            self._close_log_handlers()
            if self.msg_queue:
                self.msg_queue.put(("summary", summary))
                self.msg_queue.put(("done", "Klar"))

    async def _shutdown(self):
        await self._close_conversion_queue(cancel=True)
        await self._conversion_runner.close()
        # Bryt kvarvarande nätverksväntan innan Chromium/Playwright avslutas.
        if self._browser_proxy:
            await self._browser_proxy.close()
        try:
            if self.req_session and not self.req_session.closed:
                await self.req_session.close()
        except Exception:
            pass
        try:
            await self.db.close()
        except Exception:
            pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    async def _crawl_inner(self):
        await self.db.connect()
        self._initial_page_files = {
            url for url, _, _, _, filename in await self.db.get_all_records()
            if filename and os.path.isfile(os.path.join(self.output_dir, 'texter', os.path.basename(filename)))
        }
        await self._resolve_private_policy()

        if self.config.get("headless_mode") == "login_then_headless":
            loaded = self._load_cookie_file()
            if not loaded and not await self._interactive_login():
                return
            self.req_session = await self._create_session()
            if await self._verify_login() is False:
                if loaded and self.msg_queue is not None:
                    self._log("⚠ De sparade cookies har gått ut — loggar in på nytt",
                              LogLevel.WARNING)
                    await self.req_session.close()
                    if not await self._interactive_login():
                        return
                    self.req_session = await self._create_session()
                elif loaded:
                    self.fatal_error = ("De sparade cookies har gått ut. Skapa en ny "
                                        "cookie_file genom en inloggad körning i GUI:t.")
                    self._log(f"⛔ {self.fatal_error}", LogLevel.ERROR)
                    return
                else:
                    self._log("⚠ Startsidan ser fortfarande ut som en inloggningssida — "
                              "inloggningen kan ha misslyckats.", LogLevel.WARNING)
        else:
            self.req_session = await self._create_session()

        if self.config.get("respect_robots", True) or self.find_sitemap:
            await self._load_robots_txt()

        await self._enqueue_cached_urls()

        self.stats.start_time = datetime.now()
        self.active_tasks = 0
        self._start_conversion_queue()

        async def bounded_process(url, depth):
            try:
                while self.state == CrawlerState.PAUSED:
                    await asyncio.sleep(0.3)
                if self.state == CrawlerState.STOPPED:
                    return
                async with self.semaphore:
                    while self.state == CrawlerState.PAUSED:
                        await asyncio.sleep(0.3)
                    if self.state == CrawlerState.STOPPED:
                        return
                    await self.process_page(url, depth)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.stats.pages_failed += 1
                self._log(f"💥 Oväntat fel i bounded_process ({url}): {e}",
                          LogLevel.ERROR)
            finally:
                self.active_tasks = max(0, self.active_tasks - 1)

        try:
            async with asyncio.TaskGroup() as tg:
                while self.state != CrawlerState.STOPPED:
                    if self.state == CrawlerState.PAUSED:
                        await asyncio.sleep(0.5)
                        continue

                    # max_pages: räkna både färdiga och pågående sidor, annars
                    # skulle en förfylld kö (sitemap) dispatchas i sin helhet.
                    if (self.max_pages > 0
                            and self.stats.pages_visited + self.active_tasks >= self.max_pages):
                        if self.active_tasks == 0:
                            break
                        await asyncio.sleep(0.05)
                        continue

                    # Backpressure: skapa inte fler tasks än vad som kan köras
                    if self.active_tasks >= self.concurrency * 2:
                        await asyncio.sleep(0.02)
                        continue

                    queue_item = self.url_queue.get_next()
                    if not queue_item:
                        if self.url_queue.size() == 0 and self.active_tasks == 0:
                            self._completed_naturally = True
                            break
                        await asyncio.sleep(0.1)
                        continue

                    self.active_tasks += 1
                    task = tg.create_task(bounded_process(queue_item[1], queue_item[0]))
                    self._url_tasks.add(task)
                    task.add_done_callback(self._url_tasks.discard)
        except asyncio.CancelledError:
            self.state = CrawlerState.STOPPED
            raise
        except Exception as e:
            self.fatal_error = self.fatal_error or f"{type(e).__name__}: {e}"
            self._log(f"💥 Oväntat fel i async crawl: {e}", LogLevel.ERROR)
        finally:
            await self._close_conversion_queue(cancel=self.state == CrawlerState.STOPPED)
            if self.state == CrawlerState.STOPPED:
                self._completed_naturally = False
            await self._finalize_page_duplicates()
            await self._prepare_export()
            self._check_publication_loss()
            await self._generate_index()
            await self._generate_manifest()
            if self.config.get("export_jsonl", True):
                try:
                    n = await blocking_complete(self._write_chunks_jsonl)
                    self._log(f"📦 chunks.jsonl skapad: {n} chunks")
                except Exception as e:
                    self._export_failed('chunks.jsonl', e)
                    self._log(f"⚠ Fel vid skapande av chunks.jsonl: {e}", LogLevel.ERROR)
            else:
                for name in ('chunks.jsonl', 'chunks_quarantine.jsonl'):
                    path = os.path.join(self.output_dir, name)
                    if os.path.isfile(path):
                        os.remove(path)
            self._export_ready = not bool(self.report['export_failures'])


# ─────────────────────────────────────────────────────────────
#  SERVER / CLI LÄGE
# ─────────────────────────────────────────────────────────────
async def _post_webhook(webhook_url: str, text: str):
    """Skickar en webhook-notis (Slack/Teams-format). Fel loggas, kraschar aldrig körningen."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(webhook_url, json={"text": text}) as resp:
                if resp.status >= 400:
                    print(f"⚠ Webhook svarade HTTP {resp.status}")
    except Exception as e:
        print(f"⚠ Kunde inte skicka webhook: {e}")


async def run_cli_mode(config_file: str, webhook_url: Optional[str] = None,
                       output_dir: Optional[str] = None) -> int:
    """Kör alla sajter i `config_file`. Returnerar processens exit-kod (0 = allt gick bra)."""
    print(f"🚀 Startar Webbdammsugare Pro {VERSION} serverläge med filen: {config_file}")
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            sites_config = json.load(f)
    except (OSError, ValueError) as e:
        print(f"⛔ Kunde inte läsa konfigurationsfilen: {e}")
        return 2
    if isinstance(sites_config, dict):
        sites_config = [sites_config]
    if not isinstance(sites_config, list) or not sites_config:
        print("⛔ Konfigurationen måste vara en lista med minst en sajt.")
        return 2

    base_out = os.path.abspath(output_dir or "server_data")
    site_semaphore = asyncio.Semaphore(3)
    failures: List[str] = []
    config_failures: List[str] = []
    valid_sites = []
    used_folders: Set[str] = set()

    for idx, site in enumerate(sites_config, start=1):
        errors, warnings = validate_site_config(site)
        label = site.get("name", f"#{idx}") if isinstance(site, dict) else f"#{idx}"
        for w in warnings:
            print(f"⚠ {label}: {w}")
        if errors:
            for e in errors:
                print(f"⛔ {label}: {e}")
            failures.append(f"{label}: ogiltig konfiguration")
            config_failures.append(f"{label}: ogiltig konfiguration")
            continue
        folder = slugify(site.get("name", "") or urlparse(site["start_url"]).netloc) or f"sajt-{idx}"
        if folder in used_folders:          # två sajter får aldrig dela utmapp/cache
            folder = f"{folder}-{idx}"
        used_folders.add(folder)
        valid_sites.append((folder, site))

    async def run_single_site(folder, site):
        async with site_semaphore:
            config = {
                **site,
                "output_dir": os.path.join(base_out, folder),
                "headless_mode": site.get("headless_mode", "headless"),
                "use_hybrid": site.get("use_hybrid", True),
                "incremental": site.get("incremental", True),
            }
            crawler = AsyncWebCrawler(config)
            await crawler.crawl()
            return site.get("name", folder), crawler

    start_time = time.time()
    results = await asyncio.gather(
        *[run_single_site(folder, site) for folder, site in valid_sites],
        return_exceptions=True)

    summary_lines = []
    for (folder, site), r in zip(valid_sites, results):
        if isinstance(r, Exception):
            summary_lines.append(f" - {site.get('name', folder)}: FEL: {r}")
            failures.append(f"{site.get('name', folder)}: {r}")
        else:
            name, c = r
            summary_lines.append(f" - {name}: {c.summary_text()}")
            if c.delivery_status() != 'success':
                failures.append(f"{name}: {c.fatal_error or c.delivery_status()}")
    summary_lines.extend(f" - {f}" for f in config_failures)
    summary = "\n".join(summary_lines)
    status = "✅ Klart" if not failures else "⚠ Klart med fel"
    print(f"\n{status} på {time.time() - start_time:.1f} sekunder!\n{summary}")

    if webhook_url:
        await _post_webhook(webhook_url, f"{status} – dammsugningen är färdig.\n{summary}")
    return 1 if failures else 0


# ─────────────────────────────────────────────────────────────
#  GRAFISKT GRÄNSSNITT (GUI - CustomTkinter)
# ─────────────────────────────────────────────────────────────
class AppGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.lang = "sv"
        self.texts = {
            "status_login_required": {"sv": "Inloggning krävs", "en": "Login required"},
            "run_success": {"sv": "Lyckad körning", "en": "Successful run"},
            "run_partial": {"sv": "Partiell körning", "en": "Partial run"},
            "run_failed": {"sv": "Misslyckad körning", "en": "Failed run"},
            "window_title": {"sv": f"Webbdammsugare Pro (v{VERSION})", "en": f"Web Crawler Pro (v{VERSION})"},
            "tab_basic": {"sv": "⚙️ Grundinställningar", "en": "⚙️ Basic Settings"},
            "tab_adv": {"sv": "🔧 Avancerat", "en": "🔧 Advanced"},
            "lbl_url": {"sv": "🌐 Startadress:", "en": "🌐 Start URL:"},
            "btn_help": {"sv": "❓ Hjälp", "en": "❓ Help"},
            "lbl_delay": {"sv": "Fördröjning (sek):", "en": "Delay (sec):"},
            "lbl_max_pages": {"sv": "Max sidor (0=Oändligt):", "en": "Max pages (0=Infinite):"},
            "lbl_max_depth": {"sv": "Max djup (0=Oändligt):", "en": "Max depth (0=Infinite):"},
            "lbl_format": {"sv": "Filformat:", "en": "File Format:"},
            "lbl_concurrency": {"sv": "Samtidighet:", "en": "Concurrency:"},
            "cb_docs": {"sv": "Ladda ner dokument (PDF m.m.)", "en": "Download documents (PDF etc.)"},
            "cb_convert_docs": {"sv": "📄 Konvertera dokument till Markdown", "en": "📄 Convert documents to Markdown"},
            "lbl_mode": {"sv": "Körläge:", "en": "Run Mode:"},
            "lbl_folder": {"sv": "Spara Mapp:", "en": "Save Folder:"},
            "btn_folder": {"sv": "Välj Mapp...", "en": "Browse..."},
            "cb_hybrid": {"sv": "⚡ Hybrid-motor (Requests + Playwright)", "en": "⚡ Hybrid Engine (Requests + Playwright)"},
            "cb_traf": {"sv": "🧠 Använd Trafilatura för text", "en": "🧠 Use Trafilatura for text extraction"},
            "cb_sitemap": {"sv": "Läs Sitemap.xml", "en": "Parse Sitemap.xml"},
            "cb_robots": {"sv": "Respektera robots.txt", "en": "Respect robots.txt"},
            "cb_strict": {"sv": "Strikt Domän", "en": "Strict Domain"},
            "lbl_exclude": {"sv": "Uteslut ord i URL:", "en": "Exclude words in URL:"},
            "lbl_require": {"sv": "Kräv ord i URL (något av):", "en": "Require words in URL (any of):"},
            "cb_rm_email": {"sv": "Radera E-post", "en": "Remove Email"},
            "cb_rm_phone": {"sv": "Radera Telefonnummer", "en": "Remove Phone Numbers"},
            "cb_rm_pnr": {"sv": "Radera Personnummer", "en": "Remove Swedish SSN"},
            "cb_rm_ip": {"sv": "Radera IP-adresser", "en": "Remove IP Addresses"},
            "cb_full": {"sv": "Full omcrawl (ignorera cache)", "en": "Full re-crawl (ignore cache)"},
            "lbl_lang_filter": {"sv": "Bara språk (t.ex. sv, en):", "en": "Only languages (e.g. sv, en):"},
            "btn_open": {"sv": "📂 Öppna mapp", "en": "📂 Open folder"},
            "err_title": {"sv": "Ogiltigt värde", "en": "Invalid value"},
            "err_url": {"sv": "Ange en giltig startadress, t.ex. https://www.kommunen.se",
                        "en": "Enter a valid start URL, e.g. https://www.example.com"},
            "err_num": {"sv": ("Kontrollera att Fördröjning, Max sidor, Max djup och Samtidighet "
                               "är giltiga tal (använd punkt som decimalavgränsare)."),
                        "en": ("Please check that Delay, Max pages, Max depth and Concurrency "
                               "are valid numbers (use dot as decimal separator).")},
            "err_range": {"sv": ("Fördröjning måste vara minst 0.1 s, Samtidighet mellan 1 och 50, "
                                 "och Max sidor/djup får inte vara negativa."),
                          "en": ("Delay must be at least 0.1 s, Concurrency between 1 and 50, "
                                 "and Max pages/depth cannot be negative.")},
            "err_start": {"sv": "Kunde inte starta crawlen:", "en": "Could not start the crawl:"},
            "login_msg": {"sv": ("Logga in i webbläsarfönstret som öppnats. Kontrollera att du "
                                 "ser intranätets startsida och klicka sedan OK här.\n\n"
                                 "Avbryt stoppar crawlen."),
                          "en": ("Log in in the browser window that opened. Make sure you can see "
                                 "the intranet start page, then click OK here.\n\n"
                                 "Cancel stops the crawl.")},
            "tips": {
                "hybrid": {"sv": "Hämtar sidor snabbt med vanlig HTTP och använder webbläsaren (Playwright) bara när sidan kräver JavaScript.",
                           "en": "Fetches pages with plain HTTP and only uses the browser (Playwright) when a page needs JavaScript."},
                "traf": {"sv": "Trafilatura plockar ut själva brödtexten och skiljer den från meny och sidfot. Rekommenderas.",
                         "en": "Trafilatura extracts the main text and separates it from menus and footers. Recommended."},
                "sitemap": {"sv": "Läser sitemap.xml för att hitta sidor som inte är länkade från andra sidor.",
                            "en": "Reads sitemap.xml to find pages that are not linked from other pages."},
                "robots": {"sv": "Följer webbplatsens robots.txt och dess Crawl-delay. Stäng bara av om du äger sajten.",
                           "en": "Obeys the site's robots.txt and Crawl-delay. Only disable if you own the site."},
                "strict": {"sv": "Stannar på exakt den angivna domänen. Länkar till andra domäner och underdomäner följs inte.",
                           "en": "Stays on exactly the given domain. Links to other domains and subdomains are not followed."},
                "full": {"sv": "Hämtar alla sidor på nytt utan att använda den sparade cachen. Annars hämtas bara nytt/ändrat.",
                         "en": "Re-fetches every page without using the saved cache. Otherwise only new/changed pages are fetched."},
                "convert": {"sv": "Extraherar text ur PDF, Word, Excel och PowerPoint och sparar som .md med käll-URL.",
                            "en": "Extracts text from PDF, Word, Excel and PowerPoint and saves it as .md with the source URL."},
                "exclude": {"sv": "Kommaseparerade ord. URL:er som innehåller något av dem hoppas över.",
                            "en": "Comma-separated words. URLs containing any of them are skipped."},
                "lang": {"sv": "Kommaseparerade språkkoder. Sidor vars html-språk är ett annat hoppas över. Tomt = alla språk.",
                         "en": "Comma-separated language codes. Pages whose html language differs are skipped. Empty = all languages."},
                "require": {"sv": "Kommaseparerade ord. Bara URL:er som innehåller minst ett av dem besöks.",
                            "en": "Comma-separated words. Only URLs containing at least one of them are visited."},
            },
            "btn_start": {"sv": "▶ Starta", "en": "▶ Start"},
            "btn_pause": {"sv": "⏸ Pausa", "en": "⏸ Pause"},
            "lbl_template": {"sv": "📋 Mall:", "en": "📋 Template:"},
            "template_none": {"sv": "— Ingen mall —", "en": "— No template —"},
            "template_loaded": {"sv": "✓ Mall laddad: {}", "en": "✓ Template loaded: {}"},
            "template_not_found": {"sv": "Ingen sites.json hittades", "en": "No sites.json found"},
            "btn_resume": {"sv": "▶ Fortsätt", "en": "▶ Resume"},
            "btn_stop": {"sv": "■ Stoppa", "en": "■ Stop"},
            "col_status": {"sv": "Status", "en": "Status"},
            "col_title": {"sv": "Sido-titel", "en": "Page Title"},
            "status_wait": {"sv": "Väntar på start...", "en": "Waiting to start..."},
            "stats_fmt": {
                "sv": "Besökta: {} | Sidor: {} | Dokument: {} | Cachade: {} | I Kö: {} | Fel: {} | Tid kvar: {}",
                "en": "Visited: {} | Pages: {} | Docs: {} | Cached: {} | Queued: {} | Errors: {} | ETA: {}"
            },
            "help_title": {"sv": "❓ Hjälp & Instruktioner", "en": "❓ Help & Instructions"},
            "run_modes": {
                "headless": {"sv": "Standard (ingen inloggning)", "en": "Standard (no login)"},
                "login_then_headless": {"sv": "Logga in först, sen automatiskt", "en": "Log in first, then automatic"},
                "visible": {"sv": "Synlig webbläsare (felsökning)", "en": "Visible browser (debugging)"}
            },
            "help_content": {
                "sv": ("⚙️ GRUNDINSTÄLLNINGAR\n-------------------------\n"
                       "* Startadress: URL där programmet börjar leta.\n"
                       "* Fördröjning: Tid mellan sidbesök (per domän).\n"
                       "* Max sidor/djup: 0 betyder oändligt.\n"
                       "* Samtidighet: antal parallella sidor (default 10).\n"
                       "* Körläge:\n"
                       "  - Standard: för sajter utan inloggning.\n"
                       "  - Logga in först: ett fönster öppnas där du loggar in, sedan körs allt automatiskt.\n"
                       "  - Synlig webbläsare: för felsökning av JavaScript-sidor.\n"
                       "* Filformat:\n"
                       "  - .json: Strukturerad data anpassad för Vektordatabaser och AI.\n"
                       "  - .md: Markdown, bra för generella LLM-läsningar.\n"
                       "  - Ingen text: Skrapar enbart dokument (om ikryssat).\n\n"
                       "📋 MALLAR\n-------------------------\n"
                       "Lägg en sites.json i samma mapp som programmet.\n"
                       "Välj en mall i dropdown-menyn så fylls alla inställningar i automatiskt.\n\n"
                       "🔧 AVANCERAT\n-------------------------\n"
                       "* Hybrid-motor: Rekommenderas för modern webb.\n"
                       "* URL-Filter: Filtrerar på ord i URL:en, inte i sidans text.\n"
                       "* PII-Tvätt: Raderar personuppgifter automatiskt innan sparning.\n\n"
                       "♻️ INKREMENTELL CRAWL\n-------------------------\n"
                       "Programmet kommer ihåg ETag/Last-Modified och länkarna på varje sida.\n"
                       "Vid omkörning hämtas bara nytt/ändrat; borttagna sidor (404/410) raderas.\n"
                       "Kryssa i 'Full omcrawl' för att ignorera cachen.\n"
                       "Efter varje körning skrivs changes.jsonl (nytt/ändrat/borttaget) och\n"
                       "crawl_report.json (luckor, fel och varningar) i utmappen.\n\n"
                       "📋 DOKUMENT-MANIFEST\n-------------------------\n"
                       "När du laddar ner dokument (PDF m.m.) skapas också en manifest.json\n"
                       "i utmappen som listar varje dokument tillsammans med vilken intranät-\n"
                       "sida som hade länken till det.\n\n"
                       "📄 DOKUMENT → MARKDOWN\n-------------------------\n"
                       "Kryssa i 'Konvertera dokument till Markdown' så extraheras texten\n"
                       "ur PDF, Word, Excel och PowerPoint och sparas som .md-filer med\n"
                       "intranät-URL:en i toppen. Perfekt för RAG-system som Svea där\n"
                       "AI:n behöver se källan direkt i texten för att citera rätt.\n"
                       "Kräver: pip install PyMuPDF python-docx openpyxl python-pptx\n\n"
                       "💻 SERVER-LÄGE\n-------------------------\n"
                       "Körs via CMD för automatisering:\n"
                       "python ultimate-web-crawler.py --config sites.json [--output mapp]\n\n"
                       "💡 TIPS: Dubbelklicka på en rad i tabellen för att öppna länken!"),
                "en": ("⚙️ BASIC SETTINGS\n-------------------------\n"
                       "* Start URL: Where the crawler begins.\n"
                       "* Delay: Seconds to wait between requests (per domain).\n"
                       "* Concurrency: number of parallel pages (default 10).\n"
                       "* Run Mode:\n"
                       "  - Standard: for sites without login.\n"
                       "  - Log in first: a window opens for you to log in, then everything runs automatically.\n"
                       "  - Visible browser: for debugging JavaScript pages.\n"
                       "* File Format:\n"
                       "  - .json: Structured output for Vector Databases and AI.\n"
                       "  - .md: Markdown.\n"
                       "  - No text: Only downloads documents (if checked).\n\n"
                       "📋 TEMPLATES\n-------------------------\n"
                       "Place a sites.json in the same folder as the program.\n\n"
                       "🔧 ADVANCED\n-------------------------\n"
                       "* Hybrid Engine: Recommended for modern web.\n"
                       "* URL Filters: Filters on URL substrings.\n"
                       "* PII Wash: Removes personal data before saving.\n\n"
                       "♻️ INCREMENTAL CRAWL\n-------------------------\n"
                       "ETag, Last-Modified and links are cached per URL.\n"
                       "On re-runs, the server responds '304 Not Modified' for unchanged pages\n"
                       "— often 10-50x faster than the first crawl.\n\n"
                       "📋 DOCUMENT MANIFEST\n-------------------------\n"
                       "When documents (PDFs etc.) are downloaded, a manifest.json is also\n"
                       "created in the output folder.\n\n"
                       "📄 DOCUMENTS → MARKDOWN\n-------------------------\n"
                       "Check 'Convert documents to Markdown' to extract text from PDF,\n"
                       "Word, Excel and PowerPoint files and save as .md with the intranet\n"
                       "URL at the top. Perfect for RAG systems where the AI needs the\n"
                       "source URL directly in the text to cite correctly.\n"
                       "Requires: pip install PyMuPDF python-docx openpyxl python-pptx\n\n"
                       "💻 SERVER MODE\n-------------------------\n"
                       "python ultimate-web-crawler.py --config sites.json [--output folder]\n\n"
                       "💡 TIP: Double-click a row to open the URL!")
            }
        }

        self.root.title(self.texts["window_title"][self.lang])
        # Anpassa fönstret efter skärmen (bärbara med 1366×768 eller hög skalning)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(1000, max(760, screen_w - 40))
        height = min(880, max(560, screen_h - 100))
        self.root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 3)}")
        self.root.minsize(760, 560)
        self.msg_queue = queue.Queue()
        self.crawler_instance: Optional[AsyncWebCrawler] = None
        self.crawl_thread: Optional[threading.Thread] = None
        self._tooltip_win = None
        self._update_treeview_style("Light")
        self._build_ui()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self.process_queue)

    # ─── Sparade inställningar ──────────────────────────────
    SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".ultimate_web_crawler_settings.json")

    def _collect_settings(self) -> Dict:
        return {
            "lang": self.lang,
            "theme": "Dark" if self.theme_switch.get() == 1 else "Light",
            "url": self.url_entry.get().strip(),
            "delay": self.delay_entry.get().strip(),
            "max_pages": self.max_pages_entry.get().strip(),
            "max_depth": self.max_depth_entry.get().strip(),
            "concurrency": self.concurrency_entry.get().strip(),
            "format": self.format_var.get(),
            "docs": self.docs_var.get(),
            "convert_docs": self.convert_docs_var.get(),
            "mode": {v[self.lang]: k for k, v in self.texts["run_modes"].items()}.get(
                self.headless_var.get(), "headless"),
            "dir": self.dir_var.get(),
            "hybrid": self.hybrid_var.get(),
            "traf": self.traf_var.get(),
            "sitemap": self.sitemap_var.get(),
            "robots": self.robots_var.get(),
            "strict": self.strict_var.get(),
            "full": self.full_var.get(),
            "exclude": self.exclude_entry.get().strip(),
            "require": self.require_entry.get().strip(),
            "languages": self.lang_filter_entry.get().strip(),
            "rm_email": self.rm_email_var.get(),
            "rm_phone": self.rm_phone_var.get(),
            "rm_pnr": self.rm_pnr_var.get(),
            "rm_ip": self.rm_ip_var.get(),
        }

    def _save_settings(self):
        try:
            with open(self.SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._collect_settings(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass     # sparade inställningar är en bekvämlighet — får aldrig stoppa något

    def _load_settings(self):
        try:
            with open(self.SETTINGS_PATH, 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:
            return
        if not isinstance(st, dict):
            return

        def set_entry(entry, key):
            if key in st and isinstance(st[key], (str, int, float)):
                entry.delete(0, tk.END)
                entry.insert(0, str(st[key]))

        set_entry(self.url_entry, "url")
        set_entry(self.delay_entry, "delay")
        set_entry(self.max_pages_entry, "max_pages")
        set_entry(self.max_depth_entry, "max_depth")
        set_entry(self.concurrency_entry, "concurrency")
        set_entry(self.exclude_entry, "exclude")
        set_entry(self.require_entry, "require")
        set_entry(self.lang_filter_entry, "languages")
        for key, var in (("docs", self.docs_var), ("convert_docs", self.convert_docs_var),
                         ("hybrid", self.hybrid_var), ("traf", self.traf_var),
                         ("sitemap", self.sitemap_var), ("robots", self.robots_var),
                         ("strict", self.strict_var), ("full", self.full_var),
                         ("rm_email", self.rm_email_var), ("rm_phone", self.rm_phone_var),
                         ("rm_pnr", self.rm_pnr_var), ("rm_ip", self.rm_ip_var)):
            if isinstance(st.get(key), bool):
                var.set(st[key])
        if not HAS_TRAFILATURA:
            self.traf_var.set(False)
        if st.get("format") in (".json", ".md", ".txt"):
            self.format_var.set(st["format"])
        if st.get("mode") in self.texts["run_modes"]:
            self.headless_var.set(self.texts["run_modes"][st["mode"]][self.lang])
        if isinstance(st.get("dir"), str) and st["dir"]:
            self.dir_var.set(st["dir"])
        if st.get("theme") == "Dark":
            self.theme_switch.select()
            self.change_appearance_mode_event()
        if st.get("lang") == "en":
            self.lang_var.set("🇬🇧 EN")
            self.change_language_event("🇬🇧 EN")

    # ─── Tooltips ───────────────────────────────────────────
    def _add_tooltip(self, widget, key: str):
        def show(_event=None):
            self._hide_tooltip()
            text = self.texts["tips"][key][self.lang]
            win = tk.Toplevel(self.root)
            win.wm_overrideredirect(True)
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(win, text=text, justify=tk.LEFT, background="#ffffe0",
                     foreground="black", relief=tk.SOLID, borderwidth=1,
                     wraplength=380, padx=6, pady=4).pack()
            self._tooltip_win = win
        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", lambda e: self._hide_tooltip(), add="+")

    def _hide_tooltip(self):
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _on_closing(self):
        self._save_settings()
        if self.crawler_instance:
            self.crawler_instance.stop()
            # Ge crawlern en chans att spola databasen och skriva rapporterna
            if self.crawl_thread and self.crawl_thread.is_alive():
                self.crawl_thread.join(timeout=5)
        self.root.destroy()

    def open_output_folder(self):
        folder = self.dir_var.get()
        if not os.path.isdir(folder):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)             # noqa: S606 — användarens egen mapp
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self._log_to_gui(f"Kunde inte öppna mappen: {e}")

    def change_appearance_mode_event(self):
        if self.theme_switch.get() == 1:
            ctk.set_appearance_mode("Dark")
            self.theme_switch.configure(text="🌙")
            self._update_treeview_style("Dark")
        else:
            ctk.set_appearance_mode("Light")
            self.theme_switch.configure(text="☀️")
            self._update_treeview_style("Light")

    def change_language_event(self, choice):
        inverted_map = {v[self.lang]: k for k, v in self.texts["run_modes"].items()}
        internal_mode = inverted_map.get(self.headless_var.get(), "headless")

        new_lang = "sv" if "SV" in choice else "en"
        if new_lang == self.lang:
            return

        old_basic = self.texts["tab_basic"][self.lang]
        new_basic = self.texts["tab_basic"][new_lang]
        old_adv = self.texts["tab_adv"][self.lang]
        new_adv = self.texts["tab_adv"][new_lang]
        self.tabview.rename(old_basic, new_basic)
        self.tabview.rename(old_adv, new_adv)

        self.lang = new_lang
        self.root.title(self.texts["window_title"][self.lang])

        self.lbl_url.configure(text=self.texts["lbl_url"][self.lang])
        self.help_btn.configure(text=self.texts["btn_help"][self.lang])
        self.lbl_delay.configure(text=self.texts["lbl_delay"][self.lang])
        self.lbl_max_pages.configure(text=self.texts["lbl_max_pages"][self.lang])
        self.lbl_max_depth.configure(text=self.texts["lbl_max_depth"][self.lang])
        self.lbl_format.configure(text=self.texts["lbl_format"][self.lang])
        self.lbl_concurrency.configure(text=self.texts["lbl_concurrency"][self.lang])
        self.cb_docs.configure(text=self.texts["cb_docs"][self.lang])
        self.cb_convert_docs.configure(text=self.texts["cb_convert_docs"][self.lang])
        self.lbl_mode.configure(text=self.texts["lbl_mode"][self.lang])
        self.lbl_folder.configure(text=self.texts["lbl_folder"][self.lang])
        self.btn_folder.configure(text=self.texts["btn_folder"][self.lang])

        self.cb_hybrid.configure(text=self.texts["cb_hybrid"][self.lang])
        self.cb_traf.configure(text=self.texts["cb_traf"][self.lang])
        self.cb_sitemap.configure(text=self.texts["cb_sitemap"][self.lang])
        self.cb_robots.configure(text=self.texts["cb_robots"][self.lang])
        self.cb_strict.configure(text=self.texts["cb_strict"][self.lang])
        self.lbl_exclude.configure(text=self.texts["lbl_exclude"][self.lang])
        self.lbl_require.configure(text=self.texts["lbl_require"][self.lang])

        self.cb_rm_email.configure(text=self.texts["cb_rm_email"][self.lang])
        self.cb_rm_phone.configure(text=self.texts["cb_rm_phone"][self.lang])
        self.cb_rm_pnr.configure(text=self.texts["cb_rm_pnr"][self.lang])
        self.cb_rm_ip.configure(text=self.texts["cb_rm_ip"][self.lang])
        self.cb_full.configure(text=self.texts["cb_full"][self.lang])
        self.lbl_lang_filter.configure(text=self.texts["lbl_lang_filter"][self.lang])
        self.open_btn.configure(text=self.texts["btn_open"][self.lang])
        if getattr(self, "template_menu", None) is not None:
            old_none = self.texts["template_none"]["en" if new_lang == "sv" else "sv"]
            new_none = self.texts["template_none"][new_lang]
            self.lbl_template.configure(text=self.texts["lbl_template"][new_lang])
            self.template_menu.configure(values=[new_none] + list(self.templates.keys()))
            if self.template_var.get() == old_none:
                self.template_var.set(new_none)

        self.start_btn.configure(text=self.texts["btn_start"][self.lang])
        if self.crawler_instance and self.crawler_instance.state == CrawlerState.PAUSED:
            self.pause_btn.configure(text=self.texts["btn_resume"][self.lang])
        else:
            self.pause_btn.configure(text=self.texts["btn_pause"][self.lang])
        self.stop_btn.configure(text=self.texts["btn_stop"][self.lang])

        self.tree.heading('Status', text=self.texts["col_status"][self.lang])
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang])
        if not self.crawler_instance or self.crawler_instance.state in (
            CrawlerState.IDLE, CrawlerState.STOPPED
        ):
            self.stats_label.configure(text=self.texts["status_wait"][self.lang])

        self.headless_menu.configure(values=[v[self.lang] for v in self.texts["run_modes"].values()])
        self.headless_var.set(self.texts["run_modes"][internal_mode][self.lang])

        format_vals = [".json", ".md", ".txt", "Ingen text" if self.lang == "sv" else "No text"]
        self.format_menu.configure(values=format_vals)
        if self.format_var.get() not in format_vals:
            self.format_var.set(format_vals[-1])

    def _update_treeview_style(self, mode):
        style = ttk.Style()
        style.theme_use("default")
        if mode == "Dark":
            style.configure("Treeview", background="#2b2b2b", foreground="white",
                            fieldbackground="#2b2b2b", borderwidth=0, rowheight=25)
            style.configure("Treeview.Heading", background="#565b5e", foreground="white",
                            font=('Arial', 10, 'bold'), relief="flat")
            style.map('Treeview', background=[('selected', '#1f538d')])
            style.map("Treeview.Heading", background=[('active', '#343638')])
        else:
            style.configure("Treeview", background="#ffffff", foreground="black",
                            fieldbackground="#ffffff", borderwidth=0, rowheight=25)
            style.configure("Treeview.Heading", background="#e5e5e5", foreground="black",
                            font=('Arial', 10, 'bold'), relief="flat")
            style.map('Treeview', background=[('selected', '#3a7ebf')])
            style.map("Treeview.Heading", background=[('active', '#d1d1d1')])

    def open_help_window(self):
        help_win = ctk.CTkToplevel(self.root)
        help_win.title(self.texts["help_title"][self.lang])
        help_win.geometry("600x600")
        help_text = ctk.CTkTextbox(help_win, wrap=tk.WORD,
                                   font=ctk.CTkFont(family="Arial", size=13))
        help_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        help_text.insert(tk.END, self.texts["help_content"][self.lang])
        help_text.configure(state="disabled")

    def choose_directory(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.configure(state="normal")
            self.dir_var.set(d)
            self.dir_entry.configure(state="readonly")

    def _on_convert_docs_toggled(self):
        """Auto-aktivera dokumentnedladdning om konvertering kryssas i."""
        if self.convert_docs_var.get():
            self.docs_var.set(True)

    def _find_templates(self) -> Dict:
        if getattr(sys, 'frozen', False):
            script_dir = os.path.dirname(sys.executable)
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(script_dir, "sites.json")
        if os.path.isfile(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    sites = json.load(f)
                if isinstance(sites, list) and sites:
                    return {site.get("name", f"Sajt {i+1}"): site
                            for i, site in enumerate(sites)}
            except Exception:
                pass
        return {}

    def _apply_template(self, choice):
        none_label = self.texts["template_none"][self.lang]
        if choice == none_label:
            self._template_conversion_options = {}
            return

        site = self.templates.get(choice)
        if not site:
            return
        self._template_conversion_options = {key: site[key] for key in (
            'conversion_workers', 'conversion_queue_size', 'conversion_timeout',
            'generation_keep', 'generation_min_age_hours') if key in site}

        if site.get("start_url"):
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, site["start_url"])
        if "delay" in site:
            self.delay_entry.delete(0, tk.END)
            self.delay_entry.insert(0, str(site["delay"]))
        if "max_pages" in site:
            self.max_pages_entry.delete(0, tk.END)
            self.max_pages_entry.insert(0, str(site["max_pages"]))
        if "max_depth" in site:
            self.max_depth_entry.delete(0, tk.END)
            self.max_depth_entry.insert(0, str(site["max_depth"]))
        if "concurrency" in site:
            self.concurrency_entry.delete(0, tk.END)
            self.concurrency_entry.insert(0, str(site["concurrency"]))
        if "save_format" in site:
            self.format_var.set(site["save_format"])
        if "download_docs" in site:
            self.docs_var.set(site["download_docs"])
        if "headless_mode" in site:
            mode_key = site["headless_mode"]
            if mode_key in self.texts["run_modes"]:
                self.headless_var.set(self.texts["run_modes"][mode_key][self.lang])

        if "use_hybrid" in site:
            self.hybrid_var.set(site["use_hybrid"])
        if "use_trafilatura" in site:
            self.traf_var.set(site["use_trafilatura"])
        if "find_sitemap" in site:
            self.sitemap_var.set(site["find_sitemap"])
        if "respect_robots" in site:
            self.robots_var.set(site["respect_robots"])
        if "strict_domain" in site:
            self.strict_var.set(site["strict_domain"])

        if "exclude_keywords" in site:
            self.exclude_entry.delete(0, tk.END)
            kws = site["exclude_keywords"]
            self.exclude_entry.insert(0, ", ".join(kws) if isinstance(kws, list) else kws)
        if "require_keywords" in site:
            self.require_entry.delete(0, tk.END)
            kws = site["require_keywords"]
            self.require_entry.insert(0, ", ".join(kws) if isinstance(kws, list) else kws)

        if "languages" in site:
            langs = site["languages"]
            self.lang_filter_entry.delete(0, tk.END)
            self.lang_filter_entry.insert(0, ", ".join(langs) if isinstance(langs, list) else str(langs))
        if "incremental" in site:
            self.full_var.set(not site["incremental"])
        if "remove_email" in site: self.rm_email_var.set(site["remove_email"])
        if "remove_phone" in site: self.rm_phone_var.set(site["remove_phone"])
        if "remove_pnr" in site: self.rm_pnr_var.set(site["remove_pnr"])
        if "remove_ip" in site: self.rm_ip_var.set(site["remove_ip"])
        if "convert_docs_to_md" in site:
            self.convert_docs_var.set(site["convert_docs_to_md"])
            if site["convert_docs_to_md"]:
                self.docs_var.set(True)

        if site.get("name"):
            base_dir = os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output")
            safe_folder_name = slugify(site["name"])
            self.dir_var.set(os.path.join(base_dir, safe_folder_name))

        self._log_to_gui(self.texts["template_loaded"][self.lang].format(choice))

    def _log_to_gui(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.log_area.insert(tk.END, f"[{t}] {msg}\n")
        self.log_area.see(tk.END)

    def _build_ui(self):
        main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        # Top URL bar
        url_frame = ctk.CTkFrame(main_frame)
        url_frame.pack(fill=tk.X, pady=(0, 10))
        self.lbl_url = ctk.CTkLabel(url_frame, text=self.texts["lbl_url"][self.lang],
                                    font=ctk.CTkFont(weight="bold"))
        self.lbl_url.pack(side=tk.LEFT, padx=(15, 10), pady=15)
        self.url_entry = ctk.CTkEntry(url_frame, width=250, placeholder_text="https://...")
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 15), pady=15)
        self.url_entry.insert(0, "https://")

        self.lang_var = ctk.StringVar(value="🇸🇪 SV")
        self.lang_switch = ctk.CTkSegmentedButton(
            url_frame, values=["🇸🇪 SV", "🇬🇧 EN"],
            variable=self.lang_var, command=self.change_language_event
        )
        self.lang_switch.pack(side=tk.RIGHT, padx=15, pady=15)
        self.theme_switch = ctk.CTkSwitch(url_frame, text="☀️", width=40,
                                          command=self.change_appearance_mode_event)
        self.theme_switch.pack(side=tk.RIGHT, padx=(0, 15), pady=15)
        self.theme_switch.deselect()
        self.help_btn = ctk.CTkButton(
            url_frame, text=self.texts["btn_help"][self.lang], width=80,
            fg_color=("#d9d9d9", "#4a4a4a"),
            text_color=("black", "white"),
            hover_color=("#c9c9c9", "#5a5a5a"),
            command=self.open_help_window
        )
        self.help_btn.pack(side=tk.RIGHT, padx=(0, 15), pady=15)

        # Templates
        self.templates = self._find_templates()
        if self.templates:
            tpl_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
            tpl_frame.pack(fill=tk.X, pady=(0, 5))
            self.lbl_template = ctk.CTkLabel(
                tpl_frame, text=self.texts["lbl_template"][self.lang],
                font=ctk.CTkFont(weight="bold"))
            self.lbl_template.pack(side=tk.LEFT, padx=(15, 10))
            none_label = self.texts["template_none"][self.lang]
            template_names = [none_label] + list(self.templates.keys())
            self.template_var = ctk.StringVar(value=none_label)
            self.template_menu = ctk.CTkOptionMenu(
                tpl_frame, variable=self.template_var, values=template_names,
                width=300, command=self._apply_template)
            self.template_menu.pack(side=tk.LEFT, padx=(0, 15))

        # Tabs
        self.tabview = ctk.CTkTabview(main_frame, height=260)
        self.tabview.pack(fill=tk.X, pady=(0, 10))
        tab_basic = self.tabview.add(self.texts["tab_basic"][self.lang])
        tab_adv = self.tabview.add(self.texts["tab_adv"][self.lang])

        # Tab 1: Basic
        tab_basic.grid_columnconfigure(1, weight=1)
        tab_basic.grid_columnconfigure(3, weight=1)

        self.lbl_delay = ctk.CTkLabel(tab_basic, text=self.texts["lbl_delay"][self.lang])
        self.lbl_delay.grid(row=0, column=0, padx=(10, 5), pady=8, sticky="e")
        self.delay_entry = ctk.CTkEntry(tab_basic, width=80)
        self.delay_entry.insert(0, "0.5")
        self.delay_entry.grid(row=0, column=1, padx=(0, 20), pady=8, sticky="w")

        self.lbl_max_pages = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_pages"][self.lang])
        self.lbl_max_pages.grid(row=0, column=2, padx=(10, 5), pady=8, sticky="e")
        self.max_pages_entry = ctk.CTkEntry(tab_basic, width=80)
        self.max_pages_entry.insert(0, "0")
        self.max_pages_entry.grid(row=0, column=3, padx=(0, 10), pady=8, sticky="w")

        self.lbl_max_depth = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_depth"][self.lang])
        self.lbl_max_depth.grid(row=1, column=0, padx=(10, 5), pady=8, sticky="e")
        self.max_depth_entry = ctk.CTkEntry(tab_basic, width=80)
        self.max_depth_entry.insert(0, "0")
        self.max_depth_entry.grid(row=1, column=1, padx=(0, 20), pady=8, sticky="w")

        self.lbl_format = ctk.CTkLabel(tab_basic, text=self.texts["lbl_format"][self.lang])
        self.lbl_format.grid(row=1, column=2, padx=(10, 5), pady=8, sticky="e")
        # Default: .md (LLM-vänligt, fungerar bäst med RAG-pipelines som
        # chunkar i stycken). .json finns kvar för dem som vill ha strukturerade
        # fält per sida.
        self.format_var = ctk.StringVar(value=".md")
        self.format_menu = ctk.CTkOptionMenu(
            tab_basic, variable=self.format_var,
            values=[".json", ".md", ".txt", "Ingen text"], width=100)
        self.format_menu.grid(row=1, column=3, padx=(0, 10), pady=8, sticky="w")

        self.lbl_concurrency = ctk.CTkLabel(tab_basic, text=self.texts["lbl_concurrency"][self.lang])
        self.lbl_concurrency.grid(row=2, column=0, padx=(10, 5), pady=8, sticky="e")
        self.concurrency_entry = ctk.CTkEntry(tab_basic, width=80)
        self.concurrency_entry.insert(0, "10")
        self.concurrency_entry.grid(row=2, column=1, padx=(0, 20), pady=8, sticky="w")

        # Default: ladda ner och konvertera dokument — det är den vanligaste
        # användningen för RAG-system. Användaren kan stänga av om de bara
        # vill ha sidor.
        self.docs_var = ctk.BooleanVar(value=True)
        self.cb_docs = ctk.CTkCheckBox(tab_basic, text=self.texts["cb_docs"][self.lang],
                                       variable=self.docs_var)
        self.cb_docs.grid(row=2, column=2, padx=10, pady=8, sticky="w")

        self.convert_docs_var = ctk.BooleanVar(value=True)
        self.cb_convert_docs = ctk.CTkCheckBox(
            tab_basic, text=self.texts["cb_convert_docs"][self.lang],
            variable=self.convert_docs_var,
            command=self._on_convert_docs_toggled)
        self.cb_convert_docs.grid(row=2, column=3, padx=10, pady=8, sticky="w")

        self.lbl_mode = ctk.CTkLabel(tab_basic, text=self.texts["lbl_mode"][self.lang])
        self.lbl_mode.grid(row=3, column=0, padx=(10, 5), pady=8, sticky="e")
        self.headless_var = ctk.StringVar(value=self.texts["run_modes"]["headless"][self.lang])
        self.headless_menu = ctk.CTkOptionMenu(
            tab_basic, variable=self.headless_var,
            values=[v[self.lang] for v in self.texts["run_modes"].values()], width=180)
        self.headless_menu.grid(row=3, column=1, columnspan=3, padx=(0, 10), pady=8, sticky="w")

        self.lbl_folder = ctk.CTkLabel(tab_basic, text=self.texts["lbl_folder"][self.lang])
        self.lbl_folder.grid(row=4, column=0, padx=(10, 5), pady=8, sticky="e")
        self.dir_var = ctk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output"))
        self.dir_entry = ctk.CTkEntry(tab_basic, textvariable=self.dir_var, state="readonly")
        self.dir_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=8)
        self.btn_folder = ctk.CTkButton(
            tab_basic, text=self.texts["btn_folder"][self.lang],
            width=100, command=self.choose_directory)
        self.btn_folder.grid(row=4, column=3, padx=(0, 10), pady=8, sticky="w")

        # Tab 2: Advanced
        tab_adv.grid_columnconfigure(1, weight=1)

        self.hybrid_var = ctk.BooleanVar(value=True)
        self.cb_hybrid = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_hybrid"][self.lang],
                                         variable=self.hybrid_var)
        self.cb_hybrid.grid(row=0, column=0, padx=10, pady=5, sticky="w")

        self.traf_var = ctk.BooleanVar(value=HAS_TRAFILATURA)
        self.cb_traf = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_traf"][self.lang],
                                       variable=self.traf_var)
        self.cb_traf.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        if not HAS_TRAFILATURA:
            self.cb_traf.configure(state="disabled")

        self.sitemap_var = ctk.BooleanVar(value=True)
        self.cb_sitemap = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_sitemap"][self.lang],
                                          variable=self.sitemap_var)
        self.cb_sitemap.grid(row=1, column=0, padx=10, pady=5, sticky="w")

        self.robots_var = ctk.BooleanVar(value=True)
        self.cb_robots = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_robots"][self.lang],
                                         variable=self.robots_var)
        self.cb_robots.grid(row=1, column=1, padx=10, pady=5, sticky="w")

        self.strict_var = ctk.BooleanVar(value=True)
        self.cb_strict = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_strict"][self.lang],
                                         variable=self.strict_var)
        self.cb_strict.grid(row=1, column=2, padx=10, pady=5, sticky="w")

        self.lbl_exclude = ctk.CTkLabel(tab_adv, text=self.texts["lbl_exclude"][self.lang])
        self.lbl_exclude.grid(row=2, column=0, padx=(10, 5), pady=5, sticky="e")
        self.exclude_entry = ctk.CTkEntry(tab_adv, placeholder_text="images, login, kalender")
        self.exclude_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)

        self.lbl_require = ctk.CTkLabel(tab_adv, text=self.texts["lbl_require"][self.lang])
        self.lbl_require.grid(row=3, column=0, padx=(10, 5), pady=5, sticky="e")
        self.require_entry = ctk.CTkEntry(tab_adv, placeholder_text="intranat, bibliotek")
        self.require_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)

        self.lbl_lang_filter = ctk.CTkLabel(tab_adv, text=self.texts["lbl_lang_filter"][self.lang])
        self.lbl_lang_filter.grid(row=5, column=0, padx=(10, 5), pady=5, sticky="e")
        self.lang_filter_entry = ctk.CTkEntry(tab_adv, placeholder_text="sv")
        self.lang_filter_entry.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)

        self.full_var = ctk.BooleanVar(value=False)
        self.cb_full = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_full"][self.lang],
                                       variable=self.full_var)
        self.cb_full.grid(row=0, column=2, padx=10, pady=5, sticky="w")

        arow4 = ctk.CTkFrame(tab_adv, fg_color="transparent")
        arow4.grid(row=4, column=0, columnspan=3, pady=(10, 0), sticky="w")

        self.rm_email_var = ctk.BooleanVar(value=True)
        self.cb_rm_email = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_email"][self.lang],
                                           variable=self.rm_email_var)
        self.cb_rm_email.grid(row=0, column=0, padx=10, pady=5, sticky="w")

        self.rm_phone_var = ctk.BooleanVar(value=True)
        self.cb_rm_phone = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_phone"][self.lang],
                                           variable=self.rm_phone_var)
        self.cb_rm_phone.grid(row=0, column=1, padx=10, pady=5, sticky="w")

        self.rm_pnr_var = ctk.BooleanVar(value=True)
        self.cb_rm_pnr = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_pnr"][self.lang],
                                         variable=self.rm_pnr_var)
        self.cb_rm_pnr.grid(row=0, column=2, padx=10, pady=5, sticky="w")

        self.rm_ip_var = ctk.BooleanVar(value=False)
        self.cb_rm_ip = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_ip"][self.lang],
                                        variable=self.rm_ip_var)
        self.cb_rm_ip.grid(row=1, column=0, padx=10, pady=5, sticky="w")

        for widget, key in ((self.cb_hybrid, "hybrid"), (self.cb_traf, "traf"),
                            (self.cb_sitemap, "sitemap"), (self.cb_robots, "robots"),
                            (self.cb_strict, "strict"), (self.cb_full, "full"),
                            (self.cb_convert_docs, "convert"),
                            (self.lbl_exclude, "exclude"), (self.exclude_entry, "exclude"),
                            (self.lbl_require, "require"), (self.require_entry, "require"),
                            (self.lbl_lang_filter, "lang"), (self.lang_filter_entry, "lang")):
            self._add_tooltip(widget, key)

        # Buttons
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(pady=5)
        self.start_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_start"][self.lang],
            font=ctk.CTkFont(weight="bold"),
            fg_color="#1f6aa5", command=self.start_crawl)
        self.start_btn.pack(side=tk.LEFT, padx=10)
        self.pause_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_pause"][self.lang],
            state="disabled",
            fg_color=("#d9d9d9", "#4a4a4a"),
            text_color=("black", "white"),
            hover_color=("#c9c9c9", "#5a5a5a"),
            command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=10)
        self.stop_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_stop"][self.lang],
            state="disabled",
            fg_color=("#d35b5b", "#a51f1f"),
            hover_color=("#c42b2b", "#8a1a1a"),
            text_color=("white", "white"),
            command=self.stop_crawl)
        self.stop_btn.pack(side=tk.LEFT, padx=10)
        self.open_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_open"][self.lang],
            fg_color=("#d9d9d9", "#4a4a4a"),
            text_color=("black", "white"),
            hover_color=("#c9c9c9", "#5a5a5a"),
            command=self.open_output_folder)
        self.open_btn.pack(side=tk.LEFT, padx=10)

        # Stats & display
        self.stats_label = ctk.CTkLabel(
            main_frame, text=self.texts["status_wait"][self.lang],
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
            text_color="#4caf50")
        self.stats_label.pack(fill=tk.X, pady=5)
        self.progress_bar = ctk.CTkProgressBar(main_frame, orientation="horizontal")

        table_frame = ctk.CTkFrame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table_frame, columns=('URL', 'Status', 'Titel'),
                                 show='headings', height=7)
        self.tree.heading('URL', text='URL')
        self.tree.heading('Status', text=self.texts["col_status"][self.lang])
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang])
        self.tree.column('URL', width=300)
        self.tree.column('Status', width=130)
        self.tree.column('Titel', width=300)

        scrollbar = ctk.CTkScrollbar(table_frame, orientation="vertical",
                                     command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tree.bind(
            "<Double-1>",
            lambda e: webbrowser.open(self.tree.item(self.tree.selection()[0])['values'][0])
            if self.tree.selection()
            and self.tree.item(self.tree.selection()[0])['values'][0].startswith("http")
            else None
        )

        self.log_area = ctk.CTkTextbox(main_frame,
                                       font=ctk.CTkFont(family="Consolas", size=11),
                                       height=100)
        self.log_area.pack(fill=tk.BOTH, expand=False, pady=(10, 0))

    def process_queue(self):
        try:
            for _ in range(150):
                try:
                    msg_type, data = self.msg_queue.get_nowait()
                    if msg_type == "log":
                        t = datetime.now().strftime("%H:%M:%S")
                        self.log_area.insert(tk.END, f"[{t}] {data}\n")
                        self.log_area.see(tk.END)
                        if int(self.log_area.index('end-1c').split('.')[0]) > 500:
                            self.log_area.delete("1.0", "2.0")
                    elif msg_type == "table":
                        if data[1] == self.texts['status_login_required']['sv']:
                            data = (data[0], self.texts['status_login_required'][self.lang], data[2])
                        self.tree.insert('', 0, values=data)
                        if len(self.tree.get_children()) > 100:
                            self.tree.delete(self.tree.get_children()[-1])
                    elif msg_type == "stats_data":
                        self.stats_label.configure(
                            text=self.texts["stats_fmt"][self.lang].format(*data))
                        if self.progress_bar and self.max_pages_entry.get() != "0":
                            try:
                                max_p = int(self.max_pages_entry.get())
                                if max_p > 0:
                                    self.progress_bar.set(data[0] / max_p)
                            except ValueError:
                                pass
                    elif msg_type == "login_wait":
                        proceed = messagebox.askokcancel(
                            "Inloggning / Login", self.texts["login_msg"][self.lang])
                        if self.crawler_instance:
                            if not proceed:
                                self.crawler_instance.stop()
                            else:
                                self.crawler_instance.login_event.set()
                    elif msg_type == "summary":
                        for key in ('run_success', 'run_partial', 'run_failed'):
                            prefix = self.texts[key]['sv'] + ':'
                            if data.startswith(prefix):
                                data = self.texts[key][self.lang] + ':' + data[len(prefix):]
                                break
                        self.stats_label.configure(text=data)
                        self._log_to_gui(data)
                    elif msg_type == "done":
                        self.start_btn.configure(state="normal")
                        self.pause_btn.configure(state="disabled")
                        self.stop_btn.configure(state="disabled")
                        self.progress_bar.stop()
                        self.progress_bar.pack_forget()
                except queue.Empty:
                    break
                except Exception as inner_e:
                    self.log_area.insert(tk.END, f"[GUI FEL] Kunde inte rita rad: {inner_e}\n")
        finally:
            self.root.after(100, self.process_queue)

    def _error_dialog(self, key: str, extra: str = ""):
        messagebox.showerror(self.texts["err_title"][self.lang],
                             self.texts[key][self.lang] + (f"\n\n{extra}" if extra else ""))

    def start_crawl(self):
        url = self.url_entry.get().strip()
        # Saknas schema ("kommunen.se") antar vi https, annars blir resultatet tyst tomt
        if url and "://" not in url:
            url = "https://" + url
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, url)
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or ("." not in parsed.netloc and parsed.hostname != "localhost")):
            self._error_dialog("err_url")
            return
        try:
            delay_val = float(self.delay_entry.get())
            max_pages_val = int(self.max_pages_entry.get())
            max_depth_val = int(self.max_depth_entry.get())
            concurrency_val = int(self.concurrency_entry.get())
        except ValueError:
            self._error_dialog("err_num")
            return
        if delay_val < 0.1 or not 1 <= concurrency_val <= 50 \
                or max_pages_val < 0 or max_depth_val < 0:
            self._error_dialog("err_range")
            return

        inverted_map = {v[self.lang]: k for k, v in self.texts["run_modes"].items()}

        config = {
            **getattr(self, '_template_conversion_options', {}),
            "start_url": url,
            "output_dir": self.dir_var.get(),
            "delay": delay_val,
            "max_pages": max_pages_val,
            "max_depth": max_depth_val,
            "concurrency": concurrency_val,
            "save_format": self.format_var.get(),
            "headless_mode": inverted_map.get(self.headless_var.get(), "headless"),
            "respect_robots": self.robots_var.get(),
            "find_sitemap": self.sitemap_var.get(),
            "use_hybrid": self.hybrid_var.get(),
            "use_trafilatura": self.traf_var.get(),
            "download_docs": self.docs_var.get(),
            "strict_domain": self.strict_var.get(),
            "exclude_keywords": [k.strip().lower()
                                 for k in self.exclude_entry.get().split(",") if k.strip()],
            "require_keywords": [k.strip().lower()
                                 for k in self.require_entry.get().split(",") if k.strip()],
            "remove_email": self.rm_email_var.get(),
            "remove_phone": self.rm_phone_var.get(),
            "remove_pnr": self.rm_pnr_var.get(),
            "remove_ip": self.rm_ip_var.get(),
            "convert_docs_to_md": self.convert_docs_var.get(),
            "incremental": not self.full_var.get(),
            "languages": [x.strip().lower() for x in re.split(r"[,\s]+", self.lang_filter_entry.get())
                          if x.strip()],
        }

        # Skapa crawlern först: misslyckas det (saknade beroenden, ej skrivbar mapp)
        # ska användaren få veta det — inte mötas av en död Start-knapp.
        try:
            crawler = AsyncWebCrawler(config, self.msg_queue)
        except Exception as e:
            self._error_dialog("err_start", str(e))
            return
        self.crawler_instance = crawler
        self._save_settings()

        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text=self.texts["btn_pause"][self.lang])
        self.stop_btn.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.log_area.delete("1.0", tk.END)

        self.progress_bar.pack(fill=tk.X, pady=(0, 10))
        if max_pages_val > 0:
            self.progress_bar.configure(mode='determinate')
            self.progress_bar.set(0)
        else:
            self.progress_bar.configure(mode='indeterminate')
            self.progress_bar.start()

        def run():
            try:
                asyncio.run(crawler.crawl())
            except Exception as e:      # crawl() fångar det mesta, det här är sista skyddsnätet
                self.msg_queue.put(("log", f"[ERROR] Crawlen kraschade: {e}"))
                self.msg_queue.put(("done", "Fel"))

        self.crawl_thread = threading.Thread(target=run, daemon=True)
        self.crawl_thread.start()

    def toggle_pause(self):
        if self.crawler_instance:
            is_paused = self.crawler_instance.pause()
            self.pause_btn.configure(
                text=self.texts["btn_resume"][self.lang]
                if is_paused else self.texts["btn_pause"][self.lang]
            )

    def stop_crawl(self):
        if self.crawler_instance:
            self.stop_btn.configure(state="disabled")
            self.crawler_instance.stop()


def main():
    parser = argparse.ArgumentParser(
        description=f"Webbdammsugare Pro v{VERSION} — webbcrawler för RAG/AI-underlag")
    parser.add_argument("--config", type=str,
                        help="JSON-fil med sajter att crawla (serverläge utan GUI)")
    parser.add_argument("--output", type=str, default=None,
                        help="Utmapp för serverläget (standard: ./server_data)")
    parser.add_argument("--webhook", type=str,
                        help="Webhook-URL för färdig-notis (eller env WEBHOOK_URL)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument('--conversion-worker', nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.conversion_worker:
        from conversion_worker import main as run_document_worker
        sys.exit(run_document_worker(*args.conversion_worker, converter_module=sys.modules[__name__]))

    # Windows-konsoler och omdirigerad utdata (Task Scheduler) är ofta cp1252 och
    # kraschar på emoji i loggraderna. pythonw.exe har ingen stdout alls.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if HAS_UVLOOP:
        uvloop.install()

    if args.config:
        code = asyncio.run(run_cli_mode(args.config,
                                        args.webhook or os.environ.get("WEBHOOK_URL"),
                                        args.output))
        sys.exit(code)

    if not HAS_GUI:
        print("⛔ Grafiskt gränssnitt saknas (tkinter/customtkinter). "
              "Kör i serverläge: python ultimate-web-crawler.py --config sites.json")
        sys.exit(2)
    AppGUI(ctk.CTk()).root.mainloop()


if __name__ == "__main__":
    main()
