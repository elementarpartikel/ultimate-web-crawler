#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webbdammsugare Pro (v1.4) - Professionellt verktyg för webbskrapning
Skapad av Fredrik Eriksson

Funktioner: AI-optimerad, Hybrid, SQLite (WAL), Concurrency Safe, Auto-Index CSV
"""

import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog, ttk
import threading
import queue
import time
import os
import hashlib
import sqlite3
import csv
import gc
import re
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, unquote
from urllib.robotparser import RobotFileParser
from collections import deque
from dataclasses import dataclass, field
from typing import Set, Dict, List, Optional, Tuple
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Frivilliga, men kraftfulla integrationer
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    HAS_SELENIUM_MANAGER = True
except ImportError:
    HAS_SELENIUM_MANAGER = False
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        pass

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ─────────────────────────────────────────────────────────────
#  ENUMS OCH DATACLASSES
# ─────────────────────────────────────────────────────────────

class LogLevel(Enum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR

class CrawlPriority(Enum):
    SITEMAP = 1
    HIGH = 5
    MEDIUM = 10
    LOW = 15

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
    selenium_fallbacks: int = 0
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
#  HJÄLPFUNKTIONER OCH DATABAS (SQLite)
# ─────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = str(text).replace('å', 'a').replace('ä', 'a').replace('ö', 'o')
    text = text.replace('Å', 'A').replace('Ä', 'A').replace('Ö', 'O')
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '-', text).strip('-').lower()

def normalize_url(url: str, ignore_query_params: Optional[List[str]] = None) -> str:
    if ignore_query_params is None:
        ignore_query_params = [
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'fbclid', 'gclid', 'msclkid', 'ref', 'source', 'sessionid', 'phpsessid', 'sid'
        ]
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
    except Exception:
        return url.strip()

def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

class CrawlDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL") # FIX: WAL-mode för prestanda
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS page_cache (
                    url TEXT PRIMARY KEY,
                    content_hash TEXT,
                    title TEXT,
                    crawled_at TEXT,
                    content_length INTEGER
                )
            ''')
            self.conn.commit()

    def get_cache(self, url: str) -> Optional[Dict]:
        with self.lock:
            cursor = self.conn.execute("SELECT content_hash, title, crawled_at, content_length FROM page_cache WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row:
                return {'hash': row[0], 'title': row[1], 'crawled_at': row[2], 'content_length': row[3]}
        return None

    def save_cache(self, url: str, content_hash: str, title: str, length: int):
        with self.lock:
            self.conn.execute('''
                INSERT OR REPLACE INTO page_cache (url, content_hash, title, crawled_at, content_length)
                VALUES (?, ?, ?, ?, ?)
            ''', (url, content_hash, title, datetime.now().isoformat(), length))
            self.conn.commit()
            
    def get_all_records(self):
        with self.lock:
            cursor = self.conn.execute("SELECT url, title, crawled_at FROM page_cache ORDER BY crawled_at DESC")
            return cursor.fetchall()
        
    def close(self):
        with self.lock:
            self.conn.close()


# ─────────────────────────────────────────────────────────────
#  RATE LIMITER & KÖ
# ─────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.delay = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self.last_request = datetime.min
        self._lock = threading.Lock()
    
    def wait_if_needed(self):
        # FIX: Sover utanför låset för att förhindra tråd-blockering
        sleep_time = 0
        with self._lock:
            now = datetime.now()
            elapsed = (now - self.last_request).total_seconds()
            if elapsed < self.delay:
                sleep_time = self.delay - elapsed
                self.last_request = now + timedelta(seconds=sleep_time)
            else:
                self.last_request = now
        
        if sleep_time > 0:
            time.sleep(sleep_time)

class PriorityURLQueue:
    def __init__(self):
        self.queue = queue.PriorityQueue()
        self.seen_urls: Set[str] = set()
        self._lock = threading.Lock()
    
    def add_url(self, url: str, depth: int = 0, priority: int = CrawlPriority.MEDIUM.value):
        normalized = normalize_url(url)
        with self._lock:
            if normalized not in self.seen_urls:
                self.queue.put((priority, time.time(), depth, normalized))
                self.seen_urls.add(normalized)
                return True
        return False
    
    def get_next(self) -> Optional[Tuple[int, str]]:
        try: 
            item = self.queue.get_nowait()
            return (item[2], item[3])
        except queue.Empty: 
            return None
    
    def size(self) -> int: return self.queue.qsize()


# ─────────────────────────────────────────────────────────────
#  WEBB CRAWLER CORE
# ─────────────────────────────────────────────────────────────

class WebCrawler:
    def __init__(self, config: dict, msg_queue: queue.Queue):
        self.config = config
        self.msg_queue = msg_queue
        self.state = CrawlerState.RUNNING
        self.stats = CrawlStats()
        
        self.start_url = normalize_url(config["start_url"])
        self.output_dir = config["output_dir"]
        self.delay = config["delay"]
        self.max_pages = config["max_pages"]
        self.max_depth = config.get("max_depth", 0)
        self.save_format = config.get("save_format", ".md")
        
        self.use_hybrid = config.get("use_hybrid", True)
        self.use_trafilatura = config.get("use_trafilatura", HAS_TRAFILATURA)
        self.find_sitemap = config.get("find_sitemap", True)
        self.strict_domain = config.get("strict_domain", True) # FIX: Nu inställningsbar
        
        parsed_start = urlparse(self.start_url)
        self.domain = parsed_start.netloc.lower()
        self.base_url = f"{parsed_start.scheme}://{parsed_start.netloc}"
        
        os.makedirs(self.output_dir, exist_ok=True)
        db_path = os.path.join(self.output_dir, f"{slugify(self.domain)}_cache.db")
        self.db = CrawlDatabase(db_path)
        
        self.url_queue = PriorityURLQueue()
        self.url_queue.add_url(self.start_url, depth=0, priority=CrawlPriority.HIGH.value)
        self.rate_limiter = RateLimiter(requests_per_second=1.0 / max(self.delay, 0.1))
        
        self.download_pool = ThreadPoolExecutor(max_workers=3)
        self.downloaded_files: Set[str] = set()
        self.download_lock = threading.Lock() # FIX: Trådlås för dokument
        self.visited_sitemaps: Set[str] = set()
        
        self.login_event = threading.Event() # FIX: Händelse för inloggnings-popup
        
        self.doc_extensions = {
            '.pdf', '.doc', '.docx', '.docm', '.xls', '.xlsx', '.xlsm', 
            '.ppt', '.pptx', '.pptm', '.csv', '.rtf', '.txt', '.md', '.pub', '.odt', '.ods'
        }
        self.ignore_extensions = {
            '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.heic', '.heif', 
            '.bmp', '.ico', '.tiff', '.zip', '.rar', '.7z', '.tar', '.gz', 
            '.exe', '.msi', '.dmg', '.mp3', '.mp4', '.avi', '.mov', 
            '.css', '.js', '.json', '.xml'
        }
        
        self.crawl_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._setup_logging()
        
        self.req_session = self._create_robust_session()
        
        self.robot_parser = None
        if config["respect_robots"]: self._load_robots_txt()
        
        self.driver = None
        self._log(f"🚀 Initierar crawl för {self.domain} (V1.4 - Pro Edition)")

    def _create_robust_session(self):
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _setup_logging(self):
        self.logger = logging.getLogger(f'Crawler_{id(self)}')
        self.logger.setLevel(logging.DEBUG)
        log_dir = os.path.join(self.output_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, f'crawl_{self.crawl_session_id}.log'), maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
        self.logger.addHandler(fh)

    def _log(self, msg: str, level=LogLevel.INFO):
        if level == LogLevel.DEBUG: self.logger.debug(msg)
        elif level == LogLevel.INFO: self.logger.info(msg)
        elif level == LogLevel.WARNING: self.logger.warning(msg)
        elif level == LogLevel.ERROR: self.logger.error(msg)
        self.msg_queue.put(("log", f"[{level.name}] {msg}"))

    def _gui_update(self, url: str, status: str, title: str):
        self.msg_queue.put(("table", (url, status, title)))
        
        # FIX: Tydligare statisktik i GUI
        nya_eller_sparade = self.stats.pages_visited - self.stats.pages_unchanged
        stats_msg = (
            f"Besökta totalt: {self.stats.pages_visited} | "
            f"Nya/Sparade: {nya_eller_sparade} | "
            f"Oförändrade: {self.stats.pages_unchanged} | "
            f"I Kö: {self.url_queue.size()} | "
            f"Fel: {self.stats.pages_failed}"
        )
        self.msg_queue.put(("stats", stats_msg))

    def _load_robots_txt(self):
        try:
            resp = self.req_session.get(f"{self.base_url}/robots.txt", timeout=5)
            if resp.status_code == 200:
                self.robot_parser = RobotFileParser()
                self.robot_parser.parse(resp.text.splitlines())
                self._log("✓ robots.txt inläst")
                
                if self.find_sitemap:
                    sitemaps = [line.split(': ', 1)[1].strip() for line in resp.text.splitlines() if line.lower().startswith('sitemap:')]
                    if not sitemaps: sitemaps = [f"{self.base_url}/sitemap.xml"]
                    for sm in sitemaps: self._parse_sitemap(sm)
        except Exception as e:
            self._log(f"Kunde inte läsa robots.txt: {e}", LogLevel.DEBUG)

    def _parse_sitemap(self, url: str):
        if url in self.visited_sitemaps: return
        self.visited_sitemaps.add(url)
        
        self._log(f"🗺️ Letar i sitemap: {url}")
        try:
            resp = self.req_session.get(url, timeout=15)
            if resp.status_code == 200:
                # FIX: Fallback till html.parser om lxml saknas
                try:
                    soup = BeautifulSoup(resp.content, 'xml')
                except Exception:
                    soup = BeautifulSoup(resp.content, 'html.parser')
                
                sub_sitemaps = soup.find_all('sitemap')
                for sm in sub_sitemaps:
                    loc = sm.find('loc')
                    if loc: self._parse_sitemap(loc.text.strip())
                
                count = 0
                for url_node in soup.find_all('url'):
                    loc = url_node.find('loc')
                    if loc and self.url_queue.add_url(loc.text.strip(), depth=0, priority=CrawlPriority.SITEMAP.value):
                        count += 1
                if count > 0: self._log(f"✓ Hittade {count} URLs i sitemap/index")
        except Exception as e:
            self._log(f"⚠ Fel vid sitemap-läsning: {e}", LogLevel.DEBUG)

    def is_valid_url(self, url: str) -> bool:
        if len(url) > 2000:
            return False

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ['http', 'https']: return False
            ext = os.path.splitext(parsed.path)[1].lower()
            if ext in self.ignore_extensions or ext in self.doc_extensions: return False
            
            domain_core = self.domain.replace('www.', '')
            link_domain = parsed.netloc.lower().replace('www.', '')
            
            if self.strict_domain and link_domain != domain_core: return False
            if not self.strict_domain and domain_core not in link_domain: return False
            
            lower_url = url.lower()
            
            for kw in self.config.get("exclude_keywords", []):
                if kw and kw in lower_url: return False
                
            req_kws = self.config.get("require_keywords", [])
            if req_kws:
                has_required = False
                for kw in req_kws:
                    if kw in lower_url:
                        has_required = True
                        break
                if not has_required:
                    return False
                
            if self.robot_parser and not self.robot_parser.can_fetch('*', url): return False
            return True
        except Exception: 
            return False

    def get_chrome_options(self, is_headless: bool = False) -> Options:
        opts = Options()
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)
        
        prefs = {}
        if self.config.get("download_docs", False):
            docs_dir = os.path.abspath(os.path.join(self.output_dir, "dokument"))
            os.makedirs(docs_dir, exist_ok=True)
            prefs["download.default_directory"] = docs_dir
            prefs["download.prompt_for_download"] = False
            prefs["download.directory_upgrade"] = True
            prefs["plugins.always_open_pdf_externally"] = True
        else:
            prefs["download.download_restrictions"] = 3
            
        opts.add_experimental_option("prefs", prefs)

        if is_headless:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--blink-settings=imagesEnabled=false")
        return opts

    def create_driver(self, is_headless: bool = False):
        opts = self.get_chrome_options(is_headless)
        try:
            if HAS_SELENIUM_MANAGER:
                driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
            else:
                driver = webdriver.Chrome(options=opts)
            
            driver.implicitly_wait(10)
            driver.set_page_load_timeout(30)
            return driver
        except Exception as e:
            self._log(f"Kunde inte starta Chrome: {e}", LogLevel.ERROR)
            return None

    def get_chrome_driver(self):
        if not self.driver:
            self.driver = self.create_driver(is_headless=(self.config["headless_mode"] == "headless"))
            if not self.driver:
                self.use_hybrid = True 
        return self.driver

    def extract_text(self, html: str) -> Tuple[str, str]:
        title = ""
        text = ""
        soup = BeautifulSoup(html, 'html.parser')
        if soup.title: title = soup.title.string.strip() if soup.title.string else ""
        
        is_markdown = (self.save_format == ".md")
        
        if self.use_trafilatura and HAS_TRAFILATURA:
            try:
                if is_markdown:
                    extracted = trafilatura.extract(html, include_links=False, include_images=False, output_format="markdown")
                else:
                    extracted = trafilatura.extract(html, include_links=False, include_images=False, include_formatting=True)
                
                if extracted: return title, extracted
            except Exception as e:
                self._log(f"  ⚠ Trafilatura misslyckades ({e}), byter till reserv-motor...", LogLevel.DEBUG)
            
        for tag in ['script', 'style', 'header', 'footer', 'nav', 'noscript', 'aside', 'svg', 'form']:
            for el in soup.find_all(tag): el.decompose()
            
        raw_text = soup.get_text(separator='\n')
        lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 1]
        
        text = '\n'.join(lines)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return title, text

    def check_canonical(self, soup: BeautifulSoup, current_url: str) -> Optional[str]:
        link = soup.find('link', rel='canonical')
        if link and link.get('href'):
            canonical_url = normalize_url(urljoin(current_url, link['href']))
            if canonical_url != current_url and self.is_valid_url(canonical_url):
                return canonical_url
        return None

    def url_to_filename(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.netloc + parsed.path
        if parsed.query:
            path += "_" + parsed.query
            
        if path.endswith('/'):
            path += "index"
        
        # FIX: Använd slugify istället för att läcka åäö till filsystemet
        safe_name = slugify(path)
        hash_str = hashlib.md5(url.encode('utf-8')).hexdigest()[:6]
        
        if len(safe_name) > 150:
            safe_name = safe_name[:150]
            
        return f"{safe_name}_{hash_str}{self.save_format}"

    def process_page(self, url: str, depth: int) -> bool:
        self.rate_limiter.wait_if_needed()
        html = ""
        source = "Requests"
        
        try:
            if self.use_hybrid:
                resp = self.req_session.get(url, timeout=15, stream=True)
                resp.raise_for_status()
                
                content_type = resp.headers.get('Content-Type', '').lower()
                
                if 'image/' in content_type or 'video/' in content_type or 'audio/' in content_type:
                    self._log(f"  ⊘ Ignorerar mediafil (Content-Type): {url}", LogLevel.DEBUG)
                    return False
                
                doc_types = ['application/pdf', 'application/vnd', 'application/msword', 'application/epub', 'application/octet-stream']
                if any(dt in content_type for dt in doc_types):
                    if self.config["download_docs"]:
                        self._log(f"  ↳ Upptäckte fil via Content-Type, styr om till dokument-mappen...", LogLevel.DEBUG)
                        self.download_pool.submit(self.download_document, url)
                    return True

                html = resp.text
                
                js_warnings = ["du måste aktivera javascript", "enable javascript", "requires javascript", "javascript is disabled"]
                needs_js = len(html) < 1000 or any(warn in html.lower() for warn in js_warnings)
                
                if needs_js:
                    self._log("  ↻ Sidan verkar kräva JS, byter till Selenium...", LogLevel.DEBUG)
                    driver = self.get_chrome_driver()
                    if driver:
                        driver.get(url)
                        time.sleep(2) 
                        html = driver.page_source
                        source = "Selenium"
                        self.stats.selenium_fallbacks += 1
            else:
                driver = self.get_chrome_driver()
                driver.get(url)
                time.sleep(self.delay)
                html = driver.page_source
                source = "Selenium"

            soup = BeautifulSoup(html, 'html.parser')
            
            canonical = self.check_canonical(soup, url)
            if canonical:
                self._log(f"  ↳ Noterade Canonical URL: {canonical}", LogLevel.DEBUG)
                if canonical != url:
                    self.url_queue.add_url(canonical, depth=depth, priority=CrawlPriority.HIGH.value)
                    self._gui_update(url, "Hoppad (Canonical)", "-")
                    return True

            title, page_text = self.extract_text(html)
            if not title: title = "(ingen titel)"
            
            content_hash = get_hash(page_text)
            cached = self.db.get_cache(url)
            
            if self.config["incremental"] and cached and cached.get('hash') == content_hash:
                self.stats.pages_unchanged += 1
                self._gui_update(url, "Oförändrad", title)
                self.db.save_cache(url, content_hash, title, len(page_text))
            else:
                self.db.save_cache(url, content_hash, title, len(page_text))
                self._gui_update(url, f"Hämtad ({source})", title)
                
                texts_dir = os.path.join(self.output_dir, "texter")
                os.makedirs(texts_dir, exist_ok=True)
                
                if len(page_text) > 50:
                    safe_filename = self.url_to_filename(url)
                    file_path = os.path.join(texts_dir, safe_filename)
                    
                    header = f"KÄLLA: {url}\nTITEL: {title}\nHÄMTAD: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n\n"
                    full_content = header + page_text
                    
                    try:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(full_content)
                        self._log(f"  ✓ Sparade text: {safe_filename[:40]}...")
                    except Exception as file_e:
                        self._log(f"  ⚠ Kunde inte spara textfil {safe_filename}: {file_e}", LogLevel.ERROR)

            if self.max_depth == 0 or depth < self.max_depth:
                links_found = 0
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')): continue
                    
                    full_url = urljoin(url, href)
                    ext = os.path.splitext(urlparse(full_url).path)[1].lower()
                    
                    if ext in self.doc_extensions and self.config["download_docs"]:
                        self.download_pool.submit(self.download_document, full_url)
                    elif self.is_valid_url(full_url):
                        if self.url_queue.add_url(full_url, depth=depth + 1):
                            links_found += 1
                            
                if links_found > 0: self._log(f"  → Hittade {links_found} nya länkar", LogLevel.DEBUG)
                
            self.stats.pages_visited += 1
            return True
            
        except Exception as e:
            self._log(f"  ✗ Fel vid besök: {str(e)[:50]}", LogLevel.ERROR)
            self.stats.pages_failed += 1
            self._gui_update(url, "Fel", str(e)[:30])
            return False

    def download_document(self, url: str):
        # FIX: Trådsäkert set för nedladdningar
        with self.download_lock:
            if url in self.downloaded_files: return
            self.downloaded_files.add(url)
        
        try:
            docs_dir = os.path.join(self.output_dir, "dokument")
            os.makedirs(docs_dir, exist_ok=True)
            
            resp = self.req_session.get(url, stream=True, timeout=30)
            if resp.status_code != 200: return
            
            filename = ""
            if 'content-disposition' in resp.headers:
                cd = resp.headers['content-disposition']
                match = re.findall(r'filename="?([^"]+)"?', cd)
                if match: filename = match[0]
            
            if not filename:
                filename = os.path.basename(unquote(urlparse(url).path))
                
            if not filename or '.' not in filename:
                ext = ".pdf"
                ct = resp.headers.get('content-type', '')
                if 'wordprocessingml' in ct: ext = '.docx'
                elif 'spreadsheetml' in ct: ext = '.xlsx'
                elif 'presentationml' in ct: ext = '.pptx'
                filename = f"dokument{ext}"
                
            filename_base = slugify(filename.split('.')[0])
            ext = os.path.splitext(filename)[1].lower()
            hash_str = hashlib.md5(url.encode('utf-8')).hexdigest()[:6]
            
            if len(filename_base) > 100:
                filename_base = filename_base[:100]
                
            safe_filename = f"{filename_base}_{hash_str}{ext}"
            filepath = os.path.join(docs_dir, safe_filename)
            
            if os.path.exists(filepath): return
            
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(8192): f.write(chunk)
            self.stats.documents_downloaded += 1
            self._log(f"  ⬇ Dokument sparat: {safe_filename}")
        except Exception as e:
            self._log(f"  ⚠ Fel vid nerladdning av {url}: {e}", LogLevel.WARNING)

    def _check_memory_usage(self):
        if not HAS_PSUTIL: return
        try:
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            if memory_mb > 1500:
                self._log(f"⚠ Hög minnesanvändning: {memory_mb:.0f} MB", LogLevel.WARNING)
                if hasattr(self, 'driver') and self.driver:
                    self.driver.execute_script("window.localStorage.clear();")
                    self.driver.execute_script("window.sessionStorage.clear();")
                gc.collect()
        except Exception:
            pass

    def _generate_index(self):
        self._log("📊 Skapar index-fil (index.csv) över databasen...")
        index_path = os.path.join(self.output_dir, "index.csv")
        try:
            records = self.db.get_all_records()
            with open(index_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['URL', 'Titel', 'Hämtad_Datum', 'Filnamn'])
                for url, title, date in records:
                    filename = self.url_to_filename(url)
                    writer.writerow([url, title, date, filename])
            self._log("✓ Index skapat framgångsrikt!")
        except Exception as e:
            self._log(f"⚠ Kunde inte skapa index.csv: {e}", LogLevel.ERROR)

    def stop(self):
        self.state = CrawlerState.STOPPED
        self._log("🛑 Avbryter crawl (väntar på att pågående uppgifter ska slutföras)...")
        self.login_event.set() # Släpper eventuella väntande inloggningar

    def pause(self):
        if self.state == CrawlerState.RUNNING:
            self.state = CrawlerState.PAUSED
            self._log("⏸ Dammsugaren är pausad.")
            return True
        elif self.state == CrawlerState.PAUSED:
            self.state = CrawlerState.RUNNING
            self._log("▶ Dammsugaren återupptas...")
            return False

    def crawl(self):
        try:
            start_headless = self.config["headless_mode"] == "headless"
            headless_after_login = self.config["headless_mode"] == "login_then_headless"
            
            if headless_after_login:
                self.driver = self.create_driver(is_headless=False)
                if not self.driver: return
                
                self._log("👤 Navigerar till start-URL för manuell inloggning...")
                self.driver.get(self.start_url)
                
                # FIX: Visar popup i GUI och väntar tills användaren klickar OK
                self._log("\n⏳ VÄNTAR PÅ MANUELL INLOGGNING...")
                self.msg_queue.put(("login_wait", None)) 
                self.login_event.wait() 
                
                if self.state == CrawlerState.STOPPED: return
                
                self._log("🔄 Sparar cookies och byter till osynligt läge...")
                cookies = self.driver.get_cookies()
                self.driver.quit()
                
                self.driver = self.create_driver(is_headless=True)
                if not self.driver: return
                
                self.driver.get(self.base_url)
                for cookie in cookies:
                    try: 
                        self.driver.add_cookie(cookie)
                        self.req_session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))
                    except Exception: pass
                    
                self.driver.get(self.start_url)
                time.sleep(2)
                
            elif start_headless:
                self.driver = self.get_chrome_driver()
            
            self._log(f"\n{'='*60}\n✅ REDO ATT BÖRJA CRAWLA\n{'='*60}\n")
            
            while self.state != CrawlerState.STOPPED:
                if self.state == CrawlerState.PAUSED:
                    time.sleep(1)
                    continue
                    
                if self.max_pages > 0 and self.stats.pages_visited >= self.max_pages:
                    self._log("\n📊 Max antal sidor nått.")
                    break
                    
                if self.stats.pages_visited > 0 and self.stats.pages_visited % 50 == 0:
                    self._check_memory_usage()
                        
                queue_item = self.url_queue.get_next()
                if not queue_item:
                    if self.url_queue.size() == 0:
                        self._log("\n✅ Inga fler URL:er i kön.")
                        break
                    time.sleep(1)
                    continue
                
                depth, next_url = queue_item
                self.process_page(next_url, depth)
                
        except Exception as e:
            self._log(f"\n💥 Oväntat fel: {e}", LogLevel.ERROR)
        finally:
            self._log("\n🧹 Städar upp och väntar på att sista filerna ska ladda ner...")
            self.download_pool.shutdown(wait=True)
            self._generate_index() 
            self.db.close()
            
            if self.driver:
                try: self.driver.quit()
                except Exception: pass
                
            self.stats.end_time = datetime.now()
            self._log(f"Färdig! Total tid: {self.stats.duration}. Hastighet: {self.stats.pages_per_second:.1f} sid/sek.")
            self.msg_queue.put(("done", "Avbruten" if self.state == CrawlerState.STOPPED else "Klar"))


# ─────────────────────────────────────────────────────────────
#  GRAFISKT GRÄNSSNITT (GUI)
# ─────────────────────────────────────────────────────────────

class AppGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Webbdammsugare Pro (v1.4)")
        self.root.geometry("1000x850")
        
        style = ttk.Style()
        if "clam" in style.theme_names(): style.theme_use("clam")
        
        self.crawler_instance = None
        self.msg_queue = queue.Queue()
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self.process_queue)
        
        self._build_ui()
    
    def _on_closing(self):
        if self.crawler_instance: self.crawler_instance.stop()
        self.root.destroy()

    def open_help_window(self):
        help_win = tk.Toplevel(self.root)
        help_win.title("❓ Hjälp & Instruktioner")
        help_win.geometry("800x650")
        
        help_text = scrolledtext.ScrolledText(help_win, wrap=tk.WORD, font=("Arial", 10), padx=15, pady=15)
        help_text.pack(fill=tk.BOTH, expand=True)
        
        instruktioner = """Välkommen till Webbdammsugare Pro!

Här är en genomgång av vad alla inställningar betyder:

⚙️ GRUNDINSTÄLLNINGAR
--------------------------------------------------
* Startadress: Detta är URL:en där dammsugaren börjar leta (t.ex. https://exempel.se).
* Fördröjning (sek): Tiden programmet väntar mellan varje sidnedladdning. En lägre siffra är snabbare, men en högre siffra (t.ex. 1.0 eller 2.0) är snällare mot webbservern och minskar risken för att du blir blockerad.
* Max sidor: Hur många sidor programmet får ladda ner totalt. Sätt till 0 för oändligt.
* Max djup: Anger hur många klick bort från startsidan dammsugaren får gå. Sätt till 0 för oändligt djup.
* Filformat:
  - .md (Markdown): Bäst om du ska använda texten i AI-system (som ChatGPT/SveaGPT). Detta format bevarar rubriker och listor på ett sätt som AI förstår.
  - .txt (Klassisk): Sparar som vanlig, ostrukturerad text.
* Ladda ner dokument: Kryssa i denna om du vill att programmet även ska spara ner PDF, Excel, Word och andra dokument den hittar. Dessa sparas i en undermapp som heter 'dokument'.
* Körläge:
  - headless: Webbläsaren körs helt osynligt i bakgrunden. (Snabbast)
  - visible: Du ser webbläsaren när den arbetar. Bra för felsökning.
  - login_then_headless: Perfekt för intranät! En synlig webbläsare öppnas så du kan logga in i lugn och ro. Tryck OK i dialogrutan när du är klar, så fortsätter programmet osynligt.
* Mapp: Välj var på din dator du vill att alla filer (texter, dokument, loggar) ska sparas. (När körningen är klar skapas även en "index.csv" här som listar alla nerladdade sidor).

🔧 AVANCERAT / MOTOR
--------------------------------------------------
* Hybrid-motor: Rekommenderas varmt! Använder blixtsnabba nätverksanrop för vanliga sidor, men byter automatiskt till en riktig webbläsare om sidan är kodad i JavaScript eller är tom.
* Använd Trafilatura: Ett avancerat AI-verktyg för att tvätta webbsidor. Den klipper bort menyer, sidfötter och reklam och lämnar bara den riktiga brödtexten.
* Letar automatiskt efter Sitemap.xml: Hjälper programmet att hitta dolda sidor snabbare genom att läsa webbplatsens egna karta.
* Respektera robots.txt: Gör så att programmet inte besöker sidor som webbplatsens ägare har bett sökmotorer att ignorera.
* Strikt Domän: Tvingar dammsugaren att bara stanna på exakt den domän du angav. Om du vill att den ska kunna följa länkar till underdomäner (t.ex. från utb.tyreso.se till www.tyreso.se) ska denna kryssas ur.

🚷 URL-FILTER (Viktigt för intranät!)
--------------------------------------------------
* Uteslut ord i URL: Här kan du skriva in ord (separerade med kommatecken) för att undvika skräp. Skriver du in t.ex. "kalender, profilsida" kommer programmet att hoppa över alla adresser som innehåller dessa ord.
* Kräv ord i URL: Denna är superviktig om du skrapar Google Sites eller Wordpress! Om du skriver in "utb.tyreso.se" här, kommer programmet garanterat att kasta bort alla länkar som leder till andra kommuner (som utb.helsingborg.se), även om de råkar ligga på samma plattform.
"""
        help_text.insert(tk.END, instruktioner)
        help_text.config(state=tk.DISABLED)
    
    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))
        
        url_frame = ttk.LabelFrame(top_frame, text="🌐 Startadress", padding="10")
        url_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(url_frame, text="URL:").pack(side=tk.LEFT)
        self.url_entry = ttk.Entry(url_frame, width=70)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))
        self.url_entry.insert(0, "https://")
        
        help_btn = ttk.Button(top_frame, text="❓ Hjälp", command=self.open_help_window)
        help_btn.pack(side=tk.RIGHT, padx=(10, 0), pady=(10, 0))
        
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.X, pady=(0, 10))
        
        tab_basic = ttk.Frame(notebook, padding="10")
        tab_adv = ttk.Frame(notebook, padding="10")
        notebook.add(tab_basic, text="⚙️ Grundinställningar")
        notebook.add(tab_adv, text="🔧 Avancerat / Motor")
        
        # --- FLIK: Grundinställningar ---
        row1 = ttk.Frame(tab_basic)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Fördröjning (sek):").pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=0.5)
        ttk.Spinbox(row1, from_=0.1, to=10.0, increment=0.1, textvariable=self.delay_var, width=5).pack(side=tk.LEFT, padx=(2, 10))
        
        ttk.Label(row1, text="Max sidor:").pack(side=tk.LEFT)
        self.max_pages_var = tk.IntVar(value=0)
        ttk.Spinbox(row1, from_=0, to=10000, textvariable=self.max_pages_var, width=6).pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(row1, text="Max djup:").pack(side=tk.LEFT)
        self.max_depth_var = tk.IntVar(value=0)
        ttk.Spinbox(row1, from_=0, to=50, textvariable=self.max_depth_var, width=4).pack(side=tk.LEFT, padx=(2, 10))
        
        ttk.Label(row1, text="Filformat:").pack(side=tk.LEFT)
        self.format_var = tk.StringVar(value=".md (Bäst för AI)")
        ttk.Combobox(row1, textvariable=self.format_var, values=[".md (Bäst för AI)", ".txt (Klassisk)"], state="readonly", width=16).pack(side=tk.LEFT, padx=(2, 15))

        row2 = ttk.Frame(tab_basic)
        row2.pack(fill=tk.X, pady=8)
        self.docs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Ladda ner dokument (PDF m.m.)", variable=self.docs_var).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row2, text="Körläge:").pack(side=tk.LEFT)
        self.headless_var = tk.StringVar(value="headless")
        ttk.Combobox(row2, textvariable=self.headless_var, values=["headless", "login_then_headless", "visible"], state="readonly", width=25).pack(side=tk.LEFT, padx=(5, 15))

        row3 = ttk.Frame(tab_basic)
        row3.pack(fill=tk.X, pady=5)
        ttk.Label(row3, text="Mapp:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output"))
        ttk.Entry(row3, textvariable=self.dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row3, text="Välj...", command=lambda: self.dir_var.set(filedialog.askdirectory() or self.dir_var.get())).pack(side=tk.LEFT)
        
        # --- FLIK: Avancerat ---
        arow1 = ttk.Frame(tab_adv)
        arow1.pack(fill=tk.X, pady=2)
        self.hybrid_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(arow1, text="⚡ Hybrid-motor (Requests + Selenium fallback)", variable=self.hybrid_var).pack(side=tk.LEFT, padx=(0,15))
        
        self.traf_var = tk.BooleanVar(value=HAS_TRAFILATURA)
        cb_traf = ttk.Checkbutton(arow1, text="🧠 Använd Trafilatura för text", variable=self.traf_var)
        cb_traf.pack(side=tk.LEFT)
        if not HAS_TRAFILATURA: cb_traf.config(state=tk.DISABLED, text="Trafilatura saknas")

        arow2 = ttk.Frame(tab_adv)
        arow2.pack(fill=tk.X, pady=2)
        self.sitemap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(arow2, text="Läs Sitemap.xml", variable=self.sitemap_var).pack(side=tk.LEFT, padx=(0,15))
        
        self.robots_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(arow2, text="Respektera robots.txt", variable=self.robots_var).pack(side=tk.LEFT, padx=(0,15))
        
        self.strict_var = tk.BooleanVar(value=True) # FIX: Checkbox för strikt domän
        ttk.Checkbutton(arow2, text="Strikt Domän", variable=self.strict_var).pack(side=tk.LEFT)

        arow3 = ttk.Frame(tab_adv)
        arow3.pack(fill=tk.X, pady=5)
        ttk.Label(arow3, text="Uteslut ord:").pack(side=tk.LEFT)
        self.exclude_entry = ttk.Entry(arow3, width=25)
        self.exclude_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        ttk.Label(arow3, text="Kräv ord:").pack(side=tk.LEFT, padx=(10,0))
        self.require_entry = ttk.Entry(arow3, width=25)
        self.require_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=5)
        self.start_btn = ttk.Button(btn_frame, text="▶ Starta", command=self.start_crawl, width=15)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.pause_btn = ttk.Button(btn_frame, text="⏸ Pausa", command=self.toggle_pause, state=tk.DISABLED, width=15)
        self.pause_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="■ Stoppa", command=self.stop_crawl, state=tk.DISABLED, width=15)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.stats_label = ttk.Label(main_frame, text="Väntar på start...", font=("Consolas", 10, "bold"), foreground="#005500")
        self.stats_label.pack(fill=tk.X, pady=5)
        
        table_frame = ttk.LabelFrame(main_frame, text="🔍 Live Data", padding="5")
        table_frame.pack(fill=tk.BOTH, expand=True)
        
        cols = ('URL', 'Status', 'Titel')
        self.tree = ttk.Treeview(table_frame, columns=cols, show='headings', height=8)
        self.tree.heading('URL', text='URL')
        self.tree.heading('Status', text='Status')
        self.tree.heading('Titel', text='Sido-titel')
        self.tree.column('URL', width=300)
        self.tree.column('Status', width=100)
        self.tree.column('Titel', width=300)
        
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        log_frame = ttk.LabelFrame(main_frame, text="📋 Systemlogg", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10,0))
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9), height=6, bg="#1e1e1e", fg="#d4d4d4")
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def process_queue(self):
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "log":
                    t = datetime.now().strftime("%H:%M:%S")
                    self.log_area.insert(tk.END, f"[{t}] {data}\n")
                    self.log_area.see(tk.END)
                elif msg_type == "table":
                    url, status, title = data
                    self.tree.insert('', 0, values=(url, status, title))
                    if len(self.tree.get_children()) > 100:
                        self.tree.delete(self.tree.get_children()[-1])
                elif msg_type == "stats":
                    self.stats_label.config(text=data)
                elif msg_type == "login_wait":
                    # FIX: Smidig popup för inloggning!
                    messagebox.showinfo("Inloggning krävs", "Ett webbläsarfönster har öppnats.\n\nLogga in på sidan i lugn och ro. När du är helt färdig, tryck på OK här för att starta dammsugningen!")
                    if self.crawler_instance:
                        self.crawler_instance.login_event.set()
                elif msg_type == "done":
                    self.start_btn.config(state=tk.NORMAL)
                    self.pause_btn.config(state=tk.DISABLED)
                    self.stop_btn.config(state=tk.DISABLED)
                    messagebox.showinfo("Crawl slutförd", f"Körningen är klar. Status: {data}")
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)
    
    def start_crawl(self):
        url = self.url_entry.get().strip()
        if not url or url == "https://":
            messagebox.showwarning("Varning", "Ange en giltig URL!")
            return
            
        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL, text="⏸ Pausa")
        self.stop_btn.config(state=tk.NORMAL)
        
        for item in self.tree.get_children(): self.tree.delete(item)
        self.log_area.delete(1.0, tk.END)
        
        exclude_list = [k.strip().lower() for k in self.exclude_entry.get().split(",") if k.strip()]
        require_list = [k.strip().lower() for k in self.require_entry.get().split(",") if k.strip()]
        
        chosen_format = ".txt" if ".txt" in self.format_var.get() else ".md"
        
        config = {
            "start_url": url,
            "output_dir": self.dir_var.get(),
            "delay": self.delay_var.get(),
            "max_pages": self.max_pages_var.get(),
            "max_depth": self.max_depth_var.get(),
            "save_format": chosen_format,
            "headless_mode": self.headless_var.get(),
            "respect_robots": self.robots_var.get(),
            "find_sitemap": self.sitemap_var.get(),
            "use_hybrid": self.hybrid_var.get(),
            "use_trafilatura": self.traf_var.get(),
            "download_docs": self.docs_var.get(),
            "strict_domain": self.strict_var.get(), # Kopplat till checkboxen
            "exclude_keywords": exclude_list,
            "require_keywords": require_list,
            "incremental": True
        }
        
        self.crawler_instance = WebCrawler(config, self.msg_queue)
        threading.Thread(target=self.crawler_instance.crawl, daemon=True).start()

    def toggle_pause(self):
        if self.crawler_instance:
            is_paused = self.crawler_instance.pause()
            self.pause_btn.config(text="▶ Återuppta" if is_paused else "⏸ Pausa")

    def stop_crawl(self):
        if self.crawler_instance:
            self.stop_btn.config(state=tk.DISABLED, text="Stoppar...")
            self.crawler_instance.stop()

def main():
    root = tk.Tk()
    app = AppGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()