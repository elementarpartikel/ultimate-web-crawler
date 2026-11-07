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
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import ExitStack
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, CData, Comment, NavigableString, Tag
from crawler_runtime import (ConversionProcessRunner, PublicationWorkspace, remove_tree,
                             blocking_complete, write_private_json)

from crawler_text import (LogLevel, slugify, metadata_line, file_sha256, markdown_table, add_table_row_text,
                          downgrade_body_h1, atomic_write_text, detect_prompt_injection,
                          clean_pii_text, ROLE_MAILBOX_LOCALPARTS, _INJECTION_RE,
                          redact_diagnostic_urls, redact_diagnostic_data, pdf_page_url, pdf_page_reference,
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
DIAGNOSTIC_URL_VERSION = 1

# GUI-beroenden är frivilliga så att serverläget (--config) fungerar på
# headless-servrar utan tkinter/display.
try:
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk
    import tkinter.font as tkfont
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
    # Temats ursprungliga font (Roboto, som följer med CustomTkinter) är reserv där Segoe UI saknas.
    CTK_THEME_FONT = dict(ctk.ThemeManager.theme["CTkFont"])

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


RUN_SUMMARY_TEXTS = {
    'success': {'sv': 'Lyckad körning', 'en': 'Successful run'},
    'partial': {'sv': 'Partiell körning', 'en': 'Partial run'},
    'failed': {'sv': 'Misslyckad körning', 'en': 'Failed run'},
    'counts': {
        'sv': 'Besökta sidor {0}, nya {1}, ändrade {2}, borttagna {3}, dokument {4}, sidfel {5}',
        'en': 'Pages visited {0}, new {1}, updated {2}, removed {3}, documents {4}, page errors {5}',
    },
    'download_failures': {'sv': 'Dokument med hämtningsfel {0}', 'en': 'Document download failures {0}'},
    'robots_unreachable': {'sv': 'Värdar med otillgänglig robots.txt {0}', 'en': 'Hosts with unavailable robots.txt {0}'},
    'conversion_failures': {'sv': 'Dokument med konverteringsfel {0}', 'en': 'Document conversion failures {0}'},
    'short_pages': {'sv': 'Sidor med för lite text {0}', 'en': 'Pages with too little text {0}'},
    'possible_prompt_injection': {'sv': 'Källor med möjlig prompt-injektion {0}', 'en': 'Sources with possible prompt injection {0}'},
    'fatal_error': {'sv': 'Körningen avbröts av ett fel', 'en': 'The run ended with an error'},
    'report_hint': {'sv': 'Se körningsrapporten.', 'en': 'See the run report.'},
}


@dataclass(frozen=True)
class CrawlSummary:
    status: str
    counts: Tuple[int, int, int, int, int, int]
    warnings: Tuple[Tuple[str, int], ...]
    fatal_error: Optional[str]
    report_path: Optional[str]

    def render(self, lang: str, texts: Dict, *, include_error_detail: bool = False) -> str:
        parts = [texts[self.status][lang], texts['counts'][lang].format(*self.counts)]
        notices = [texts[key][lang].format(count) for key, count in self.warnings]
        if self.fatal_error:
            notices.append(self.fatal_error if include_error_detail else texts['fatal_error'][lang])
        if notices:
            parts.append(' · '.join(notices).rstrip('.'))
        result = '. '.join(parts) + '.'
        if notices and self.report_path:
            result += ' ' + texts['report_hint'][lang]
        return result


# ─────────────────────────────────────────────────────────────
#  HJÄLPFUNKTIONER & CHUNKING
# ─────────────────────────────────────────────────────────────


# Standard-parametrar som aldrig påverkar sidans innehåll och därför ska strippas
# före URL-deduplicering. Lägg till nya här när du upptäcker dubletter i utmappen.
DEFAULT_IGNORE_QUERY_PARAMS = (
    # Generisk kampanj-/spårningsspårning
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'msclkid', 'mc_cid', 'mc_eid',
    'igshid', 'mibextid',
    # Session-/auth-tokens som varierar mellan besök men inte ändrar innehåll
    'sessionid', 'jsessionid', 'phpsessid',
    # Sitevision (svenska kommuners CMS — Tyresö m.fl.).
    'logout', 'printerfriendly',
)

# Dessa parametrar kunde tidigare ge olika källor samma cachenyckel.
CONTENT_QUERY_PARAMS = ('ref', 'source', 'state')

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
                chunk["page_url"] = pdf_page_url(source_url, p_num)
    return chunks


def markdown_to_plain(text: str) -> str:
    """Enkel Markdown → ren text för .txt-utdata."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]*)\]\(([^)]+)\)',
                  lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) else m.group(2),
                  text)
    text = text.replace('**', '').replace('__', '')
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def markdown_header_field(header: str, label: str) -> str:
    m = re.search(r'^\*\*' + re.escape(label) + r':\*\*[ \t]*(.+)$', header, re.MULTILINE)
    return m.group(1).strip() if m else ''


def plain_header_fields(lines) -> Dict[str, str]:
    fields = {}
    for line in lines:
        key, separator, value = line.partition(':')
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def markdown_file_to_chunks(text: str, is_document: bool = False) -> Tuple[Dict, List[Dict]]:
    """Läser en av crawlerns .md-filer och returnerar (metadata, chunks)."""
    parts = text.split('\n---\n')
    header = parts[0]

    def field(label: str) -> str:
        # Brödtexten får inte kunna fylla i eller förfalska dokumentets metadata.
        return markdown_header_field(header, label)

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
            level, heading, content = 0, "Huvudinnehåll", raw
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
            if level == 2 and page_num is not None:
                # Sidhänvisningen finns som separata fält i JSONL.
                reference = pdf_page_reference(url, page_num)
                if content == reference:
                    continue
                content = content.removeprefix(reference + '\n\n')
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
    fields = plain_header_fields(header[1:])
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
# av en RAG-tjänst letar efter. Bevaras bara om `keep_role_emails` är påslaget.


def html_table_cell_text(cell: Tag) -> str:
    """Bevara explicita radbrytningar utan att ta med HTML-källans indrag."""
    lines = [[]]
    for node in cell.descendants:
        if node.name == 'br':
            lines.append([])
        elif type(node) in (NavigableString, CData):
            text = re.sub(r'[ \t\r\n\f]+', ' ', node).strip(' ')
            if text:
                lines[-1].append(text)
    return '\n'.join(' '.join(line) for line in lines)


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
                     'fetched_at', 'conversion_context', 'fetch_url'):
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

    async def get_page_url_candidates(self):
        async with self.conn.execute(
                "SELECT url, fetch_url, filename FROM page_cache "
                "WHERE url != fetch_url AND instr(fetch_url, '?') > 0 ORDER BY url") as cursor:
            return await cursor.fetchall()

    async def rekey_page(self, old_url: str, new_url: str, filename: Optional[str]):
        await self.conn.execute("UPDATE page_cache SET url=?, filename=? WHERE url=?",
                                (new_url, filename, old_url))
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
            "conversion_profile, fetched_at, conversion_context, fetch_url FROM doc_cache WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return {'filename': row[0], 'size_bytes': row[1], 'etag': row[2],
                'last_modified': row[3], 'referer_url': row[4] or '',
                'referer_title': row[5] or '', 'link_text': row[6] or '',
                'content_hash': row[7], 'converted_hash': row[8],
                'md_filename': row[9], 'conversion_profile': row[10],
                'fetched_at': row[11], 'conversion_context': row[12], 'fetch_url': row[13]}

    async def save_doc(self, url: str, filename: str, size_bytes: int,
                       etag: Optional[str], last_modified: Optional[str],
                       referer_url: str = "", referer_title: str = "",
                       link_text: str = "", content_hash: Optional[str] = None,
                       fetched_at: Optional[str] = None, fetch_url: Optional[str] = None):
        prior = await self.get_doc(url)
        same_version = bool(prior and content_hash and prior['content_hash'] == content_hash)
        await self.conn.execute(
            'INSERT OR REPLACE INTO doc_cache (url, filename, size_bytes, etag, '
            'last_modified, referer_url, referer_title, link_text, downloaded_at, '
            'content_hash, converted_hash, md_filename, conversion_profile, fetched_at, conversion_context, fetch_url) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (url, filename, size_bytes, etag, last_modified, referer_url,
             referer_title, link_text, datetime.now().isoformat(), content_hash,
             prior['converted_hash'] if same_version else None,
             prior['md_filename'] if same_version else None,
             prior['conversion_profile'] if same_version else None,
             datetime.now().isoformat(timespec='seconds') if fetched_at is None else fetched_at,
             prior['conversion_context'] if same_version else None, fetch_url))
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
                'md_filename', 'conversion_profile', 'fetched_at', 'conversion_context', 'fetch_url')
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

    async def get_page_crawl_records(self):
        async with self.conn.execute(
            "SELECT url, title, crawled_at, content_hash, filename, fetch_url, "
            "processing_profile, content_length FROM page_cache ORDER BY crawled_at DESC"
        ) as cursor:
            columns = [column[0] for column in cursor.description]
            return {row[0]: dict(zip(columns, row)) for row in await cursor.fetchall()}

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
        self.last_requests: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def async_wait(self, domain: str):
        async with self._locks.setdefault(domain, asyncio.Lock()):
            # Räkna från faktisk start så att försenade väntare inte släpps i klump.
            while True:
                now = time.monotonic()
                delay = max(self.domain_delays.get(domain, self.delay), self.domain_minimums.get(domain, 0))
                last = self.last_requests.get(domain)
                remaining = last + delay - now if last is not None else 0
                if remaining <= 0:
                    self.last_requests[domain] = now
                    return
                await asyncio.sleep(remaining)

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
    '.pdf', '.doc', '.docx', '.dotx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.odt', '.ods', '.odp', '.rtf', '.csv', '.zip',
}
DOCUMENT_CONTENT_TYPES = {
    'application/pdf': '.pdf',
    'application/msword': '.doc',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.ms-powerpoint': '.ppt',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template': '.dotx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.oasis.opendocument.text': '.odt',
    'application/vnd.oasis.opendocument.spreadsheet': '.ods',
    'application/vnd.oasis.opendocument.presentation': '.odp',
    'application/rtf': '.rtf',
    'text/rtf': '.rtf',
    'text/csv': '.csv',
    'application/zip': '.zip',
    'application/x-zip': '.zip',
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


class OffDomainRedirectError(BlockedHostError):
    """Ett navigationsmål ligger utanför crawlens tillåtna domäner."""
    def __init__(self, target: str):
        self.target = target
        super().__init__('Omdirigerad utanför tillåtna domäner')


class BlockedDNSError(OSError):
    """En DNS-policynekning som aiohttp kan bädda in i ett anslutningsfel."""


_IPV6_TRANSITION_NETWORKS = tuple(map(ipaddress.IPv6Network, (
    '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32',
)))
_INTRANET_IPV4 = tuple(map(ipaddress.IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
_INTRANET_IPV6 = ipaddress.IPv6Network('fc00::/7')


def is_public_ip(value) -> bool:
    """En publik anslutning får inte gå via kända IPv6-övergångsprefix."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif any(address in network for network in _IPV6_TRANSITION_NETWORKS):
            return False
    return address.is_global and not (address.is_multicast or address.is_reserved)


def _is_intranet_ip(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _INTRANET_IPV4)
    return address in _INTRANET_IPV6


def is_non_public_host(host: Optional[str]) -> bool:
    """True om `host` är en IP-literal eller localhost som inte är publik."""
    if not host:
        return False
    host = host.strip("[]").lower().rstrip('.')
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not is_public_ip(ipaddress.ip_address(host))
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
            if not self.allow_private and any(not is_public_ip(info['host']) for info in infos):
                raise BlockedDNSError(f'Blockerat icke-publikt DNS-mål för {host}')
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
        if not infos or any(not is_public_ip(i['host']) for i in infos):
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
_HIDDEN_CLASSES = frozenset(('sr-only', 'visually-hidden', 'd-none'))
_HIDDEN_STYLE_RE = re.compile(
    r'(?:^|;)\s*(?:display\s*:\s*none|'
    r'visibility\s*:\s*hidden|'
    r'font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|pt|%)?|'
    r'opacity\s*:\s*0(?:\.0+)?|'
    r'color\s*:\s*transparent|'
    r'clip\s*:\s*rect\(\s*0(?:px)?(?:(?:\s*,\s*|\s+)0(?:px)?){3}\s*\)|'
    r'clip-path\s*:\s*inset\(\s*(?:50|100)%\s*\)|'
    r'text-indent\s*:\s*-[0-9]{4,}px|'
    r'left\s*:\s*-[0-9]{4,}px)'
    r'(?:\s*!important)?\s*(?:;|$)',
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
    'conversion_memory_mb',
    'generation_keep', 'generation_min_age_hours', 'changes_history_max_entries',
    'spa_wait_seconds', 'spa_ready_selector',
    'allow_libreoffice',
}

_FLOAT_CONFIG_RULES = {
    'delay': (0.5, 0), 'max_page_mb': (10, 0.000001),
    'max_download_mb': (100, 0.000001), 'sitemap_lastmod_max_age_days': (14, 0),
    'conversion_timeout': (180, 0.1),
    'conversion_memory_mb': (1024, 64),
    'generation_min_age_hours': (24, 0.01),
    'spa_wait_seconds': (3, 0),
}
_INT_CONFIG_RULES = {
    'max_pages': (0, 0), 'max_depth': (0, 0), 'concurrency': (10, 1),
    'playwright_concurrency': (2, 1), 'max_path_segments': (15, 1),
    'max_query_params': (5, 0), 'ocr_max_pages': (50, 0),
    'conversion_workers': (2, 1), 'conversion_queue_size': (8, 1),
    'generation_keep': (0, 0), 'changes_history_max_entries': (0, 0),
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
    'allow_libreoffice',
}


def normalize_site_config(site: dict) -> Dict:
    """Gemensamma typer och standardvärden för GUI och CLI efter validering."""
    result = dict(site)
    result.setdefault('incremental', True)
    result['spa_ready_selector'] = site.get('spa_ready_selector', '').strip()
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


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and not any(char.isspace() for char in parsed.netloc)
                and parsed.port != 0 and not parsed.username and not parsed.password)
    except ValueError:
        return False


def validate_site_config(site: dict) -> Tuple[List[str], List[str]]:
    """Returnerar (fel, varningar) för en sajt-post i sites.json."""
    errors, warnings = [], []
    if not isinstance(site, dict):
        return ["posten är inte ett JSON-objekt"], []
    url = str(site.get("start_url", "")).strip()
    if not _valid_http_url(url):
        errors.append('start_url saknas eller är ogiltig (kräver http(s), värd och giltig port utan inloggningsuppgifter)')
    if site.get('session_check_url') and not _valid_http_url(str(site['session_check_url'])):
        errors.append('session_check_url måste vara en http(s)-adress utan inloggningsuppgifter')
    if 'spa_ready_selector' in site and not isinstance(site['spa_ready_selector'], str):
        errors.append('spa_ready_selector måste vara text')
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
                if key == 'spa_wait_seconds' and value > 60:
                    errors.append('spa_wait_seconds får vara högst 60 sekunder')
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


class _SitemapTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError('Sitemap med DTD eller XML-entiteter avvisad')


def parse_sitemap_xml(content: bytes, limit: int):
    """Packa upp och tolka XML utan tillgång till crawlerns tillstånd."""
    if content.startswith(b'\x1f\x8b'):
        content = safe_gunzip(content, limit)
    if HAS_DEFUSEDXML:
        root = SafeET.fromstring(content, forbid_dtd=True)
    else:
        root = ET.fromstring(content, parser=ET.XMLParser(target=_SitemapTreeBuilder()))
    indexes, pages = [], []
    for node in root:
        kind = node.tag.rsplit('}', 1)[-1]
        if kind not in ('sitemap', 'url'):
            continue
        fields = {child.tag.rsplit('}', 1)[-1]: (child.text or '').strip() for child in node}
        if loc := fields.get('loc'):
            if kind == 'sitemap':
                indexes.append(loc)
            else:
                pages.append((loc, fields.get('lastmod', '')))
    return indexes, pages


def document_retry_delay(attempt: int, retry_after: str = '') -> float:
    """Tolka Retry-After som sekunder eller HTTP-datum, annars exponentiell väntan."""
    retry_after = retry_after.strip()
    if retry_after.isascii() and retry_after.isdecimal():
        return float(retry_after)
    if retry_after:
        try:
            when = parsedate_to_datetime(retry_after)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 2 ** attempt + random.random() * 0.5


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
    if ext == '.rtf':
        return head.startswith(b'{\\rtf')
    return True


class _DocumentZipReader(io.FileIO):
    """Begränsa ZIP-katalogen som standardbiblioteket läser in i minnet."""
    _MAX_READ = 4 * 1024 * 1024

    def read(self, size=-1):
        if size > self._MAX_READ:
            raise zipfile.BadZipFile('För stor ZIP-katalog')
        data = super().read(self._MAX_READ + 1 if size < 0 else size)
        if len(data) > self._MAX_READ:
            raise zipfile.BadZipFile('För stor ZIP-katalog')
        return data


def verified_document_extension(path: str, declared: str, previous: str) -> str:
    """Identifiera dokument med signatur och paketstruktur utan att extrahera text."""
    with open(path, 'rb') as source:
        head = source.read(1024)
    if not head or re.match(rb'<(?:!doctype\s+html|html\b|head\b|body\b|form\b)',
                            head.removeprefix(b'\xef\xbb\xbf').lstrip(), re.I):
        return ''
    if magic_ok('.pdf', head):
        return '.pdf'
    if magic_ok('.rtf', head):
        return '.rtf'
    if magic_ok('.zip', head):
        try:
            with _DocumentZipReader(path, 'rb') as source, zipfile.ZipFile(source) as package:
                if ('content.xml' in package.NameToInfo and 'mimetype' in package.NameToInfo
                        and package.getinfo('mimetype').file_size <= 128):
                    with package.open('mimetype') as metadata:
                        media_type = metadata.read(129).decode('ascii', errors='replace')
                    if media_type.startswith('application/vnd.oasis.opendocument.'):
                        return DOCUMENT_CONTENT_TYPES.get(media_type, '.zip')
                if '[Content_Types].xml' in package.NameToInfo:
                    for member, ext in (('word/document.xml', '.docx'),
                                        ('xl/workbook.xml', '.xlsx'),
                                        ('ppt/presentation.xml', '.pptx')):
                        if member in package.NameToInfo:
                            if ext == '.docx' and package.getinfo('[Content_Types].xml').file_size <= 262144:
                                with package.open('[Content_Types].xml') as metadata:
                                    if b'wordprocessingml.template.main+xml' in metadata.read(262145):
                                        return '.dotx'
                            return ext
                if declared in ('.docx', '.dotx', '.xlsx', '.pptx', '.odt', '.ods', '.odp'):
                    return ''
                return '.zip'
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
            return ''
    if magic_ok('.doc', head):
        # Äldre Office-format delar behållarsignatur. Serverns uppgift väljer parser.
        return next((ext for ext in (declared, previous) if ext in ('.doc', '.xls', '.ppt')), '')
    # Textformat och okända bilagor saknar unik signatur. Behåll tidigare typ.
    ext = previous or declared
    return ext if magic_ok(declared, head) and magic_ok(ext, head) else ''




class AsyncWebCrawler:
    LOGIN_EXPIRED_LIMIT = 5      # antal sessionsfel i rad innan crawlen avbryts
    max_sitemaps = 500

    def __init__(self, config: dict, msg_queue: Optional[queue.Queue] = None, *, server_mode: bool = False):
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
        self.server_mode = server_mode
        self.state = CrawlerState.RUNNING
        self.stats = CrawlStats()
        self.active_tasks = 0
        self.fatal_error: Optional[str] = None
        self._latest_report_path: Optional[str] = None
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
            workers=config['conversion_workers'], timeout=config['conversion_timeout'],
            memory_mb=config['conversion_memory_mb'])

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
        self._private_policy_reason = {True: 'explicit_allow', False: 'explicit_deny'}.get(
            config.get('allow_private_hosts'), 'server_public_default' if server_mode else 'public_default')

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
        self._rekeyed_page_files: Dict[str, str] = {}

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
                allow_libreoffice=config.get('allow_libreoffice', False),
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
        self._sitemap_slots = asyncio.Semaphore(min(4, self.concurrency))
        self._sitemap_tasks = set()
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
        self._chunk_export_stats: Dict = {}
        self._changes_history_trimmed = False

        # Ändringslogg och rapport (skrivs som changes.jsonl / crawl_report.json)
        self.changes: List[Dict] = []
        self.counts = {"added": 0, "updated": 0, "removed": 0, "docs_added": 0,
                       "docs_updated": 0, "docs_removed": 0}
        self.report: Dict[str, List] = {
            "short_pages": [], "blocked_pages": [], "soft_404": [], "spa_timeouts": [],
            "gone_pages": [], "off_domain_redirects": [],
            "conversion_failures": [], "download_failures": [],
            'legacy_conversion_skipped': [],
            "possible_prompt_injection": [], "session_expired": [],
            'prompt_injection_signals': [],
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
    def _log(self, msg: str, level=LogLevel.INFO, *, gui_key: Optional[str] = None, show_in_gui: bool = True):
        msg = redact_diagnostic_urls(msg)
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
            if show_in_gui and level != LogLevel.DEBUG:
                data = (level.name, gui_key) if gui_key else f"[{level.name}] {msg}"
                self.msg_queue.put(("log", data))
        else:
            site = f"[{self.domain}] " if self.server_mode else ''
            line = f"[{level.name}] {site}{msg}"
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
        if self.msg_queue:
            safe_title = title.replace('\x00', '') if title else "Ingen titel"
            self.msg_queue.put(("table", (url, status, safe_title)))
            self._gui_stats()

    def _gui_stats(self):
        if not self.msg_queue:
            return
        cached = (self.stats.pages_unchanged + self.stats.pages_not_modified_304
                  + self.stats.pages_skipped_lastmod)
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

        self.msg_queue.put(("stats_data", (
            pages_done, pages_done - cached, self.stats.documents_downloaded,
            cached, queue_size, self.stats.pages_failed, self._conversion_pending, eta_str,
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
        """Identifiera intranät utan att ge särskilda IPv6-adresser privat behörighet."""
        if 'allow_private_hosts' in self.config:
            if self._allow_private:
                self._log('⚠ Privata nätverksmål tillåts enligt konfigurationen', LogLevel.WARNING,
                          gui_key='private_network_explicit')
            return
        if self.server_mode:
            return
        self._allow_private = False
        self._private_policy_reason = 'public_default'
        host = (urlparse(self.start_url).hostname or '').lower().rstrip('.')
        if not host:
            return
        if host == 'localhost' or host.endswith('.localhost'):
            self._private_policy_reason = 'intranet_literal'
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
                    if infos and all(_is_intranet_ip(ipaddress.ip_address(info[4][0])) for info in infos):
                        self._private_policy_reason = 'intranet_dns'
                except (OSError, ValueError):
                    pass
            else:
                if _is_intranet_ip(address):
                    self._private_policy_reason = 'intranet_literal'
        self._allow_private = self._private_policy_reason in ('intranet_literal', 'intranet_dns')
        if self._allow_private:
            self._log('⚠ Automatiskt intranätläge aktiverat. Privata nätverksmål tillåts', LogLevel.WARNING,
                      gui_key='private_network_automatic')

    async def _on_redirect(self, session, ctx, params):
        """Kontrollera domän, nätverksmål och robots före redirectens anslutning."""
        loc = params.response.headers.get('Location', '')
        target = urlparse(urljoin(str(params.url), loc))
        if target.scheme not in ('http', 'https'):
            raise BlockedHostError(f"Redirect till otillåtet schema: {target.scheme}")
        if not self._allow_private and is_non_public_host(target.hostname):
            raise BlockedHostError(f"Redirect till icke-publik adress: {target.hostname}")
        self._check_crawl_scope(target.geturl(), str(params.url))
        # Robots-hämtningen själv måste kunna följa redirects utan rekursion.
        obey = (getattr(ctx, 'trace_request_ctx', None) or {}).get('obey_robots', True)
        if obey and not await self._robots_allowed(target.geturl()):
            raise RobotsDeniedError("Redirect stoppad av robots.txt")
        if not await self._wait_for_request(target.geturl()):
            raise RobotsDeniedError('Körningen stoppad')

    async def _wait_until_running(self) -> bool:
        while self.state == CrawlerState.PAUSED:
            await asyncio.sleep(0.1)
        return self.state != CrawlerState.STOPPED

    async def _wait_for_request(self, url: str) -> bool:
        domain = urlparse(url).netloc.lower()
        while await self._wait_until_running():
            await self.rate_limiter.async_wait(domain)
            if self.state != CrawlerState.PAUSED:
                return self.state != CrawlerState.STOPPED
        return False

    def _check_crawl_scope(self, target: str, source: str):
        if self.config.get('strict_domain', True) and not self._domain_allowed(urlparse(target).netloc):
            entry = {'url': source, 'final_url': target}
            if entry not in self.report['off_domain_redirects']:
                self.report['off_domain_redirects'].append(entry)
            raise OffDomainRedirectError(target)

    def _check_http_target(self, url: str):
        """IP-literaler passerar inte aiohttp-resolvern och måste kontrolleras här."""
        target = urlparse(url)
        if target.scheme not in ('http', 'https') or not target.hostname:
            raise BlockedHostError("Otillåten nätverksadress")
        if not self._allow_private and is_non_public_host(target.hostname):
            raise BlockedHostError("Nätverksadressen är inte publik")

    async def _create_session(self) -> aiohttp.ClientSession:
        """Skapar aiohttp-session med riktig CookieJar och Brotli om tillgängligt.

        Endast sajtens cookies överförs, med bevarad domän/värd, path och flaggor.
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
        for c in self._site_cookies(cookies):
            try:
                raw_domain = c.get('domain') or ''
                domain = raw_domain.lstrip('.') or urlparse(c.get('url') or self.start_url).hostname
                # Bygg en URL för update_cookies så att domain/path bevaras
                scheme = 'https' if c.get('secure') else 'http'
                authority = f'[{domain}]' if ':' in domain else domain
                cookie_url = f"{scheme}://{authority}{c.get('path', '/')}"

                sc = SimpleCookie()
                sc[c['name']] = c['value']
                m = sc[c['name']]
                if raw_domain.startswith('.'):
                    m['domain'] = raw_domain
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

    def _site_cookies(self, cookies: List[Dict]) -> List[Dict]:
        """Spara endast cookies som gäller minst en av sajtens tillåtna värdar.

        IdP-cookies får finnas i den interaktiva browserns minne, men inte på disk
        eller i aiohttp. En föräldradomäncookie kan krävas för en tillåten undervärd.
        """
        hosts = {urlparse(self.start_url).hostname.lower(), *self.allowed_domains}
        result = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            raw = cookie.get('domain') or ''
            domain = (raw.lstrip('.') or urlparse(cookie.get('url') or self.start_url).hostname or '').lower()
            if (self._domain_allowed(domain)
                    or raw.startswith('.') and any(host == domain or host.endswith('.' + domain) for host in hosts)):
                result.append(cookie)
        return result

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
            if not await self._wait_for_request(url):
                return None

            try:
                if method == 'HEAD':
                    async with self.req_session.head(
                        url, allow_redirects=True,
                        trace_request_ctx={'obey_robots': obey_robots},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        self._check_crawl_scope(str(resp.url), url)
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
                    self._check_crawl_scope(str(resp.url), url)
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
            except OffDomainRedirectError as e:
                if self._login_detection_enabled and self.login_detector.is_login_redirect(url, e.target):
                    return FetchResult(status=401, final_url=url)
                return None
            except BlockedHostError as e:
                self._log(f"  ⛔ Blockerad: {url} ({e})", LogLevel.WARNING)
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if isinstance(getattr(e, 'os_error', None), BlockedDNSError):
                    self._log(f"  ⛔ Blockerad: {url} ({e.os_error})", LogLevel.WARNING)
                    return None
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

    async def _read_sitemap(self, url: str):
        async with self._sitemap_slots:
            if not await self._wait_until_running():
                return None
            self._log(f"🗺️ Letar i sitemap: {url}")
            try:
                result = await self.fetch(url, max_retries=2, decode_text=False,
                                          max_bytes=self.max_sitemap_bytes)
                if not result or not result.body or self.state == CrawlerState.STOPPED:
                    return None
                indexes, pages = await asyncio.to_thread(parse_sitemap_xml, result.body,
                                                          self.max_sitemap_bytes)
                return result.final_url or url, indexes, pages
            except Exception as e:
                self._log(f"⚠ Fel vid sitemap-läsning: {e}", LogLevel.DEBUG)
                return None

    async def _parse_sitemap(self, url: str):
        """Följ index breddvis med begränsad kö och begränsat antal aktiva jobb."""
        pending = deque()

        def schedule(value: str, base: str):
            if len(self.visited_sitemaps) >= 500:
                return
            try:
                target = request_url(urljoin(base, value))
                parsed = urlparse(target)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                    return
                if self.config.get('strict_domain', True) and not self._domain_allowed(parsed.netloc):
                    self._log(f"  ⛔ Hoppar över sitemap utanför domänen: {target}", LogLevel.DEBUG)
                    return
            except ValueError:
                return
            if target not in self.visited_sitemaps and len(self.visited_sitemaps) < self.max_sitemaps:
                self.visited_sitemaps.add(target)
                pending.append(target)

        schedule(url, self.base_url + '/')
        while pending and await self._wait_until_running():
            batch = [asyncio.create_task(self._read_sitemap(pending.popleft()))
                     for _ in range(min(4, len(pending)))]
            self._sitemap_tasks.update(batch)
            try:
                results = await asyncio.gather(*batch, return_exceptions=True)
            finally:
                self._sitemap_tasks.difference_update(batch)
            if not await self._wait_until_running():
                return

            count = 0
            for result in results:
                if result is None or isinstance(result, BaseException):
                    continue
                base, indexes, pages = result
                for index, child in enumerate(indexes):
                    if len(self.visited_sitemaps) >= self.max_sitemaps:
                        break
                    if index % 128 == 0:
                        await asyncio.sleep(0)
                        if not await self._wait_until_running():
                            return
                    schedule(child, base)
                for index, (page_url, lastmod) in enumerate(pages):
                    if index % 128 == 0:
                        await asyncio.sleep(0)
                        if not await self._wait_until_running():
                            return
                    if not self.is_valid_url(page_url):
                        continue
                    if lastmod:
                        self.sitemap_lastmod[normalize_url(page_url, self.ignore_query_params)] = lastmod
                    if self.config.get('download_docs', False) and self._looks_like_document_url(page_url):
                        await self.manifest.record_link(
                            doc_url=normalize_url(page_url, self.ignore_query_params),
                            referer_url="", referer_title="", link_text="(via sitemap)",
                        )
                    if self.url_queue.add_url(page_url, depth=0,
                                              base_priority=CrawlPriority.SITEMAP.value):
                        count += 1
            if count:
                self._log(f"✓ Hittade {count} (godkända) URLs i sitemap/index")

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
                allowed = bool(infos) and all(is_public_ip(info[4][0]) for info in infos)
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
                 'started': False, 'success': False, 'reason': '', 'closed': False,
                 'bytes': 0, 'limited': False, 'blocked_url': ''}

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
                        self._check_crawl_scope(target, page.url)
                        if not await self._robots_allowed(target):
                            raise RobotsDeniedError('Robots.txt nekar navigation')
                        if not await self._wait_for_request(target):
                            raise RobotsDeniedError('Körningen stoppad')
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
                    guard['document_headers'] = headers
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
                if status >= 200 and status not in (204, 304) and not 300 <= status < 400:
                    if not await self._fulfill_browser_resource(cdp, request_id, event, guard):
                        await cdp.send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'})
                    return
                await cdp.send('Fetch.continueRequest', {'requestId': request_id})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if isinstance(e, OffDomainRedirectError):
                    guard['blocked_url'] = e.target
                guard['reason'] = guard['reason'] or redact_diagnostic_urls(str(e))[:160]
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
        patterns = [{'urlPattern': '*', 'requestStage': 'Request'},
                    {'urlPattern': '*', 'requestStage': 'Response'}]
        await cdp.send('Fetch.enable', {'patterns': patterns})
        return guard

    async def _fulfill_browser_resource(self, cdp, request_id: str, event: Dict, guard: Dict) -> bool:
        """Begränsa sidans samlade svarskroppar innan Chromium får rendera dem."""
        headers = event.get('responseHeaders', [])
        length = next((h['value'] for h in headers if h['name'].lower() == 'content-length'), '')
        remaining = self.max_page_bytes - guard['bytes']
        if guard['limited'] or length.isdigit() and int(length) > remaining:
            guard.update(limited=True, reason='Sidans storleksgräns överskreds')
            return False
        stream = None
        body = bytearray()
        try:
            stream = (await cdp.send('Fetch.takeResponseBodyAsStream', {'requestId': request_id}))['stream']
            async with asyncio.timeout(20):
                while self.state != CrawlerState.STOPPED:
                    block = await cdp.send('IO.read', {'handle': stream,
                        'size': min(65536, max(1, self.max_page_bytes - guard['bytes'] + 1))})
                    data = (base64.b64decode(block['data']) if block.get('base64Encoded')
                            else block['data'].encode('utf-8'))
                    guard['bytes'] += len(data)
                    if guard['limited'] or guard['bytes'] > self.max_page_bytes:
                        guard.update(limited=True, reason='Sidans storleksgräns överskreds')
                        return False
                    body.extend(data)
                    if block.get('eof'):
                        # CDP-streamen är avkodad; komprimerings-/längdheaders gäller inte längre.
                        clean_headers = [h for h in headers if h['name'].lower() not in (
                            'content-encoding', 'content-length', 'transfer-encoding')]
                        await cdp.send('Fetch.fulfillRequest', {
                            'requestId': request_id, 'responseCode': event['responseStatusCode'],
                            'responseHeaders': clean_headers, 'body': base64.b64encode(body).decode('ascii')})
                        return True
            return False
        finally:
            if stream:
                await cdp.send('IO.close', {'handle': stream})

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

    async def _wait_for_spa_content(self, page, pending: Set, guard: Dict) -> bool:
        """Ge huvudtext och dataanrop tid att bli klara efter DOMContentLoaded."""
        budget = self.config['spa_wait_seconds']
        if not budget:
            return True
        selector = self.config['spa_ready_selector']
        start = time.monotonic()
        previous, changed_at = None, start
        try:
            async with asyncio.timeout(budget):
                if selector:
                    await page.locator(selector).first.wait_for(state='visible', timeout=0)
                    return True
                while not guard['reason'] and await self._wait_until_running():
                    sample = await page.evaluate('''() => {
                        const root = document.querySelector('main, [role="main"], #root, #app, #__next')
                            || document.body;
                        const text = root?.innerText.trim() || '';
                        return [text.length, text.slice(0, 256), text.slice(-256)];
                    }''')
                    now = time.monotonic()
                    if sample != previous or pending:
                        previous, changed_at = sample, now
                    if sample[0] and not pending and now - start >= 0.75 and now - changed_at >= 0.25:
                        return True
                    await asyncio.sleep(0.1)
        except TimeoutError:
            # Långlivade dataanrop får inte hålla hela crawlen öppen.
            return not selector and bool(previous and previous[0])
        return False

    async def _render_response_with_playwright(self, url: str) -> FetchResult:
        """Bevara HTTP-status även när Chromium renderar svaret."""
        async with self.playwright_semaphore:
            ctx = await self.get_playwright_context()
            if ctx is None:
                return FetchResult()
            page = await ctx.new_page()
            pending = set()
            navigation_status = None

            def track_request(request):
                if request.resource_type in ('fetch', 'xhr'):
                    pending.add(request)

            def finish_request(request):
                pending.discard(request)

            def track_response(response):
                nonlocal navigation_status
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    navigation_status = response.status

            guard = None
            try:
                page.on('request', track_request)
                page.on('requestfinished', finish_request)
                page.on('requestfailed', finish_request)
                page.on('response', track_response)
                guard = await self._install_browser_guard(page)
                # Kort timeout — om sidan inte är klar inom 12s tar vi vad vi har
                goto_ok = False
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    goto_ok = True
                except Exception as e:
                    self._log(f"  ⚠ Playwright kunde inte ladda {url}: {redact_diagnostic_urls(str(e))[:80]}",
                              LogLevel.DEBUG)
                ready = True
                if goto_ok and not guard['reason'] and (not response or 200 <= response.status < 300):
                    ready = await self._wait_for_spa_content(page, pending, guard)
                # Avbryt om goto misslyckades eller crawl stoppats
                if guard['limited']:
                    self._log('⚠ Sidans storleksgräns överskreds i webbläsaren', LogLevel.WARNING)
                    return FetchResult(status=413, final_url=url)
                if guard['blocked_url'] and self._login_detection_enabled and self.login_detector.is_login_redirect(url, guard['blocked_url']):
                    return FetchResult(status=401, final_url=url)
                if not goto_ok or guard['reason'] or not await self._wait_until_running():
                    return FetchResult()
                if not ready:
                    self.report['spa_timeouts'].append({'url': url})
                    return FetchResult(status=408, final_url=url)
                # Kontrollera DOM i webbläsaren innan hela serialiseringen förs till Python.
                within_limit = await asyncio.wait_for(page.evaluate('''limit => {
                    const encoder = new TextEncoder();
                    const walker = document.createTreeWalker(document, NodeFilter.SHOW_ALL);
                    let node;
                    const count = text => {
                        if (text.length > limit) return false;
                        limit -= encoder.encode(text).length;
                        return limit >= 0;
                    };
                    while ((node = walker.nextNode())) {
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            if (!count(node.tagName + node.tagName + '<></>')) return false;
                            for (const attr of node.attributes)
                                if (!count(attr.name) || !count(attr.value) || !count('="" ')) return false;
                        } else if (!count(node.nodeValue || '')) return false;
                    }
                    return true;
                }''', self.max_page_bytes), timeout=12)
                if not within_limit:
                    self._log('⚠ Sidans DOM överskred storleksgränsen', LogLevel.WARNING)
                    return FetchResult(status=413, final_url=url)
                html = await asyncio.wait_for(page.content(), timeout=12)
                if guard['limited'] or len(html.encode('utf-8')) > self.max_page_bytes:
                    return FetchResult(status=413, final_url=url)
                if guard['reason'] or self.state == CrawlerState.STOPPED:
                    return FetchResult()
                final_url = page.url
                self._check_crawl_scope(final_url, url)
                status = navigation_status or (response.status if response else 200)
                if 200 <= status < 300:
                    self.rate_limiter.succeeded(urlparse(final_url).netloc.lower())
                return FetchResult(text=html, final_url=final_url, status=status)
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
        html_changed = False
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')
            html_changed = True

        base_tag = soup.find('base', href=True)
        base_url = urljoin(url, base_tag['href'].strip()) if base_tag else url
        if urlparse(base_url).scheme not in ('http', 'https'):
            base_url = url

        # Dold text och kommentarer kan innehålla instruktioner riktade mot en
        # LLM (indirekt prompt-injektion). Vi undersöker dolda element och kommentarer:
        # Om ett dolt element innehåller ett injektionsmönster raderas det så att det
        # varken når Trafilatura eller reservvägen.
        # Normala dolda element (accordions, FAQ-paneler, flikar, liten text) bevaras!
        hidden_style, hidden_attr, hidden_aria, hidden_class = [], [], [], []
        comments, anchors, meta_tags = [], [], []
        for node in soup.descendants:
            if isinstance(node, Tag):
                attrs = node.attrs
                if node.name == 'a' and 'href' in attrs:
                    anchors.append(node)
                elif node.name == 'meta':
                    meta_tags.append(node)
                if (style := attrs.get('style')) and _HIDDEN_STYLE_RE.search(style):
                    hidden_style.append(node)
                elif 'hidden' in attrs:
                    hidden_attr.append(node)
                elif attrs.get('aria-hidden') == 'true':
                    hidden_aria.append(node)
                elif node.name == 'template' or ((classes := attrs.get('class'))
                                                and not _HIDDEN_CLASSES.isdisjoint(classes)):
                    hidden_class.append(node)
            elif isinstance(node, Comment):
                comments.append(node)

        hidden_texts: List[str] = []
        # Behåll ordningen mellan markeringstyperna för nästlade element.
        for el in (*hidden_style, *hidden_attr, *hidden_aria, *hidden_class):
            if el.decomposed if hasattr(el, "decomposed") else False:
                continue
            t = ' '.join(str(node) for node in el.find_all(string=True)
                         if not isinstance(node, Comment))
            if t:
                hidden_texts.append(t)
                if detect_prompt_injection(t):
                    el.decompose()
                    html_changed = True

        for c in comments:
            if getattr(c, 'parent', None) is None:
                continue
            c_str = str(c).strip()
            if c_str:
                hidden_texts.append(c_str)
            c.extract()
            html_changed = True

        meta_tags = [tag for tag in meta_tags if not tag.decomposed]

        def find_meta(attribute, criterion):
            if isinstance(criterion, re.Pattern):
                return next((tag for tag in meta_tags if criterion.search(tag.get(attribute, ''))), None)
            return next((tag for tag in meta_tags if tag.get(attribute) in criterion), None)

        page_links = []
        for a_tag in anchors:
            if a_tag.decomposed:
                continue
            raw_href = a_tag['href']
            href = raw_href.strip()
            if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                full_url = urljoin(base_url, href)
                if full_url != raw_href:
                    a_tag['href'] = full_url
                    html_changed = True
                # Länktexten kan innehålla namn/e-post och ska tvättas som all annan text
                link_text = self.clean_pii(a_tag.get_text(separator=' ', strip=True)[:200])
                page_links.append({"url": full_url, "text": link_text})

        raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
        og_title_tag = find_meta('property', ('og:title',))
        og_title = (og_title_tag.get('content', '').strip()
                    if og_title_tag and og_title_tag.get('content') else "")
        h1_tag = soup.find('h1')
        h1_text = h1_tag.get_text(' ', strip=True) if h1_tag else ""
        title = clean_page_title(raw_title or og_title, self.domain)
        if not title and 3 <= len(h1_text) <= 200:
            title = h1_text
        title = metadata_line(self.clean_pii(title or "Okänd"))

        keywords = []
        meta_kw = find_meta('name', re.compile(r'keywords', re.I))
        if meta_kw and meta_kw.get('content'):
            keywords = [self.clean_pii(k.strip()) for k in meta_kw['content'].split(',')]

        description = ""
        meta_desc = find_meta('name', re.compile(r'description', re.I))
        if meta_desc and meta_desc.get('content'):
            description = self.clean_pii(meta_desc['content'].strip())

        author_tag = find_meta('name', ('author', 'DC.creator'))
        author = self.clean_pii(author_tag['content']) if author_tag and author_tag.get('content') else ""

        pub_date = ""
        mod_date = ""

        meta_pub = (find_meta('property', re.compile(r'article:published_time|og:pubdate', re.I))
                    or find_meta('name', re.compile(r'pubdate|date', re.I)))
        if meta_pub and meta_pub.get('content'):
            pub_date = meta_pub['content'].strip()

        meta_mod = (find_meta('property', re.compile(r'article:modified_time|og:updated_time', re.I))
                    or find_meta('name', re.compile(r'last-modified|revised', re.I))
                    or find_meta('itemprop', ('dateModified',)))
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

        og_type_tag = find_meta('property', ('og:type',))
        og_type = og_type_tag['content'] if og_type_tag and og_type_tag.get('content') else ""

        full_text = ""
        structured_sections: List[Dict] = []

        if self.use_trafilatura and HAS_TRAFILATURA:
            try:
                extracted = trafilatura.extract(
                    str(soup) if html_changed else html, include_links=True, include_images=False,
                    include_tables=True, include_formatting=True,
                    output_format="markdown", url=url,
                )
                if extracted:
                    extracted = self.clean_pii(add_table_row_text(extracted, allow_headerless=True))
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
            # Brus-rensning på klass/id. Matchar hela klasstoken från början
            # ("nav-item", "menu") men aldrig t.ex. <body class="has-nav">, och
            # rör aldrig html/body/main/article.
            for el in soup.find_all(True):
                if el.decomposed if hasattr(el, "decomposed") else False:
                    continue
                if el.name in ('script', 'style', 'nav', 'footer', 'aside', 'iframe', 'svg', 'button', 'form'):
                    el.decompose()
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
                    rows = []
                    for row in el.find_all('tr'):
                        if row.find_parent('table') is not el:
                            continue
                        cells = row.find_all(['td', 'th'], recursive=False)
                        if cells:
                            rows.append([html_table_cell_text(cell) for cell in cells])
                    table = markdown_table(rows)
                    if table:
                        current_text.append(f"\n{add_table_row_text(table)}\n")
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

        data = {
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
            "flags": [],
            "plain_text": full_text,
            "chunks": semantic_chunk_text(structured_sections, source_url=url, title=title),
            "page_links": page_links,
        }
        # Metadata och rubrikvägar kan nå RAG-tjänsten även när de saknas i brödtexten.
        metadata = [value for key, value in data.items() if key != 'plain_text' and isinstance(value, str)]
        groups = {
            'hidden_html': hidden_texts,
            'metadata': [*metadata, *keywords],
            'links': [value for link in page_links for value in link.values()],
            'page_text': [full_text, *(chunk[key] for chunk in data['chunks']
                                      for key in ('heading', 'heading_path', 'context', 'content'))],
        }
        data['injection_signals'] = [name for name, values in groups.items() if detect_prompt_injection(*values)]
        if data['injection_signals']:
            data['flags'].append('possible_prompt_injection')
        return data

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
                       extractor_version=9)
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
        if confirmed and self.config.get('download_docs', False):
            # Dokument utan filändelse passerar sidflödet innan filtypen blir känd.
            prior_doc = await self.db.get_doc(url)
            if prior_doc:
                await self._handle_gone_document(url, prior_doc, status)
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
                             and cached.get('fetch_url')
                             and normalize_url(cached['fetch_url'], self.ignore_query_params) == url
                             and not self.config['spa_ready_selector']
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

                self._check_crawl_scope(result.final_url or fetch_url, fetch_url)
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
                media_type = content_type.partition(';')[0].strip()
                if (media_type in DOCUMENT_CONTENT_TYPES or media_type.startswith('application/vnd')
                        or (media_type == 'application/octet-stream' and await self.db.get_doc(url))):
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
                if self.config['spa_ready_selector'] or self._needs_javascript(html):
                    if self.state == CrawlerState.STOPPED:
                        return False
                    rendered = await self._render_response_with_playwright(final_url)
                    rendered_html, rendered_url = rendered.text, rendered.final_url
                    render_status = rendered.status
                    self._check_crawl_scope(rendered_url or final_url, fetch_url)
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
                final_url = rendered_url or fetch_url
                if rendered_html:
                    html = rendered_html
                    final_url = rendered_url or fetch_url
                    source = "Webbläsare"

            self._check_crawl_scope(final_url, fetch_url)
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

            # ─── Bot-skydd ───
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
            injection_signals = data.pop('injection_signals', [])
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
                self.report['prompt_injection_signals'].append({'url': url, 'signals': injection_signals})

            page_links = data.get("page_links", [])

            # ─── Språkfilter ───
            if self.languages:
                page_lang = (data.get("language") or "").lower().replace("_", "-").split("-")[0]
                if page_lang and page_lang not in self.languages:
                    self.report["wrong_language"].append({"url": url, "language": page_lang})
                    await self._drop_saved_page(url, cached, "fel språk")
                    await self._save_page_cache(url, data, content_hash, etag, last_mod, [], None, fetch_url,
                                                rendered=source == 'Webbläsare')
                    async with self.async_stats_lock:
                        self.stats.pages_visited += 1
                    self._gui_update(url, f"Hoppar över språk ({page_lang})", data["title"])
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

            if not unchanged:
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
                if unchanged:
                    self.stats.pages_unchanged += 1
            self._gui_update(url, "Oförändrad" if unchanged else f"Hämtad ({source})", data["title"])
            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = redact_diagnostic_urls(str(e))
            self._log(f"  ✗ Fel vid besök ({url}): {reason[:80]}", LogLevel.ERROR)
            async with self.async_stats_lock:
                self.stats.pages_failed += 1
            self._gui_update(url, "Fel", reason[:30])
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
                       converter_version=22)
        return hashlib.sha256(json.dumps(options, sort_keys=True).encode('utf-8')).hexdigest()

    def _conversion_context(self, ref: Dict, last_modified: Optional[str]) -> str:
        values = {key: metadata_line(self.clean_pii(ref.get(key, '') or ''))
                  for key in ('referer_url', 'referer_title', 'link_text')}
        values['server_modified'] = DocumentConverter._parse_date(last_modified, http=True)
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode('utf-8')).hexdigest()

    async def _invalidate_document_text(self, url: str, prior: Optional[Dict], **change_details):
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
                    self._record_change('document_text_removed', url, file=name, **change_details)

    async def _handle_gone_document(self, url: str, prior: Optional[Dict], status: int):
        await self._invalidate_document_text(url, prior, reason=f'HTTP {status}')
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
                'ocr', 'ocr_languages', 'ocr_max_pages', 'allow_libreoffice')
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
            header = result['text'].partition('\n---\n')[0]
            flags = markdown_header_field(header, 'Flaggor')
            if 'possible_prompt_injection' in (flag.strip() for flag in flags.split(',')):
                self.report['possible_prompt_injection'].append(url)
                self.report['prompt_injection_signals'].append({'url': url, 'signals': ['document']})
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
                        'url': job['url'], 'file': job['filename'],
                        'reason': redact_diagnostic_urls(str(error))[:200]})
                    self._log(f'⚠ Konverteringsjobb misslyckades: {error}', LogLevel.ERROR)
                finally:
                    if job is not None:
                        self._conversion_pending -= 1
                        self._gui_stats()
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
        if ext in DocumentConverter.LEGACY_TARGETS and not self.config.get('allow_libreoffice', False):
            self.report['legacy_conversion_skipped'].append({'url': job['url'], 'file': job['filename']})
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

    async def _download_via_aiohttp(self, url: str, part_path: str, headers: Dict[str, str]) -> Dict:
        """Gör högst tre strömmande försök med ny arbetsfil efter nätfel."""
        for attempt in range(3):
            if not await self._wait_for_request(url):
                return {'status': 'aborted'}
            outcome = await self._download_document_attempt(url, part_path, headers)
            if outcome['status'] != 'retry':
                return outcome
            if attempt == 2:
                break
            delay = document_retry_delay(attempt, outcome.get('retry_after', ''))
            if delay > 60:
                break
            if os.path.exists(part_path):
                os.remove(part_path)
            await asyncio.sleep(delay)
        return {'status': 'failed'}

    async def _download_document_attempt(self, url: str, part_path: str, headers: Dict[str, str]) -> Dict:
        """Strömma ett svar till arbetsfilen före filtypskontrollen."""
        try:
            self._check_http_target(url)
            async with self.req_session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                self._check_crawl_scope(str(resp.url), url)
                if resp.status in (429, 503):
                    self.rate_limiter.slow_down(domain=urlparse(str(resp.url)).netloc.lower())
                elif resp.status in (200, 304):
                    self.rate_limiter.succeeded(urlparse(str(resp.url)).netloc.lower())
                if resp.status == 304:
                    return {"status": "not_modified", 'etag': resp.headers.get('ETag'),
                            'last_modified': resp.headers.get('Last-Modified')}
                if resp.status in (404, 410):
                    return {"status": "gone", "http_status": resp.status}
                if resp.status in (429, 500, 502, 503, 504):
                    return {'status': 'retry', 'retry_after': resp.headers.get('Retry-After', '')}
                if resp.status != 200:
                    self._log(f"  ⚠ aiohttp HTTP {resp.status} för dokument: {url}",
                              LogLevel.DEBUG)
                    return {"status": "failed", "browser_retry": resp.status in (401, 403)}

                content_type = resp.headers.get('Content-Type', '').lower()
                cd_raw = resp.headers.get('Content-Disposition', '')
                if content_type.partition(';')[0].strip() in ('text/html', 'application/xhtml+xml'):
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

                written = 0
                digest = hashlib.sha256()
                with open(part_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        if self.state == CrawlerState.STOPPED:
                            return {"status": "aborted"}
                        written += len(chunk)
                        if written > self.max_download_bytes:
                            self._log(f"  ⚠ Dokumentet överskred storleksgränsen: {url}",
                                      LogLevel.WARNING)
                            return {"status": "too_large"}
                        f.write(chunk)
                        digest.update(chunk)
                if written == 0:
                    return {"status": "failed"}
                return {"status": "ok", "etag": etag, "last_modified": last_mod,
                        "content_hash": digest.hexdigest(), "content-type": content_type,
                        "content-disposition": cd_raw, "final_url": str(resp.url)}
        except (BlockedHostError, RobotsDeniedError):
            return {"status": "blocked"}
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as e:
            if isinstance(getattr(e, 'os_error', None), BlockedDNSError):
                return {'status': 'blocked'}
            self._log(f"  ⚠ aiohttp-fel vid dokumentnedladdning: {e}", LogLevel.DEBUG)
            return {"status": "failed" if isinstance(e, aiohttp.ClientSSLError) else "retry"}
        except aiohttp.ClientError as e:
            self._log(f"  ⚠ aiohttp-fel vid dokumentnedladdning: {e}", LogLevel.DEBUG)
            return {"status": "failed"}

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

        part_path = None
        installing = False
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
            # En äldre post utan begärd adress kan innehålla en annan variant.
            if (prior_path and prior.get('fetch_url')
                    and normalize_url(prior['fetch_url'], self.ignore_query_params) == url
                    and (not prior.get('content_hash') or prior['content_hash'] == prior_hash)):
                if prior['etag']:
                    headers['If-None-Match'] = prior['etag']
                if prior['last_modified']:
                    headers['If-Modified-Since'] = prior['last_modified']

            part_path = os.path.join(docs_dir, uuid.uuid4().hex + '.part')
            outcome = await self._download_via_aiohttp(fetch_url, part_path, headers)
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
                response = {}
                if await self._download_via_playwright(fetch_url, part_path, response):
                    outcome = {**response, 'status': 'ok',
                               'content_hash': await blocking_complete(file_sha256, part_path)}
                    status = 'ok'

            filename = os.path.basename(prior_path) if prior_path else default_name
            if status == 'ok':
                if prior_path and prior_hash == outcome['content_hash']:
                    status = 'same'
                else:
                    match = re.search(r"""filename\*?=(?:UTF-8'')?["']?([^"';]+)""",
                                      outcome.get('content-disposition', ''), re.I)
                    name_hint = unquote(match.group(1)).strip() if match else ''
                    media_type = outcome.get('content-type', '').partition(';')[0].strip().lower()
                    declared = (self._safe_ext(posixpath.splitext(name_hint)[1])
                                or DOCUMENT_CONTENT_TYPES.get(media_type)
                                or self._safe_ext(posixpath.splitext(urlparse(outcome.get('final_url', '')).path)[1])
                                or url_ext)
                    stem, previous_ext = os.path.splitext(filename)
                    ext = await blocking_complete(verified_document_extension, part_path, declared,
                                                  previous_ext if prior_path else '')
                    if self.state == CrawlerState.STOPPED:
                        return
                    if not ext:
                        status = 'felaktig filtyp'
                    else:
                        if not prior_path and name_hint:
                            stem = f"{slugify(name_hint.split('.')[0])[:50] or slug_base}_{url_hash}"
                        filename = stem + ext
                        installing = True
                        await self._invalidate_document_text(url, prior)
                        os.replace(part_path, os.path.join(docs_dir, filename))

            referers = self.manifest._referers.get(url, [])
            ref = dict(referers[0]) if referers else {}
            if not ref and prior:
                ref = {'referer_url': prior['referer_url'],
                       'referer_title': prior['referer_title'],
                       'link_text': prior['link_text']}

            if status in ("ok", "not_modified", "same"):
                self._successful_documents += 1
                filepath = os.path.join(docs_dir, filename)
                size = os.path.getsize(filepath)
                content_hash = outcome.get('content_hash') or prior_hash
                if not content_hash:
                    content_hash = await asyncio.to_thread(file_sha256, filepath)
                profile = self._conversion_profile()
                lm = outcome.get('last_modified')
                if status != 'ok' and not lm:
                    lm = (prior or {}).get('last_modified')
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
                if not conversion_valid and not installing:
                    await self._invalidate_document_text(url, prior)

                if status == "ok":
                    async with self.async_stats_lock:
                        self.stats.documents_downloaded += 1
                        self.stats.bytes_downloaded += size
                    kind = "docs_updated" if prior_path else "docs_added"
                    self.counts[kind] += 1
                    replaced = {'previous_file': os.path.basename(prior_path)} if prior_path and prior_path != filepath else {}
                    self._record_change("document_updated" if prior_path else "document_added",
                                        url, file=filename, **replaced)
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
                                       fetched_at=fetched_at, fetch_url=fetch_url)
                if installing and prior_path and prior_path != filepath:
                    os.remove(prior_path)
                installing = False

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
            if installing:
                self._export_failed('dokumentbyte', e)
            self._log(f"  ✗ Filnedladdning misslyckades: {url}, {e}", LogLevel.ERROR)
            self.report['download_failures'].append({'url': url, 'reason': redact_diagnostic_urls(str(e))[:160]})
            async with self.async_download_lock:
                self.downloaded_files.discard(url)

        finally:
            if part_path and os.path.exists(part_path):
                os.remove(part_path)

    async def _download_via_playwright(self, url: str, filepath: str,
                                       response: Optional[Dict] = None) -> bool:
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
                elif response is not None:
                    headers = guard['document_headers']
                    response.update({'content-type': headers.get('content-type', ''),
                                     'content-disposition': headers.get('content-disposition', ''),
                                     'last_modified': headers.get('last-modified'),
                                     'final_url': guard['final_url']})
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
        if not (self._canonical_pending or self.dedupe_content):
            return
        records = await self.db.get_page_crawl_records()
        for source, target in self._canonical_pending.items():
            visited = {source}
            while target in self._canonical_pending and target not in visited:
                visited.add(target)
                target = self._canonical_pending[target]
            if target in visited:
                target = min(visited)  # behåll en deterministisk kopia vid en cykel
            if source == target:
                continue
            target_cache = records.get(target)
            confirmed = bool(target_cache and target_cache.get('crawled_at', '')
                             >= self.stats.start_time.isoformat())
            if confirmed and self.is_valid_url(target) and self._exportable_page(target_cache):
                source_cache = records.pop(source, None)
                await self._drop_duplicate_page(source, source_cache, f'canonical → {target}')
                self.report['canonical_skipped'].append({'url': source, 'canonical': target})
                self._gui_update(source, 'Duplikat (canonical)', (source_cache or {}).get('title', ''))
            else:
                self.report['canonical_unresolved'].append({'url': source, 'canonical': target})

        if not self.dedupe_content:
            return
        owners = {}
        unresolved = {entry['url'] for entry in self.report['canonical_unresolved']}
        for url in sorted(records, key=lambda url: (url not in self._initial_page_files, url)):
            cached = records[url]
            content_hash = cached['content_hash']
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

    async def _migrate_page_url_keys(self):
        """Flytta endast gamla sidnycklar som den sparade begäran kan identifiera."""
        retained = set(CONTENT_QUERY_PARAMS).difference(self.ignore_query_params)
        if not retained:
            return
        reason = 'ändrad URL-identitet'
        for old_url, fetch_url, filename in await self.db.get_page_url_candidates():
            url = normalize_url(fetch_url, self.ignore_query_params)
            if url == old_url:
                continue
            old_query = parse_qs(urlparse(old_url).query, keep_blank_values=True)
            old_names = {name.lower() for name in old_query}
            legacy_ignore = (*self.ignore_query_params, *(retained - old_names))
            if normalize_url(fetch_url, legacy_ignore) != old_url:
                continue
            target = await self.db.get_cache(url)
            if target:
                await self.db.delete_page(old_url)
                if filename:
                    self.counts['removed'] += 1
                    self._record_change('removed', old_url, file=filename, reason=reason)
                continue
            new_name = None
            if filename:
                filename = os.path.basename(filename)
                new_name = stable_filename(url, os.path.splitext(filename)[1])
                for folder in ('texter', 'texter_karantan'):
                    old_path = os.path.join(self.output_dir, folder, filename)
                    if os.path.isfile(old_path) and new_name != filename:
                        new_path = os.path.join(self.output_dir, folder, new_name)
                        if os.path.exists(new_path):
                            raise FileExistsError(f'Filnamnskollision vid flytt av {filename}')
                        await blocking_complete(os.replace, old_path, new_path)
                if new_name != filename:
                    self._rekeyed_page_files[filename] = new_name
                    self.counts['updated'] += 1
                    self._record_change('updated', url, file=new_name, previous_file=filename, reason=reason)
            await self.db.rekey_page(old_url, url, new_name)

    async def _enqueue_cached_urls(self):
        """Återkontrollera kända URL:er sist, även när de försvunnit ur navigeringen.

        Djupbegränsade körningar saknar säker djupinformation för gamla URL:er.
        De återkontrolleras först i nästa körning utan djupbegränsning.
        """
        if not self.config.get('incremental') or self.max_depth != 0:
            return
        for url, cached in (await self.db.get_page_crawl_records()).items():
            if self.is_valid_url(url):
                self.url_queue.add_url(cached.get('fetch_url') or url, depth=0,
                                       base_priority=CrawlPriority.LOW.value)
        if self.config.get('download_docs', False):
            for doc in await self.db.get_all_docs():
                # Vid en delad äldre identitet ska både den gamla källadressen
                # och den faktiskt begärda adressen kunna kontrolleras.
                for url in dict.fromkeys((doc['fetch_url'] or doc['url'], doc['url'])):
                    if self.is_valid_url(url):
                        self.url_queue.add_url(url, depth=0,
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

        Äldre filer utan aktiv registrering flyttas ut ur de exporterade textmapparna till
        texter_utgangna. De raderas inte eftersom de kan komma från äldre körningar.
        """
        allowed = set()
        flagged_urls = set(self.report['possible_prompt_injection'])
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
                        if name.endswith('.json'):
                            language = json.load(source).get('language', '')
                        else:
                            header = []
                            for line in source:
                                if line.rstrip('\n') == '---':
                                    break
                                header.append(line)
                            if name.endswith('.txt'):
                                language = plain_header_fields(''.join(header).strip().splitlines()[1:]).get('Språk', '')
                            else:
                                language = markdown_header_field(''.join(header), 'Språk')
                    language = language.lower().replace('_', '-').split('-')[0]
                    if language and language not in self.languages:
                        self._intentional_export_exclusions.add(name)
                        self.report['wrong_language'].append({'url': url, 'language': language})
                        continue
            allowed.add(name)
            if url not in flagged_urls and os.path.isfile(os.path.join(self.output_dir, 'texter_karantan', name)):
                self.report['possible_prompt_injection'].append(url)
                self.report['prompt_injection_signals'].append({'url': url, 'signals': ['previously_quarantined']})
                flagged_urls.add(url)
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
                        and doc['url'] not in flagged_urls):
                    self.report['possible_prompt_injection'].append(doc['url'])
                    self.report['prompt_injection_signals'].append({'url': doc['url'], 'signals': ['document']})
                    flagged_urls.add(doc['url'])
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
            if (change.get('reason') in ('HTTP 404', 'HTTP 410', 'fel språk', 'HTTP 403')
                    or change['event'] == 'removed' and change.get('reason') == 'ändrad URL-identitet'):
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
        explained.update(old for old, new in self._rekeyed_page_files.items()
                         if new in now or new in explained)
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
        reason = redact_diagnostic_urls(str(error))
        self.report['export_failures'].append({'artifact': artifact, 'reason': reason[:160]})
        message = (f'JSONL-export misslyckades: {reason}' if artifact == 'chunks.jsonl'
                   else f'Export av {artifact} misslyckades: {reason}')
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

    @staticmethod
    def _chunk_file_signature(path):
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        # Hårdlänkning ändrar ctime på vissa filsystem. Storlek och mtime
        # upptäcker ändringar även om en fil utanför crawlern skrivits på plats.
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def _read_chunk_index(self, index_path, outputs, quarantine_enabled):
        try:
            with open(index_path, encoding='utf-8') as source:
                index = json.load(source)
            if (index['version'] != 1 or index['quarantine'] != quarantine_enabled
                    or index['outputs'] != [self._chunk_file_signature(path) for path in outputs]
                    or index['outputs'][0] is None):
                return None
            offsets = [0, 0]
            for _, bucket, start, length, count in index['sources'].values():
                if (any(type(value) is not int for value in (bucket, start, length, count))
                        or bucket not in (0, 1) or start != offsets[bucket] or length < 0
                        or not 0 <= count <= length or bool(count) != bool(length)):
                    return None
                offsets[bucket] += length
            if offsets == [entry[2] if entry else 0 for entry in index['outputs']]:
                return index
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            pass
        return None

    def _append_source_chunks(self, path, name, force_quarantine, quarantine_enabled, targets):
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
            else:
                meta, chunks = plain_file_to_chunks(raw)
                flags = meta.get('flags') or []
        except Exception as e:
            if self._export_files is not None:
                raise RuntimeError(f'Registrerad källfil kunde inte exporteras: {name}') from e
            self._log(f"  ⚠ Hoppar över {name} i chunks.jsonl: {e}", LogLevel.DEBUG)
            return 0, targets[0].tell(), 0, 0

        if force_quarantine and not flags:
            flags = ["quarantined"]
            meta["flags"] = flags

        key = hashlib.md5((meta.get("url") or name).encode('utf-8')).hexdigest()[:10]
        bucket = int(bool(flags or force_quarantine) and quarantine_enabled)
        target = targets[bucket]
        start = target.tell()
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
                record["page_url"] = ch.get("page_url") or pdf_page_url(record['url'], ch['page_number'])
            for field_name in ('heading_paths', 'slide_number'):
                if field_name in ch:
                    record[field_name] = ch[field_name]
            target.write((json.dumps(record, ensure_ascii=False) + os.linesep).encode('utf-8'))
        return bucket, start, target.tell() - start, len(chunks)

    def _write_chunks_jsonl(self) -> int:
        """Publicera hela JSONL-korpusen och återanvänd oförändrade källors byteintervall."""
        names = ('chunks.jsonl', 'chunks_quarantine.jsonl')
        outputs = [os.path.join(self.output_dir, name) for name in names]
        quarantine_enabled = bool(self.config.get("quarantine_flagged_chunks", True))
        index_name = PublicationWorkspace.CHUNK_INDEX
        use_index = self._publication is not None and self._export_files is not None
        previous = self._publication.previous_directory if use_index else None
        index_path = os.path.join(self.output_dir, index_name)
        sources = {}
        for folder in ('texter', 'texter_karantan'):
            directory = os.path.join(self.output_dir, folder)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if (name.endswith(('.md', '.json', '.txt'))
                        and (self._export_files is None or name in self._export_files)):
                    sources[f'{folder}/{name}'] = (
                        self._chunk_file_signature(os.path.join(directory, name)) if use_index else None)

        previous_outputs = [os.path.join(previous, name) for name in names] if previous else []
        previous_index_path = os.path.join(previous, index_name) if previous else None
        index = (self._read_chunk_index(previous_index_path, previous_outputs, quarantine_enabled)
                 if previous_index_path else None)
        old_sources = index['sources'] if index else {}
        reusable = {key for key, stamp in sources.items()
                    if stamp and stamp[1] and key in old_sources and stamp == old_sources[key][0]}
        counts = [0, 0]
        if (index and len(reusable) == len(sources) and list(sources) == list(old_sources)
                and [self._chunk_file_signature(path) for path in outputs] == index['outputs']):
            self._publication._link_or_copy(previous_index_path, index_path)
            for _, bucket, _, _, count in old_sources.values():
                counts[bucket] += count
            self._chunk_export_stats = {'sources': len(sources), 'reused_sources': len(reusable),
                                        'reused_jsonl': True, 'bytes': sum(
                                            entry[2] for entry in index['outputs'] if entry)}
            return counts[0]

        entries = {}
        temp_paths = [path + '.tmp' for path in outputs]
        with ExitStack() as stack:
            targets = [stack.enter_context(open(path, 'wb')) for path in temp_paths]
            originals = ([stack.enter_context(open(path, 'rb')) if stamp else None
                          for path, stamp in zip(previous_outputs, index['outputs'])] if reusable else [])
            for relative, stamp in sources.items():
                if relative in reusable:
                    _, bucket, offset, length, count = old_sources[relative]
                    target = targets[bucket]
                    start = target.tell()
                    if length:
                        source = originals[bucket]
                        source.seek(offset)
                        remaining = length
                        while remaining:
                            block = source.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise OSError('Tidigare chunkexport är ofullständig')
                            target.write(block)
                            remaining -= len(block)
                else:
                    folder, name = relative.split('/', 1)
                    bucket, start, length, count = self._append_source_chunks(
                        os.path.join(self.output_dir, folder, name), name, folder == 'texter_karantan',
                        quarantine_enabled, targets)
                entries[relative] = [stamp, bucket, start, length, count]
                counts[bucket] += count

        os.replace(temp_paths[0], outputs[0])
        if counts[1]:
            os.replace(temp_paths[1], outputs[1])
            self._log(f"🛡️ {counts[1]} chunks sattes i karantän i chunks_quarantine.jsonl "
                      f"(flaggade för säkerhetsrisker)", LogLevel.WARNING)
        else:
            os.remove(temp_paths[1])
            if os.path.exists(outputs[1]):
                os.remove(outputs[1])
        stamps = [self._chunk_file_signature(path) for path in outputs]
        if use_index:
            # Höj versionen när JSONL-rader eller indexformat ändras.
            index = {'version': 1, 'quarantine': quarantine_enabled, 'outputs': stamps, 'sources': entries}
            atomic_write_text(index_path, json.dumps(index, ensure_ascii=False, separators=(',', ':')))
        self._chunk_export_stats = {'sources': len(sources), 'reused_sources': len(reusable),
                                    'reused_jsonl': False, 'bytes': sum(entry[2] for entry in stamps if entry)}
        return counts[0]

    @staticmethod
    def _history_tail_start(previous, keep):
        """Sök bakåt till de senaste hela JSONL-raderna utan att läsa äldre historik."""
        end = previous.seek(0, os.SEEK_END)
        if not keep or not end:
            return end
        previous.seek(end - 1)
        position = end - (previous.read(1) == b'\n')
        while position:
            start = max(0, position - 65536)
            previous.seek(start)
            block = previous.read(position - start)
            lines = block.count(b'\n')
            if lines >= keep:
                return start + len(block.rsplit(b'\n', keep)[0]) + 1
            keep -= lines
            position = start
        return 0

    def _write_changes(self):
        if not self._publication:
            return  # En körning utan utmappslås får inte migrera den delade historiken.
        path = os.path.join(self.output_dir, 'changes.jsonl')
        exists = os.path.isfile(path)
        migrate = exists
        previous_limit = 0
        if exists and self._publication.previous_directory:
            previous_report = os.path.join(self.output_dir, 'crawl_report.json')
            try:
                with open(previous_report, encoding='utf-8') as report_file:
                    report = json.load(report_file)
                migrate = report.get('url_diagnostics_version') != DIAGNOSTIC_URL_VERSION
                previous_limit = report.get('changes_history', {}).get('max_entries', 0)
            except (OSError, ValueError, AttributeError):
                pass
        def json_line(change):
            return (json.dumps(redact_diagnostic_data(change), ensure_ascii=False) + '\n').encode('utf-8')

        limit = self.config['changes_history_max_entries']
        if not (self.changes or migrate or limit and limit != previous_limit):
            return
        with ExitStack() as stack:
            previous = stack.enter_context(open(path, 'rb')) if exists and (limit or migrate or self.changes) else None
            offset = self._history_tail_start(previous, max(0, limit - len(self.changes))) if limit and previous else 0
            if not (self.changes or migrate or offset):
                return
            with open(path + '.tmp', 'wb') as output:
                if previous:
                    previous.seek(offset)
                    if migrate:
                        for line in previous:
                            if line.strip():
                                output.write(json_line(json.loads(line)))
                    else:
                        shutil.copyfileobj(previous, output)
                        if self.changes and output.tell():
                            previous.seek(-1, os.SEEK_END)
                            if previous.read(1) != b'\n':
                                output.write(b'\n')
                for change in self.changes:
                    output.write(json_line({**change, 'run': self.crawl_session_id}))
        os.replace(path + '.tmp', path)
        self._changes_history_trimmed = bool(offset)

    async def _generate_report(self):
        """Skriver ändringshistorik och körningsrapport med maskerade URL:er."""
        try:
            await blocking_complete(self._write_changes)
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

            history_path = os.path.join(self.output_dir, 'changes.jsonl')
            report = {
                "run": self.crawl_session_id,
                "domain": self.domain,
                "version": VERSION,
                'url_diagnostics_version': DIAGNOSTIC_URL_VERSION,
                'chunk_export': self._chunk_export_stats,
                'changes_history': {'max_entries': self.config['changes_history_max_entries'],
                                    'trimmed': self._changes_history_trimmed,
                                    'bytes': os.path.getsize(history_path) if os.path.isfile(history_path) else 0},
                'network_policy': {'allow_private_hosts': self._allow_private,
                                   'reason': self._private_policy_reason},
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
                              json.dumps(redact_diagnostic_data(report), ensure_ascii=False, indent=2))
            if self._publication is None:
                self._latest_report_path = report_path
        except Exception as e:
            self._export_failed('crawl_report.json', e)
            self._log(f"⚠ Fel vid skapande av crawl_report.json: {e}", LogLevel.ERROR)

    def run_summary(self) -> CrawlSummary:
        stats = self.stats
        warning_keys = ('download_failures', 'robots_unreachable', 'conversion_failures',
                        'short_pages', 'possible_prompt_injection')
        return CrawlSummary(
            status=self.delivery_status(),
            counts=(stats.pages_visited, self.counts['added'], self.counts['updated'],
                    self.counts['removed'], stats.documents_downloaded, stats.pages_failed),
            warnings=tuple((key, len(self.report[key])) for key in warning_keys if self.report[key]),
            fatal_error=redact_diagnostic_urls(self.fatal_error) if self.fatal_error else None,
            report_path=self._latest_report_path,
        )

    def summary_text(self) -> str:
        return self.run_summary().render('sv', RUN_SUMMARY_TEXTS, include_error_detail=True)

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
        for task in [*self._url_tasks, *self._sitemap_tasks, *self._conversion_tasks]:
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
                self.saved_cookies = self._site_cookies(cookies)
                self._log(f"✓ Läste {len(self.saved_cookies)} sparade cookies från {path}")
                return bool(self.saved_cookies)
        except Exception as e:
            self._log(f"⚠ Kunde inte läsa cookie-filen {path}: {e}", LogLevel.WARNING)
        return False

    def _save_cookie_file(self):
        path = self.config.get("cookie_file")
        if not path or not self.saved_cookies:
            return
        try:
            write_private_json(path, self._site_cookies(self.saved_cookies))
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
        attempt_path = os.path.join(self._output_root, 'crawl_report.json')

        def write_attempt_report():
            atomic_write_text(attempt_path, json.dumps(redact_diagnostic_data(latest_report),
                                                       ensure_ascii=False, indent=2))
            self._latest_report_path = attempt_path

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
                    self.report['generation_cleanup_failures'].append(
                        {'reason': redact_diagnostic_urls(str(error))[:160]})
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
            write_attempt_report()
        except Exception as error:
            self._export_failed('publicering', error)
            if latest_report:
                latest_report.update(status='failed', fatal_error=self.fatal_error,
                                     publication_committed=publication.published,
                                     export_failures=self.report['export_failures'][:200],
                                     export_failures_count=len(self.report['export_failures']))
                write_attempt_report()
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
                try:
                    await self._generate_report()
                    await self._finish_publication()
                finally:
                    await self._shutdown()
            except asyncio.CancelledError:
                self.state = CrawlerState.STOPPED
                raise
            except Exception as e:
                self._export_failed('avslut', e)
                self._log(f"⚠ Fel vid avslut av körningen: {e}", LogLevel.ERROR)
            finally:
                rate = self.stats.pages_per_second
                self._log(f"Färdig! Total tid: {self.stats.duration} "
                          f"({rate:.1f} sidor/sek, "
                          f"{self.stats.pages_not_modified_304} via 304-cache)")
                summary = self.run_summary()
                self._log(summary.render('sv', RUN_SUMMARY_TEXTS, include_error_detail=True), show_in_gui=False)
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
        await self._migrate_page_url_keys()
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
            self._gui_stats()
            await self._close_conversion_queue(cancel=self.state == CrawlerState.STOPPED)
            if self.msg_queue and self.state != CrawlerState.STOPPED:
                self.msg_queue.put(('phase', 'phase_exporting'))
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
        print(redact_diagnostic_urls(f"⚠ Kunde inte skicka webhook: {e}"))


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
            print(redact_diagnostic_urls(f"⚠ {label}: {w}"))
        if errors:
            for e in errors:
                print(redact_diagnostic_urls(f"⛔ {label}: {e}"))
            failures.append(f"{label}: ogiltig konfiguration")
            config_failures.append(f"{label}: ogiltig konfiguration")
            continue
        base_folder = slugify(site.get("name", "") or urlparse(site["start_url"]).netloc) or f"sajt-{idx}"
        folder, suffix = base_folder, idx
        while folder in used_folders:
            folder = f"{base_folder}-{suffix}"
            suffix += 1
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
            crawler = AsyncWebCrawler(config, server_mode=True)
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
    summary = redact_diagnostic_urls("\n".join(summary_lines))
    status = "✅ Klart" if not failures else "⚠ Klart med fel"
    print(f"\n{status} på {time.time() - start_time:.1f} sekunder!\n{summary}")

    if webhook_url:
        await _post_webhook(webhook_url, f"{status} – dammsugningen är färdig.\n{summary}")
    return 1 if failures else 0


# ─────────────────────────────────────────────────────────────
#  GRAFISKT GRÄNSSNITT (GUI - CustomTkinter)
# ─────────────────────────────────────────────────────────────
class AppGUI:
    RUN_STATUS_COLORS = {
        'success': ("#176536", "#75d59b"),
        'partial': ("#835100", "#ffc46b"),
        'failed': ("#a51f2b", "#ff8d94"),
    }
    STATUS_COLOR = ("#182435", "#eef2f6")
    WINDOW_SURFACE = ("#f3f6f9", "#1b2026")
    PANEL_SURFACE = ("#ffffff", "#262b31")
    PANEL_BORDER = ("#cdd5de", "#414a55")
    MUTED_TEXT = ("#526174", "#b2bdca")
    FIELD_SURFACE = ("#f7f9fc", "#2f3741")
    CONTROL_HOVER = ("#e9f0f7", "#3d4a59")
    SELECTED_SURFACE = ("#dbeafa", "#354c65")
    ACCENT = ("#245985", "#2e689a")
    ACCENT_HOVER = ("#1b486e", "#3877ab")
    DISABLED_TEXT = ("#687588", "#a0adba")
    # Fältkanten ska klara 3:1 mot både fältets fyllning och panelytan (WCAG 1.4.11).
    # PANEL_BORDER är fortfarande en diskret, dekorativ kant runt panelerna.
    FIELD_BORDER = ("#7b8794", "#7f8b98")
    TOOLTIP_SURFACE = ("#ffffff", "#2f3741")
    # Valt läge i flikar och språkval: fyllningen klarar 3:1 mot ovalt läge och texten 4,5:1 mot fyllningen.
    SEGMENT_SELECTED = ("#245985", "#8cb8e6")
    SEGMENT_SELECTED_HOVER = ("#1b486e", "#a3c7ec")
    SEGMENT_SELECTED_TEXT = ("#ffffff", "#10151b")
    # Fokusramen följer Windows: mörk i ljust läge, ljus i mörkt läge, med luft mot kontrollen.
    FOCUS_RING = ("#182435", "#eef2f6")
    FOCUS_RING_WIDTH = 2
    FOCUS_RING_GAP = 2
    # Textstorlek ovanpå skärmens skalning (WCAG 1.4.4 som produktmål): 100–200 %.
    TEXT_SCALES = (1.0, 1.25, 1.5, 1.75, 2.0)
    # Plats för panelrubriker, tabellhuvud och minst en rad i varje panel.
    MIN_TABLE_HEIGHT = 100
    MIN_LOG_HEIGHT = 76
    SASH_HEIGHT = 8
    CONTENT_PAD_X = 12
    RESULTS_HEIGHT_WHEN_SCROLLING = 300

    # Gemensamma textroller i logiska pixlar vid 100 % (se DESIGNPLAN-WCAG.md, del 1).
    # Alla fonter i fönstret hämtas härifrån: CustomTkinter, ttk-tabellen och Tk-etiketter.
    UI_FONT_FAMILY = "Segoe UI"
    TEXT_ROLES = {
        # 14 fet valdes efter fönsterprov: 20 fet lämnade ingen plats åt tabell och logg vid 720 × 450.
        # Panelrubriken skiljs från kolumnrubrikerna genom placering och yta (grått rubrikband).
        "panel_title": (14, "bold"),      # Resultat, Körningslogg
        "body": (14, "normal"),           # etiketter, värden, knappar, celler, logg, hjälp
        "body_strong": (14, "bold"),      # Startadress, primär åtgärd, körstatus
        "column_heading": (14, "bold"),   # Sidtitel, Status, URL
        "meta": (12, "normal"),           # sekundära detaljer
    }
    # Tabellens kolumnbredder i logiska pixlar vid 100 %.
    TREE_COLUMN_WIDTHS = {"URL": 390, "Status": 190, "Titel": 240}

    @staticmethod
    def choose_ui_font_family(available, preferred: str, fallback: str) -> str:
        """Välj gemensam font: Segoe UI på Windows, annars CustomTkinters medföljande reservfont."""
        return preferred if preferred in available else fallback

    def __init__(self, root: ctk.CTk):
        self.root = root
        self.lang = "sv"
        self.texts = {
            "status_login_required": {"sv": "Inloggning krävs", "en": "Login required"},
            "run_summary": RUN_SUMMARY_TEXTS,
            "window_title": {"sv": f"Webbdammsugare Pro (v{VERSION})", "en": f"Web Crawler Pro (v{VERSION})"},
            "tab_basic": {"sv": "Grundinställningar", "en": "Basic settings"},
            "tab_adv": {"sv": "Avancerat", "en": "Advanced"},
            "lbl_url": {"sv": "Startadress", "en": "Start URL"},
            "btn_help": {"sv": "Hjälp", "en": "Help"},
            "dark_mode": {"sv": "Mörkt läge", "en": "Dark mode"},
            "lbl_delay": {"sv": "Fördröjning (sek):", "en": "Delay (sec):"},
            "lbl_max_pages": {"sv": "Max sidor (0=Oändligt):", "en": "Max pages (0=Infinite):"},
            "lbl_max_depth": {"sv": "Max djup (0=Oändligt):", "en": "Max depth (0=Infinite):"},
            "lbl_format": {"sv": "Filformat:", "en": "File Format:"},
            "lbl_concurrency": {"sv": "Samtidighet:", "en": "Concurrency:"},
            "cb_docs": {"sv": "Ladda ner dokument (PDF m.m.)", "en": "Download documents (PDF etc.)"},
            "cb_convert_docs": {"sv": "Konvertera dokument till Markdown", "en": "Convert documents to Markdown"},
            "lbl_mode": {"sv": "Körläge:", "en": "Run Mode:"},
            "lbl_folder": {"sv": "Spara i mapp:", "en": "Save folder:"},
            "btn_folder": {"sv": "Välj mapp…", "en": "Browse…"},
            "cb_hybrid": {"sv": "Hybridmotor", "en": "Hybrid engine"},
            "cb_traf": {"sv": "Trafilatura för text", "en": "Trafilatura for text"},
            "cb_sitemap": {"sv": "Läs Sitemap.xml", "en": "Parse Sitemap.xml"},
            "cb_robots": {"sv": "Respektera robots.txt", "en": "Respect robots.txt"},
            "cb_strict": {"sv": "Strikt Domän", "en": "Strict Domain"},
            "lbl_exclude": {"sv": "Uteslut ord i URL:", "en": "Exclude words in URL:"},
            "lbl_require": {"sv": "Kräv ord i URL (något av):", "en": "Require words in URL (any of):"},
            "cb_rm_email": {"sv": "Maskera e-post", "en": "Mask email"},
            "cb_rm_phone": {"sv": "Maskera telefon", "en": "Mask phone"},
            "cb_rm_pnr": {"sv": "Maskera personnummer", "en": "Mask Swedish SSN"},
            "cb_rm_ip": {"sv": "Maskera IP-adresser", "en": "Mask IP addresses"},
            "cb_full": {"sv": "Full omcrawl (ignorera cache)", "en": "Full re-crawl (ignore cache)"},
            "lbl_lang_filter": {"sv": "Bara språk (t.ex. sv, en):", "en": "Only languages (e.g. sv, en):"},
            "btn_open": {"sv": "Öppna mapp", "en": "Open folder"},
            "menu_open_page": {"sv": "Öppna sidan i webbläsaren", "en": "Open page in browser"},
            "menu_copy_url": {"sv": "Kopiera adress", "en": "Copy address"},
            "menu_copy": {"sv": "Kopiera", "en": "Copy"},
            "menu_select_all": {"sv": "Markera allt", "en": "Select all"},
            "btn_report": {"sv": "Öppna rapport", "en": "Open report"},
            "report_missing": {"sv": "Körningsrapporten finns inte längre.",
                               "en": "The run report is no longer available."},
            "report_open_error": {"sv": "Kunde inte öppna rapporten. {0}",
                                  "en": "Could not open the report. {0}"},
            "err_title": {"sv": "Ogiltigt värde", "en": "Invalid value"},
            "err_url": {"sv": "Ange en giltig startadress, t.ex. https://www.kommunen.se",
                        "en": "Enter a valid start URL, e.g. https://www.example.com"},
            # Ett meddelande per fält, så att felet pekar ut fältet och vad som är tillåtet.
            "err_delay": {"sv": ("Fördröjning (sek) måste vara ett tal som är minst 0,1, till exempel 0,5. "
                                 "Både komma och punkt fungerar som decimaltecken."),
                          "en": "Delay (sec) must be a number of at least 0.1, for example 0.5."},
            "err_max_pages": {"sv": "Max sidor måste vara ett heltal som är 0 eller större. 0 betyder obegränsat.",
                              "en": "Max pages must be a whole number, 0 or greater. 0 means unlimited."},
            "err_max_depth": {"sv": "Max djup måste vara ett heltal som är 0 eller större. 0 betyder obegränsat.",
                              "en": "Max depth must be a whole number, 0 or greater. 0 means unlimited."},
            "err_concurrency": {"sv": "Samtidighet måste vara ett heltal mellan 1 och 50, till exempel 10.",
                                "en": "Concurrency must be a whole number from 1 to 50, for example 10."},
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
            "btn_pause": {"sv": "Pausa", "en": "Pause"},
            "lbl_template": {"sv": "Mall", "en": "Template"},
            "template_none": {"sv": "— Ingen mall —", "en": "— No template —"},
            "template_loaded": {"sv": "✓ Mall laddad: {}", "en": "✓ Template loaded: {}"},
            "template_not_found": {"sv": "Ingen sites.json hittades", "en": "No sites.json found"},
            "template_detached": {
                "sv": "Startadressen har bytt origin; mallens avancerade inställningar har kopplats bort.",
                "en": "The start URL changed origin; advanced template settings have been detached."},
            "browser_page_limit": {
                "sv": "Sidans storleksgräns överskreds i webbläsaren",
                "en": "The browser page exceeded the size limit"},
            "browser_dom_limit": {
                "sv": "Sidans DOM överskred storleksgränsen",
                "en": "The page DOM exceeded the size limit"},
            "private_network_explicit": {
                "sv": "⚠ Privata nätverksmål tillåts enligt konfigurationen",
                "en": "⚠ Private network targets are allowed by configuration"},
            "private_network_automatic": {
                "sv": "⚠ Automatiskt intranätläge aktiverat. Privata nätverksmål tillåts",
                "en": "⚠ Automatic intranet mode enabled. Private network targets are allowed"},
            "btn_resume": {"sv": "Fortsätt", "en": "Resume"},
            "btn_stop": {"sv": "Stoppa", "en": "Stop"},
            "col_status": {"sv": "Status", "en": "Status"},
            "col_title": {"sv": "Sidtitel", "en": "Page title"},
            "results_title": {"sv": "Resultat", "en": "Results"},
            "results_empty": {"sv": "Inga sidor eller dokument att visa.", "en": "No pages or documents to display."},
            "log_title": {"sv": "Körningslogg", "en": "Run log"},
            "log_empty": {"sv": "Här visas händelser och fel under körningen.",
                          "en": "Run events and errors appear here."},
            "status_wait": {"sv": "Redo att starta", "en": "Ready to start"},
            "status_starting": {"sv": "Startar körningen…", "en": "Starting the run…"},
            "stats_fmt": {
                "sv": "Besökta: {} | Sidor: {} | Dokument: {} | Cachade: {}\nI kö/aktiva: {} | Fel: {} | Konverteras/väntar: {} | Tid kvar: {}",
                "en": "Visited: {} | Pages: {} | Docs: {} | Cached: {}\nQueued/active: {} | Errors: {} | Converting/waiting: {} | ETA: {}"
            },
            'phase_exporting': {'sv': 'Förbereder leveransen…', 'en': 'Preparing the delivery…'},
            'phase_stopping': {'sv': 'Stoppar och slutför rapporten…', 'en': 'Stopping and finalizing the report…'},
            'confirm_close': {'sv': 'Vill du stoppa den pågående körningen och stänga när den har avslutats?',
                              'en': 'Stop the active run and close when it has finished?'},
            'confirm_unbounded': {'sv': 'Körningen kan följa länkar till andra domäner utan sid- eller djupgräns. Vill du starta?',
                                  'en': 'This run can follow links to other domains without page or depth limits. Start the run?'},
            'eta_texts': {'Beräknar...': 'Calculating…', 'Klar snart': 'Finishing soon'},
            'table_status_en': {
                'Hämtad (Standard)': 'Fetched (HTTP)', 'Hämtad (Webbläsare)': 'Fetched (browser)',
                'Oförändrad': 'Unchanged', 'Ej ändrad': 'Not modified', 'Borttagen': 'Removed',
                'Hittades inte': 'Not found', 'Fel': 'Error', 'Ingen titel': 'No title',
                'Blockerad av sajten': 'Blocked by site', 'Hoppar över språk': 'Skipping language',
                'Session utgången': 'Session expired', 'Duplikat': 'Duplicate',
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
                       "* Samtidighet: antal parallella sidor (standard 10). Fördröjningen begränsar hur ofta nästa anrop kan starta per domän.\n"
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
                       "crawl_report.json (luckor, fel och varningar) i utmappen.\n"
                       "Använd 'Öppna rapport' vid slutstatusen för att öppna körningsrapporten.\n"
                       "För en sammanhängande leverans, läs current.json en gång och använd den angivna generationen.\n\n"
                       "📋 DOKUMENT-MANIFEST\n-------------------------\n"
                       "När du laddar ner dokument (PDF m.m.) skapas också manifest_<domän>.json\n"
                       "i utmappen som listar varje dokument tillsammans med vilken intranät-\n"
                       "sida som hade länken till det.\n\n"
                       "📄 DOKUMENT → MARKDOWN\n-------------------------\n"
                       "Kryssa i 'Konvertera dokument till Markdown' så extraheras texten\n"
                       "ur PDF, Word, Excel och PowerPoint och sparas som .md-filer med\n"
                       "intranät-URL:en i toppen. Perfekt för RAG-system där\n"
                       "AI:n behöver se källan direkt i texten för att citera rätt.\n"
                       "Kräver: pip install PyMuPDF python-docx openpyxl python-pptx\n\n"
                       "💻 SERVER-LÄGE\n-------------------------\n"
                       "Körs via CMD för automatisering:\n"
                       "python ultimate-web-crawler.py --config sites.json [--output mapp]\n\n"
                       "💡 TIPS: Dubbelklicka på en rad i tabellen för att öppna länken!"),
                "en": ("⚙️ BASIC SETTINGS\n-------------------------\n"
                       "* Start URL: Where the crawler begins.\n"
                       "* Delay: Seconds to wait between requests (per domain).\n"
                       "* Concurrency: number of parallel pages (default 10). The delay limits how often requests can start per domain.\n"
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
                       "On re-runs, servers that support validators can respond '304 Not Modified'.\n"
                       "Use 'Open report' after a run to open its report.\n"
                       "For a consistent delivery, read current.json once and use the generation it names.\n\n"
                       "📋 DOCUMENT MANIFEST\n-------------------------\n"
                       "When documents (PDFs etc.) are downloaded, manifest_<domain>.json is also\n"
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
        scale = self.root._get_window_scaling()
        width = min(1000, max(720, int((screen_w - 40) / scale)))
        height = min(880, max(450, int((screen_h - 75) / scale)))
        x = max(0, (screen_w - round(width * scale)) // 2)
        y = max(0, (screen_h - round(height * scale)) // 3)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.minsize(720, 450)
        self.msg_queue = queue.Queue()
        self.crawler_instance: Optional[AsyncWebCrawler] = None
        self.crawl_thread: Optional[threading.Thread] = None
        self._last_summary: Optional[CrawlSummary] = None
        self._closing = False
        self._tooltip_win = None
        self._tooltip_keys = {}
        self.text_scale = 1.0
        self._scroll_update_pending = False
        self._scale_focus_job = None
        self._compact = False
        self._stats_wrap = None
        self._build_ui()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self.process_queue)

    # ─── Sparade inställningar ──────────────────────────────
    SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".ultimate_web_crawler_settings.json")

    def _collect_settings(self) -> Dict:
        self._template_options_for(self.url_entry.get().strip())
        return {
            "template": getattr(self, '_template_choice', None),
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
            # getattr: en ofullständigt byggd GUI får aldrig hindra att övriga inställningar sparas.
            "text_scale": getattr(self, "text_scale", 1.0),
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

        choice = st.get('template')
        site = getattr(self, 'templates', {}).get(choice) if isinstance(choice, str) else None
        if site and self._same_template_origin(st.get('url', ''), site.get('start_url', '')):
            self._apply_template(choice)
            if getattr(self, 'template_var', None) is not None:
                self.template_var.set(choice)

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
        if st.get("format") in (".json", ".md", ".txt", "Ingen text", "No text"):
            value = st['format']
            self.format_var.set(('Ingen text' if self.lang == 'sv' else 'No text')
                                if value in ('Ingen text', 'No text') else value)
        if st.get("mode") in self.texts["run_modes"]:
            self.headless_var.set(self.texts["run_modes"][st["mode"]][self.lang])
        if isinstance(st.get("dir"), str) and st["dir"]:
            self.dir_var.set(st["dir"])
        if st.get("theme") == "Dark":
            self.theme_switch.select()
            self.change_appearance_mode_event()
        if st.get("lang") == "en":
            self.lang_var.set("EN")
            self.change_language_event("EN")
        scale = st.get("text_scale")
        if (isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale in self.TEXT_SCALES
                and scale != getattr(self, "text_scale", 1.0) and hasattr(self, "text_size_menu")):
            self._apply_text_scale(float(scale))

    # ─── Typografi och skalning ─────────────────────────────
    def _setup_typography(self):
        """Välj gemensam font innan första kontrollen skapas och koppla ttk till samma skalning."""
        family = self.choose_ui_font_family(
            set(tkfont.families(self.root)), self.UI_FONT_FAMILY, CTK_THEME_FONT["family"])
        size, weight = self.TEXT_ROLES["body"]
        # CustomTkinter-kontroller utan uttrycklig font använder temats standardfont.
        ctk.ThemeManager.theme["CTkFont"].update(family=family, size=size, weight=weight)
        self.ui_font_family = family
        self.fonts = {role: ctk.CTkFont(family=family, size=role_size, weight=role_weight)
                      for role, (role_size, role_weight) in self.TEXT_ROLES.items()}
        # CustomTkinter skalar bara sina egna kontroller. ttk-tabellen och Tk-etiketter får
        # namngivna fonter i pixlar som räknas om från samma faktor som CustomTkinters text.
        self._ui_scale = ctk.ScalingTracker.get_widget_scaling(self.root)
        self.tk_fonts = {
            role: tkfont.Font(root=self.root, family=family,
                              size=-round(role_size * self._ui_scale), weight=role_weight)
            for role, (role_size, role_weight) in self.TEXT_ROLES.items()
        }
        # Anropas vid DPI-byte (t.ex. flytt till en annan skärm) och vid ändrad textstorlek.
        ctk.ScalingTracker.add_widget(self._on_ui_scaling, self.root)

    def _on_ui_scaling(self, widget_scaling, _window_scaling):
        """Räkna om tabellens fonter, radhöjd, kolumnbredder och delare efter ny skalning."""
        old_scale, self._ui_scale = self._ui_scale, widget_scaling
        try:
            for role, font in self.tk_fonts.items():
                font.configure(size=-round(self.TEXT_ROLES[role][0] * widget_scaling))
            self._update_treeview_style(ctk.get_appearance_mode())
            tree = getattr(self, "tree", None)
            if tree is not None and old_scale != widget_scaling:
                for column in self.TREE_COLUMN_WIDTHS:
                    # Behåll användarens egen kolumnbredd, räknad i logiska pixlar.
                    logical_width = tree.column(column, "width") / old_scale
                    tree.column(column, width=round(logical_width * widget_scaling))
            if getattr(self, "_focus_ring_target", None) is not None:
                self._show_focus_ring(self._focus_ring_target)
            if getattr(self, "_scroll_canvas", None) is not None:
                self._schedule_scroll_update()
                if old_scale != widget_scaling:
                    # Vänta tills Tk har placerat om kontrollerna och rullningslisterna.
                    if self._scale_focus_job is not None:
                        self.root.after_cancel(self._scale_focus_job)
                    self._scale_focus_job = self.root.after(40, self._restore_focus_after_scaling)
        except tk.TclError:
            pass  # fönstret håller på att stängas

    # ─── Tangentbord och fokus ──────────────────────────────
    def _setup_keyboard(self):
        """Gör reglagen nåbara med Tab, användbara med tangentbordet och synligt fokuserade."""
        # Tk-kontrollen som får fokus (sökväg) -> kontrollen som får fokusramen.
        self._focus_targets = {}
        self._focus_ring_target = None
        # Fyra smala ramar ovanpå fönstret ritar fokusramen utan att flytta layouten.
        self._focus_ring = [tk.Frame(self.root, borderwidth=0, highlightthickness=0, takefocus=0)
                            for _ in range(4)]

        buttons = (self.help_btn, self.btn_folder, self.start_btn, self.pause_btn, self.stop_btn,
                   self.open_btn, self.report_btn)
        for button in buttons:
            self._make_focusable(button, activate=button.invoke)
        for checkbox in (self.cb_docs, self.cb_convert_docs, self.cb_hybrid, self.cb_traf, self.cb_sitemap,
                         self.cb_robots, self.cb_strict, self.cb_full, self.cb_rm_email, self.cb_rm_phone,
                         self.cb_rm_pnr, self.cb_rm_ip, self.theme_switch):
            self._make_focusable(checkbox, activate=checkbox.toggle)
        for menu in (getattr(self, "template_menu", None), self.text_size_menu, self.format_menu,
                     self.headless_menu):
            if menu is not None:
                self._make_focusable(menu, activate=menu._open_dropdown_menu,
                                     step=lambda direction, m=menu: self._step_option_menu(m, direction))
        for segmented in (self.tabview._segmented_button, self.lang_switch):
            self._make_focusable(segmented, step=lambda direction, s=segmented: self._step_segmented(s, direction))
            self._mark_selected_segment(segmented)

        # Textstorlek med Ctrl+Plus, Ctrl+Minus och Ctrl+0, som i webbläsare.
        for key in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            tk.Misc.bind_all(self.root, key, lambda _event: self._step_text_scale(1), add="+")
        for key in ("<Control-minus>", "<Control-KP_Subtract>"):
            tk.Misc.bind_all(self.root, key, lambda _event: self._step_text_scale(-1), add="+")
        for key in ("<Control-Key-0>", "<Control-KP_0>"):
            tk.Misc.bind_all(self.root, key, lambda _event: (self._apply_text_scale(1.0), "break")[1], add="+")

        # Fält, tabell och logg tar redan fokus i Tk. De registreras bara för fokusramen.
        for entry in (self.url_entry, self.delay_entry, self.max_pages_entry, self.max_depth_entry,
                      self.concurrency_entry, self.dir_entry, self.exclude_entry, self.require_entry,
                      self.lang_filter_entry):
            self._focus_targets[str(entry._entry)] = entry
        self._focus_targets[str(self.tree)] = self.tree
        self._focus_targets[str(self.log_area._textbox)] = self.log_area

        # Tabbordningen följer staplingsordningen: ställ den i samma ordning som fönstret läses.
        # Full omcrawl skapas efter fälten men står överst till höger på fliken Avancerat.
        self.cb_full.lift(self.cb_traf)

        tk.Misc.bind_all(self.root, "<FocusIn>", self._on_focus_in, add="+")
        tk.Misc.bind_all(self.root, "<FocusOut>", self._on_focus_out, add="+")
        tk.Misc.bind_all(self.root, "<Escape>", lambda _event: self._hide_tooltip(), add="+")

    def _make_focusable(self, widget, activate=None, step=None):
        """Ge ett CustomTkinter-reglage plats i tabbordningen och tangentbindningar."""
        def can_take_focus(_path):
            # Ett svar från funktionen ersätter Tk:s egen kontroll, så dolda flikar måste uteslutas här.
            try:
                if not widget.winfo_viewable():
                    return 0
            except tk.TclError:
                return 0
            try:
                return 0 if widget.cget("state") == "disabled" else 1
            except (tk.TclError, ValueError):
                return 1  # reglaget saknar inställningen state och är alltid aktivt
        # CustomTkinter-reglagen är ramar. Bindningarna läggs på själva ramen, som får fokus.
        tk.Frame.configure(widget, takefocus=can_take_focus)
        self._focus_targets[str(widget)] = widget
        if activate is not None:
            for key in ("<space>", "<Return>", "<KP_Enter>"):
                tk.Misc.bind(widget, key, lambda _event: (activate(), "break")[1])
        if step is not None:
            for key, direction in (("<Left>", -1), ("<Up>", -1), ("<Right>", 1), ("<Down>", 1)):
                tk.Misc.bind(widget, key, lambda _event, d=direction: (step(d), "break")[1])
            # Alt+Pil ned öppnar en lista, som i Windows.
            if activate is not None:
                tk.Misc.bind(widget, "<Alt-Down>", lambda _event: (activate(), "break")[1])

    @staticmethod
    def _neighbour(values, current, direction):
        index = values.index(current) if current in values else 0
        return values[max(0, min(len(values) - 1, index + direction))]

    def _step_option_menu(self, menu, direction):
        values = list(menu.cget("values"))
        new = self._neighbour(values, menu.get(), direction)
        if values and new != menu.get():
            # Samma väg som ett val i listan, så att eventuell callback också körs.
            menu._dropdown_callback(new)

    def _step_segmented(self, segmented, direction):
        values = list(segmented.cget("values"))
        new = self._neighbour(values, segmented.get(), direction)
        if values and new != segmented.get():
            segmented.set(new, from_button_callback=True)

    def _mark_selected_segment(self, segmented):
        """Valt läge får egen textfärg mot den starkare fyllningen (WCAG 1.4.11)."""
        select, unselect = segmented._select_button_by_value, segmented._unselect_button_by_value

        def selected(value):
            select(value)
            segmented._buttons_dict[value].configure(text_color=self.SEGMENT_SELECTED_TEXT)

        def unselected(value):
            unselect(value)
            if value in segmented._buttons_dict:
                segmented._buttons_dict[value].configure(text_color=self.STATUS_COLOR)

        segmented._select_button_by_value = selected
        segmented._unselect_button_by_value = unselected
        if segmented.get() in segmented._buttons_dict:
            selected(segmented.get())

    def _on_focus_in(self, event):
        target = self._focus_targets.get(str(event.widget))
        if target is None:
            return
        self._scroll_into_view(target)
        self._show_focus_ring(target)
        key = self._tooltip_keys.get(target)
        if key is not None:
            # Hjälp som visas vid hovring ska också nås från tangentbordet.
            self._show_tooltip(target, key)

    def _on_focus_out(self, event):
        target = self._focus_targets.get(str(event.widget))
        if target is not None and target is self._focus_ring_target:
            self._hide_focus_ring()
            if self._tooltip_keys.get(target) is not None:
                self._hide_tooltip()

    def _show_focus_ring(self, target):
        dark = ctk.get_appearance_mode() == "Dark"
        width = max(1, round(self.FOCUS_RING_WIDTH * self._ui_scale))
        offset = round(self.FOCUS_RING_GAP * self._ui_scale) + width
        top, bottom, left, right = self._focus_ring
        try:
            for part in self._focus_ring:
                part.configure(background=self.FOCUS_RING[dark])
            top.place(in_=target, x=-offset, y=-offset, relwidth=1, width=2 * offset, height=width)
            bottom.place(in_=target, x=-offset, rely=1, y=offset - width, relwidth=1, width=2 * offset,
                         height=width)
            left.place(in_=target, x=-offset, y=-offset, relheight=1, height=2 * offset, width=width)
            right.place(in_=target, relx=1, x=offset - width, y=-offset, relheight=1, height=2 * offset,
                        width=width)
            for part in self._focus_ring:
                part.lift()
        except tk.TclError:
            return
        self._focus_ring_target = target

    def _hide_focus_ring(self):
        for part in getattr(self, "_focus_ring", ()):
            part.place_forget()
        self._focus_ring_target = None

    def _selected_result_url(self) -> Optional[str]:
        selection = self.tree.selection()
        if not selection:
            return None
        url = str(self.tree.item(selection[0])['values'][0])
        return url if url.startswith("http") else None

    def _open_selected_result(self):
        url = self._selected_result_url()
        if url:
            webbrowser.open(url)
        return "break"

    def _copy_selected_result_url(self):
        url = self._selected_result_url()
        if url:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
        return "break"

    def _focus_first_result(self):
        """Ge tabellen en aktuell rad när den får fokus, så att piltangenterna fungerar direkt."""
        rows = self.tree.get_children()
        if rows and not self.tree.focus():
            self.tree.focus(rows[0])
            if not self.tree.selection():
                self.tree.selection_set(rows[0])

    # ─── Snabbmenyer ────────────────────────────────────────
    def _build_context_menus(self):
        """Snabbmenyer för resultat och logg: högerklick, Menytangenten eller Skift+F10, som i Windows.
        De innehåller samma åtgärder som tangenterna, så att åtgärderna också syns."""
        self.result_menu = tk.Menu(self.root, tearoff=0)
        self.result_menu.add_command(command=self._open_selected_result, accelerator="Enter")
        self.result_menu.add_command(command=self._copy_selected_result_url, accelerator="Ctrl+C")
        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(command=self._copy_log_selection, accelerator="Ctrl+C")
        self.log_menu.add_command(command=self._select_all_log, accelerator="Ctrl+A")
        self._label_context_menus()
        text = self.log_area._textbox
        self.tree.bind("<Button-3>", self._show_result_menu_at_pointer)
        text.bind("<Button-3>", lambda event: self._show_log_menu(event.x_root, event.y_root))
        for key in ("<Shift-F10>", "<App>"):
            self.tree.bind(key, lambda _event: self._show_result_menu_at_row())
            text.bind(key, lambda _event: self._show_log_menu_at_cursor())
        for key in ("<Control-a>", "<Control-A>"):
            text.bind(key, lambda _event: self._select_all_log())

    def _label_context_menus(self):
        for menu, keys in ((self.result_menu, ("menu_open_page", "menu_copy_url")),
                           (self.log_menu, ("menu_copy", "menu_select_all"))):
            for index, key in enumerate(keys):
                menu.entryconfigure(index, label=self.texts[key][self.lang])

    def _popup(self, menu, x, y):
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        return "break"

    def _show_result_menu(self, x, y):
        state = "normal" if self._selected_result_url() else "disabled"
        for index in (0, 1):
            self.result_menu.entryconfigure(index, state=state)
        return self._popup(self.result_menu, x, y)

    def _show_result_menu_at_pointer(self, event):
        # Högerklick väljer raden under pekaren först, som i Utforskaren.
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
        return self._show_result_menu(event.x_root, event.y_root)

    def _show_result_menu_at_row(self):
        row = self.tree.focus() or next(iter(self.tree.selection()), "")
        box = self.tree.bbox(row) if row else ""
        x = self.tree.winfo_rootx() + (box[0] + round(16 * self._ui_scale) if box else 0)
        y = self.tree.winfo_rooty() + (box[1] + box[3] if box else 0)
        return self._show_result_menu(x, y)

    def _show_log_menu(self, x, y):
        has_selection = bool(self.log_area._textbox.tag_ranges("sel"))
        self.log_menu.entryconfigure(0, state="normal" if has_selection else "disabled")
        return self._popup(self.log_menu, x, y)

    def _show_log_menu_at_cursor(self):
        text = self.log_area._textbox
        box = text.bbox("insert") or (0, 0, 0, 0)
        return self._show_log_menu(text.winfo_rootx() + box[0], text.winfo_rooty() + box[1] + box[3])

    def _copy_log_selection(self):
        text = self.log_area._textbox
        if text.tag_ranges("sel"):
            self.root.clipboard_clear()
            self.root.clipboard_append(text.get("sel.first", "sel.last"))
        return "break"

    def _select_all_log(self):
        text = self.log_area._textbox
        text.tag_add("sel", "1.0", "end-1c")
        return "break"

    # ─── Tooltips ───────────────────────────────────────────
    def _add_tooltip(self, widget, key: str):
        self._tooltip_keys[widget] = key
        widget.bind("<Enter>", lambda _event: self._show_tooltip(widget, key), add="+")
        widget.bind("<Leave>", lambda _event: self._hide_tooltip(), add="+")

    def _show_tooltip(self, widget, key: str):
        self._hide_tooltip()
        dark = ctk.get_appearance_mode() == "Dark"
        scale = self._ui_scale
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        x = widget.winfo_rootx() + round(20 * scale)
        # Under fokusramen, så att tipset aldrig döljer den fokuserade kontrollen.
        y = widget.winfo_rooty() + widget.winfo_height() + round(8 * scale)
        win.wm_geometry(f"+{x}+{y}")
        # Ramens bakgrund blir tipsets kant, med samma kontrast som fältkanterna.
        border = max(1, round(scale))
        frame = tk.Frame(win, background=self.FIELD_BORDER[dark], padx=border, pady=border)
        frame.pack()
        tk.Label(frame, text=self.texts["tips"][key][self.lang], justify=tk.LEFT,
                 background=self.TOOLTIP_SURFACE[dark], foreground=self.STATUS_COLOR[dark],
                 font=self.tk_fonts["body"], wraplength=round(380 * scale),
                 padx=round(8 * scale), pady=round(5 * scale)).pack()
        self._tooltip_win = win

    def _hide_tooltip(self):
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _on_closing(self):
        if self._closing:
            return
        if self.crawl_thread and self.crawl_thread.is_alive():
            if not messagebox.askyesno(self.texts['window_title'][self.lang], self.texts['confirm_close'][self.lang]):
                return
            self._closing = True
            self._save_settings()
            self.stop_crawl()
            self._close_when_crawl_finishes()
            return
        self._save_settings()
        self.root.destroy()

    def _close_when_crawl_finishes(self):
        if self.crawl_thread and self.crawl_thread.is_alive():
            self.root.after(100, self._close_when_crawl_finishes)
        else:
            self.root.destroy()

    @staticmethod
    def _open_local_path(path):
        if sys.platform.startswith("win"):
            os.startfile(path)             # noqa: S606 — användarens egen utdata
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_output_folder(self):
        folder = self.dir_var.get()
        if not os.path.isdir(folder):
            return
        try:
            self._open_local_path(folder)
        except Exception as e:
            self._log_to_gui(f"Kunde inte öppna mappen: {e}")

    def open_report(self):
        path = self._last_summary.report_path if self._last_summary else None
        if not path or not os.path.isfile(path):
            self.report_btn.configure(state="disabled")
            self._log_to_gui(self.texts["report_missing"][self.lang])
            return
        try:
            self._open_local_path(path)
        except OSError as error:
            self._log_to_gui(self.texts["report_open_error"][self.lang].format(error))

    def _render_summary(self, summary: CrawlSummary) -> str:
        text = summary.render(self.lang, self.texts["run_summary"])
        self.stats_label.configure(text=text, text_color=self.RUN_STATUS_COLORS[summary.status])
        self.report_btn.configure(state="normal" if summary.report_path and os.path.isfile(summary.report_path)
                                  else "disabled")
        return text

    def change_appearance_mode_event(self):
        mode = "Dark" if self.theme_switch.get() == 1 else "Light"
        ctk.set_appearance_mode(mode)
        self._update_treeview_style(mode)
        if getattr(self, "_focus_ring_target", None) is not None:
            self._show_focus_ring(self._focus_ring_target)   # fokusramens färg följer temat

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
        # rename() lämnar flikknappen utan valt läge. Markera bara knappen igen: tabview.set()
        # skulle dölja andra flikar efter 100 ms och kunna dölja en flik som väljs under tiden.
        self.tabview._segmented_button.set(self.tabview.get())

        self.lang = new_lang
        self.root.title(self.texts["window_title"][self.lang])

        self.lbl_url.configure(text=self.texts["lbl_url"][self.lang])
        self.help_btn.configure(text=self.texts["btn_help"][self.lang])
        self.theme_switch.configure(text=self.texts["dark_mode"][self.lang])
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
        self.report_btn.configure(text=self.texts["btn_report"][self.lang])
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
        for key, label in self.output_labels.items():
            label.configure(text=self.texts[key][self.lang])
        if self._last_summary:
            self._render_summary(self._last_summary)
        elif not self.crawler_instance or self.crawler_instance.state in (
            CrawlerState.IDLE, CrawlerState.STOPPED
        ):
            self.stats_label.configure(text=self.texts["status_wait"][self.lang])

        self.headless_menu.configure(values=[v[self.lang] for v in self.texts["run_modes"].values()])
        self.headless_var.set(self.texts["run_modes"][internal_mode][self.lang])

        format_vals = [".json", ".md", ".txt", "Ingen text" if self.lang == "sv" else "No text"]
        self.format_menu.configure(values=format_vals)
        if self.format_var.get() not in format_vals:
            self.format_var.set(format_vals[-1])

        self.text_size_menu.configure(values=[self._text_scale_label(scale) for scale in self.TEXT_SCALES])
        self.text_size_menu.set(self._text_scale_label(self.text_scale))
        self._label_context_menus()
        # Nya texter ändrar bredden utan att delarnas höjd ändras, så rullytan räknas om här, och
        # en gång till när reglagen som krymper har ritats om.
        self._schedule_scroll_update()
        self.root.after(200, self._schedule_scroll_update)

    def _update_treeview_style(self, mode):
        style = ttk.Style(self.root)
        style.theme_use("default")
        dark = mode == "Dark"
        surface = self.PANEL_SURFACE[dark]
        # Samma faktor som CustomTkinters text, inte fönsterskalningen (se _on_ui_scaling).
        scale = self._ui_scale
        body = self.tk_fonts["body"]
        style.configure("Treeview", background=surface, fieldbackground=surface,
                        foreground=self.STATUS_COLOR[dark], borderwidth=0,
                        font=body, rowheight=body.metrics("linespace") + round(10 * scale))
        style.configure("Treeview.Heading", background="#343e49" if dark else "#edf2f7",
                        foreground=self.MUTED_TEXT[dark], font=self.tk_fonts["column_heading"],
                        padding=(round(8 * scale), round(5 * scale)), relief="flat")
        style.map("Treeview", background=[('selected', self.ACCENT[dark])], foreground=[('selected', 'white')])
        style.map("Treeview.Heading", background=[('active', self.CONTROL_HOVER[dark])])
        style.configure("TPanedwindow", background="#353d46" if dark else "#d9e0e7")
        style.configure("Horizontal.Sash", background="#7f8c9b", sashthickness=round(self.SASH_HEIGHT * scale))
        if getattr(self, "_scroll_canvas", None) is not None:
            self._scroll_canvas.configure(background=self.WINDOW_SURFACE[dark])

    def open_help_window(self):
        help_win = ctk.CTkToplevel(self.root)
        help_win.title(self.texts["help_title"][self.lang])
        help_win.geometry("600x600")
        help_text = ctk.CTkTextbox(help_win, wrap=tk.WORD, font=self.fonts["body"])
        help_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        help_text.insert(tk.END, self.texts["help_content"][self.lang])
        help_text.configure(state="disabled")
        # Texten kan läsas och rullas med tangentbordet. Esc stänger och fokus går tillbaka till Hjälp.
        help_text._textbox.configure(takefocus=1)

        def close(_event=None):
            help_win.destroy()
            # CTkButton.focus_set() fokuserar den inre etiketten; ramen bär tangentbindningarna.
            tk.Misc.focus_set(self.help_btn)

        tk.Misc.bind(help_win, "<Escape>", close)
        help_win.protocol("WM_DELETE_WINDOW", close)
        # CustomTkinter döljer och visar fönstret kort för titelradens färg. Fokus som sätts medan
        # fönstret är dolt kan gå förlorat, så texten fokuseras varje gång fönstret visas.
        # CustomTkinter fokuserar själva fönstret 10 ms efter att det visats igen, så texten fokuseras
        # strax efter det, och varje gång fönstret självt får fokus.
        def focus_text_soon(event):
            if event.widget is help_win:
                help_win.after(60, help_text._textbox.focus_set)

        def focus_text_now(event):
            if event.widget is help_win:
                help_text._textbox.focus_set()

        tk.Misc.bind(help_win, "<Map>", focus_text_soon, add="+")
        tk.Misc.bind(help_win, "<FocusIn>", focus_text_now, add="+")
        help_text._textbox.focus_set()

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
            self._template_options = {}
            self._template_choice = None
            self._template_start_url = ''
            return

        site = self.templates.get(choice)
        if not site:
            return
        self._template_options = {key: value for key, value in site.items() if key in KNOWN_CONFIG_KEYS}
        self._template_choice = choice
        self._template_start_url = site.get('start_url', '')

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
            value = site['save_format']
            self.format_var.set(('Ingen text' if self.lang == 'sv' else 'No text')
                                if value in ('Ingen text', 'No text') else value)
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

        if site.get('output_dir'):
            self.dir_var.set(site['output_dir'])
        elif site.get("name"):
            base_dir = os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output")
            safe_folder_name = slugify(site["name"])
            self.dir_var.set(os.path.join(base_dir, safe_folder_name))

        self._log_to_gui(self.texts["template_loaded"][self.lang].format(choice))

    @staticmethod
    def _same_template_origin(url: str, template_url: str) -> bool:
        def origin(value):
            if not value:
                return None
            parsed = urlparse(value if '://' in value else 'https://' + value)
            return (parsed.scheme.lower(), (parsed.hostname or '').lower().rstrip('.'),
                    parsed.port or (443 if parsed.scheme == 'https' else 80))
        try:
            return bool(origin(url)) and origin(url) == origin(template_url)
        except ValueError:
            return False

    def _template_options_for(self, url: str) -> Dict:
        options = getattr(self, '_template_options', {})
        if options and not self._same_template_origin(url, getattr(self, '_template_start_url', '')):
            self._template_options = {}
            self._template_choice = None
            if getattr(self, 'template_var', None) is not None:
                self.template_var.set(self.texts['template_none'][self.lang])
            self._log_to_gui(self.texts['template_detached'][self.lang])
            return {}
        return dict(options)

    def _log_to_gui(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.output_labels["log_empty"].place_forget()
        # Följ med nya rader bara när loggen redan visar slutet, som i ett terminalfönster. Den som
        # har rullat upp för att läsa en äldre rad blir kvar där.
        textbox = self.log_area._textbox
        # En loggpost kan ta flera visuella rader. Följ bara om postens slut syns.
        following = textbox.compare("end-1c", "==", "1.0") or textbox.dlineinfo("end-2c") is not None
        # Loggen är skrivskyddad för användaren och öppnas bara medan appen skriver.
        self.log_area.configure(state="normal")
        self.log_area.insert(tk.END, f"[{t}] {redact_diagnostic_urls(msg)}\n")
        if following:
            # Visa senaste textraden, inte den tomma raden efter den avslutande radbrytningen.
            self.log_area.see("end-2c")
        if int(self.log_area.index('end-1c').split('.')[0]) > 500:
            self.log_area.delete("1.0", "2.0")
        self.log_area.configure(state="disabled")

    def _output_panel(self, key):
        frame = ctk.CTkFrame(self.results_pane, fg_color=self.PANEL_SURFACE, bg_color=self.WINDOW_SURFACE,
                             border_color=self.PANEL_BORDER, border_width=1, corner_radius=8)
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill=tk.X, padx=12, pady=(5, 3))
        title = ctk.CTkLabel(header, text=self.texts[key][self.lang], height=22,
                             text_color=self.STATUS_COLOR, font=self.fonts["panel_title"])
        title.pack(side=tk.LEFT)
        self.output_labels[key] = title
        return frame, header

    # ─── Rullyta, textstorlek och loggens storlek ────────────
    def _build_scroll_area(self):
        """Rullyta för hela innehållet. Rullningslisterna syns bara när innehållet inte får plats,
        till exempel vid stor text i ett litet fönster. Annars fyller innehållet fönstret som förut."""
        viewport = ctk.CTkFrame(self.root, fg_color=self.WINDOW_SURFACE, corner_radius=0)
        viewport.pack(fill=tk.BOTH, expand=True)
        viewport.grid_rowconfigure(0, weight=1)
        viewport.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0, takefocus=0,
                           background=self.WINDOW_SURFACE[0])
        canvas.grid(row=0, column=0, sticky="nsew")
        # Varje list ligger i en vanlig Tk-ram som visas och döljs. CustomTkinter upprepar annars sina
        # egna geometrianrop vid skalningsbyte, och en dold list kan då dyka upp igen.
        self._vbar_box = tk.Frame(viewport, borderwidth=0, highlightthickness=0, takefocus=0)
        self._hbar_box = tk.Frame(viewport, borderwidth=0, highlightthickness=0, takefocus=0)
        self._vbar = ctk.CTkScrollbar(self._vbar_box, orientation="vertical", command=canvas.yview)
        self._hbar = ctk.CTkScrollbar(self._hbar_box, orientation="horizontal", command=canvas.xview)
        self._vbar.pack(fill=tk.BOTH, expand=True)
        self._hbar.pack(fill=tk.BOTH, expand=True)
        canvas.configure(yscrollcommand=self._vbar.set, xscrollcommand=self._hbar.set)
        content = ctk.CTkFrame(canvas, fg_color=self.WINDOW_SURFACE, bg_color=self.WINDOW_SURFACE,
                               corner_radius=0)
        self._scroll_window = canvas.create_window(0, 0, window=content, anchor="nw")
        self._scroll_canvas, self._scroll_content, self._viewport = canvas, content, viewport
        self._scroll_state = None
        # Ytterytan ändras bara med fönstret, inte när en rullningslist visas eller döljs.
        tk.Misc.bind(viewport, "<Configure>", self._schedule_scroll_update, add="+")
        tk.Misc.bind_all(self.root, "<MouseWheel>", self._on_mousewheel, add="+")
        tk.Misc.bind_all(self.root, "<Shift-MouseWheel>",
                         lambda event: self._on_mousewheel(event, horizontal=True), add="+")
        return content

    def _schedule_scroll_update(self, _event=None):
        if not self._scroll_update_pending:
            self._scroll_update_pending = True
            self.root.after_idle(self._update_scroll_area)

    def _update_scroll_area(self):
        self._scroll_update_pending = False
        canvas, content = self._scroll_canvas, self._scroll_content
        if getattr(self, "results_pane", None) is None:
            return  # fönstret byggs fortfarande; ett senare <Configure> gör beräkningen
        try:
            # Räkna från ytterytan, som inte ändras av rullningslisterna. Annars kan en list som
            # visas göra ytan smalare, så att den andra listen visas, och så vidare.
            width, height = self._viewport.winfo_width(), self._viewport.winfo_height()
            if width <= 1 or height <= 1:
                return
            scale = self._ui_scale
            bar_size = max(self._vbar_box.winfo_reqwidth(), self._hbar_box.winfo_reqheight())
            # Körstatusen bryts efter fönstrets bredd i stället för en fast bredd.
            wrap = max(200, round((width - 48 * scale) / scale))
            if wrap != self._stats_wrap:
                self._stats_wrap = wrap
                self.stats_label.configure(wraplength=wrap)
            # Båda panelernas innehåll behöver plats även i fönstrets minsta läge.
            other_height = content.winfo_reqheight() - self.results_pane.winfo_reqheight()
            need_height = other_height + sum(round(part * scale) for part in
                                            (self.MIN_TABLE_HEIGHT, self.MIN_LOG_HEIGHT, self.SASH_HEIGHT))
            need_width = max(part.winfo_reqwidth() for part in self._main_frame.pack_slaves()
                             if part is not self.results_pane) + 2 * round(self.CONTENT_PAD_X * scale)
            # Kompakt läge när det breda läget inte ryms.
            compact = self._wide_layout_width() > width
            if compact != self._compact:
                self._set_compact(compact)
                self._scroll_state = None
                self._schedule_scroll_update()
                return
            vertical = need_height > height - (bar_size if need_width > width else 0)
            view_width = width - (bar_size if vertical else 0)
            horizontal = need_width > view_width
            view_height = height - (bar_size if horizontal else 0)
            if vertical:
                need_height = other_height + round(self.RESULTS_HEIGHT_WHEN_SCROLLING * scale)
            state = (max(view_width, need_width), max(view_height, need_height), vertical, horizontal)
            if state != self._scroll_state:
                self._scroll_state = state
                inner_width, inner_height = state[:2]
                canvas.itemconfigure(self._scroll_window, width=inner_width, height=inner_height)
                canvas.configure(scrollregion=(0, 0, inner_width, inner_height),
                                 yscrollincrement=round(20 * scale), xscrollincrement=round(20 * scale))
                for box, needed, grid in ((self._vbar_box, vertical, dict(row=0, column=1, sticky="ns")),
                                          (self._hbar_box, horizontal, dict(row=1, column=0, sticky="ew"))):
                    if needed:
                        box.grid(**grid)
                    else:
                        box.grid_remove()
        except tk.TclError:
            pass  # fönstret håller på att stängas

    def _restore_focus_after_scaling(self):
        self._scale_focus_job = None
        if self._scroll_update_pending:
            self._scale_focus_job = self.root.after(40, self._restore_focus_after_scaling)
            return
        target = self._focus_ring_target
        if target is not None and target.winfo_viewable():
            self._scroll_into_view(target)

    def _keep_output_panels_visible(self, _event=None):
        """Lämna plats för innehåll i båda panelerna när delaren flyttas."""
        pane, scale = self.results_pane, self._ui_scale
        first = round(self.MIN_TABLE_HEIGHT * scale)
        last = pane.winfo_height() - round(self.MIN_LOG_HEIGHT * scale) - round(self.SASH_HEIGHT * scale)
        if last >= first:
            position = pane.sashpos(0)
            if not first <= position <= last:
                pane.sashpos(0, max(first, min(position, last)))

    def _wide_layout_width(self) -> int:
        """Bredden i pixlar som det breda läget behöver. Den räknas från reglagens egna önskade
        bredder, som inte beror på vilket läge som visas, så att beslutet aldrig fastnar i ett läge."""
        scale = self._ui_scale

        def width(*widgets):
            return max(widget.winfo_reqwidth() for widget in widgets)

        def pad(value):
            return round(value * scale)

        top = width(self.lbl_url) + pad(10) + width(self.url_entry) + width(self._prefs_frame) + pad(24)
        buttons = (width(self.start_btn) + width(self.pause_btn) + width(self.stop_btn)
                   + width(self.open_btn) + width(self.report_btn) + pad(18))
        basic = (width(self.lbl_delay, self.lbl_max_depth, self.lbl_concurrency) + pad(15)
                 + width(self.delay_entry, self.max_depth_entry, self.concurrency_entry) + pad(10)
                 + width(self.lbl_max_pages, self.lbl_format, self.lbl_mode) + pad(15)
                 + width(self.max_pages_entry, self.format_menu, self.headless_menu) + pad(10))
        documents = width(self.cb_docs) + width(self.cb_convert_docs) + pad(36)
        advanced = (width(self.cb_hybrid, self.cb_sitemap) + width(self.cb_traf, self.cb_robots)
                    + width(self.cb_full, self.cb_strict) + pad(60))
        privacy = sum(width(box) for box in (self.cb_rm_email, self.cb_rm_phone, self.cb_rm_pnr,
                                             self.cb_rm_ip)) + pad(68)
        # Flikvyns egen kant och marginal runt flikens innehåll. Vid flikbyte ligger båda flikarna
        # kvar i 100 ms, så den bredaste placerade fliken används.
        tabs_shown = [self.tabview.tab(self.texts[key][self.lang]) for key in ("tab_basic", "tab_adv")]
        widest_tab = max((tab.winfo_reqwidth() for tab in tabs_shown if tab.winfo_manager()), default=0)
        tab_overhead = max(0, self.tabview.winfo_reqwidth() - widest_tab)
        tabs = max(basic, documents, advanced, privacy) + tab_overhead
        return max(top, buttons, tabs) + 2 * pad(self.CONTENT_PAD_X)

    def _set_compact(self, compact: bool):
        """Kompakt layout när det breda läget inte får plats, till exempel vid stor text: inställningarna
        i en kolumn, och Hjälp/textstorlek/tema/språk och utdataknapparna på egna rader."""
        self._compact = compact
        # pack() behåller tidigare alternativ som inte anges, till exempel marginaler från det andra
        # läget. Börja därför från noll.
        self._prefs_frame.pack_forget()
        self._output_buttons.pack_forget()
        if compact:
            self._prefs_frame.pack(in_=self._source_panel, fill=tk.X, padx=12, pady=(0, 7), after=self._url_frame)
            self._output_buttons.pack(in_=self._main_frame, anchor="w", pady=(0, 3), after=self._btn_frame)
        else:
            self._prefs_frame.pack(in_=self._url_frame, side=tk.RIGHT, before=self.lbl_url)
            self._output_buttons.pack(in_=self._btn_frame, side=tk.RIGHT)
        self._grid_basic_settings(compact)
        self._grid_advanced_settings(compact)
        # Mätningen kan ske mitt i en omritning (språk- eller flikbyte). Pröva läget igen när den är klar.
        self.root.after(300, self._recheck_layout)

    def _recheck_layout(self):
        self._scroll_state = None
        self._schedule_scroll_update()

    def _grid_basic_settings(self, compact: bool):
        pairs = ((self.lbl_delay, self.delay_entry), (self.lbl_max_pages, self.max_pages_entry),
                 (self.lbl_max_depth, self.max_depth_entry), (self.lbl_format, self.format_menu),
                 (self.lbl_concurrency, self.concurrency_entry), (self.lbl_mode, self.headless_menu))
        for index, (label, field) in enumerate(pairs):
            row, column = (index, 0) if compact else divmod(index, 2)
            column = 0 if compact else column * 2
            label.grid(row=row, column=column, padx=(10, 5), pady=3, sticky="e")
            field.grid(row=row, column=column + 1, padx=(0, 10), pady=3, sticky="w")
        rows, span = (len(pairs), 2) if compact else (3, 4)
        self._documents_row.grid(row=rows, column=0, columnspan=span, padx=10, pady=3, sticky="w")
        if compact:
            self.cb_convert_docs.grid(row=1, column=0, pady=(6, 0), sticky="w")
        else:
            self.cb_convert_docs.grid(row=0, column=1, pady=0, sticky="w")
        self.lbl_folder.grid(row=rows + 1, column=0, padx=(10, 5), pady=3, sticky="e")
        self._folder_row.grid(row=rows + 1, column=1, columnspan=span - 1, padx=(0, 10), pady=3, sticky="ew")
        self.delay_entry.master.grid_columnconfigure(3, weight=0 if compact else 1)

    def _grid_advanced_settings(self, compact: bool):
        checks = (self.cb_hybrid, self.cb_traf, self.cb_full, self.cb_sitemap, self.cb_robots, self.cb_strict)
        for index, checkbox in enumerate(checks):
            row, column = (index, 0) if compact else divmod(index, 3)
            checkbox.grid(row=row, column=column, columnspan=3 if compact else 1, padx=10, pady=3, sticky="w")
        first = len(checks) if compact else 2
        fields = ((self.lbl_exclude, self.exclude_entry), (self.lbl_require, self.require_entry),
                  (self.lbl_lang_filter, self.lang_filter_entry))
        for offset, (label, entry) in enumerate(fields):
            label.grid(row=first + offset, column=0, padx=(10, 5), pady=3, sticky="e")
            entry.grid(row=first + offset, column=1, columnspan=2, padx=(0, 10), pady=3, sticky="ew")
        self._pii_row.grid(row=first + len(fields), column=0, columnspan=3, padx=10, pady=3, sticky="w")
        for index, checkbox in enumerate((self.cb_rm_email, self.cb_rm_phone, self.cb_rm_pnr, self.cb_rm_ip)):
            row, column = divmod(index, 2) if compact else (0, index)
            checkbox.grid(row=row, column=column, padx=(0, 12), pady=(0, 6) if compact else 0, sticky="w")
        tab = self.cb_hybrid.master
        for column, weight in enumerate((0, 1, 0) if compact else (1, 1, 1)):
            tab.grid_columnconfigure(column, weight=weight)

    def _scroll_into_view(self, widget):
        """Rulla så att en fokuserad kontroll syns helt (fokus får inte hamna utanför bild)."""
        canvas = getattr(self, "_scroll_canvas", None)
        if canvas is None or not (self._vbar_box.winfo_ismapped() or self._hbar_box.winfo_ismapped()):
            return
        try:
            region = [float(v) for v in str(canvas.cget("scrollregion")).split()]
            total_width, total_height = region[2], region[3]
            margin = round(12 * self._ui_scale)
            for start, size, view_start, view_size, total, moveto in (
                    (widget.winfo_rooty() - self._scroll_content.winfo_rooty(), widget.winfo_height(),
                     canvas.canvasy(0), canvas.winfo_height(), total_height, canvas.yview_moveto),
                    (widget.winfo_rootx() - self._scroll_content.winfo_rootx(), widget.winfo_width(),
                     canvas.canvasx(0), canvas.winfo_width(), total_width, canvas.xview_moveto)):
                if total <= 0:
                    continue
                if start - margin < view_start or size + 2 * margin > view_size:
                    moveto(max(0, start - margin) / total)
                elif start + size + margin > view_start + view_size:
                    moveto((start + size + margin - view_size) / total)
        except (tk.TclError, ValueError, IndexError):
            pass

    def _on_mousewheel(self, event, horizontal=False):
        # Tabellen och loggen rullar själva; hjulet rullar hela innehållet bara utanför dem.
        widget_class = event.widget.winfo_class() if hasattr(event.widget, "winfo_class") else ""
        if widget_class in ("Treeview", "Text", "Menu"):
            return
        box = self._hbar_box if horizontal else self._vbar_box
        if box.winfo_ismapped():
            steps = -2 if event.delta > 0 else 2
            (self._scroll_canvas.xview_scroll if horizontal else self._scroll_canvas.yview_scroll)(steps, "units")

    def _text_scale_label(self, scale: float) -> str:
        percent = round(scale * 100)
        return f"Text {percent} %" if self.lang == "sv" else f"Text {percent}%"

    def _apply_text_scale(self, scale: float):
        """Textstorlek 100–200 %. CustomTkinter skalar sina kontroller; _on_ui_scaling tar resten."""
        self.text_scale = min(self.TEXT_SCALES, key=lambda allowed: abs(allowed - scale))
        ctk.set_widget_scaling(self.text_scale)
        self.text_size_menu.set(self._text_scale_label(self.text_scale))
        self._schedule_scroll_update()

    def _on_text_size_chosen(self, label: str):
        labels = [self._text_scale_label(scale) for scale in self.TEXT_SCALES]
        if label in labels:
            self._apply_text_scale(self.TEXT_SCALES[labels.index(label)])

    def _step_text_scale(self, direction: int):
        index = self.TEXT_SCALES.index(self.text_scale) if self.text_scale in self.TEXT_SCALES else 0
        self._apply_text_scale(self.TEXT_SCALES[max(0, min(len(self.TEXT_SCALES) - 1, index + direction))])
        return "break"

    def _show_empty_output(self):
        self.output_labels["results_empty"].place(relx=.5, rely=.60, anchor="center")
        self.output_labels["log_empty"].place(relx=.5, rely=.5, anchor="center")

    def _build_ui(self):
        # Typsnittet måste vara valt innan första kontrollen skapas.
        self._setup_typography()
        self._update_treeview_style("Light")
        self.root.configure(fg_color=self.WINDOW_SURFACE)
        content = self._build_scroll_area()
        main_frame = ctk.CTkFrame(content, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=self.CONTENT_PAD_X, pady=8)
        self._main_frame = main_frame

        panel_style = dict(fg_color=self.PANEL_SURFACE, border_color=self.PANEL_BORDER,
                           border_width=1, corner_radius=8)
        entry_style = dict(fg_color=self.FIELD_SURFACE, border_color=self.FIELD_BORDER,
                           border_width=1, text_color=self.STATUS_COLOR,
                           placeholder_text_color=self.MUTED_TEXT)
        menu_style = dict(fg_color=self.FIELD_SURFACE, button_color=self.SELECTED_SURFACE,
                          button_hover_color=self.CONTROL_HOVER, text_color=self.STATUS_COLOR,
                          dropdown_fg_color=self.PANEL_SURFACE, dropdown_text_color=self.STATUS_COLOR,
                          dropdown_hover_color=self.CONTROL_HOVER)
        check_style = dict(fg_color=self.ACCENT, hover_color=self.ACCENT_HOVER,
                           border_color=self.MUTED_TEXT, text_color=self.STATUS_COLOR,
                           checkmark_color="#ffffff", border_width=2,
                           checkbox_width=18, checkbox_height=18, height=24)
        secondary_button = dict(fg_color=self.PANEL_SURFACE, hover_color=self.CONTROL_HOVER,
                                text_color=self.STATUS_COLOR, text_color_disabled=self.DISABLED_TEXT,
                                border_color=self.PANEL_BORDER, border_width=1)

        # Startadress, språk och visningsläge delar samma panel.
        source_panel = ctk.CTkFrame(main_frame, **panel_style)
        source_panel.pack(fill=tk.X, pady=(0, 7))
        url_frame = ctk.CTkFrame(source_panel, fg_color="transparent")
        url_frame.pack(fill=tk.X, padx=12, pady=7)
        self.lbl_url = ctk.CTkLabel(url_frame, text=self.texts["lbl_url"][self.lang],
                                    text_color=self.STATUS_COLOR, font=self.fonts["body_strong"])
        self.lbl_url.pack(side=tk.LEFT, padx=(0, 10))
        self.url_entry = ctk.CTkEntry(url_frame, width=200, placeholder_text="https://...", **entry_style)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.url_entry.insert(0, "https://")
        # Hjälp, textstorlek, tema och språk ligger i en egen ram: till höger på adressraden,
        # eller på en egen rad i kompakt läge (_set_compact). Skapas i läsordning för Tab.
        self._prefs_frame = ctk.CTkFrame(source_panel, fg_color="transparent")
        self.help_btn = ctk.CTkButton(
            self._prefs_frame, text=self.texts["btn_help"][self.lang], width=66,
            command=self.open_help_window, **secondary_button)
        # Textstorlek 100–200 %. Ctrl+Plus, Ctrl+Minus och Ctrl+0 gör samma sak.
        self.text_size_menu = ctk.CTkOptionMenu(
            self._prefs_frame, values=[self._text_scale_label(scale) for scale in self.TEXT_SCALES],
            width=96, command=self._on_text_size_chosen, **menu_style)
        self.text_size_menu.set(self._text_scale_label(self.text_scale))
        self.theme_switch = ctk.CTkSwitch(
            self._prefs_frame, text=self.texts["dark_mode"][self.lang], switch_width=30, switch_height=16,
            fg_color=self.PANEL_BORDER, progress_color=self.ACCENT,
            button_color=("#51657b", "#dce6f1"), button_hover_color=("#364a60", "#ffffff"),
            text_color=self.STATUS_COLOR, command=self.change_appearance_mode_event)
        self.lang_var = ctk.StringVar(value="SV")
        self.lang_switch = ctk.CTkSegmentedButton(
            self._prefs_frame, values=["SV", "EN"], width=66, height=28,
            variable=self.lang_var, command=self.change_language_event,
            fg_color=self.FIELD_SURFACE, selected_color=self.SEGMENT_SELECTED,
            selected_hover_color=self.SEGMENT_SELECTED_HOVER, unselected_color=self.FIELD_SURFACE,
            unselected_hover_color=self.CONTROL_HOVER, text_color=self.STATUS_COLOR)
        self.lang_switch.pack(side=tk.RIGHT, padx=(10, 0))
        self.theme_switch.pack(side=tk.RIGHT, padx=(10, 0))
        self.text_size_menu.pack(side=tk.RIGHT, padx=(10, 0))
        self.help_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self._prefs_frame.pack(in_=url_frame, side=tk.RIGHT, before=self.lbl_url)
        self._source_panel, self._url_frame = source_panel, url_frame

        self.templates = self._find_templates()
        if self.templates:
            tpl_frame = ctk.CTkFrame(source_panel, fg_color="transparent")
            tpl_frame.pack(fill=tk.X, padx=12, pady=(0, 7))
            self.lbl_template = ctk.CTkLabel(
                tpl_frame, text=self.texts["lbl_template"][self.lang],
                text_color=self.STATUS_COLOR, font=self.fonts["body_strong"])
            self.lbl_template.pack(side=tk.LEFT, padx=(0, 10))
            none_label = self.texts["template_none"][self.lang]
            template_names = [none_label] + list(self.templates.keys())
            self.template_var = ctk.StringVar(value=none_label)
            self.template_menu = ctk.CTkOptionMenu(
                tpl_frame, variable=self.template_var, values=template_names,
                width=300, command=self._apply_template, **menu_style)
            self.template_menu.pack(side=tk.LEFT)

        # Låt innehållet i den aktiva fliken bestämma höjden.
        self.tabview = ctk.CTkTabview(
            main_frame, height=60, anchor="w", **panel_style,
            segmented_button_fg_color=self.FIELD_SURFACE,
            segmented_button_selected_color=self.SEGMENT_SELECTED,
            segmented_button_selected_hover_color=self.SEGMENT_SELECTED_HOVER,
            segmented_button_unselected_color=self.FIELD_SURFACE,
            segmented_button_unselected_hover_color=self.CONTROL_HOVER,
            text_color=self.STATUS_COLOR)
        self.tabview.pack(fill=tk.X, pady=(0, 6))
        tab_basic = self.tabview.add(self.texts["tab_basic"][self.lang])
        tab_adv = self.tabview.add(self.texts["tab_adv"][self.lang])
        row_pad = 3

        # Tab 1: Basic
        tab_basic.grid_columnconfigure(1, weight=1)
        tab_basic.grid_columnconfigure(3, weight=1)

        self.lbl_delay = ctk.CTkLabel(tab_basic, text=self.texts["lbl_delay"][self.lang])
        self.lbl_delay.grid(row=0, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.delay_entry = ctk.CTkEntry(tab_basic, width=80, **entry_style)
        self.delay_entry.insert(0, "0.5")
        self.delay_entry.grid(row=0, column=1, padx=(0, 10), pady=row_pad, sticky="w")

        self.lbl_max_pages = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_pages"][self.lang])
        self.lbl_max_pages.grid(row=0, column=2, padx=(10, 5), pady=row_pad, sticky="e")
        self.max_pages_entry = ctk.CTkEntry(tab_basic, width=80, **entry_style)
        self.max_pages_entry.insert(0, "0")
        self.max_pages_entry.grid(row=0, column=3, padx=(0, 10), pady=row_pad, sticky="w")

        self.lbl_max_depth = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_depth"][self.lang])
        self.lbl_max_depth.grid(row=1, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.max_depth_entry = ctk.CTkEntry(tab_basic, width=80, **entry_style)
        self.max_depth_entry.insert(0, "0")
        self.max_depth_entry.grid(row=1, column=1, padx=(0, 10), pady=row_pad, sticky="w")

        self.lbl_format = ctk.CTkLabel(tab_basic, text=self.texts["lbl_format"][self.lang])
        self.lbl_format.grid(row=1, column=2, padx=(10, 5), pady=row_pad, sticky="e")
        # Default: .md (LLM-vänligt, fungerar bäst med RAG-pipelines som
        # chunkar i stycken). .json finns kvar för dem som vill ha strukturerade
        # fält per sida.
        self.format_var = ctk.StringVar(value=".md")
        self.format_menu = ctk.CTkOptionMenu(
            tab_basic, variable=self.format_var,
            values=[".json", ".md", ".txt", "Ingen text"], width=100, **menu_style)
        self.format_menu.grid(row=1, column=3, padx=(0, 10), pady=row_pad, sticky="w")

        self.lbl_concurrency = ctk.CTkLabel(tab_basic, text=self.texts["lbl_concurrency"][self.lang])
        self.lbl_concurrency.grid(row=2, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.concurrency_entry = ctk.CTkEntry(tab_basic, width=80, **entry_style)
        self.concurrency_entry.insert(0, "10")
        self.concurrency_entry.grid(row=2, column=1, padx=(0, 10), pady=row_pad, sticky="w")

        self.lbl_mode = ctk.CTkLabel(tab_basic, text=self.texts["lbl_mode"][self.lang])
        self.lbl_mode.grid(row=2, column=2, padx=(10, 5), pady=row_pad, sticky="e")
        self.headless_var = ctk.StringVar(value=self.texts["run_modes"]["headless"][self.lang])
        self.headless_menu = ctk.CTkOptionMenu(
            tab_basic, variable=self.headless_var,
            values=[v[self.lang] for v in self.texts["run_modes"].values()], width=220, **menu_style)
        self.headless_menu.grid(row=2, column=3, padx=(0, 10), pady=row_pad, sticky="w")

        # Default: ladda ner och konvertera dokument — det är den vanligaste
        # användningen för RAG-system. Användaren kan stänga av om de bara
        # vill ha sidor.
        documents_row = self._documents_row = ctk.CTkFrame(tab_basic, fg_color="transparent")
        documents_row.grid(row=3, column=0, columnspan=4, padx=10, pady=row_pad, sticky="w")
        self.docs_var = ctk.BooleanVar(value=True)
        self.cb_docs = ctk.CTkCheckBox(documents_row, text=self.texts["cb_docs"][self.lang],
                                       variable=self.docs_var, **check_style)
        self.cb_docs.grid(row=0, column=0, padx=(0, 16), sticky="w")

        self.convert_docs_var = ctk.BooleanVar(value=True)
        self.cb_convert_docs = ctk.CTkCheckBox(
            documents_row, text=self.texts["cb_convert_docs"][self.lang],
            variable=self.convert_docs_var,
            command=self._on_convert_docs_toggled, **check_style)
        self.cb_convert_docs.grid(row=0, column=1, sticky="w")

        self.lbl_folder = ctk.CTkLabel(tab_basic, text=self.texts["lbl_folder"][self.lang])
        self.lbl_folder.grid(row=4, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        folder_row = self._folder_row = ctk.CTkFrame(tab_basic, fg_color="transparent")
        folder_row.grid(row=4, column=1, columnspan=3, padx=(0, 10), pady=row_pad, sticky="ew")
        folder_row.grid_columnconfigure(0, weight=1)
        self.dir_var = ctk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output"))
        self.dir_entry = ctk.CTkEntry(folder_row, textvariable=self.dir_var, state="readonly", **entry_style)
        self.dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.btn_folder = ctk.CTkButton(
            folder_row, text=self.texts["btn_folder"][self.lang],
            width=100, command=self.choose_directory, **secondary_button)
        self.btn_folder.grid(row=0, column=1)

        # Tab 2: Advanced
        tab_adv.grid_columnconfigure((0, 1, 2), weight=1)

        self.hybrid_var = ctk.BooleanVar(value=True)
        self.cb_hybrid = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_hybrid"][self.lang],
                                         variable=self.hybrid_var, **check_style)
        self.cb_hybrid.grid(row=0, column=0, padx=10, pady=row_pad, sticky="w")

        self.traf_var = ctk.BooleanVar(value=HAS_TRAFILATURA)
        self.cb_traf = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_traf"][self.lang],
                                       variable=self.traf_var, **check_style)
        self.cb_traf.grid(row=0, column=1, padx=10, pady=row_pad, sticky="w")
        if not HAS_TRAFILATURA:
            self.cb_traf.configure(state="disabled")

        self.sitemap_var = ctk.BooleanVar(value=True)
        self.cb_sitemap = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_sitemap"][self.lang],
                                          variable=self.sitemap_var, **check_style)
        self.cb_sitemap.grid(row=1, column=0, padx=10, pady=row_pad, sticky="w")

        self.robots_var = ctk.BooleanVar(value=True)
        self.cb_robots = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_robots"][self.lang],
                                         variable=self.robots_var, **check_style)
        self.cb_robots.grid(row=1, column=1, padx=10, pady=row_pad, sticky="w")

        self.strict_var = ctk.BooleanVar(value=True)
        self.cb_strict = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_strict"][self.lang],
                                         variable=self.strict_var, **check_style)
        self.cb_strict.grid(row=1, column=2, padx=10, pady=row_pad, sticky="w")

        self.lbl_exclude = ctk.CTkLabel(tab_adv, text=self.texts["lbl_exclude"][self.lang])
        self.lbl_exclude.grid(row=2, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.exclude_entry = ctk.CTkEntry(tab_adv, placeholder_text="images, login, kalender", **entry_style)
        self.exclude_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=row_pad)

        self.lbl_require = ctk.CTkLabel(tab_adv, text=self.texts["lbl_require"][self.lang])
        self.lbl_require.grid(row=3, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.require_entry = ctk.CTkEntry(tab_adv, placeholder_text="intranat, bibliotek", **entry_style)
        self.require_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=row_pad)

        self.lbl_lang_filter = ctk.CTkLabel(tab_adv, text=self.texts["lbl_lang_filter"][self.lang])
        self.lbl_lang_filter.grid(row=4, column=0, padx=(10, 5), pady=row_pad, sticky="e")
        self.lang_filter_entry = ctk.CTkEntry(tab_adv, placeholder_text="sv", **entry_style)
        self.lang_filter_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=row_pad)

        self.full_var = ctk.BooleanVar(value=False)
        self.cb_full = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_full"][self.lang],
                                       variable=self.full_var, **check_style)
        self.cb_full.grid(row=0, column=2, padx=10, pady=row_pad, sticky="w")

        pii_row = self._pii_row = ctk.CTkFrame(tab_adv, fg_color="transparent")
        pii_row.grid(row=5, column=0, columnspan=3, padx=10, pady=row_pad, sticky="w")

        self.rm_email_var = ctk.BooleanVar(value=True)
        self.cb_rm_email = ctk.CTkCheckBox(pii_row, text=self.texts["cb_rm_email"][self.lang],
                                           variable=self.rm_email_var, **check_style)
        self.cb_rm_email.grid(row=0, column=0, padx=(0, 12), sticky="w")

        self.rm_phone_var = ctk.BooleanVar(value=True)
        self.cb_rm_phone = ctk.CTkCheckBox(pii_row, text=self.texts["cb_rm_phone"][self.lang],
                                           variable=self.rm_phone_var, **check_style)
        self.cb_rm_phone.grid(row=0, column=1, padx=(0, 12), sticky="w")

        self.rm_pnr_var = ctk.BooleanVar(value=True)
        self.cb_rm_pnr = ctk.CTkCheckBox(pii_row, text=self.texts["cb_rm_pnr"][self.lang],
                                         variable=self.rm_pnr_var, **check_style)
        self.cb_rm_pnr.grid(row=0, column=2, padx=(0, 12), sticky="w")

        self.rm_ip_var = ctk.BooleanVar(value=False)
        self.cb_rm_ip = ctk.CTkCheckBox(pii_row, text=self.texts["cb_rm_ip"][self.lang],
                                        variable=self.rm_ip_var, **check_style)
        self.cb_rm_ip.grid(row=0, column=3, sticky="w")

        for widget, key in ((self.cb_hybrid, "hybrid"), (self.cb_traf, "traf"),
                            (self.cb_sitemap, "sitemap"), (self.cb_robots, "robots"),
                            (self.cb_strict, "strict"), (self.cb_full, "full"),
                            (self.cb_convert_docs, "convert"),
                            (self.lbl_exclude, "exclude"), (self.exclude_entry, "exclude"),
                            (self.lbl_require, "require"), (self.require_entry, "require"),
                            (self.lbl_lang_filter, "lang"), (self.lang_filter_entry, "lang")):
            self._add_tooltip(widget, key)

        # Körningsknappar till vänster och genvägar till utdata till höger.
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill=tk.X, pady=(1, 3))
        self.start_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_start"][self.lang], width=104,
            font=self.fonts["body_strong"], fg_color=self.ACCENT, hover_color=self.ACCENT_HOVER,
            text_color="#ffffff", text_color_disabled=self.DISABLED_TEXT, command=self.start_crawl)
        self.start_btn.pack(side=tk.LEFT)
        self.pause_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_pause"][self.lang], width=100,
            state="disabled", command=self.toggle_pause, **secondary_button)
        self.pause_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.stop_btn = ctk.CTkButton(
            btn_frame, text=self.texts["btn_stop"][self.lang], width=96,
            state="disabled", command=self.stop_crawl,
            **(secondary_button | {"text_color": self.RUN_STATUS_COLORS["failed"]}))
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        # Genvägarna till utdata ligger till höger på knappraden, eller på en egen rad i kompakt läge.
        self._output_buttons = ctk.CTkFrame(main_frame, fg_color="transparent")
        self.open_btn = ctk.CTkButton(
            self._output_buttons, text=self.texts["btn_open"][self.lang], width=108,
            command=self.open_output_folder, **secondary_button)
        self.report_btn = ctk.CTkButton(
            self._output_buttons, text=self.texts["btn_report"][self.lang], width=112,
            state="disabled", command=self.open_report, **secondary_button)
        self.report_btn.pack(side=tk.RIGHT)
        self.open_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self._output_buttons.pack(in_=btn_frame, side=tk.RIGHT)
        self._btn_frame = btn_frame

        # Stats & display
        self.stats_label = ctk.CTkLabel(
            main_frame, text=self.texts["status_wait"][self.lang],
            font=self.fonts["body_strong"],
            text_color=self.STATUS_COLOR, wraplength=660, anchor="w", justify="left")
        self.stats_label.pack(fill=tk.X, pady=3)
        self.progress_bar = ctk.CTkProgressBar(
            main_frame, orientation="horizontal", fg_color=self.PANEL_BORDER, progress_color=self.ACCENT)

        self.results_pane = ttk.Panedwindow(main_frame, orient=tk.VERTICAL)
        self.results_pane.pack(fill=tk.BOTH, expand=True)

        self.output_labels = {}
        table_frame, _table_header = self._output_panel("results_title")
        table_body = ctk.CTkFrame(table_frame, fg_color="transparent")
        table_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        table_body.grid_columnconfigure(0, weight=1)
        table_body.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_body, columns=('URL', 'Status', 'Titel'),
                                 displaycolumns=('Titel', 'Status', 'URL'), show='headings', height=6)
        self.tree.heading('URL', text='URL', anchor=tk.W)
        self.tree.heading('Status', text=self.texts["col_status"][self.lang], anchor=tk.W)
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang], anchor=tk.W)
        for column, width in self.TREE_COLUMN_WIDTHS.items():
            self.tree.column(column, width=round(width * self._ui_scale), stretch=column != 'Status')

        scrollbar = ctk.CTkScrollbar(table_body, orientation="vertical",
                                     command=self.tree.yview)
        def sync_scrollbar(first, last):
            scrollbar.set(first, last)
            if float(first) == 0 and float(last) == 1:
                scrollbar.grid_forget()
            else:
                scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.configure(yscrollcommand=sync_scrollbar)
        self.tree.grid(row=0, column=0, sticky="nsew")

        # Dubbelklick och Enter öppnar markerad sida; Ctrl+C kopierar dess adress.
        self.tree.bind("<Double-1>", lambda _event: self._open_selected_result())
        self.tree.bind("<Return>", lambda _event: self._open_selected_result())
        self.tree.bind("<KP_Enter>", lambda _event: self._open_selected_result())
        self.tree.bind("<Control-c>", lambda _event: self._copy_selected_result_url())
        self.tree.bind("<FocusIn>", lambda _event: self._focus_first_result(), add="+")

        log_frame, _log_header = self._output_panel("log_title")
        self.log_area = ctk.CTkTextbox(log_frame, fg_color=self.PANEL_SURFACE, text_color=self.STATUS_COLOR,
                                       font=self.fonts["body"],
                                       height=60, wrap=tk.WORD)
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        # Skrivskyddad men fokuserbar: raderna kan läsas, markeras och kopieras med tangentbordet.
        self.log_area.configure(state="disabled")
        self.log_area._textbox.configure(takefocus=1)
        self.results_pane.add(table_frame, weight=3)
        self.results_pane.add(log_frame, weight=1)
        self.results_pane.bind("<Configure>", self._keep_output_panels_visible, add="+")
        self.results_pane.bind("<ButtonRelease-1>", self._keep_output_panels_visible, add="+")
        for key, parent in (("results_empty", self.tree), ("log_empty", self.log_area)):
            self.output_labels[key] = ctk.CTkLabel(
                parent, text=self.texts[key][self.lang], bg_color=self.PANEL_SURFACE,
                text_color=self.MUTED_TEXT, font=self.fonts["body"])
        self._show_empty_output()
        self._build_context_menus()
        # Innehållets naturliga höjd ändras med flik, språk och textstorlek; rullytan följer med.
        for part in (source_panel, self.tabview, btn_frame, self.stats_label):
            tk.Misc.bind(part, "<Configure>", self._schedule_scroll_update, add="+")
        self._setup_keyboard()

    def process_queue(self):
        try:
            for _ in range(150):
                try:
                    msg_type, data = self.msg_queue.get_nowait()
                    if msg_type == "log":
                        if isinstance(data, tuple):
                            level, key = data
                            data = f"[{level}] {self.texts[key][self.lang]}"
                        self._log_to_gui(data)
                    elif msg_type == "table":
                        if data[1] == self.texts['status_login_required']['sv']:
                            data = (data[0], self.texts['status_login_required'][self.lang], data[2])
                        if self.lang == 'en':
                            url, status, title = data
                            translations = self.texts['table_status_en']
                            prefix, space, detail = status.partition(' (')
                            status = translations.get(status, translations.get(prefix, prefix) + space + detail)
                            data = (url, status, translations['Ingen titel'] if title == 'Ingen titel' else title)
                        self.output_labels["results_empty"].place_forget()
                        self.tree.insert('', 0, values=data)
                        if len(self.tree.get_children()) > 100:
                            self.tree.delete(self.tree.get_children()[-1])
                        if not self.tree.focus() and self.root.focus_get() is self.tree:
                            self._focus_first_result()
                    elif msg_type == "stats_data":
                        if self.crawler_instance and self.crawler_instance.state == CrawlerState.STOPPED:
                            continue
                        if self.lang == 'en':
                            data = (*data[:-1], self.texts['eta_texts'].get(data[-1], data[-1]))
                        self.stats_label.configure(
                            text=self.texts["stats_fmt"][self.lang].format(*data))
                        if self.progress_bar and self.max_pages_entry.get() != "0":
                            try:
                                max_p = int(self.max_pages_entry.get())
                                if max_p > 0:
                                    self.progress_bar.set(data[0] / max_p)
                            except ValueError:
                                pass
                    elif msg_type == 'phase':
                        if self.crawler_instance and self.crawler_instance.state != CrawlerState.STOPPED:
                            self.stats_label.configure(text=self.texts[data][self.lang])
                    elif msg_type == "login_wait":
                        proceed = messagebox.askokcancel(
                            "Inloggning / Login", self.texts["login_msg"][self.lang])
                        if self.crawler_instance:
                            if not proceed:
                                self.crawler_instance.stop()
                            else:
                                self.crawler_instance.login_event.set()
                    elif msg_type == "summary":
                        self._last_summary = data
                        self._log_to_gui(self._render_summary(data))
                    elif msg_type == "done":
                        if not self._closing:
                            self.start_btn.configure(state="normal", fg_color=self.ACCENT)
                        self.pause_btn.configure(state="disabled")
                        self.stop_btn.configure(state="disabled")
                        self.progress_bar.stop()
                        self.progress_bar.pack_forget()
                except queue.Empty:
                    break
                except Exception as inner_e:
                    self._log_to_gui(f"[GUI FEL] Kunde inte rita rad: {inner_e}")
        finally:
            self.root.after(100, self.process_queue)

    def _error_dialog(self, key: str, extra: str = ""):
        messagebox.showerror(self.texts["err_title"][self.lang],
                             self.texts[key][self.lang] + (f"\n\n{extra}" if extra else ""))

    def _focus_field(self, entry):
        """Efter ett felmeddelande: visa fältets flik, ge fältet fokus och markera texten."""
        inner = getattr(entry, "_entry", None)
        if inner is None:
            return
        try:
            for key in ("tab_basic", "tab_adv"):
                name = self.texts[key][self.lang]
                tab = self.tabview.tab(name)
                if str(inner).startswith(str(tab) + ".") and self.tabview.get() != name:
                    self.tabview.set(name)
            inner.focus_set()
            inner.select_range(0, tk.END)
            inner.icursor(tk.END)
        except (tk.TclError, ValueError, AttributeError):
            pass

    def start_crawl(self):
        url = self.url_entry.get().strip()
        # Saknas schema ("kommunen.se") antar vi https, annars blir resultatet tyst tomt
        if url and "://" not in url:
            url = "https://" + url
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, url)
        if not _valid_http_url(url):
            self._error_dialog("err_url")
            self._focus_field(self.url_entry)
            return
        # Kontrollera ett fält i taget. Felmeddelandet pekar ut fältet, och sedan hamnar fokus där.
        checks = ((self.delay_entry, float, lambda v: v >= 0.1, "err_delay"),
                  (self.max_pages_entry, int, lambda v: v >= 0, "err_max_pages"),
                  (self.max_depth_entry, int, lambda v: v >= 0, "err_max_depth"),
                  (self.concurrency_entry, int, lambda v: 1 <= v <= 50, "err_concurrency"))
        values = []
        for entry, convert, allowed, error in checks:
            try:
                # Decimalkomma accepteras, eftersom det är vad svenska användare skriver.
                value = convert(entry.get().strip().replace(",", "."))
            except ValueError:
                value = None
            if value is None or not allowed(value):
                self._error_dialog(error)
                self._focus_field(entry)
                return
            values.append(value)
        delay_val, max_pages_val, max_depth_val, concurrency_val = values
        if not self.strict_var.get() and max_pages_val == max_depth_val == 0:
            if not messagebox.askyesno(self.texts['window_title'][self.lang], self.texts['confirm_unbounded'][self.lang]):
                return

        inverted_map = {v[self.lang]: k for k, v in self.texts["run_modes"].items()}

        config = {
            **self._template_options_for(url),
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
        self._last_summary = None
        self._save_settings()

        self.start_btn.configure(state="disabled", fg_color=self.FIELD_SURFACE)
        self.report_btn.configure(state="disabled")
        self.stats_label.configure(text=self.texts["status_starting"][self.lang], text_color=self.STATUS_COLOR)
        self.pause_btn.configure(state="normal", text=self.texts["btn_pause"][self.lang])
        self.stop_btn.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.log_area.delete("1.0", tk.END)
        self._show_empty_output()

        self.progress_bar.pack(before=self.results_pane, fill=tk.X, pady=(0, 4))
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
                crawler.fatal_error = f"{type(e).__name__}: {e}"
                self.msg_queue.put(("log", redact_diagnostic_urls(f"[ERROR] Crawlen kraschade: {e}")))
                self.msg_queue.put(("summary", crawler.run_summary()))
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
            self.pause_btn.configure(state="disabled")
            self.stats_label.configure(text=self.texts['phase_stopping'][self.lang])
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
