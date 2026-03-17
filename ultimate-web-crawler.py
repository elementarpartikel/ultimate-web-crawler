#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webbdammsugare Pro (v1.8) - Performance, Dark Mode & AI/RAG Edition
Skapad av Fredrik Eriksson

Funktioner: Bilingual (SV/EN), CustomTkinter GUI, Temaväxlare, SQLite (WAL), Concurrency Safe, Anti-stutter
"""

import tkinter as tk
from tkinter import messagebox, filedialog, ttk
import customtkinter as ctk
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
import webbrowser # NYTT: För dubbelklick i tabellen
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
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

ctk.set_appearance_mode("Dark")
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
#  HJÄLPFUNKTIONER OCH DATABAS
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

def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

class CrawlDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL") 
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            self.conn.execute('CREATE TABLE IF NOT EXISTS page_cache (url TEXT PRIMARY KEY, content_hash TEXT, title TEXT, crawled_at TEXT, content_length INTEGER)')
            self.conn.commit()

    def get_cache(self, url: str) -> Optional[Dict]:
        with self.lock:
            cursor = self.conn.execute("SELECT content_hash, title, crawled_at, content_length FROM page_cache WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row: return {'hash': row[0], 'title': row[1], 'crawled_at': row[2], 'content_length': row[3]}
        return None

    def save_cache(self, url: str, content_hash: str, title: str, length: int):
        with self.lock:
            self.conn.execute('INSERT OR REPLACE INTO page_cache (url, content_hash, title, crawled_at, content_length) VALUES (?, ?, ?, ?, ?)', 
                              (url, content_hash, title, datetime.now().isoformat(), length))
            self.conn.commit()
            
    def get_all_records(self):
        with self.lock:
            cursor = self.conn.execute("SELECT url, title, crawled_at FROM page_cache ORDER BY crawled_at DESC")
            return cursor.fetchall()
        
    def close(self):
        with self.lock: self.conn.close()

class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.delay = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self.last_request = datetime.min
        self._lock = threading.Lock()
    
    def wait_if_needed(self):
        sleep_time = 0
        with self._lock:
            now = datetime.now()
            elapsed = (now - self.last_request).total_seconds()
            if elapsed < self.delay:
                sleep_time = self.delay - elapsed
                self.last_request = now + timedelta(seconds=sleep_time)
            else: self.last_request = now
        if sleep_time > 0: time.sleep(sleep_time)

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
        except queue.Empty: return None
    
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
        self.strict_domain = config.get("strict_domain", True) 
        
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
        self.download_lock = threading.Lock() 
        self.visited_sitemaps: Set[str] = set()
        self.login_event = threading.Event() 
        
        self.doc_extensions = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.txt', '.md'}
        self.ignore_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.zip', '.rar', '.exe', '.mp4', '.css', '.js'}
        
        self.crawl_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._setup_logging()
        self.req_session = self._create_robust_session()
        
        self.robot_parser = None
        if config["respect_robots"]: self._load_robots_txt()
        
        self.driver = None
        self._log(f"🚀 Initierar crawl för {self.domain} (v1.8)")

    def _create_robust_session(self):
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
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
        nya_eller_sparade = self.stats.pages_visited - self.stats.pages_unchanged
        self.msg_queue.put(("stats_data", (self.stats.pages_visited, nya_eller_sparade, self.stats.pages_unchanged, self.url_queue.size(), self.stats.pages_failed)))

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
        except Exception as e: self._log(f"Kunde inte läsa robots.txt: {e}", LogLevel.DEBUG)

    def _parse_sitemap(self, url: str):
        if url in self.visited_sitemaps: return
        self.visited_sitemaps.add(url)
        self._log(f"🗺️ Letar i sitemap: {url}")
        try:
            resp = self.req_session.get(url, timeout=15)
            if resp.status_code == 200:
                try: soup = BeautifulSoup(resp.content, 'xml')
                except Exception: soup = BeautifulSoup(resp.content, 'html.parser')
                for sm in soup.find_all('sitemap'):
                    loc = sm.find('loc')
                    if loc: self._parse_sitemap(loc.text.strip())
                count = 0
                for url_node in soup.find_all('url'):
                    loc = url_node.find('loc')
                    if loc and self.url_queue.add_url(loc.text.strip(), depth=0, priority=CrawlPriority.SITEMAP.value): count += 1
                if count > 0: self._log(f"✓ Hittade {count} URLs i sitemap/index")
        except Exception as e: self._log(f"⚠ Fel vid sitemap-läsning: {e}", LogLevel.DEBUG)

    def is_valid_url(self, url: str) -> bool:
        if len(url) > 2000: return False
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
                if not any(kw in lower_url for kw in req_kws): return False
            if self.robot_parser and not self.robot_parser.can_fetch('*', url): return False
            return True
        except Exception as e: 
            self._log(f"  ⚠ Fel vid URL-validering ({url}): {e}", LogLevel.DEBUG)
            return False

    def get_chrome_options(self, is_headless: bool = False) -> Options:
        opts = Options()
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        prefs = {}
        if self.config.get("download_docs", False):
            docs_dir = os.path.abspath(os.path.join(self.output_dir, "dokument"))
            os.makedirs(docs_dir, exist_ok=True)
            prefs["download.default_directory"] = docs_dir
            prefs["download.prompt_for_download"] = False
            prefs["plugins.always_open_pdf_externally"] = True
        else: prefs["download.download_restrictions"] = 3
        opts.add_experimental_option("prefs", prefs)
        if is_headless: opts.add_argument("--headless=new")
        return opts

    def create_driver(self, is_headless: bool = False):
        try:
            if HAS_SELENIUM_MANAGER: return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=self.get_chrome_options(is_headless))
            else: return webdriver.Chrome(options=self.get_chrome_options(is_headless))
        except Exception as e:
            self._log(f"Kunde inte starta Chrome: {e}", LogLevel.ERROR)
            return None

    def get_chrome_driver(self):
        if not self.driver:
            self.driver = self.create_driver(is_headless=(self.config["headless_mode"] == "headless"))
            if not self.driver: self.use_hybrid = True 
        return self.driver

    def extract_text(self, html: str) -> Tuple[str, str]:
        title = ""
        soup = BeautifulSoup(html, 'html.parser')
        if soup.title: title = soup.title.string.strip() if soup.title.string else ""
        if self.use_trafilatura and HAS_TRAFILATURA:
            try:
                extracted = trafilatura.extract(html, include_links=False, include_images=False, output_format="markdown" if self.save_format == ".md" else "txt")
                if extracted: return title, extracted
            except Exception as e: self._log(f"  ⚠ Trafilatura misslyckades ({e})", LogLevel.DEBUG)
            
        for tag in ['script', 'style', 'header', 'footer', 'nav', 'noscript', 'aside', 'svg']:
            for el in soup.find_all(tag): el.decompose()
        raw_text = soup.get_text(separator='\n')
        lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 1]
        return title, re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))

    def url_to_filename(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.netloc + parsed.path
        if parsed.query: path += "_" + parsed.query
        if path.endswith('/'): path += "index"
        safe_name = slugify(path)[:150]
        hash_str = hashlib.md5(url.encode('utf-8')).hexdigest()[:6]
        return f"{safe_name}_{hash_str}{self.save_format}"

    def process_page(self, url: str, depth: int) -> bool:
        self.rate_limiter.wait_if_needed()
        html, source = "", "Requests"
        try:
            if self.use_hybrid:
                resp = self.req_session.get(url, timeout=15, stream=True)
                resp.raise_for_status()
                content_type = resp.headers.get('Content-Type', '').lower()
                if 'image/' in content_type or 'video/' in content_type: return False
                
                doc_types = ['application/pdf', 'application/vnd', 'application/msword', 'application/octet-stream']
                if any(dt in content_type for dt in doc_types):
                    if self.config["download_docs"]: self.download_pool.submit(self.download_document, url)
                    return True

                html = resp.text
                if len(html) < 1000 or "enable javascript" in html.lower():
                    driver = self.get_chrome_driver()
                    if driver:
                        driver.get(url)
                        time.sleep(2) 
                        html = driver.page_source
                        source = "Selenium"
            else:
                driver = self.get_chrome_driver()
                driver.get(url)
                time.sleep(self.delay)
                html = driver.page_source
                source = "Selenium"

            soup = BeautifulSoup(html, 'html.parser')
            link = soup.find('link', rel='canonical')
            if link and link.get('href'):
                canonical = normalize_url(urljoin(url, link['href']))
                if canonical != url and self.is_valid_url(canonical):
                    self.url_queue.add_url(canonical, depth=depth, priority=CrawlPriority.HIGH.value)
                    self._gui_update(url, "Hoppad (Canonical)", "-")
                    return True

            title, page_text = self.extract_text(html)
            title = title or "(ingen titel)"
            content_hash = get_hash(page_text)
            cached = self.db.get_cache(url)
            
            if self.config["incremental"] and cached and cached.get('hash') == content_hash:
                self.stats.pages_unchanged += 1
                self._gui_update(url, "Oförändrad", title)
            else:
                self._gui_update(url, f"Hämtad ({source})", title)
                if len(page_text) > 50:
                    texts_dir = os.path.join(self.output_dir, "texter")
                    os.makedirs(texts_dir, exist_ok=True)
                    file_path = os.path.join(texts_dir, self.url_to_filename(url))
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(f"KÄLLA: {url}\nTITEL: {title}\nHÄMTAD: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n\n{page_text}")
            
            self.db.save_cache(url, content_hash, title, len(page_text))

            if self.max_depth == 0 or depth < self.max_depth:
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    if href.startswith(('#', 'javascript:', 'mailto:')): continue
                    full_url = urljoin(url, href)
                    if self.is_valid_url(full_url): self.url_queue.add_url(full_url, depth=depth + 1)
            
            self.stats.pages_visited += 1
            return True
        except Exception as e:
            self._log(f"  ✗ Fel vid besök: {str(e)[:50]}", LogLevel.ERROR)
            self.stats.pages_failed += 1
            self._gui_update(url, "Fel", str(e)[:30])
            return False

    def download_document(self, url: str):
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
                match = re.findall(r'filename="?([^"]+)"?', resp.headers['content-disposition'])
                if match: filename = match[0]
            if not filename: filename = os.path.basename(unquote(urlparse(url).path)) or "dokument.pdf"
            ext = os.path.splitext(filename)[1].lower() or ".pdf"
            safe_filename = f"{slugify(filename.split('.')[0])[:100]}_{hashlib.md5(url.encode('utf-8')).hexdigest()[:6]}{ext}"
            filepath = os.path.join(docs_dir, safe_filename)
            if not os.path.exists(filepath):
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(8192): f.write(chunk)
                self.stats.documents_downloaded += 1
                self._log(f"  ⬇ Dokument sparat: {safe_filename}")
        except Exception: pass

    def _generate_index(self):
        self._log("📊 Skapar index-fil (index.csv)...")
        try:
            with open(os.path.join(self.output_dir, "index.csv"), 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['URL', 'Titel', 'Hämtad_Datum', 'Filnamn'])
                for url, title, date in self.db.get_all_records(): writer.writerow([url, title, date, self.url_to_filename(url)])
        except Exception: pass

    def stop(self):
        self.state = CrawlerState.STOPPED
        self._log("🛑 Avbryter crawl...")
        self.login_event.set() 

    def pause(self):
        if self.state == CrawlerState.RUNNING:
            self.state = CrawlerState.PAUSED
            return True
        elif self.state == CrawlerState.PAUSED:
            self.state = CrawlerState.RUNNING
            return False

    def crawl(self):
        try:
            if self.config["headless_mode"] == "login_then_headless":
                self.driver = self.create_driver(is_headless=False)
                if not self.driver: return
                self.driver.get(self.start_url)
                self.msg_queue.put(("login_wait", None)) 
                self.login_event.wait() 
                if self.state == CrawlerState.STOPPED: return
                cookies = self.driver.get_cookies()
                self.driver.quit()
                self.driver = self.create_driver(is_headless=True)
                self.driver.get(self.base_url)
                for cookie in cookies:
                    try: 
                        self.driver.add_cookie(cookie)
                        self.req_session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))
                    except Exception: pass
            elif self.config["headless_mode"] == "headless":
                self.driver = self.get_chrome_driver()
            
            while self.state != CrawlerState.STOPPED:
                if self.state == CrawlerState.PAUSED:
                    time.sleep(1)
                    continue
                if self.max_pages > 0 and self.stats.pages_visited >= self.max_pages: break
                if HAS_PSUTIL and self.stats.pages_visited % 50 == 0:
                    if psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024 > 1500: gc.collect()
                
                queue_item = self.url_queue.get_next()
                if not queue_item:
                    if self.url_queue.size() == 0: break
                    time.sleep(1)
                    continue
                self.process_page(queue_item[1], queue_item[0])
                
        except Exception as e: self._log(f"💥 Oväntat fel: {e}", LogLevel.ERROR)
        finally:
            self.download_pool.shutdown(wait=True)
            self._generate_index() 
            self.db.close()
            if self.driver:
                try: self.driver.quit()
                except Exception: pass
            self.stats.end_time = datetime.now()
            self._log(f"Färdig! Total tid: {self.stats.duration}")
            self.msg_queue.put(("done", "Avbruten" if self.state == CrawlerState.STOPPED else "Klar"))

# ─────────────────────────────────────────────────────────────
#  GRAFISKT GRÄNSSNITT (GUI - CustomTkinter)
# ─────────────────────────────────────────────────────────────
class AppGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        
        # --- SPRÅK & TEXTER ---
        self.lang = "sv"
        self.texts = {
            "window_title": {"sv": "Webbdammsugare Pro (v1.8)", "en": "Web Crawler Pro (v1.8)"},
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
            "cb_hybrid": {"sv": "⚡ Hybrid-motor (Requests + Selenium)", "en": "⚡ Hybrid Engine (Requests + Selenium)"},
            "cb_traf": {"sv": "🧠 Använd Trafilatura för text", "en": "🧠 Use Trafilatura for text extraction"},
            "cb_sitemap": {"sv": "Läs Sitemap.xml", "en": "Parse Sitemap.xml"},
            "cb_robots": {"sv": "Respektera robots.txt", "en": "Respect robots.txt"},
            "cb_strict": {"sv": "Strikt Domän", "en": "Strict Domain"},
            "lbl_exclude": {"sv": "Uteslut ord (komma-separerat):", "en": "Exclude words (comma separated):"},
            "lbl_require": {"sv": "Kräv ord i URL:", "en": "Require words in URL:"},
            "btn_start": {"sv": "▶ Starta Dammsugning", "en": "▶ Start Crawling"},
            "btn_pause": {"sv": "⏸ Pausa", "en": "⏸ Pause"},
            "btn_resume": {"sv": "▶ Återuppta", "en": "▶ Resume"},
            "btn_stop": {"sv": "■ Stoppa", "en": "■ Stop"},
            "col_status": {"sv": "Status", "en": "Status"},
            "col_title": {"sv": "Sido-titel", "en": "Page Title"},
            "status_wait": {"sv": "Väntar på start...", "en": "Waiting to start..."},
            "stats_fmt": {
                "sv": "Besökta: {} | Sparade: {} | Oförändrade: {} | I Kö: {} | Fel: {}",
                "en": "Visited: {} | Saved: {} | Unchanged: {} | Queued: {} | Errors: {}"
            },
            "help_title": {"sv": "❓ Hjälp & Instruktioner", "en": "❓ Help & Instructions"},
            "help_content": {
                "sv": "⚙️ GRUNDINSTÄLLNINGAR\n-------------------------\n* Startadress: URL där programmet börjar leta.\n* Fördröjning: Tid (sekunder) mellan sidbesök.\n* Max sidor/djup: 0 betyder oändligt.\n* Körläge:\n  - headless: Osynligt (Snabbast)\n  - visible: Synlig webbläsare\n  - login_then_headless: Logga in manuellt först.\n\n🔧 AVANCERAT\n-------------------------\n* Hybrid-motor: Rekommenderas för modern webb.\n* Trafilatura: Optimerar text för AI/GPT.\n* Strikt domän: Lämna inte startadressens domän.\n* URL-Filter: Styr exakt vilka sidor som tas med.\n\n💡 TIPS: Dubbelklicka på en rad i tabellen för att öppna länken i din webbläsare!",
                "en": "⚙️ BASIC SETTINGS\n-------------------------\n* Start URL: Where the crawler begins.\n* Delay: Seconds to wait between requests.\n* Max pages/depth: 0 means infinite.\n* Run Mode:\n  - headless: Invisible (Fastest)\n  - visible: Visible browser\n  - login_then_headless: Manual login first.\n\n🔧 ADVANCED\n-------------------------\n* Hybrid Engine: Recommended for modern web.\n* Trafilatura: Optimizes text for AI/GPT.\n* Strict Domain: Stay on the initial domain.\n* URL Filters: Control exactly what to crawl.\n\n💡 TIP: Double-click a row in the table to open the link in your browser!"
            }
        }
        
        self.root.title(self.texts["window_title"][self.lang])
        self.root.geometry("1000x850")
        
        self.crawler_instance = None
        self.msg_queue = queue.Queue()
        
        self._update_treeview_style("Dark")
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self.process_queue)
        
        self._build_ui()
    
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
        self.lang = "sv" if "SV" in choice else "en"
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
    
    def _build_ui(self):
        main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        url_frame = ctk.CTkFrame(main_frame)
        url_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.lbl_url = ctk.CTkLabel(url_frame, text=self.texts["lbl_url"][self.lang], font=ctk.CTkFont(weight="bold"))
        self.lbl_url.pack(side=tk.LEFT, padx=(15, 10), pady=15)
        
        self.url_entry = ctk.CTkEntry(url_frame, width=250, placeholder_text="https://...")
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 15), pady=15)
        self.url_entry.insert(0, "https://")
        
        # 🌍 Språk-väljare (Segmented Button)
        self.lang_var = ctk.StringVar(value="🇸🇪 SV")
        self.lang_switch = ctk.CTkSegmentedButton(url_frame, values=["🇸🇪 SV", "🇬🇧 EN"], variable=self.lang_var, command=self.change_language_event)
        self.lang_switch.pack(side=tk.RIGHT, padx=15, pady=15)

        # ☀️🌙 Tema-väljare
        self.theme_switch = ctk.CTkSwitch(url_frame, text="🌙", width=40, command=self.change_appearance_mode_event)
        self.theme_switch.pack(side=tk.RIGHT, padx=(0, 15), pady=15)
        self.theme_switch.select() 
        
        self.help_btn = ctk.CTkButton(url_frame, text=self.texts["btn_help"][self.lang], width=80, fg_color=("#d9d9d9", "#4a4a4a"), text_color=("black", "white"), hover_color=("#c9c9c9", "#5a5a5a"), command=self.open_help_window)
        self.help_btn.pack(side=tk.RIGHT, padx=(0, 15), pady=15)
        
        self.tabview = ctk.CTkTabview(main_frame, height=200)
        self.tabview.pack(fill=tk.X, pady=(0, 10))
        tab_basic = self.tabview.add("⚙️ 1")
        tab_adv = self.tabview.add("🔧 2")
        
        # --- FLIK 1 ---
        row1 = ctk.CTkFrame(tab_basic, fg_color="transparent")
        row1.pack(fill=tk.X, pady=5)
        
        self.lbl_delay = ctk.CTkLabel(row1, text=self.texts["lbl_delay"][self.lang])
        self.lbl_delay.pack(side=tk.LEFT, padx=(0, 5))
        self.delay_entry = ctk.CTkEntry(row1, width=40)
        self.delay_entry.pack(side=tk.LEFT, padx=(0, 20))
        self.delay_entry.insert(0, "0.5")
        
        self.lbl_max_pages = ctk.CTkLabel(row1, text=self.texts["lbl_max_pages"][self.lang])
        self.lbl_max_pages.pack(side=tk.LEFT, padx=(0, 5))
        self.max_pages_entry = ctk.CTkEntry(row1, width=50)
        self.max_pages_entry.pack(side=tk.LEFT, padx=(0, 20))
        self.max_pages_entry.insert(0, "0")

        self.lbl_max_depth = ctk.CTkLabel(row1, text=self.texts["lbl_max_depth"][self.lang])
        self.lbl_max_depth.pack(side=tk.LEFT, padx=(0, 5))
        self.max_depth_entry = ctk.CTkEntry(row1, width=40)
        self.max_depth_entry.pack(side=tk.LEFT, padx=(0, 20))
        self.max_depth_entry.insert(0, "0")
        
        self.lbl_format = ctk.CTkLabel(row1, text=self.texts["lbl_format"][self.lang])
        self.lbl_format.pack(side=tk.LEFT, padx=(0, 5))
        self.format_var = ctk.StringVar(value=".md")
        ctk.CTkOptionMenu(row1, variable=self.format_var, values=[".md", ".txt"], width=80).pack(side=tk.LEFT)

        row2 = ctk.CTkFrame(tab_basic, fg_color="transparent")
        row2.pack(fill=tk.X, pady=10)
        
        self.docs_var = ctk.BooleanVar(value=False)
        self.cb_docs = ctk.CTkCheckBox(row2, text=self.texts["cb_docs"][self.lang], variable=self.docs_var)
        self.cb_docs.pack(side=tk.LEFT, padx=(0, 30))

        self.lbl_mode = ctk.CTkLabel(row2, text=self.texts["lbl_mode"][self.lang])
        self.lbl_mode.pack(side=tk.LEFT, padx=(0, 10))
        self.headless_var = ctk.StringVar(value="headless")
        ctk.CTkOptionMenu(row2, variable=self.headless_var, values=["headless", "login_then_headless", "visible"], width=180).pack(side=tk.LEFT)

        row3 = ctk.CTkFrame(tab_basic, fg_color="transparent")
        row3.pack(fill=tk.X, pady=5)
        
        self.lbl_folder = ctk.CTkLabel(row3, text=self.texts["lbl_folder"][self.lang])
        self.lbl_folder.pack(side=tk.LEFT, padx=(0, 10))
        self.dir_var = ctk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "crawl_output"))
        ctk.CTkEntry(row3, textvariable=self.dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.btn_folder = ctk.CTkButton(row3, text=self.texts["btn_folder"][self.lang], width=100, command=lambda: self.dir_var.set(filedialog.askdirectory() or self.dir_var.get()))
        self.btn_folder.pack(side=tk.LEFT)
        
        # --- FLIK 2 ---
        arow1 = ctk.CTkFrame(tab_adv, fg_color="transparent")
        arow1.pack(fill=tk.X, pady=5)
        
        self.hybrid_var = ctk.BooleanVar(value=True)
        self.cb_hybrid = ctk.CTkCheckBox(arow1, text=self.texts["cb_hybrid"][self.lang], variable=self.hybrid_var)
        self.cb_hybrid.pack(side=tk.LEFT, padx=(0,20))
        
        self.traf_var = ctk.BooleanVar(value=HAS_TRAFILATURA)
        self.cb_traf = ctk.CTkCheckBox(arow1, text=self.texts["cb_traf"][self.lang], variable=self.traf_var)
        self.cb_traf.pack(side=tk.LEFT)
        if not HAS_TRAFILATURA: self.cb_traf.configure(state="disabled")

        arow2 = ctk.CTkFrame(tab_adv, fg_color="transparent")
        arow2.pack(fill=tk.X, pady=10)
        
        self.sitemap_var = ctk.BooleanVar(value=True)
        self.cb_sitemap = ctk.CTkCheckBox(arow2, text=self.texts["cb_sitemap"][self.lang], variable=self.sitemap_var)
        self.cb_sitemap.pack(side=tk.LEFT, padx=(0,20))
        
        self.robots_var = ctk.BooleanVar(value=True)
        self.cb_robots = ctk.CTkCheckBox(arow2, text=self.texts["cb_robots"][self.lang], variable=self.robots_var)
        self.cb_robots.pack(side=tk.LEFT, padx=(0,20))
        
        self.strict_var = ctk.BooleanVar(value=True) 
        self.cb_strict = ctk.CTkCheckBox(arow2, text=self.texts["cb_strict"][self.lang], variable=self.strict_var)
        self.cb_strict.pack(side=tk.LEFT)

        arow3 = ctk.CTkFrame(tab_adv, fg_color="transparent")
        arow3.pack(fill=tk.X, pady=5)
        
        self.lbl_exclude = ctk.CTkLabel(arow3, text=self.texts["lbl_exclude"][self.lang])
        self.lbl_exclude.pack(side=tk.LEFT, padx=(0, 5))
        self.exclude_entry = ctk.CTkEntry(arow3, width=150)
        self.exclude_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 20))
        
        self.lbl_require = ctk.CTkLabel(arow3, text=self.texts["lbl_require"][self.lang])
        self.lbl_require.pack(side=tk.LEFT, padx=(0, 5))
        self.require_entry = ctk.CTkEntry(arow3, width=150)
        self.require_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # --- KNAPPAR ---
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(pady=5)
        
        self.start_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_start"][self.lang], font=ctk.CTkFont(weight="bold"), fg_color="#1f6aa5", command=self.start_crawl)
        self.start_btn.pack(side=tk.LEFT, padx=10)
        
        self.pause_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_pause"][self.lang], state="disabled", fg_color=("#d9d9d9", "#4a4a4a"), text_color=("black", "white"), hover_color=("#c9c9c9", "#5a5a5a"), command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=10)
        
        self.stop_btn = ctk.CTkButton(btn_frame, text=self.texts["btn_stop"][self.lang], state="disabled", fg_color=("#d35b5b", "#a51f1f"), hover_color=("#c42b2b", "#8a1a1a"), text_color=("white", "white"), command=self.stop_crawl)
        self.stop_btn.pack(side=tk.LEFT, padx=10)
        
        # --- STATUS OCH TABELL ---
        self.stats_label = ctk.CTkLabel(main_frame, text=self.texts["status_wait"][self.lang], font=ctk.CTkFont(family="Consolas", size=12, weight="bold"), text_color="#4caf50")
        self.stats_label.pack(fill=tk.X, pady=5)
        
        table_frame = ctk.CTkFrame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)
        
        cols = ('URL', 'Status', 'Titel')
        self.tree = ttk.Treeview(table_frame, columns=cols, show='headings', height=7)
        self.tree.heading('URL', text='URL')
        self.tree.heading('Status', text=self.texts["col_status"][self.lang])
        self.tree.heading('Titel', text=self.texts["col_title"][self.lang])
        self.tree.column('URL', width=300)
        self.tree.column('Status', width=100)
        self.tree.column('Titel', width=300)
        
        # FIX: Uppdaterad snygg CTkScrollbar!
        scrollbar = ctk.CTkScrollbar(table_frame, orientation="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # FIX: Dubbelklick för att öppna länkar!
        def on_tree_double_click(event):
            item_id = self.tree.selection()
            if item_id:
                url = self.tree.item(item_id[0])['values'][0]
                if url.startswith("http"):
                    webbrowser.open(url)
                    
        self.tree.bind("<Double-1>", on_tree_double_click)
        
        # --- LOGG ---
        self.log_area = ctk.CTkTextbox(main_frame, font=ctk.CTkFont(family="Consolas", size=11), height=100)
        self.log_area.pack(fill=tk.BOTH, expand=False, pady=(10,0))

    def process_queue(self):
        try:
            # FIX: Bearbeta upp till 20 meddelanden i taget för att undvika frysningar
            for _ in range(20):
                msg_type, data = self.msg_queue.get_nowait()
                
                if msg_type == "log":
                    t = datetime.now().strftime("%H:%M:%S")
                    self.log_area.insert(tk.END, f"[{t}] {data}\n")
                    self.log_area.see(tk.END)
                    # FIX: Rensa gamla loggrader för att spara RAM-minne
                    if int(self.log_area.index('end-1c').split('.')[0]) > 500:
                        self.log_area.delete("1.0", "2.0")
                        
                elif msg_type == "table":
                    url, status, title = data
                    self.tree.insert('', 0, values=(url, status, title))
                    if len(self.tree.get_children()) > 100:
                        self.tree.delete(self.tree.get_children()[-1])
                        
                elif msg_type == "stats_data":
                    fmt_str = self.texts["stats_fmt"][self.lang]
                    self.stats_label.configure(text=fmt_str.format(*data))
                    
                elif msg_type == "login_wait":
                    msg = "Logga in i webbläsaren. Tryck OK här när du är klar!" if self.lang == "sv" else "Please login in the browser. Click OK here when done!"
                    messagebox.showinfo("Inloggning / Login", msg)
                    if self.crawler_instance: self.crawler_instance.login_event.set()
                    
                elif msg_type == "done":
                    self.start_btn.configure(state="normal")
                    self.pause_btn.configure(state="disabled")
                    self.stop_btn.configure(state="disabled")
                    
        except queue.Empty:
            pass
            
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
        
        for item in self.tree.get_children(): self.tree.delete(item)
        self.log_area.delete("1.0", tk.END)
        
        exclude_list = [k.strip().lower() for k in self.exclude_entry.get().split(",") if k.strip()]
        require_list = [k.strip().lower() for k in self.require_entry.get().split(",") if k.strip()]
        
        config = {
            "start_url": url,
            "output_dir": self.dir_var.get(),
            "delay": delay_val,
            "max_pages": max_pages_val,
            "max_depth": max_depth_val,
            "save_format": self.format_var.get(),
            "headless_mode": self.headless_var.get(),
            "respect_robots": self.robots_var.get(),
            "find_sitemap": self.sitemap_var.get(),
            "use_hybrid": self.hybrid_var.get(),
            "use_trafilatura": self.traf_var.get(),
            "download_docs": self.docs_var.get(),
            "strict_domain": self.strict_var.get(),
            "exclude_keywords": exclude_list,
            "require_keywords": require_list,
            "incremental": True
        }
        
        self.crawler_instance = WebCrawler(config, self.msg_queue)
        threading.Thread(target=self.crawler_instance.crawl, daemon=True).start()

    def toggle_pause(self):
        if self.crawler_instance:
            is_paused = self.crawler_instance.pause()
            self.pause_btn.configure(text=self.texts["btn_resume"][self.lang] if is_paused else self.texts["btn_pause"][self.lang])

    def stop_crawl(self):
        if self.crawler_instance:
            self.stop_btn.configure(state="disabled")
            self.crawler_instance.stop()

def main():
    app = AppGUI(ctk.CTk())
    app.root.mainloop()

if __name__ == "__main__":
    main()