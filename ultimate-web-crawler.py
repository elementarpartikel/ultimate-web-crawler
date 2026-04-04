#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webbdammsugare Pro (v6.0)
Skapad av Fredrik Eriksson

Funktioner: Semantic Chunking, PII-tvätt, HEAD-requests, Kaskad-datum, Krocksäker Playwright, Perfekt Cookie-hantering
"""

import tkinter as tk
from tkinter import messagebox, filedialog, ttk
import customtkinter as ctk
import threading
import queue
import time
import os
import sys
import hashlib
import sqlite3
import csv
import gc
import re
import logging
import webbrowser
import asyncio
import json
import argparse
import gzip
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, unquote
from urllib.robotparser import RobotFileParser
from dataclasses import dataclass, field
from typing import Set, Dict, List, Optional, Tuple
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Frivilliga integrationer
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from playwright.sync_api import sync_playwright
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import aiohttp
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

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

# ─────────────────────────────────────────────────────────────
#  ENUMS OCH DATACLASSES
# ─────────────────────────────────────────────────────────────
class LogLevel(Enum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR

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
def slugify(text: str) -> str:
    text = str(text).replace('å', 'a').replace('ä', 'a').replace('ö', 'o')
    text = text.replace('Å', 'A').replace('Ä', 'A').replace('Ö', 'O')
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '-', text).strip('-').lower()

def normalize_url(url: str, ignore_query_params: Optional[List[str]] = None) -> str:
    if ignore_query_params is None:
        ignore_query_params = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid', 'ref', 'source']
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or 'http'
        netloc = parsed.netloc.lower()
        path = parsed.path or '/'
        path = re.sub(r'/index\.(html|htm|php)$', '/', path, flags=re.IGNORECASE)
        if path != '/' and path.endswith('/'): path = path.rstrip('/')
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        filtered_params = {k: sorted(v) for k, v in query_params.items() if k.lower() not in ignore_query_params}
        query_string = urlencode(sorted(filtered_params.items()), doseq=True) if filtered_params else ""
        normalized = f"{scheme}://{netloc}{path}"
        if query_string: normalized += f"?{query_string}"
        return normalized.split('#')[0]
    except Exception: return url.strip()

def get_clean_hash(text: str) -> str:
    clean_text = re.sub(r'\s+', '', text).lower()
    return hashlib.sha256(clean_text.encode('utf-8')).hexdigest()

def semantic_chunk_text(sections: List[Dict], max_words=400, overlap_words=50) -> List[Dict]:
    """Chunkar strukturerade sektioner till {heading, content}-objekt.
    Tar emot lista av {"heading": str, "text": str}."""
    if not sections: return []
    
    chunks = []
    for sec in sections:
        heading = sec.get("heading", "Huvudinnehåll")
        text = sec.get("text", "").strip()
        if not text: continue
        
        words = text.split()
        if len(words) <= max_words:
            chunks.append({"heading": heading, "content": text})
        else:
            part_num = 1
            current_words = []
            for w in words:
                current_words.append(w)
                if len(current_words) >= max_words:
                    chunk_heading = heading if part_num == 1 else f"{heading} (del {part_num})"
                    chunks.append({"heading": chunk_heading, "content": " ".join(current_words)})
                    part_num += 1
                    current_words = current_words[-overlap_words:]
            
            if len(current_words) > overlap_words:
                chunk_heading = heading if part_num == 1 else f"{heading} (del {part_num})"
                chunks.append({"heading": chunk_heading, "content": " ".join(current_words)})
                
    # Lägg till index-metadata för RAG-ranking
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i + 1
        chunk["total_chunks"] = total
                
    return chunks

def absolutize_markdown_links(text: str, base_url: str) -> str:
    """Gör relativa markdown-länkar [text](href) och rena /sökvägar absoluta."""
    def _fix_md_link(m):
        href = m.group(2)
        if href and not href.startswith(('http://', 'https://', 'mailto:', '#')):
            href = urljoin(base_url, href)
        return f"[{m.group(1)}]({href})"
    text = re.sub(r'\[([^\]]*)\]\(([^)]+)\)', _fix_md_link, text)
    return text

# ─────────────────────────────────────────────────────────────
#  DATABAS
# ─────────────────────────────────────────────────────────────
class AsyncCrawlDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self._init_db()

    async def _init_db(self):
        await self.conn.execute('CREATE TABLE IF NOT EXISTS page_cache (url TEXT PRIMARY KEY, content_hash TEXT, title TEXT, crawled_at TEXT, content_length INTEGER)')
        await self.conn.commit()

    async def get_cache(self, url: str) -> Optional[Dict]:
        async with self.conn.execute("SELECT content_hash, title, crawled_at, content_length FROM page_cache WHERE url = ?", (url,)) as cursor:
            row = await cursor.fetchone()
            if row: return {'hash': row[0], 'title': row[1], 'crawled_at': row[2], 'content_length': row[3]}
        return None

    async def save_cache(self, url: str, content_hash: str, title: str, length: int):
        await self.conn.execute('INSERT OR REPLACE INTO page_cache (url, content_hash, title, crawled_at, content_length) VALUES (?, ?, ?, ?, ?)', 
                              (url, content_hash, title, datetime.now().isoformat(), length))
        await self.conn.commit()
            
    async def get_all_records(self):
        async with self.conn.execute("SELECT url, title, crawled_at, content_hash FROM page_cache ORDER BY crawled_at DESC") as cursor:
            return await cursor.fetchall()
        
    async def close(self):
        if self.conn:
            await self.conn.close()

# ─────────────────────────────────────────────────────────────
#  RATE LIMITER & QUEUE
# ─────────────────────────────────────────────────────────────
class PerDomainRateLimiter:
    def __init__(self, requests_per_second: float):
        self.delay = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self.last_requests = {}
        self._lock = threading.Lock()
    
    async def async_wait(self, domain: str):
        sleep_time = 0
        with self._lock:
            now = datetime.now()
            last_req = self.last_requests.get(domain, datetime.min)
            elapsed = (now - last_req).total_seconds()
            if elapsed < self.delay:
                sleep_time = self.delay - elapsed
                self.last_requests[domain] = now + timedelta(seconds=sleep_time)
            else:
                self.last_requests[domain] = now
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

class PriorityURLQueue:
    def __init__(self):
        self.queue = queue.PriorityQueue()
        self.seen_urls: Set[str] = set()
        self._lock = threading.Lock()
        
        self.boost_words = ["policy", "om-oss", "kontakt", "regler", "guide"]
        self.penalty_words = ["nyheter", "arkiv", "blogg", "kalender", "202"]
    
    def add_url(self, url: str, depth: int = 0, base_priority: int = CrawlPriority.MEDIUM.value):
        normalized = normalize_url(url)
        with self._lock:
            if normalized in self.seen_urls:
                return False
            
            score = base_priority
            lower_url = normalized.lower()
            if any(w in lower_url for w in self.boost_words): score -= 3
            if any(w in lower_url for w in self.penalty_words): score += 5
            score += depth 
            
            self.queue.put((score, time.time(), depth, normalized))
            self.seen_urls.add(normalized)
            return True
    
    def get_next(self) -> Optional[Tuple[int, str]]:
        try: 
            item = self.queue.get_nowait()
            return (item[2], item[3])
        except queue.Empty: return None
    
    def size(self) -> int: return self.queue.qsize()

# ─────────────────────────────────────────────────────────────
#  ASYNC WEBB CRAWLER CORE (V6.0)
# ─────────────────────────────────────────────────────────────
class AsyncWebCrawler:
    def __init__(self, config: dict, msg_queue: Optional[queue.Queue] = None):
        self.config = config
        self.msg_queue = msg_queue
        self.state = CrawlerState.RUNNING
        self.stats = CrawlStats()
        self.active_tasks = 0
        
        self.start_url = normalize_url(config["start_url"])
        self.output_dir = config["output_dir"]
        self.delay = config["delay"]
        self.max_pages = config["max_pages"]
        self.max_depth = config.get("max_depth", 0)
        self.save_format = config.get("save_format", ".md")
        self.use_hybrid = config.get("use_hybrid", True)
        self.use_trafilatura = config.get("use_trafilatura", HAS_TRAFILATURA)
        
        self.find_sitemap = config.get("find_sitemap", True)
        self.robot_parser = None
        
        parsed_start = urlparse(self.start_url)
        self.domain = parsed_start.netloc.lower()
        self.base_url = f"{parsed_start.scheme}://{parsed_start.netloc}"
        
        os.makedirs(self.output_dir, exist_ok=True)
        self.db = AsyncCrawlDatabase(os.path.join(self.output_dir, f"{slugify(self.domain)}_cache.db"))
        self.url_queue = PriorityURLQueue()
        self.url_queue.add_url(self.start_url, depth=0, base_priority=CrawlPriority.CRITICAL.value)
        
        self.rate_limiter = PerDomainRateLimiter(requests_per_second=1.0 / max(self.delay, 0.1))
        self.downloaded_files: Set[str] = set()
        self.visited_sitemaps: Set[str] = set()
        self.login_event = threading.Event() 
        
        self.async_download_lock = asyncio.Lock()
        self.async_pw_lock = asyncio.Lock()
        self.async_stats_lock = asyncio.Lock()
        self.async_sitemap_lock = asyncio.Lock()
        
        self.semaphore = asyncio.Semaphore(10)
        self.playwright_semaphore = asyncio.Semaphore(2)
        
        self.crawl_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.logger = logging.getLogger(f'Crawler_{id(self)}')
        self.logger.setLevel(logging.DEBUG)
        log_dir = os.path.join(self.output_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, f'crawl_{self.crawl_session_id}.log'), maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
        self.logger.addHandler(fh)
        
        self._log(f"🚀 Initierar ASYNC crawl för {self.domain} (v6.0)")

    def _log(self, msg: str, level=LogLevel.INFO):
        if level == LogLevel.DEBUG: self.logger.debug(msg)
        elif level == LogLevel.INFO: self.logger.info(msg)
        elif level == LogLevel.WARNING: self.logger.warning(msg)
        elif level == LogLevel.ERROR: self.logger.error(msg)
        if self.msg_queue: self.msg_queue.put(("log", f"[{level.name}] {msg}"))
        else: print(f"[{level.name}] {msg}")

    def _gui_update(self, url: str, status: str, title: str):
        nya_eller_sparade = self.stats.pages_visited - self.stats.pages_unchanged
        queue_size = self.url_queue.size() + self.active_tasks
        pages_done = self.stats.pages_visited
        
        eta_str = "Beräknar..."
        if pages_done > 2:
            avg_time = self.stats.duration.total_seconds() / pages_done
            
            remaining = queue_size
            if self.max_pages > 0:
                remaining = min(queue_size, self.max_pages - pages_done)
                
            eta_sec = avg_time * remaining
            
            if remaining <= 0:
                eta_str = "Klar snart"
            elif eta_sec > 3600:
                eta_str = f"{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
            elif eta_sec > 60:
                eta_str = f"{int(eta_sec // 60)}m {int(eta_sec % 60)}s"
            else:
                eta_str = f"{int(eta_sec)}s"

        if self.msg_queue:
            # Säkerställ att inga ogiltiga tecken skickas till GUI:t
            safe_title = title.replace('\x00', '') if title else "Ingen titel"
            self.msg_queue.put(("table", (url, status, safe_title)))
            self.msg_queue.put(("stats_data", (
                self.stats.pages_visited, 
                nya_eller_sparade, 
                self.stats.documents_downloaded, 
                self.stats.pages_unchanged, 
                queue_size, 
                self.stats.pages_failed,
                eta_str
            )))

    def is_valid_url(self, url: str) -> bool:
        if len(url) > 2000: return False
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ['http', 'https']: return False
            
            parsed_path = parsed.path.lower()
            bad_exts = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.bmp', '.tiff', '.zip', '.rar', '.exe', '.mp4', '.mp3', '.avi', '.mov', '.css', '.js', '.woff', '.woff2'}
            
            ext = os.path.splitext(parsed_path)[1]
            if ext in bad_exts: 
                return False
                
            if '/images/' in parsed_path or '/media/' in parsed_path or '/assets/' in parsed_path:
                if any(img in parsed_path for img in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                    return False

            domain_core = self.domain.replace('www.', '')
            link_domain = parsed.netloc.lower().replace('www.', '')
            if self.config.get("strict_domain", True) and link_domain != domain_core: return False
            
            lower_url = url.lower()
            for kw in self.config.get("exclude_keywords", []):
                if kw and kw in lower_url: return False
            req_kws = self.config.get("require_keywords", [])
            if req_kws:
                if not any(kw in lower_url for kw in req_kws): return False
            
            if getattr(self, 'robot_parser', None) and not self.robot_parser.can_fetch('*', url):
                return False

            return True
        except: return False

    async def _create_robust_session(self):
        connector = aiohttp.TCPConnector(limit=100)
        return aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'},
            connector=connector
        )

    async def fetch_with_retry(self, url: str, max_retries=3, raw=False, method='GET'):
        base_delay = 1.0
        for attempt in range(max_retries + 1):
            
            while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)
            if self.state == CrawlerState.STOPPED: return None, None
                
            try:
                if method == 'HEAD':
                    async with self.req_session.head(url, timeout=5, allow_redirects=True) as resp:
                        if resp.status in [403, 405]: 
                            return None
                        if resp.status in [429, 500, 502, 503, 504]:
                            if attempt == max_retries: resp.raise_for_status()
                            sleep_time = base_delay * (2 ** attempt)
                            await asyncio.sleep(sleep_time)
                            continue
                        resp.raise_for_status()
                        return None, resp.headers.get('Content-Type', '').lower()
                else:
                    async with self.req_session.get(url, timeout=15) as resp:
                        if resp.status in [429, 500, 502, 503, 504]:
                            if attempt == max_retries: resp.raise_for_status()
                            sleep_time = base_delay * (2 ** attempt)
                            await asyncio.sleep(sleep_time)
                            continue
                        resp.raise_for_status()
                        
                        content_type = resp.headers.get('Content-Type', '').lower()
                        if raw:
                            content = await resp.read()
                            return content, content_type
                            
                        if 'text' not in content_type and 'html' not in content_type and 'json' not in content_type and 'xml' not in content_type:
                            return None, content_type
                            
                        content = await resp.text()
                        return content, content_type
            except Exception as e:
                if attempt == max_retries: 
                    if method == 'HEAD': return None
                    raise e
                await asyncio.sleep(base_delay * (2 ** attempt))

    async def _load_robots_txt(self):
        sitemaps_found = False
        try:
            result = await self.fetch_with_retry(f"{self.base_url}/robots.txt", max_retries=1)
            if result and result[0]:
                text, _ = result
                self.robot_parser = RobotFileParser()
                self.robot_parser.parse(text.splitlines())
                self._log("✓ robots.txt inläst")
                
                delay = self.robot_parser.crawl_delay("*")
                if delay:
                    self.rate_limiter.delay = float(delay)
                
                if self.find_sitemap:
                    sitemaps = [line.split(': ', 1)[1].strip() for line in text.splitlines() if line.lower().startswith('sitemap:')]
                    if sitemaps:
                        await asyncio.gather(*[self._parse_sitemap(sm) for sm in sitemaps])
                        sitemaps_found = True
        except Exception as e: 
            self._log(f"Kunde inte läsa robots.txt: {e}", LogLevel.DEBUG)

        if self.find_sitemap and not sitemaps_found:
            self._log("Letar efter sitemap.xml...")
            await self._parse_sitemap(f"{self.base_url}/sitemap.xml")

    async def _parse_sitemap(self, url: str):
        async with self.async_sitemap_lock:
            if url in self.visited_sitemaps: return
            self.visited_sitemaps.add(url)
            
        self._log(f"🗺️ Letar i sitemap: {url}")
        try:
            result = await self.fetch_with_retry(url, max_retries=2, raw=True)
            if not result or not result[0]: return
            content, _ = result
            
            if url.lower().endswith('.gz'):
                content = gzip.decompress(content)
                
            soup = BeautifulSoup(content, 'lxml-xml')
            sitemap_urls = [loc.text.strip() for sm in soup.find_all('sitemap') if (loc := sm.find('loc'))]
            if sitemap_urls:
                await asyncio.gather(*[self._parse_sitemap(s_url) for s_url in sitemap_urls])

            count = 0
            for url_node in soup.find_all('url'):
                loc = url_node.find('loc')
                if loc:
                    url_str = loc.text.strip()
                    if self.is_valid_url(url_str):
                        if self.url_queue.add_url(url_str, depth=0, base_priority=CrawlPriority.SITEMAP.value): 
                            count += 1
            if count > 0: self._log(f"✓ Hittade {count} (godkända) URLs i sitemap/index")
        except Exception as e: self._log(f"⚠ Fel vid sitemap-läsning: {e}", LogLevel.DEBUG)

    async def get_playwright_context(self):
        if not HAS_PLAYWRIGHT: return None
        if getattr(self, '_context', None) is None:
            async with self.async_pw_lock:
                if getattr(self, '_context', None) is None:
                    self._pw = await async_playwright().start()
                    # FIX BUG 1: Starta dold så länge läget inte explicit är "visible"
                    is_headless = self.config.get("headless_mode", "headless") != "visible"
                    self._browser = await self._pw.chromium.launch(headless=is_headless)
                    self._context = await self._browser.new_context(ignore_https_errors=True)
                    if hasattr(self, 'saved_cookies') and self.saved_cookies:
                        await self._context.add_cookies(self.saved_cookies)
        return self._context

    def clean_pii(self, text: str) -> str:
        if not text: return text
        if self.config.get("remove_email"):
            text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[E-POST]', text)
        if self.config.get("remove_pnr"):
            pnr_pattern = r'(?<!\d)(?:19|20)?\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]|[6-9]\d)[\-\+]?\d{4}(?!\d)'
            text = re.sub(pnr_pattern, '[PERSONNUMMER]', text)
        if self.config.get("remove_phone"):
            phone_pattern = r'(?<!\d)(?:(?:\+|00)46[\s\-]*\(?0\)?[\s\-]*[1-9]|0[\s\-]*\(?[1-9]\)?)[\s\-]*\d(?:[\s\-]*\d){4,8}\b'
            text = re.sub(phone_pattern, '[TELEFON]', text)
        if self.config.get("remove_ip"):
            text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP-ADRESS]', text)
        return text

    def extract_structured_data(self, html: str, url: str) -> Dict:
        soup = BeautifulSoup(html, 'lxml')
        
        title = soup.title.string.strip() if soup.title else "Okänd"
        title = self.clean_pii(title)
        
        keywords = []
        meta_kw = soup.find('meta', attrs={'name': re.compile(r'keywords', re.I)})
        if meta_kw and meta_kw.get('content'):
            keywords = [self.clean_pii(k.strip()) for k in meta_kw['content'].split(',')]
            
        description = ""
        meta_desc = soup.find('meta', attrs={'name': re.compile(r'description', re.I)})
        if meta_desc and meta_desc.get('content'):
            description = self.clean_pii(meta_desc['content'].strip())

        author = soup.find('meta', attrs={'name': ['author', 'DC.creator']})
        author = self.clean_pii(author['content']) if author and author.get('content') else ""
        
        pub_date = ""
        mod_date = ""

        meta_pub = soup.find('meta', attrs={'property': re.compile(r'article:published_time|og:pubdate', re.I)}) or \
                   soup.find('meta', attrs={'name': re.compile(r'pubdate|date', re.I)})
        if meta_pub and meta_pub.get('content'): pub_date = meta_pub['content'].strip()

        meta_mod = soup.find('meta', attrs={'property': re.compile(r'article:modified_time|og:updated_time', re.I)}) or \
                   soup.find('meta', attrs={'name': re.compile(r'last-modified|revised', re.I)}) or \
                   soup.find('meta', attrs={'itemprop': 'dateModified'})
        if meta_mod and meta_mod.get('content'): mod_date = meta_mod['content'].strip()

        if not mod_date:
            time_tag = soup.find('time', attrs={'itemprop': 'dateModified'}) or soup.find('time', class_=re.compile(r'update|modify', re.I))
            if time_tag: 
                mod_date = time_tag.get('datetime', time_tag.get_text(strip=True))

        if not mod_date or not pub_date:
            for script in soup.find_all('script', type='application/ld+json'):
                if script.string:
                    if not mod_date:
                        match = re.search(r'"dateModified"\s*:\s*"([^"]+)"', script.string)
                        if match: mod_date = match.group(1)
                    if not pub_date:
                        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string)
                        if match: pub_date = match.group(1)

        if not mod_date or not pub_date:
            text_content = soup.get_text(separator=' ', strip=True)
            date_pattern = r'(?i)(?:senast\s+uppdaterad|uppdaterad|publicerad|ändrad)[\s\:\*]*(?P<date>\d{1,2}\s+(?:januari|februari|mars|april|maj|juni|juli|augusti|september|oktober|november|december)\s+\d{4}|\d{4}-\d{2}-\d{2})'
            matches = list(re.finditer(date_pattern, text_content))
            if matches:
                extracted_date = matches[-1].group('date')
                if not mod_date and "uppdaterad" in matches[-1].group(0).lower():
                    mod_date = extracted_date
                if not pub_date and "publicerad" in matches[-1].group(0).lower():
                    pub_date = extracted_date
                if not mod_date and not pub_date:
                    mod_date = extracted_date

        lang = soup.find('html').get('lang', '') if soup.find('html') else ""
        
        og_type = soup.find('meta', attrs={'property': 'og:type'})
        og_type = og_type['content'] if og_type and og_type.get('content') else ""

        full_text = ""
        structured_sections = []
        
        if self.use_trafilatura and HAS_TRAFILATURA:
            try:
                extracted = trafilatura.extract(
                    html, 
                    include_links=True,
                    include_images=False, 
                    include_tables=True,
                    include_formatting=True,
                    output_format="markdown",
                    url=url
                )
                if extracted:
                    extracted = self.clean_pii(extracted)
                    extracted = absolutize_markdown_links(extracted, url)
                    
                    # Parsa markdown-rubriker till strukturerade sektioner
                    raw_sections = re.split(r'(?=^#{1,6}\s)', extracted, flags=re.MULTILINE)
                    for raw in raw_sections:
                        raw = raw.strip()
                        if not raw: continue
                        heading_match = re.match(r'^#{1,6}\s+(.+)', raw)
                        if heading_match:
                            heading = heading_match.group(1).strip()
                            content = raw[heading_match.end():].strip()
                        else:
                            heading = "Huvudinnehåll"
                            content = raw
                        if content:
                            structured_sections.append({"heading": heading, "text": content})
                    
                    full_text = extracted
            except Exception as e:
                self._log(f"  ⚠ Trafilatura misslyckades ({e})", LogLevel.DEBUG)
        
        if not full_text:
            noise = re.compile(r'cookie|banner|menu|nav|sidebar|footer|share|social', re.I)
            for tag in ['script', 'style', 'nav', 'footer', 'aside', 'iframe', 'svg', 'button', 'form']:
                for el in soup.find_all(tag): el.decompose()
            for el in soup.find_all(attrs={"class": noise}): el.decompose()
            for el in soup.find_all(attrs={"id": noise}): el.decompose()
            
            # FIX 5: Gör alla <a href> absoluta innan textextraktion
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if href and not href.startswith(('http://', 'https://', 'mailto:', '#', 'javascript:')):
                    a_tag['href'] = urljoin(url, href)
            
            sections = []
            current_heading = "Huvudinnehåll"
            current_text = []
            
            for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol', 'table']):
                if el.name.startswith('h'):
                    if current_text:
                        text_block = "\n".join(current_text).strip()
                        if text_block: sections.append({"heading": current_heading, "text": text_block})
                    current_heading = el.get_text(separator=' ', strip=True)
                    current_text = []
                elif el.name in ['ul', 'ol']:
                    for li in el.find_all('li'):
                        # Bevara länkar i listpunkter som markdown
                        parts = []
                        for child in li.children:
                            if hasattr(child, 'name') and child.name == 'a' and child.get('href'):
                                link_text = child.get_text(strip=True)
                                parts.append(f"[{link_text}]({child['href']})")
                            else:
                                t = child.get_text(strip=True) if hasattr(child, 'get_text') else str(child).strip()
                                if t: parts.append(t)
                        txt = " ".join(parts)
                        if txt: current_text.append(f"• {txt}")
                elif el.name == 'table':
                    rows = el.find_all('tr')
                    for i, row in enumerate(rows):
                        cols = [c.get_text(separator=' ', strip=True) for c in row.find_all(['td', 'th'])]
                        if cols:
                            current_text.append("| " + " | ".join(cols) + " |")
                            if i == 0: 
                                current_text.append("|" + "|".join(["---"] * len(cols)) + "|")
                else:
                    # Bevara länkar i <p> som markdown
                    parts = []
                    for child in el.children:
                        if hasattr(child, 'name') and child.name == 'a' and child.get('href'):
                            link_text = child.get_text(strip=True)
                            parts.append(f"[{link_text}]({child['href']})")
                        else:
                            t = child.get_text(strip=True) if hasattr(child, 'get_text') else str(child).strip()
                            if t: parts.append(t)
                    txt = " ".join(parts)
                    if len(txt) > 5: current_text.append(txt)
                    
            if current_text:
                text_block = "\n".join(current_text).strip()
                if text_block: sections.append({"heading": current_heading, "text": text_block})

            for s in sections:
                s['heading'] = self.clean_pii(s['heading'])
                s['text'] = self.clean_pii(s['text'])

            structured_sections = sections
            full_text = "\n\n".join([f"## {s['heading']}\n{s['text']}" for s in sections])
        
        return {
            "title": title,
            "url": url,
            "crawled_at": datetime.now().isoformat(),
            "author": author,
            "published_date": pub_date,
            "modified_date": mod_date,
            "language": lang,
            "og_type": og_type,
            "description": description,
            "keywords": keywords,
            "plain_text": full_text,
            "chunks": semantic_chunk_text(structured_sections) 
        }

    async def process_page(self, url: str, depth: int) -> bool:
        if self.state == CrawlerState.STOPPED: return False
        while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)

        domain = urlparse(url).netloc
        await self.rate_limiter.async_wait(domain) 
        
        if self.state == CrawlerState.STOPPED: return False
        while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)
        
        html, source = "", "Standard"
        try:
            if self.use_hybrid:
                head_result = await self.fetch_with_retry(url, method='HEAD', max_retries=1)
                if head_result:
                    _, content_type = head_result
                    
                    if any(t in content_type for t in ['image/', 'video/', 'audio/', 'font/', 'zip', 'octet-stream']): 
                        return False
                    
                    doc_types = ['application/pdf', 'application/vnd', 'application/msword']
                    if any(dt in content_type for dt in doc_types):
                        if self.config.get("download_docs", False): 
                            await self.download_document(url)
                        return True
                
                result = await self.fetch_with_retry(url)
                if not result or not result[0]: return False
                
                html, content_type = result
                if not html: return False

                if len(html) < 1000 or "enable javascript" in html.lower():
                    async with self.playwright_semaphore:
                        ctx = await self.get_playwright_context()
                        if ctx:
                            page = await ctx.new_page()
                            async def intercept_route(route):
                                try:
                                    if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                                        await route.abort()
                                    else:
                                        await route.continue_()
                                except Exception:
                                    pass
                            
                            try:
                                await page.route("**/*", intercept_route)
                                try: await page.goto(url, wait_until="networkidle", timeout=15000)
                                except: pass
                                html = await page.content()
                                source = "Webbläsare"
                                async with self.async_stats_lock: self.stats.playwright_fallbacks += 1
                            finally: 
                                try: await page.close()
                                except: pass
            else:
                async with self.playwright_semaphore:
                    ctx = await self.get_playwright_context()
                    if ctx:
                        page = await ctx.new_page()
                        async def intercept_route(route):
                            try:
                                if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                                    await route.abort()
                                else:
                                    await route.continue_()
                            except Exception:
                                pass
                        
                        try:
                            await page.route("**/*", intercept_route)
                            try: await page.goto(url, wait_until="networkidle", timeout=15000)
                            except: pass
                            html = await page.content()
                            source = "Webbläsare"
                        finally: 
                            try: await page.close()
                            except: pass

            if self.state == CrawlerState.STOPPED: return False

            # FIX: Detektera utgången session (intranät skickar tillbaka inloggningssida)
            if self.config.get("headless_mode") == "login_then_headless" and html:
                html_lower = html.lower()
                login_indicators = ['name="password"', 'action="login"', 'id="loginform"', 
                                    'type="password"', 'cas/login', 'adfs/ls', 'saml', 
                                    'inloggning krävs', 'du måste logga in']
                if any(indicator in html_lower for indicator in login_indicators):
                    self._log(f"⚠ Session utgången — inloggningssida detekterad för: {url}", LogLevel.WARNING)
                    async with self.async_stats_lock: self.stats.pages_failed += 1
                    self._gui_update(url, "Session utgången", "")
                    return False

            data = await asyncio.to_thread(self.extract_structured_data, html, url)
            content_hash = get_clean_hash(data["plain_text"])
            cached = await self.db.get_cache(url)
            
            text_length = len(data["plain_text"])
            
            if self.config["incremental"] and cached and cached.get('hash') == content_hash:
                async with self.async_stats_lock: self.stats.pages_unchanged += 1
                self._gui_update(url, "Oförändrad", data["title"])
            else:
                self._gui_update(url, f"Hämtad ({source})", data["title"])
                
                if text_length > 50 and self.save_format not in ["Ingen text", "No text"]:
                    texts_dir = os.path.join(self.output_dir, "texter")
                    os.makedirs(texts_dir, exist_ok=True)
                    safe_name = f"{slugify(urlparse(url).path)[:50]}_{content_hash[:6]}"
                    
                    if self.save_format == ".json":
                        del data["plain_text"]
                        with open(os.path.join(texts_dir, safe_name + ".json"), 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                    else:
                        with open(os.path.join(texts_dir, safe_name + self.save_format), 'w', encoding='utf-8') as f:
                            f.write(f"# {data['title']}\nKälla: {url}\n\n{data['plain_text']}")

            await self.db.save_cache(url, content_hash, data["title"], text_length)

            if self.max_depth == 0 or depth < self.max_depth:
                soup = BeautifulSoup(html, 'lxml')
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if href.startswith(('#', 'javascript:', 'mailto:')): continue
                    full_url = urljoin(url, href)
                    if self.is_valid_url(full_url): self.url_queue.add_url(full_url, depth=depth + 1)
            
            async with self.async_stats_lock: self.stats.pages_visited += 1
            return True
            
        except Exception as e:
            self._log(f"  ✗ Fel vid besök: {str(e)[:50]}", LogLevel.ERROR)
            async with self.async_stats_lock: self.stats.pages_failed += 1
            self._gui_update(url, "Fel", str(e)[:30])
            return False

    async def download_document(self, url: str):
        if self.state == CrawlerState.STOPPED: return
        while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)
            
        async with self.async_download_lock:
            if url in self.downloaded_files: return
            self.downloaded_files.add(url)
        
        try:
            docs_dir = os.path.join(self.output_dir, "dokument")
            os.makedirs(docs_dir, exist_ok=True)
            async with self.req_session.get(url, timeout=30) as resp:
                if resp.status != 200: return
                filename = ""
                if 'content-disposition' in resp.headers:
                    match = re.findall(r'filename="?([^"]+)"?', resp.headers['content-disposition'])
                    if match: filename = match[0]
                if not filename: filename = os.path.basename(unquote(urlparse(url).path)) or "dokument.pdf"
                ext = os.path.splitext(filename)[1].lower() or ".pdf"
                safe_filename = f"{slugify(filename.split('.')[0])[:50]}_{hashlib.md5(url.encode('utf-8')).hexdigest()[:6]}{ext}"
                filepath = os.path.join(docs_dir, safe_filename)

                if not os.path.exists(filepath):
                    with open(filepath, 'wb') as f:
                        while True:
                            if self.state == CrawlerState.STOPPED: return
                            chunk = await resp.content.read(8192)
                            if not chunk: break
                            f.write(chunk)
                    async with self.async_stats_lock:
                        self.stats.documents_downloaded += 1
                    self._log(f"  ⬇ Dokument sparat: {safe_filename}")
        except Exception as e: 
            self._log(f"  ✗ Fildnedladdning misslyckades: {url}, {e}", LogLevel.ERROR)

    async def _generate_index(self):
        self._log("📊 Skapar index-fil (index.csv)...")
        try:
            records = await self.db.get_all_records()
            with open(os.path.join(self.output_dir, "index.csv"), 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['URL', 'Titel', 'Hämtad_Datum', 'Filnamn'])
                for url, title, date, content_hash in records: 
                    safe_name = f"{slugify(urlparse(url).path)[:50]}_{content_hash[:6]}"
                    filename = f"{safe_name}.json" if self.save_format == ".json" else f"{safe_name}{self.save_format}"
                    writer.writerow([url, title, date, filename])
        except Exception as e: 
            self._log(f"⚠ Fel vid skapande av index.csv: {e}", LogLevel.ERROR)

    def pause(self):
        if self.state == CrawlerState.RUNNING:
            self.state = CrawlerState.PAUSED
            return True
        elif self.state == CrawlerState.PAUSED:
            self.state = CrawlerState.RUNNING
            return False

    def stop(self):
        self.state = CrawlerState.STOPPED
        self._log("🛑 Avbryter crawl (väntar på aktiva processer)...")
        self.login_event.set()

    async def crawl(self):
        await self.db.connect()
        
        if self.config["headless_mode"] == "login_then_headless":
            if HAS_PLAYWRIGHT:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=False)
                    context = await browser.new_context()
                    page = await context.new_page()
                    
                    self._log("👤 Navigerar till start-URL för manuell inloggning...")
                    await page.goto(self.start_url)
                    
                    self._log("\n⏳ VÄNTAR PÅ MANUELL INLOGGNING...")
                    if self.msg_queue: self.msg_queue.put(("login_wait", None)) 
                    await asyncio.to_thread(self.login_event.wait) 
                    
                    if self.state == CrawlerState.STOPPED: return
                    
                    self._log("🔄 Sparar cookies och byter till osynligt läge...")
                    self.saved_cookies = await context.cookies()
                    await browser.close()
            else:
                self._log("⚠ Playwright saknas, kan inte utföra manuell inloggning!", LogLevel.ERROR)

        self.req_session = await self._create_robust_session()
        
        # FIX BUG 2: Överför cookies korrekt till aiohttp via Cookie-headern
        if self.config.get("headless_mode") == "login_then_headless" and hasattr(self, 'saved_cookies'):
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in self.saved_cookies)
            self.req_session._default_headers["Cookie"] = cookie_header

        if self.config.get("respect_robots", True):
            await self._load_robots_txt()

        self.stats.start_time = datetime.now()

        try:
            self.active_tasks = 0
            
            async def bounded_process(url, depth):
                try:
                    while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)
                    if self.state == CrawlerState.STOPPED: return
                    
                    async with self.semaphore:
                        while self.state == CrawlerState.PAUSED: await asyncio.sleep(0.5)
                        if self.state == CrawlerState.STOPPED: return
                        await self.process_page(url, depth)
                except asyncio.CancelledError:
                    pass  # TaskGroup-avbrytning vid stopp, inte ett fel
                except Exception as e:
                    self._log(f"💥 Oväntat fel i bounded_process ({url}): {e}", LogLevel.ERROR)
                finally:
                    try:
                        async with self.async_stats_lock:
                            self.active_tasks -= 1
                    except Exception:
                        self.active_tasks = max(0, self.active_tasks - 1)
            
            async with asyncio.TaskGroup() as tg:
                while self.state != CrawlerState.STOPPED:
                    if self.state == CrawlerState.PAUSED:
                        await asyncio.sleep(1)
                        continue
                    if self.max_pages > 0 and self.stats.pages_visited >= self.max_pages: break
                    
                    queue_item = self.url_queue.get_next()
                    if not queue_item:
                        async with self.async_stats_lock:
                            tasks_are_zero = (self.active_tasks == 0)
                        
                        if self.url_queue.size() == 0 and tasks_are_zero: 
                            break
                        await asyncio.sleep(0.5)
                        continue
                        
                    async with self.async_stats_lock:
                        self.active_tasks += 1
                    tg.create_task(bounded_process(queue_item[1], queue_item[0]))

        except Exception as e: self._log(f"💥 Oväntat fel i async crawl: {e}", LogLevel.ERROR)
        finally:
            await self._generate_index()
            if self.req_session: await self.req_session.close()
            await self.db.close()
            if getattr(self, '_browser', None):
                try: await self._browser.close()
                except: pass
            if getattr(self, '_pw', None):
                try: await self._pw.stop()
                except: pass
            self.stats.end_time = datetime.now()
            self._log(f"Färdig! Total tid: {self.stats.duration}")
            if self.msg_queue: self.msg_queue.put(("done", "Klar"))

# ─────────────────────────────────────────────────────────────
#  SERVER / CLI LÄGE
# ─────────────────────────────────────────────────────────────
async def run_cli_mode(config_file: str, webhook_url: Optional[str] = None):
    print(f"🚀 Startar Webbdammsugare Pro Serverläge med filen: {config_file}")
    with open(config_file, 'r', encoding='utf-8') as f: sites_config = json.load(f)
    
    base_out = os.path.abspath("server_data")
    site_semaphore = asyncio.Semaphore(3)

    async def run_single_site(site):
        async with site_semaphore:
            site_out = os.path.join(base_out, slugify(site.get("name", "Unknown")))
            config = {**site, "output_dir": site_out, "headless_mode": "headless", "use_hybrid": True, "use_async": True}
            crawler = AsyncWebCrawler(config)
            await crawler.crawl()
            return site.get("name"), crawler

    tasks = [run_single_site(site) for site in sites_config]
    start_time = time.time()
    results = await asyncio.gather(*tasks)
    
    summary = "\n".join([f" - {name}: {c.stats.pages_visited} besökta, {c.stats.pages_failed} fel." for name, c in results])
    print(f"\n✅ Klart på {time.time() - start_time:.1f} sekunder!\n{summary}")

    if webhook_url:
        try: requests.post(webhook_url, json={"text": f"🚀 Nattens dammsugning klar!\n{summary}"})
        except: pass

# ─────────────────────────────────────────────────────────────
#  GRAFISKT GRÄNSSNITT (GUI - CustomTkinter)
# ─────────────────────────────────────────────────────────────
class AppGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.lang = "sv"
        self.texts = {
            "window_title": {"sv": "Webbdammsugare Pro (v6.0)", "en": "Web Crawler Pro (v6.0)"},
            "tab_basic": {"sv": "⚙️ Grundinställningar", "en": "⚙️ Basic Settings"},
            "tab_adv": {"sv": "🔧 Avancerat", "en": "🔧 Advanced"},
            "lbl_url": {"sv": "🌐 Startadress:", "en": "🌐 Start URL:"},
            "btn_help": {"sv": "❓ Hjälp", "en": "❓ Help"},
            "lbl_delay": {"sv": "Fördröjning (sek):", "en": "Delay (sec):"},
            "lbl_max_pages": {"sv": "Max sidor (0=Oändligt):", "en": "Max pages (0=Infinite):"},
            "lbl_max_depth": {"sv": "Max djup (0=Oändligt):", "en": "Max depth (0=Infinite):"},
            "lbl_format": {"sv": "Filformat:", "en": "File Format:"},
            "cb_docs": {"sv": "Ladda ner dokument (PDF m.m.)", "en": "Download documents (PDF etc.)"},
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
                "sv": "Besökta: {} | Sidor: {} | Dokument: {} | Oförändrade: {} | I Kö: {} | Fel: {} | Tid kvar: {}",
                "en": "Visited: {} | Pages: {} | Docs: {} | Unchanged: {} | Queued: {} | Errors: {} | ETA: {}"
            },
            "help_title": {"sv": "❓ Hjälp & Instruktioner", "en": "❓ Help & Instructions"},
            "run_modes": {
                "headless": {"sv": "Snabb (dold)", "en": "Fast (hidden)"},
                "login_then_headless": {"sv": "Logga in, sen dold", "en": "Login, then hidden"},
                "visible": {"sv": "Synlig (felsökning)", "en": "Visible (debugging)"}
            },
            "help_content": {
                "sv": "⚙️ GRUNDINSTÄLLNINGAR\n-------------------------\n* Startadress: URL där programmet börjar leta.\n* Fördröjning: Tid mellan sidbesök.\n* Max sidor/djup: 0 betyder oändligt.\n* Körläge:\n  - Snabb (dold): Snabbast, körs i bakgrunden.\n  - Logga in: Öppnar fönster för inloggning, kör sen dolt.\n* Filformat:\n  - .json: Strukturerad data anpassad för Vektordatabaser och AI.\n  - .md: Markdown, bra för generella LLM-läsningar.\n  - Ingen text: Skrapar enbart dokument (om ikryssat).\n\n📋 MALLAR\n-------------------------\nLägg en sites.json i samma mapp som programmet.\nVälj en mall i dropdown-menyn så fylls alla inställningar i automatiskt.\nDu behöver bara välja mapp och klicka Starta.\nSamma sites.json fungerar i serverläge (--config).\n\n🔧 AVANCERAT\n-------------------------\n* Hybrid-motor: Rekommenderas för modern webb.\n* URL-Filter: Filtrerar på ord i URL:en, inte i sidans text.\n  - Uteslut: Hoppar över URL:er som innehåller dessa ord.\n  - Kräv: Laddar BARA ner URL:er med minst ett av orden (ELLER-logik).\n* PII-Tvätt: Raderar personuppgifter automatiskt innan sparning.\n\n💻 SERVER-LÄGE\n-------------------------\nKörs via CMD för automatisering:\npython ultimate-web-crawler.py --config sites.json\n\n💡 TIPS: Dubbelklicka på en rad i tabellen för att öppna länken i din webbläsare!",
                "en": "⚙️ BASIC SETTINGS\n-------------------------\n* Start URL: Where the crawler begins.\n* Delay: Seconds to wait between requests.\n* Run Mode:\n  - Fast (hidden): Fastest option, background.\n  - Login: Shows browser for login, then background.\n* File Format:\n  - .json: Structured output tailored for Vector Databases and AI.\n  - .md: Markdown, great for general reading.\n  - No text: Only downloads documents (if checked).\n\n📋 TEMPLATES\n-------------------------\nPlace a sites.json in the same folder as the program.\nSelect a template from the dropdown to auto-fill all settings.\nJust choose an output folder and click Start.\nThe same sites.json works in server mode (--config).\n\n🔧 ADVANCED\n-------------------------\n* Hybrid Engine: Recommended for modern web.\n* URL Filters: Filters on words in the URL, not page text.\n  - Exclude: Skips URLs containing these words.\n  - Require: ONLY downloads URLs with at least one of the words (OR logic).\n* PII Wash: Automatically removes personal data before saving.\n\n💻 SERVER MODE\n-------------------------\nLaunch without GUI via CMD for automation:\npython ultimate-web-crawler.py --config sites.json\n\n💡 TIP: Double-click a row in the table to open the link in your browser!"
            }
        }
        
        self.root.title(self.texts["window_title"][self.lang])
        self.root.geometry("1000x850")
        self.msg_queue = queue.Queue()
        self.crawler_instance = None
        self._update_treeview_style("Light")
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self.process_queue)
    
    def _on_closing(self):
        if self.crawler_instance: self.crawler_instance.stop()
        self.root.destroy()

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
        if new_lang == self.lang: return

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
        self.cb_docs.configure(text=self.texts["cb_docs"][self.lang])
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
        
        self.start_btn.configure(text=self.texts["btn_start"][self.lang])
        if self.crawler_instance and self.crawler_instance.state == CrawlerState.PAUSED:
            self.pause_btn.configure(text=self.texts["btn_resume"][self.lang])
        else:
            self.pause_btn.configure(text=self.texts["btn_pause"][self.lang])
        self.stop_btn.configure(text=self.texts["btn_stop"][self.lang])
        
        self.tree.heading('Status', text=self.texts["col_status"][self.lang])
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang])
        if not self.crawler_instance or self.crawler_instance.state in [CrawlerState.IDLE, CrawlerState.STOPPED]:
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
            style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", borderwidth=0, rowheight=25)
            style.configure("Treeview.Heading", background="#565b5e", foreground="white", font=('Arial', 10, 'bold'), relief="flat")
            style.map('Treeview', background=[('selected', '#1f538d')])
            style.map("Treeview.Heading", background=[('active', '#343638')])
        else:
            style.configure("Treeview", background="#ffffff", foreground="black", fieldbackground="#ffffff", borderwidth=0, rowheight=25)
            style.configure("Treeview.Heading", background="#e5e5e5", foreground="black", font=('Arial', 10, 'bold'), relief="flat")
            style.map('Treeview', background=[('selected', '#3a7ebf')]) 
            style.map("Treeview.Heading", background=[('active', '#d1d1d1')])

    def open_help_window(self):
        help_win = ctk.CTkToplevel(self.root)
        help_win.title(self.texts["help_title"][self.lang])
        help_win.geometry("600x550")
        help_text = ctk.CTkTextbox(help_win, wrap=tk.WORD, font=ctk.CTkFont(family="Arial", size=13))
        help_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        help_text.insert(tk.END, self.texts["help_content"][self.lang])
        help_text.configure(state="disabled")

    def choose_directory(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.configure(state="normal")
            self.dir_var.set(d)
            self.dir_entry.configure(state="readonly")
    
    def _find_templates(self) -> Dict:
        """Letar efter sites.json i samma mapp som skriptet (eller .exe-filen)."""
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
                    return {site.get("name", f"Sajt {i+1}"): site for i, site in enumerate(sites)}
            except Exception:
                pass
        return {}

    def _apply_template(self, choice):
        """Fyller i GUI-fälten från vald mall."""
        none_label = self.texts["template_none"][self.lang]
        if choice == none_label:
            return
        
        site = self.templates.get(choice)
        if not site:
            return
        
        # URL
        if site.get("start_url"):
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, site["start_url"])
        
        # Grundinställningar
        if "delay" in site:
            self.delay_entry.delete(0, tk.END)
            self.delay_entry.insert(0, str(site["delay"]))
        if "max_pages" in site:
            self.max_pages_entry.delete(0, tk.END)
            self.max_pages_entry.insert(0, str(site["max_pages"]))
        if "max_depth" in site:
            self.max_depth_entry.delete(0, tk.END)
            self.max_depth_entry.insert(0, str(site["max_depth"]))
        if "save_format" in site:
            self.format_var.set(site["save_format"])
        if "download_docs" in site:
            self.docs_var.set(site["download_docs"])
        if "headless_mode" in site:
            mode_key = site["headless_mode"]
            if mode_key in self.texts["run_modes"]:
                self.headless_var.set(self.texts["run_modes"][mode_key][self.lang])
        
        # Avancerat
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
        
        # URL-filter
        if "exclude_keywords" in site:
            self.exclude_entry.delete(0, tk.END)
            kws = site["exclude_keywords"]
            self.exclude_entry.insert(0, ", ".join(kws) if isinstance(kws, list) else kws)
        if "require_keywords" in site:
            self.require_entry.delete(0, tk.END)
            kws = site["require_keywords"]
            self.require_entry.insert(0, ", ".join(kws) if isinstance(kws, list) else kws)
        
        # PII
        if "remove_email" in site: self.rm_email_var.set(site["remove_email"])
        if "remove_phone" in site: self.rm_phone_var.set(site["remove_phone"])
        if "remove_pnr" in site: self.rm_pnr_var.set(site["remove_pnr"])
        if "remove_ip" in site: self.rm_ip_var.set(site["remove_ip"])
        
        # Skapa en unik undermapp baserat på mallens namn
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
        
        # Top URL Bar
        url_frame = ctk.CTkFrame(main_frame)
        url_frame.pack(fill=tk.X, pady=(0, 10))
        self.lbl_url = ctk.CTkLabel(url_frame, text=self.texts["lbl_url"][self.lang], font=ctk.CTkFont(weight="bold"))
        self.lbl_url.pack(side=tk.LEFT, padx=(15, 10), pady=15)
        self.url_entry = ctk.CTkEntry(url_frame, width=250, placeholder_text="https://...")
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 15), pady=15)
        self.url_entry.insert(0, "https://")
        
        self.lang_var = ctk.StringVar(value="🇸🇪 SV")
        self.lang_switch = ctk.CTkSegmentedButton(url_frame, values=["🇸🇪 SV", "🇬🇧 EN"], variable=self.lang_var, command=self.change_language_event)
        self.lang_switch.pack(side=tk.RIGHT, padx=15, pady=15)
        self.theme_switch = ctk.CTkSwitch(url_frame, text="☀️", width=40, command=self.change_appearance_mode_event)
        self.theme_switch.pack(side=tk.RIGHT, padx=(0, 15), pady=15)
        self.theme_switch.deselect() 
        self.help_btn = ctk.CTkButton(url_frame, text=self.texts["btn_help"][self.lang], width=80, fg_color=("#d9d9d9", "#4a4a4a"), text_color=("black", "white"), hover_color=("#c9c9c9", "#5a5a5a"), command=self.open_help_window)
        self.help_btn.pack(side=tk.RIGHT, padx=(0, 15), pady=15)
        
        # Template row
        self.templates = self._find_templates()
        if self.templates:
            tpl_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
            tpl_frame.pack(fill=tk.X, pady=(0, 5))
            self.lbl_template = ctk.CTkLabel(tpl_frame, text=self.texts["lbl_template"][self.lang], font=ctk.CTkFont(weight="bold"))
            self.lbl_template.pack(side=tk.LEFT, padx=(15, 10))
            none_label = self.texts["template_none"][self.lang]
            template_names = [none_label] + list(self.templates.keys())
            self.template_var = ctk.StringVar(value=none_label)
            self.template_menu = ctk.CTkOptionMenu(tpl_frame, variable=self.template_var, values=template_names, width=300, command=self._apply_template)
            self.template_menu.pack(side=tk.LEFT, padx=(0, 15))
        
        # Tabs
        self.tabview = ctk.CTkTabview(main_frame, height=230)
        self.tabview.pack(fill=tk.X, pady=(0, 10))
        tab_basic = self.tabview.add(self.texts["tab_basic"][self.lang])
        tab_adv = self.tabview.add(self.texts["tab_adv"][self.lang])
        
        # -------------------------------------------------------------
        # Tab 1: Grundinställningar
        # -------------------------------------------------------------
        tab_basic.grid_columnconfigure(1, weight=1)
        tab_basic.grid_columnconfigure(3, weight=1)

        self.lbl_delay = ctk.CTkLabel(tab_basic, text=self.texts["lbl_delay"][self.lang])
        self.lbl_delay.grid(row=0, column=0, padx=(10, 5), pady=10, sticky="e")
        self.delay_entry = ctk.CTkEntry(tab_basic, width=80)
        self.delay_entry.insert(0, "0.5")
        self.delay_entry.grid(row=0, column=1, padx=(0, 20), pady=10, sticky="w")

        self.lbl_max_pages = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_pages"][self.lang])
        self.lbl_max_pages.grid(row=0, column=2, padx=(10, 5), pady=10, sticky="e")
        self.max_pages_entry = ctk.CTkEntry(tab_basic, width=80)
        self.max_pages_entry.insert(0, "0")
        self.max_pages_entry.grid(row=0, column=3, padx=(0, 10), pady=10, sticky="w")

        self.lbl_max_depth = ctk.CTkLabel(tab_basic, text=self.texts["lbl_max_depth"][self.lang])
        self.lbl_max_depth.grid(row=1, column=0, padx=(10, 5), pady=10, sticky="e")
        self.max_depth_entry = ctk.CTkEntry(tab_basic, width=80)
        self.max_depth_entry.insert(0, "0")
        self.max_depth_entry.grid(row=1, column=1, padx=(0, 20), pady=10, sticky="w")

        self.lbl_format = ctk.CTkLabel(tab_basic, text=self.texts["lbl_format"][self.lang])
        self.lbl_format.grid(row=1, column=2, padx=(10, 5), pady=10, sticky="e")
        self.format_var = ctk.StringVar(value=".json")
        self.format_menu = ctk.CTkOptionMenu(tab_basic, variable=self.format_var, values=[".json", ".md", ".txt", "Ingen text"], width=100)
        self.format_menu.grid(row=1, column=3, padx=(0, 10), pady=10, sticky="w")

        self.docs_var = ctk.BooleanVar(value=False)
        self.cb_docs = ctk.CTkCheckBox(tab_basic, text=self.texts["cb_docs"][self.lang], variable=self.docs_var)
        self.cb_docs.grid(row=2, column=0, columnspan=2, padx=10, pady=10, sticky="w")

        self.lbl_mode = ctk.CTkLabel(tab_basic, text=self.texts["lbl_mode"][self.lang])
        self.lbl_mode.grid(row=2, column=2, padx=(10, 5), pady=10, sticky="e")
        self.headless_var = ctk.StringVar(value=self.texts["run_modes"]["headless"][self.lang])
        self.headless_menu = ctk.CTkOptionMenu(tab_basic, variable=self.headless_var, values=[v[self.lang] for v in self.texts["run_modes"].values()], width=180)
        self.headless_menu.grid(row=2, column=3, padx=(0, 10), pady=10, sticky="w")

        self.lbl_folder = ctk.CTkLabel(tab_basic, text=self.texts["lbl_folder"][self.lang])
        self.lbl_folder.grid(row=3, column=0, padx=(10, 5), pady=10, sticky="e")
        self.dir_var = ctk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output"))
        self.dir_entry = ctk.CTkEntry(tab_basic, textvariable=self.dir_var, state="readonly")
        self.dir_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=10)
        self.btn_folder = ctk.CTkButton(tab_basic, text=self.texts["btn_folder"][self.lang], width=100, command=self.choose_directory)
        self.btn_folder.grid(row=3, column=3, padx=(0, 10), pady=10, sticky="w")

        # -------------------------------------------------------------
        # Tab 2: Avancerat
        # -------------------------------------------------------------
        tab_adv.grid_columnconfigure(1, weight=1)

        self.hybrid_var = ctk.BooleanVar(value=True)
        self.cb_hybrid = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_hybrid"][self.lang], variable=self.hybrid_var)
        self.cb_hybrid.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        
        self.traf_var = ctk.BooleanVar(value=HAS_TRAFILATURA)
        self.cb_traf = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_traf"][self.lang], variable=self.traf_var)
        self.cb_traf.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        if not HAS_TRAFILATURA: self.cb_traf.configure(state="disabled")

        self.sitemap_var = ctk.BooleanVar(value=True)
        self.cb_sitemap = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_sitemap"][self.lang], variable=self.sitemap_var)
        self.cb_sitemap.grid(row=1, column=0, padx=10, pady=5, sticky="w")
        
        self.robots_var = ctk.BooleanVar(value=True)
        self.cb_robots = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_robots"][self.lang], variable=self.robots_var)
        self.cb_robots.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        
        self.strict_var = ctk.BooleanVar(value=True) 
        self.cb_strict = ctk.CTkCheckBox(tab_adv, text=self.texts["cb_strict"][self.lang], variable=self.strict_var)
        self.cb_strict.grid(row=1, column=2, padx=10, pady=5, sticky="w")

        self.lbl_exclude = ctk.CTkLabel(tab_adv, text=self.texts["lbl_exclude"][self.lang])
        self.lbl_exclude.grid(row=2, column=0, padx=(10, 5), pady=5, sticky="e")
        self.exclude_entry = ctk.CTkEntry(tab_adv, placeholder_text="images, login, kalender")
        self.exclude_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)
        
        self.lbl_require = ctk.CTkLabel(tab_adv, text=self.texts["lbl_require"][self.lang])
        self.lbl_require.grid(row=3, column=0, padx=(10, 5), pady=5, sticky="e")
        self.require_entry = ctk.CTkEntry(tab_adv, placeholder_text="intranat, bibliotek")
        self.require_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5)
        
        arow4 = ctk.CTkFrame(tab_adv, fg_color="transparent")
        arow4.grid(row=4, column=0, columnspan=3, pady=(10,0), sticky="w")
        
        self.rm_email_var = ctk.BooleanVar(value=False)
        self.cb_rm_email = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_email"][self.lang], variable=self.rm_email_var)
        self.cb_rm_email.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        
        self.rm_phone_var = ctk.BooleanVar(value=False)
        self.cb_rm_phone = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_phone"][self.lang], variable=self.rm_phone_var)
        self.cb_rm_phone.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        
        self.rm_pnr_var = ctk.BooleanVar(value=False)
        self.cb_rm_pnr = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_pnr"][self.lang], variable=self.rm_pnr_var)
        self.cb_rm_pnr.grid(row=0, column=2, padx=10, pady=5, sticky="w")
        
        self.rm_ip_var = ctk.BooleanVar(value=False)
        self.cb_rm_ip = ctk.CTkCheckBox(arow4, text=self.texts["cb_rm_ip"][self.lang], variable=self.rm_ip_var)
        self.cb_rm_ip.grid(row=1, column=0, padx=10, pady=5, sticky="w")

        # -------------------------------------------------------------
        # Control Buttons
        # -------------------------------------------------------------
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(pady=5)
        self.start_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_start"][self.lang], font=ctk.CTkFont(weight="bold"), fg_color="#1f6aa5", command=self.start_crawl)
        self.start_btn.pack(side=tk.LEFT, padx=10)
        self.pause_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_pause"][self.lang], state="disabled", fg_color=("#d9d9d9", "#4a4a4a"), text_color=("black", "white"), hover_color=("#c9c9c9", "#5a5a5a"), command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=10)
        self.stop_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_stop"][self.lang], state="disabled", fg_color=("#d35b5b", "#a51f1f"), hover_color=("#c42b2b", "#8a1a1a"), text_color=("white", "white"), command=self.stop_crawl)
        self.stop_btn.pack(side=tk.LEFT, padx=10)
        
        # Stats & Display
        self.stats_label = ctk.CTkLabel(main_frame, text=self.texts["status_wait"][self.lang], font=ctk.CTkFont(family="Consolas", size=12, weight="bold"), text_color="#4caf50")
        self.stats_label.pack(fill=tk.X, pady=5)
        self.progress_bar = ctk.CTkProgressBar(main_frame, orientation="horizontal")
        
        table_frame = ctk.CTkFrame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table_frame, columns=('URL', 'Status', 'Titel'), show='headings', height=7)
        self.tree.heading('URL', text='URL')
        self.tree.heading('Status', text=self.texts["col_status"][self.lang])
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang])
        self.tree.column('URL', width=300)
        self.tree.column('Status', width=100)
        self.tree.column('Titel', width=300)
        
        scrollbar = ctk.CTkScrollbar(table_frame, orientation="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.tree.bind("<Double-1>", lambda e: webbrowser.open(self.tree.item(self.tree.selection()[0])['values'][0]) if self.tree.selection() and self.tree.item(self.tree.selection()[0])['values'][0].startswith("http") else None)
        
        self.log_area = ctk.CTkTextbox(main_frame, font=ctk.CTkFont(family="Consolas", size=11), height=100)
        self.log_area.pack(fill=tk.BOTH, expand=False, pady=(10,0))

    def process_queue(self):
        # FIX BUG 3: Try/Except inuti loopen så att GUI:t aldrig fryser om ett fel inträffar
        try:
            for _ in range(20):
                try:
                    msg_type, data = self.msg_queue.get_nowait()
                    if msg_type == "log":
                        t = datetime.now().strftime("%H:%M:%S")
                        self.log_area.insert(tk.END, f"[{t}] {data}\n")
                        self.log_area.see(tk.END)
                        if int(self.log_area.index('end-1c').split('.')[0]) > 500:
                            self.log_area.delete("1.0", "2.0")
                    elif msg_type == "table":
                        self.tree.insert('', 0, values=data)
                        if len(self.tree.get_children()) > 100: self.tree.delete(self.tree.get_children()[-1])
                    elif msg_type == "stats_data":
                        self.stats_label.configure(text=self.texts["stats_fmt"][self.lang].format(*data))
                        if self.progress_bar and self.max_pages_entry.get() != "0":
                            try:
                                max_p = int(self.max_pages_entry.get())
                                if max_p > 0: self.progress_bar.set(data[0] / max_p)
                            except ValueError: pass
                    elif msg_type == "login_wait":
                        messagebox.showinfo("Inloggning / Login", "Logga in i webbläsaren. Tryck OK här när du är klar!" if self.lang == "sv" else "Please login in the browser. Click OK here when done!")
                        if self.crawler_instance: self.crawler_instance.login_event.set()
                    elif msg_type == "done":
                        self.start_btn.configure(state="normal")
                        self.pause_btn.configure(state="disabled")
                        self.stop_btn.configure(state="disabled")
                        self.progress_bar.stop()
                        self.progress_bar.pack_forget()
                except queue.Empty:
                    break
                except Exception as inner_e:
                    # Kasta ut eventuella rit-fel till loggen istället för att krascha
                    self.log_area.insert(tk.END, f"[GUI FEL] Kunde inte rita rad: {inner_e}\n")
        finally:
            # Garanterar att loopen alltid fortsätter att lyssna
            self.root.after(100, self.process_queue)
    
    def start_crawl(self):
        url = self.url_entry.get().strip()
        if not url or url == "https://": return
        try:
            delay_val = float(self.delay_entry.get())
            max_pages_val = int(self.max_pages_entry.get())
            max_depth_val = int(self.max_depth_entry.get())
        except ValueError: return

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

        inverted_map = {v[self.lang]: k for k, v in self.texts["run_modes"].items()}
        
        config = {
            "start_url": url,
            "output_dir": self.dir_var.get(),
            "delay": delay_val,
            "max_pages": max_pages_val,
            "max_depth": max_depth_val,
            "save_format": self.format_var.get(),
            "headless_mode": inverted_map.get(self.headless_var.get(), "headless"),
            "respect_robots": self.robots_var.get(),
            "find_sitemap": self.sitemap_var.get(),
            "use_hybrid": self.hybrid_var.get(),
            "use_trafilatura": self.traf_var.get(),
            "download_docs": self.docs_var.get(),
            "strict_domain": self.strict_var.get(),
            "exclude_keywords": [k.strip().lower() for k in self.exclude_entry.get().split(",") if k.strip()],
            "require_keywords": [k.strip().lower() for k in self.require_entry.get().split(",") if k.strip()],
            "remove_email": self.rm_email_var.get(),
            "remove_phone": self.rm_phone_var.get(),
            "remove_pnr": self.rm_pnr_var.get(),
            "remove_ip": self.rm_ip_var.get(),
            "incremental": True
        }
        
        self.crawler_instance = AsyncWebCrawler(config, self.msg_queue)
        threading.Thread(target=lambda: asyncio.run(self.crawler_instance.crawl()), daemon=True).start()

    def toggle_pause(self):
        if self.crawler_instance:
            is_paused = self.crawler_instance.pause()
            self.pause_btn.configure(text=self.texts["btn_resume"][self.lang] if is_paused else self.texts["btn_pause"][self.lang])

    def stop_crawl(self):
        if self.crawler_instance:
            self.stop_btn.configure(state="disabled")
            self.crawler_instance.stop()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str)
    parser.add_argument("--webhook", type=str)
    args, _ = parser.parse_known_args()

    if HAS_UVLOOP: uvloop.install()

    if args.config:
        asyncio.run(run_cli_mode(args.config, args.webhook or os.environ.get("WEBHOOK_URL")))
    else:
        AppGUI(ctk.CTk()).root.mainloop()

if __name__ == "__main__":
    main()