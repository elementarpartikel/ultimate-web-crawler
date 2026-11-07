"""PDF/Office/OCR utan GUI, crawler, HTML-parser eller webbläsarimport."""
from __future__ import annotations
import hashlib
import importlib.util
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from crawler_runtime import document_process_environment, remove_tree
from crawler_text import (LogLevel, slugify, metadata_line, file_sha256, markdown_table, add_table_row_text,
                          downgrade_body_h1, atomic_write_text, detect_prompt_injection, clean_pii_text,
                          pdf_page_reference, _split_long_text)
from pdf_source_corrections import verified_pdf_tables
from pdf_scan_tables import scan_table_items
from pdf_dense_currency import read_dense_currency_table

HAS_PYMUPDF = bool(importlib.util.find_spec('pymupdf') or importlib.util.find_spec('fitz'))
HAS_DOCX = bool(importlib.util.find_spec('docx'))
HAS_OPENPYXL = bool(importlib.util.find_spec('openpyxl'))
HAS_PPTX = bool(importlib.util.find_spec('pptx'))


def __getattr__(name):
    """En PDF-worker ska inte importera Office-bibliotek i onödan."""
    # Statiska importuttryck gör biblioteken synliga även för paketeringsverktyg.
    if name == 'pymupdf':
        try:
            import pymupdf as value
        except ImportError:
            import fitz as value
    elif name == 'DocxDocument':
        from docx import Document as value
    elif name == 'DocxTable':
        from docx.table import Table as value
    elif name == 'DocxParagraph':
        from docx.text.paragraph import Paragraph as value
    elif name == 'xl_load_workbook':
        from openpyxl import load_workbook as value
    elif name == 'PptxPresentation':
        from pptx import Presentation as value
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value


def _load_parser(ext):
    if ext == '.pdf':
        if 'pymupdf' not in globals():
            __getattr__('pymupdf')
    elif ext in ('.docx', '.dotx'):
        for name in ('DocxDocument', 'DocxTable', 'DocxParagraph'):
            if name not in globals():
                __getattr__(name)
    elif ext == '.xlsx' and 'xl_load_workbook' not in globals():
        __getattr__('xl_load_workbook')
    elif ext == '.pptx' and 'PptxPresentation' not in globals():
        __getattr__('PptxPresentation')

class DocumentConverter:
    """Konverterar binära dokument (PDF, Word, Excel, PPTX) till Markdown.

    Extraherar text och skapar en .md-fil med intranät-URL:en i toppen,
    så att RAG-system kan citera rätt källa även för dokument-text.
    Originaldokumentet behålls alltid i dokument/-mappen.
    """

    SUPPORTED = {
        '.pdf': 'PDF', '.docx': 'Word', '.dotx': 'Word-mall',
        '.xlsx': 'Excel', '.pptx': 'PowerPoint',
    }
    # Äldre format stöds inte direkt av Python-biblioteken. Om LibreOffice
    # (soffice) finns på datorn konverteras de först till moderna format.
    LEGACY_TARGETS = {
        '.doc': 'docx', '.rtf': 'docx', '.odt': 'docx',
        '.xls': 'xlsx', '.ods': 'xlsx',
        '.ppt': 'pptx', '.odp': 'pptx',
    }
    LEGACY_NAMES = {
        '.doc': 'Word (äldre)', '.rtf': 'RTF', '.odt': 'OpenDocument Text',
        '.xls': 'Excel (äldre)', '.ods': 'OpenDocument Calc',
        '.ppt': 'PowerPoint (äldre)', '.odp': 'OpenDocument Presentation',
    }
    MAX_XLSX_ROWS_PER_SHEET = 5000
    LEGACY_TIMEOUT_S = 120

    # Metadata-titlar som är skräp och inte ska användas som dokumenttitel.
    _JUNK_TITLE_RE = re.compile(
        r'^(microsoft (word|excel|powerpoint)\b.*|untitled.*|namnlöst.*|'
        r'document\s*\d*|dokument\s*\d*|powerpoint presentation|presentation\d*|'
        r'slide 1|bild 1|.*\.(docx?|xlsx?|pptx?|pdf|odt)|\s*)$',
        re.IGNORECASE)

    # Generiska länktexter som inte är beskrivande nog att använda som
    # dokument-titel. Matchning sker case-insensitivt efter strip().
    GENERIC_LINK_TEXTS = {
        'ladda ner', 'ladda ner fil', 'ladda ned', 'ladda ned fil',
        'hämta', 'hämta fil', 'download', 'download file',
        'öppna', 'öppna fil', 'öppna dokument',
        'visa', 'visa fil', 'visa dokument',
        'klicka här', 'läs mer', 'read more', 'click here',
        'länk', 'link', 'pdf', 'dokument', 'document',
        '(via sitemap)', 'via sitemap',
    }

    def __init__(self, output_dir: str,
                 log_fn=None, pii_cleaner=None, ocr: bool = True,
                 ocr_languages: str = "swe+eng", ocr_max_pages: int = 50,
                 work_dir: Optional[str] = None, allow_libreoffice: bool = False):
        self.texts_dir = os.path.join(output_dir, "texter")
        self._log = log_fn
        self._clean_pii = pii_cleaner
        self.ocr_enabled = ocr
        self.ocr_languages = ocr_languages
        self.ocr_max_pages = ocr_max_pages
        self._ocr_ok: Optional[bool] = None
        self.work_dir = work_dir
        self.last_used_ocr = False    # True om senaste dokumentets text kommer från OCR
        self.last_error = ""          # orsak till senaste misslyckade konvertering
        self.last_raw_injection = False
        self.allow_libreoffice = allow_libreoffice
        self._soffice = (shutil.which("soffice") or shutil.which("libreoffice")
                         or shutil.which("soffice.exe")) if allow_libreoffice else None
        os.makedirs(self.texts_dir, exist_ok=True)

    @staticmethod
    def md_filename(doc_url: str, filename: str) -> str:
        """Stabilt, kollisionsfritt namn på den konverterade .md-filen.

        Bygger alltid på samma två delar — en kapad slug av filnamnet och en
        hash av dokument-URL:en — så att crawlern och konverteraren aldrig kan
        komma överens om olika namn, och så att hashen aldrig kapas bort.
        """
        base = os.path.splitext(os.path.basename(filename))[0]
        base = re.sub(r'_[0-9a-f]{6}$', '', base)          # ta bort ev. gammal hash
        slug = slugify(base)[:40] or "dokument"
        url_hash = hashlib.md5(doc_url.encode('utf-8')).hexdigest()[:8]
        return f"{slug}_{url_hash}_doc.md"

    def md_path_for(self, doc_url: str, filename: str) -> str:
        return os.path.join(self.texts_dir, self.md_filename(doc_url, filename))

    def can_convert(self, filepath: str) -> bool:
        ext = os.path.splitext(filepath)[1].lower()
        if ext in self.LEGACY_TARGETS:
            return self.allow_libreoffice and bool(self._soffice)
        if ext == '.pdf' and not HAS_PYMUPDF:
            return False
        if ext in ('.docx', '.dotx') and not HAS_DOCX:
            return False
        if ext == '.xlsx' and not HAS_OPENPYXL:
            return False
        if ext == '.pptx' and not HAS_PPTX:
            return False
        return ext in self.SUPPORTED

    def _legacy_to_modern(self, filepath: str, ext: str, workdir: str) -> Optional[str]:
        """Konverterar .doc/.xls/.ppt m.fl. med LibreOffice. Returnerar sökväg eller None."""
        if not self.allow_libreoffice:
            self.last_error = 'LibreOffice-konvertering är avstängd'
            return None
        target = self.LEGACY_TARGETS[ext]
        try:
            subprocess.run(
                [self._soffice, '-env:UserInstallation=' + Path(
                    os.path.join(workdir, 'lo-profile')).as_uri(),
                 "--headless", "--norestore", "--convert-to", target,
                 "--outdir", workdir, filepath],
                check=True, capture_output=True, timeout=self.LEGACY_TIMEOUT_S,
                cwd=workdir, env=document_process_environment(workdir))
        except Exception as e:
            self.last_error = f"LibreOffice-konvertering misslyckades: {e}"
            return None
        out = os.path.join(workdir, os.path.splitext(os.path.basename(filepath))[0]
                           + "." + target)
        return out if os.path.exists(out) else None

    def convert(self, filepath: str, doc_url: str,
                referer_url: str = "", referer_title: str = "",
                link_text: str = "", fetched_at: Optional[str] = None,
                server_last_modified: str = '') -> Optional[str]:
        """Konverterar dokument till .md. Returnerar sökväg eller None.

        Metadata bäddas in i brödtexten (inte bara i header) eftersom RAG-
        pipelines ofta kapar de första raderna före retrieval. Källan
        upprepas också sist i filen — om RAG-tjänstens chunkning delar dokumentet
        i flera bitar säkrar det att åtminstone en chunk har källan med.
        """
        self.last_error = ""
        self.last_used_ocr = False
        self.last_raw_injection = False
        tmpdir = None
        ext = os.path.splitext(filepath)[1].lower()
        src_path, src_ext = filepath, ext
        try:
            if ext in self.LEGACY_TARGETS:
                if not self.allow_libreoffice:
                    self.last_error = 'LibreOffice-konvertering är avstängd'
                    return None
                if not self._soffice:
                    self.last_error = "Äldre format kräver LibreOffice (soffice)"
                    return None
                tmpdir = tempfile.mkdtemp(prefix="uwc_", dir=self.work_dir)
                src_path = self._legacy_to_modern(filepath, ext, tmpdir)
                if not src_path:
                    return None
                src_ext = os.path.splitext(src_path)[1].lower()

            extractors = {
                '.pdf': self._extract_pdf,
                '.docx': self._extract_docx,
                '.dotx': self._extract_docx,
                '.xlsx': self._extract_xlsx,
                '.pptx': self._extract_pptx,
            }
            extractor = extractors.get(src_ext)
            if not extractor:
                self.last_error = f"Filtypen {ext} stöds inte"
                return None

            office_metadata = {}
            text = (extractor(src_path) if src_ext == '.pdf'
                    else extractor(src_path, metadata=office_metadata))
            if not text or len(text.strip()) < 20:
                if ext == '.pdf':
                    if self.ocr_available():
                        self.last_error = "Ingen text i PDF:en, och OCR gav heller ingen text"
                    else:
                        self.last_error = ("Ingen text i PDF:en — troligen en skannad bild. "
                                           "Installera Tesseract för OCR")
                else:
                    self.last_error = "För lite text i dokumentet"
                if self._log:
                    self._log(
                        f"  ⚠ Konvertering gav för lite text "
                        f"({len(text.strip()) if text else 0} tecken): "
                        f"{os.path.basename(filepath)} — {self.last_error}",
                        LogLevel.WARNING)
                return None

            self.last_raw_injection |= detect_prompt_injection(text)
            text = add_table_row_text(text)
            if self._clean_pii:
                text = self._clean_pii(text)

            # Titel-prioritet (faller från bäst till sista utvägen):
            #   1. link_text   — <a>-taggens text (t.ex. "Eko ack 2025 Januari"),
            #                    kräver >5 tecken OCH att texten inte är generisk
            #                    ("Ladda ner fil", "Download", etc.)
            #   2. metadata_title — dokumentets egen title (PDF/Office metadata)
            #   3. humaniserat filnamn ("Eko ack 2025 januari")
            #   4. referer_title — sidans <title> (ofta för generisk,
            #                      t.ex. "Startsida - Tyresö kommun")
            #   5. rå slug som sista utväg
            filename = os.path.basename(filepath)
            base_name = os.path.splitext(filename)[0]
            metadata_title = (self._extract_pdf_metadata_title(src_path) if src_ext == '.pdf'
                              else office_metadata.get('title', ''))
            if metadata_title and self._JUNK_TITLE_RE.match(metadata_title.strip()):
                metadata_title = ""
            humanized = self._humanize_filename(base_name)
            clean_link = (link_text or "").strip()

            # De-duplicera dubbla länktexter ("Ladda ner fil Ladda ner fil")
            if clean_link:
                total_len = len(clean_link)
                if total_len % 2 == 1:  # udda längd → testa med mellanslag i mitten
                    mid = total_len // 2
                    if (clean_link[mid] == ' '
                            and clean_link[:mid] == clean_link[mid + 1:]):
                        clean_link = clean_link[:mid]
                elif total_len % 2 == 0:
                    mid = total_len // 2
                    if clean_link[:mid].rstrip() == clean_link[mid:].lstrip():
                        clean_link = clean_link[:mid].rstrip()

            def _is_usable_link_text(text: str) -> bool:
                """True om link_text är tillräckligt beskrivande för titel."""
                if len(text) <= 5:
                    return False
                return text.lower() not in self.GENERIC_LINK_TEXTS

            raw_title = (
                (clean_link if _is_usable_link_text(clean_link) else "")
                or metadata_title
                or humanized
                or (referer_title.strip() if referer_title else "")
                or base_name
            )

            # Strippa storleks-suffix som "pdf, 535.8 kB, öppnas i nytt fönster"
            title = re.sub(
                r'\s*(pdf|docx?|xlsx?|pptx?|pages?)?\s*,?\s*'
                r'\d+(?:[.,]\d+)?\s*[kKmMgG]?[bB][, .].*$',
                '', raw_title, flags=re.IGNORECASE).strip()
            title = re.sub(r'\s*[, .]+\s*$', '', title) or raw_title
            title = metadata_line(title)
            # Titeln kan komma från länktext/metadata som inte passerat PII-tvätten
            if self._clean_pii:
                title = self._clean_pii(title)
                if self._clean_pii and referer_title:
                    referer_title = self._clean_pii(referer_title)

            file_type = (self.SUPPORTED.get(ext) or self.LEGACY_NAMES.get(ext)
                         or "Dokument")

            # Bygg metadata-block som SYNLIG brödtext (fetstil), inte ren header.
            # RAG-tjänsters chunkning hoppar ofta över rubriktext men behåller brödtext.
            meta_lines = [f"# {title}", ""]
            meta_lines.append(f"**Källa:** {referer_url or doc_url}")
            if doc_url:
                meta_lines.append(f"**Dokument-URL:** {doc_url}")
            if filename:
                # Visa originalfilnamnet (utan vår interna hash-suffix)
                display_name = re.sub(r'_[0-9a-f]{6}(\.\w+)$', r'\1', filename)
                display_name = metadata_line(self._clean_pii(display_name) if self._clean_pii else display_name)
                meta_lines.append(f"**Filnamn:** {display_name}")
            meta_lines.append(f"**Filtyp:** {file_type}")
            # LibreOffice kan ändra temporärfilens metadata under konverteringen.
            dates = {}
            if src_path == filepath:
                dates = self._extract_pdf_dates(src_path) if src_ext == '.pdf' else office_metadata
            fetched = datetime.now().isoformat(timespec='seconds') if fetched_at is None else fetched_at
            server_modified = self._parse_date(server_last_modified, http=True)
            date_lines = []
            for label, value in (('Hämtad', fetched), ('Dokumentets ändringsdatum', dates.get('modified')),
                                 ('Serverns ändringsdatum', server_modified), ('Skapad', dates.get('created'))):
                if value:
                    date_lines.append(f'**{label}:** {metadata_line(str(value))}')
            meta_lines.extend(date_lines)
            if self.last_raw_injection or detect_prompt_injection(text, *meta_lines):
                meta_lines.append('**Flaggor:** possible_prompt_injection')
            if self.last_used_ocr:
                meta_lines.append("**Textkälla:** OCR (automatisk textigenkänning — kan innehålla fel)")
            meta_lines.append("")
            meta_lines.append("---")
            meta_lines.append("")

            # Upprepa källan sist i filen — om dokumentet chunkas i flera
            # bitar har åtminstone första och sista bitarna källinformation.
            footer_lines = ["", "", "---", ""]
            footer_lines.append(f"**Källa:** {referer_url or doc_url}")
            if doc_url:
                footer_lines.append(f"**Dokument-URL:** {doc_url}")

            text = downgrade_body_h1(text)
            # Gör varje sida/bild och långa avsnitt begripliga också vid extern chunkning.
            text = self._repeat_document_context(text, title, doc_url, referer_url, date_lines,
                                                 is_pdf=src_ext == '.pdf')
            md_content = ("\n".join(meta_lines) + text
                          + "\n".join(footer_lines))

            md_path = self.md_path_for(doc_url, filename)

            atomic_write_text(md_path, md_content)
            return md_path

        except Exception as e:
            self.last_error = f"Konvertering misslyckades: {e}"
            if self._log:
                self._log(
                    f"  ✗ Konvertering misslyckades "
                    f"({os.path.basename(filepath)}): {e}", LogLevel.ERROR)
            return None
        finally:
            if tmpdir:
                remove_tree(self.work_dir or tempfile.gettempdir(), tmpdir)

    # ─── Titelextraktion ────────────────────────────────────
    @staticmethod
    def _repeat_document_context(text: str, title: str, doc_url: str,
                                  referer_url: str, dates: List[str], *, is_pdf: bool = False) -> str:
        context = '\n'.join([f'**Dokument:** {title}', f'**Källa:** {referer_url or doc_url}',
                             f'**Dokument-URL:** {doc_url}', *dates])
        result = []
        for part in re.split(r'(?=^##\s)', text, flags=re.MULTILINE):
            part = part.strip()
            if not part:
                continue
            match = re.match(r'^##\s+([^\n]+)\n?', part)
            heading = match.group(1).strip() if match else title
            body = part[match.end():].strip() if match else part
            unit = re.match(r'^(Sida|Bild)\s+(\d+)\b', heading, re.IGNORECASE)
            section_context = context
            if unit:
                if is_pdf and match and unit[1].lower() == 'sida' and unit.end() == len(heading):
                    section_context += '\n\n' + pdf_page_reference(doc_url, int(unit[2]))
                heading = f'{title} – {unit.group(1).lower()} {unit.group(2)}'
            for piece in _split_long_text(body, 400, 0) if body else ['']:
                result.append(f'## {heading}\n\n{section_context}\n\n{piece}')
        return '\n\n'.join(result)

    @staticmethod
    def _parse_date(value, http: bool = False) -> str:
        if not value:
            return ''
        try:
            if isinstance(value, datetime):
                parsed = value
            elif http:
                parsed = parsedate_to_datetime(str(value))
            elif str(value).startswith('D:'):
                match = re.fullmatch(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?\d{2}'?)?", str(value))
                if not match:
                    return ''
                year, month, day, hour, minute, second, offset = match.groups()
                parsed = datetime(int(year), int(month), int(day), int(hour or 0),
                                  int(minute or 0), int(second or 0))
                if offset:
                    cleaned = offset.replace("'", '')
                    if cleaned == 'Z':
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    else:
                        sign = 1 if cleaned[0] == '+' else -1
                        parsed = parsed.replace(tzinfo=timezone(sign * timedelta(
                            hours=int(cleaned[1:3]), minutes=int(cleaned[3:5]))))
                if hour is None:
                    return parsed.date().isoformat()
            else:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.isoformat(timespec='seconds')
        except (ValueError, TypeError, OverflowError):
            return ''

    def _extract_pdf_dates(self, filepath: str) -> Dict:
        # Metadata beskriver filens tekniska historik, aldrig taxans giltighet.
        try:
            with pymupdf.open(filepath) as doc:
                meta = doc.metadata or {}
                return {'created': self._parse_date(meta.get('creationDate')),
                        'modified': self._parse_date(meta.get('modDate'))}
        except Exception:
            return {}

    def _read_office_metadata(self, document, attribute: str, metadata: Optional[Dict]):
        if metadata is None:
            return
        try:
            props = getattr(document, attribute)
        except Exception:
            return
        try:
            metadata['title'] = (props.title or '').strip()
        except Exception:
            pass
        try:
            metadata.update(created=self._parse_date(props.created), modified=self._parse_date(props.modified))
        except Exception:
            pass

    @staticmethod
    def _humanize_filename(name: str) -> str:
        """Gör ett slug-namn läsbart: 'ansokan-om-x_ad7919' → 'Ansokan om x'.

        Eftersom slugify har strippat svenska tecken (ä→a, ö→o) går det
        inte att återskapa originalnamnet helt — men "Ansokan om
        parkeringstillstand" är vida bättre än "ansokan-om-parkeringstillstand_ad7919".
        """
        # Ta bort hash-suffix
        name = re.sub(r'_[0-9a-f]{6}$', '', name)
        # Bindestreck och underscore → mellanslag
        name = re.sub(r'[-_]+', ' ', name).strip()
        # Stor första bokstav (ev. övriga ord lämnas som de är —
        # vi vet inte vilka som är egennamn)
        if name:
            name = name[0].upper() + name[1:]
        return name

    def _extract_pdf_metadata_title(self, filepath: str) -> str:
        try:
            with pymupdf.open(filepath) as doc:
                title = (doc.metadata or {}).get('title', '')
                return title.strip() if title else ""
        except Exception:
            return ""

    def ocr_available(self) -> bool:
        """OCR kräver att Tesseract är installerat (PyMuPDF anropar det)."""
        if not (HAS_PYMUPDF and self.ocr_enabled):
            return False
        if self._ocr_ok is None:
            try:
                _load_parser('.pdf')
                pymupdf.get_tessdata()
                self._ocr_ok = True
            except Exception:
                self._ocr_ok = False
        return self._ocr_ok

    def _ocr_page(self, page, verified_tables=(), *, reread=None) -> str:
        textpage = self._ocr_textpage(page, full=True)
        text = ''
        try:
            if textpage is None:
                return ''
            text = page.get_text('text', textpage=textpage)
            self.last_raw_injection |= detect_prompt_injection(text)
            if verified_tables or (not page.rotation and self._pdf_scan_images(page, page.rect)):
                blocks = page.get_text('dict', textpage=textpage)['blocks']
                lines = [line for block in blocks for line in block.get('lines', ())]
                if verified_tables and text.strip():
                    return self._pdf_replace_ocr_regions(lines, verified_tables)
                words = page.get_text('words', textpage=textpage)
                if reread is not None:
                    try:
                        dense = read_dense_currency_table(page, pymupdf, words, reread)
                        if dense is not None:
                            left, top, right, bottom = dense[0]
                            if not any(top <= (y0+y1)/2 <= bottom and x0 < right and x1 > left
                                       and (x0 < left-2 or x1 > right+2)
                                       for x0, y0, x1, y1 in (line['bbox'] for line in lines)):
                                return self._pdf_replace_ocr_regions(lines, (dense,))
                    except Exception:
                        # Vid misslyckad omläsning återstår helsidans OCR-text.
                        pass
                tables = self._pdf_scan_table_items(words, lines)
                if tables:
                    items = tables + [{'bbox': line['bbox'], 'text': self._pdf_line_text(line['spans'])}
                                      for line in lines if not self._pdf_scan_line_covered(line, tables)]
                    return '\n\n'.join(item['text'] for item in self._pdf_reading_order(items, page.rect.width))
        except Exception:
            # Fel i tabellanalysen får inte ta bort den lästa råtexten.
            if text.strip() and verified_tables:
                return '\n\n'.join((text.strip(), *(table for _, table in verified_tables)))
        return text

    def _pdf_replace_ocr_regions(self, lines, verified_tables):
        """Byt tabelltexten i OCR-resultatet och behåll läsordningen utanför bildområdena."""
        output = []
        emitted = set()
        for line in lines:
            x0, y0, x1, y1 = line['bbox']
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            for index, ((left, top, right, bottom), table) in enumerate(verified_tables):
                if left <= cx <= right and top <= cy <= bottom:
                    if index not in emitted:
                        output.append(f'\n{table}\n')
                        emitted.add(index)
                    break
            else:
                output.append(self._pdf_line_text(line['spans']))
        output.extend(f'\n{table}\n' for index, (_, table) in enumerate(verified_tables)
                      if index not in emitted)
        return '\n'.join(output)

    @staticmethod
    def _pdf_scan_images(page, bounds):
        return [area for kind, bbox in page.get_bboxlog() if kind == 'fill-image'
                if (area := pymupdf.Rect(bbox) & bounds).get_area() >= bounds.get_area() * 0.8]

    @staticmethod
    def _pdf_scan_line_covered(line, tables):
        box = pymupdf.Rect(line['bbox'])
        return any((pymupdf.Rect(table['bbox']) + (-2, -2, 2, 2)).contains(box) for table in tables)

    def _pdf_scan_table_items(self, words, lines, *, excluded=()):
        tables = []
        for table in scan_table_items(words):
            area = pymupdf.Rect(table['bbox'])
            if any(area.intersects(rect) for rect in excluded):
                continue
            # Ersätt bara hela rader. En rad som korsar tabellgränsen kan bära annan text.
            if any(area.intersects(pymupdf.Rect(line['bbox']))
                   and not self._pdf_scan_line_covered(line, [table]) for line in lines):
                continue
            tables.append(table)
        return tables

    def _pdf_native_scan_tables(self, page, bounds, lines, excluded):
        if page.rotation or not (scans := self._pdf_scan_images(page, bounds)):
            return []
        traces = [span for span in page.get_texttrace() if span['chars']]
        if not any(span['type'] == 3 for span in traces):
            return []
        painted = [pymupdf.Rect(span['bbox']) for span in traces if span['type'] != 3]
        words = [word for word in page.get_text('words')
                 if any(scan.contains(pymupdf.Rect(word[:4])) for scan in scans)]
        return self._pdf_scan_table_items(words, lines, excluded=(*excluded, *painted))

    def _ocr_textpage(self, page, *, full: bool, dpi: int = 200):
        for lang in dict.fromkeys((self.ocr_languages, 'eng')):
            try:
                return page.get_textpage_ocr(language=lang, dpi=dpi, full=full)
            except Exception:
                continue
        return None

    @staticmethod
    def _pdf_image_regions(page, blocks, bounds):
        """Välj större bildytor som saknar ett täckande textlager."""
        native = [(pymupdf.Rect(line['bbox']), sum(len(span.get('text', '').split())
                                                  for span in line.get('spans', ())))
                  for block in blocks for line in block.get('lines', ())]
        images = []
        for kind, bbox in page.get_bboxlog():
            if kind != 'fill-image':
                continue
            area = pymupdf.Rect(bbox) & bounds
            if (area.get_area() >= bounds.get_area() * 0.08 and area.width >= bounds.width * 0.2
                    and area.height >= bounds.height * 0.1
                    and sum(words for rect, words in native if area.contains((rect.tl + rect.br) / 2)) < 20):
                images.append(area)
        return images

    def _pdf_mixed_ocr_lines(self, page, regions, native):
        textpage = self._ocr_textpage(page, full=False)
        if textpage is None:
            return []
        existing = {(span['text'], span['bbox']) for line in native for span in line['spans']}
        lines = []
        for block in page.get_text('dict', textpage=textpage)['blocks']:
            for line in block.get('lines', ()):
                rect = pymupdf.Rect(line['bbox'])
                if any(area.contains((rect.tl + rect.br) / 2) for area in regions):
                    spans = [span for span in line['spans'] if span.get('font') == 'GlyphLessFont'
                             and (span['text'], span['bbox']) not in existing]
                    if spans:
                        lines.append({**line, 'spans': spans})
        return lines

    @staticmethod
    def _pdf_ocr_defects(text):
        # Synliga felmönster, inte ordlista. Äldre stavning ska kunna bevaras.
        symbols = sum(c == '\ufffd' or (c not in '\t\r\n' and unicodedata.category(c) in ('Cc', 'Co'))
                      for c in text)
        broken_words = re.findall(
            r'(?<=[^\W\d_])[~&]-?(?=[^\W\d_])|(?<!\w)%(?=[a-zåäö]{2}\b)'
            r'|(?<=[^\W\d_])([^\W\d_])\1{2,}(?=[^\W\d_])', text)
        return symbols + len(broken_words)

    def _pdf_legacy_ocr_candidates(self, page, native, bounds):
        suspicious = [(i, self._pdf_line_text(line['spans'])) for i, line in enumerate(native)]
        suspicious = [(i, text) for i, text in suspicious if len(text) <= 512 and self._pdf_ocr_defects(text)]
        if not suspicious:
            return []
        scans = self._pdf_scan_images(page, bounds)
        if not scans:
            return []

        traces = page.get_texttrace()
        hidden = [span for span in traces if span['type'] == 3]
        painted = [pymupdf.Rect(span['bbox']) for span in traces if span['type'] != 3 and span['chars']]
        if not hidden:
            return []
        hidden_boxes = [pymupdf.Rect(span['bbox']) for span in hidden if span['chars']]
        candidates = []
        for i, text in suspicious:
            rect = pymupdf.Rect(native[i]['bbox'])
            if (not rect.is_empty and any((rect & scan).get_area() >= rect.get_area() * 0.8 for scan in scans)
                    and any((rect & box).get_area() >= rect.get_area() * 0.25 for box in hidden_boxes)
                    and not any(not (rect & box).is_empty for box in painted)):
                candidates.append((i, text, rect))
        return candidates

    def _pdf_repair_legacy_ocr(self, page, native, candidates):
        """Rätta belagda radfel med ny OCR utan att byta ut sidans textlager."""
        textpage = self._ocr_textpage(page, full=True, dpi=300)
        if textpage is None:
            return
        fresh = page.get_text('text', textpage=textpage)
        self.last_raw_injection |= detect_prompt_injection(fresh)
        words = page.get_text('words', textpage=textpage)
        numbers = r'[+−–-]?\d+(?:[.,:/-]\d+)*(?:[ \u00a0]*[%‰](?![^\W\d_]))?'
        anchors = r'[^\W\d_]{3,}'
        for i, original, rect in candidates:
            cy = (rect.y0 + rect.y1) / 2
            tolerance = min(rect.height * 0.45, 4.0)
            aligned = sorted(word for word in words if rect.x0 <= (word[0] + word[2]) / 2 < rect.x1
                             and abs((word[1] + word[3]) / 2 - cy) <= tolerance)
            if not aligned or any(a[2] > b[0] + 1 for a, b in zip(aligned, aligned[1:])):
                continue
            replacement = ' '.join(word[4] for word in aligned)
            old = ' '.join(original.lower().split())
            new = replacement.lower()
            if (len(new) > 512 or not set(re.findall(anchors, old)).intersection(re.findall(anchors, new))
                    or set(re.findall(r'(?<!\S)[|=~_]+(?!\S)', new)) - set(re.findall(r'(?<!\S)[|=~_]+(?!\S)', old))):
                continue
            similarity = SequenceMatcher(None, old, new,
                                         autojunk=False).ratio()
            if (similarity < 0.82 or self._pdf_ocr_defects(replacement) >= self._pdf_ocr_defects(original)
                    or re.findall(numbers, original) != re.findall(numbers, replacement)):
                continue
            native[i] = {**native[i], 'spans': [{**native[i]['spans'][0], 'text': replacement}]}
            self.last_used_ocr = True

    def _extract_pdf(self, filepath: str) -> str:
        _load_parser('.pdf')
        source_tables = verified_pdf_tables(filepath)
        doc = pymupdf.open(filepath)
        pages, font_weights = [], Counter()
        margin_counts = Counter()
        ocr_attempts = 0

        def reread(sheet):
            nonlocal ocr_attempts
            ocr_attempts += 1
            textpage = self._ocr_textpage(sheet, full=True, dpi=288)
            if textpage is not None:
                self.last_raw_injection |= detect_prompt_injection(sheet.get_text('text', textpage=textpage))
            return textpage

        try:
            for i, page in enumerate(doc):
                bounds = page.rect * page.derotation_matrix
                verified_tables = source_tables.get(i + 1, ())

                def in_verified_region(bbox):
                    x0, y0, x1, y1 = bbox
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    return any(left <= cx <= right and top <= cy <= bottom
                               for (left, top, right, bottom), _ in verified_tables)

                text = page.get_text('text', sort=True)
                self.last_raw_injection |= detect_prompt_injection(text)
                if not text.strip() and ocr_attempts < self.ocr_max_pages and self.ocr_available():
                    # Sidan saknar textlager (skannad bild) → OCR
                    ocr_attempts += 1
                    if verified_tables:
                        text = self._ocr_page(page, verified_tables)
                    elif ocr_attempts < self.ocr_max_pages:
                        text = self._ocr_page(page, reread=reread)
                    else:
                        text = self._ocr_page(page)
                    if text.strip():
                        self.last_used_ocr = True
                        self.last_raw_injection |= detect_prompt_injection(text)
                    elif verified_tables:
                        text = '\n\n'.join(table for _, table in verified_tables)
                    pages.append({'number': i + 1, 'items': [], 'fallback': text.strip(),
                                  'width': bounds.width, 'height': bounds.height})
                    continue
                items = []
                try:
                    blocks = page.get_text('dict', flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES,
                                           sort=True)['blocks']
                    native = [line for block in blocks for line in block.get('lines', ())]
                    repaired_scan_attempted = False
                    if self.ocr_enabled and ocr_attempts < self.ocr_max_pages and not page.rotation:
                        try:
                            candidates = self._pdf_legacy_ocr_candidates(page, native, bounds)
                            if candidates and self.ocr_available():
                                ocr_attempts += 1
                                repaired_scan_attempted = True
                                self._pdf_repair_legacy_ocr(page, native, candidates)
                        except Exception:
                            # Vid OCR-fel behålls det befintliga textlagret.
                            pass
                    try:
                        items = self._pdf_tables(page, native)
                    except Exception:
                        # Misslyckad tabellidentifiering får inte ta bort råtexten.
                        items = []
                    if verified_tables:
                        items = [item for item in items if not in_verified_region(item['bbox'])]
                    if items:
                        gutters = self._pdf_column_gutters([{'bbox': line['bbox']} for line in native],
                                                           bounds.width, max_line_width=0.30)
                        if len(gutters) >= 2:
                            # Grafiska paneler kan se ut som en tabell för linjedetektorn.
                            items = [item for item in items if not (
                                any(item['bbox'][0] < cut < item['bbox'][2] for cut in gutters)
                                and any((cell or '').count('\n') >= 3 for cell in item['header'])
                                and not any(re.fullmatch(r'[\d\s.,%]+', cell or '')
                                            for row in item['rows'] for cell in row if cell and cell.strip()))]
                    covered = [rect for item in items for rect in (item['bbox'], item['header_bbox']) if rect]
                    scan_tables = []
                    try:
                        excluded = [pymupdf.Rect(rect) for rect in covered]
                        excluded.extend(pymupdf.Rect(bbox) for bbox, _ in verified_tables)
                        scan_tables = self._pdf_native_scan_tables(page, bounds, native, excluded)
                    except Exception:
                        # Vid osäker geometri används textlagret som vanligt.
                        pass
                    ocr_lines = []
                    if self.ocr_enabled and ocr_attempts < self.ocr_max_pages and not repaired_scan_attempted:
                        try:
                            regions = self._pdf_image_regions(page, blocks, bounds)
                            if regions and self.ocr_available():
                                ocr_attempts += 1
                                ocr_lines = self._pdf_mixed_ocr_lines(page, regions, native)
                        except Exception:
                            # Ett OCR-fel får inte ta bort sidans ursprungliga text.
                            pass
                    if ocr_lines:
                        recognized = '\n'.join(self._pdf_line_text(line['spans']) for line in ocr_lines)
                        self.last_raw_injection |= detect_prompt_injection(recognized)
                        self.last_used_ocr |= bool(recognized.strip())
                    for index, line in enumerate(native + ocr_lines):
                        spans = line.get('spans', [])
                        line_text = self._pdf_line_text(spans)
                        if not line_text:
                            continue
                        bbox = tuple(line['bbox'])
                        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                        from_ocr = index >= len(native)
                        if verified_tables and in_verified_region(bbox):
                            continue
                        if scan_tables and self._pdf_scan_line_covered(line, scan_tables):
                            continue
                        if not from_ocr and any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in covered):
                            continue
                        size = 0 if from_ocr else max((span.get('size', 0) for span in spans), default=0)
                        bold = all(span.get('flags', 0) & 16 for span in spans if span.get('text', '').strip())
                        margin = ('top' if bbox[3] < bounds.height * 0.1 else
                                  'bottom' if bbox[1] > bounds.height * 0.9 else '')
                        key = self._pdf_margin_key(line_text, margin)
                        items.append({'text': line_text, 'bbox': bbox, 'size': size,
                                      'bold': bold, 'margin_key': key})
                        if not margin and not from_ocr:
                            for span in spans:
                                font_weights[round(span.get('size', 0), 1)] += len(span.get('text', ''))
                    margin_counts.update({item['margin_key'] for item in items if item.get('margin_key')})
                    items.extend(scan_tables)
                    items.extend({'text': table, 'bbox': bbox, 'table': True, 'columns': ()}
                                 for bbox, table in verified_tables)
                except Exception:
                    items = []
                    if verified_tables:
                        text = '\n\n'.join((text.strip(), *(table for _, table in verified_tables)))
                pages.append({'number': i + 1, 'items': items, 'fallback': text.strip(),
                              'width': bounds.width, 'height': bounds.height})
        finally:
            doc.close()
        self._pdf_continue_tables(pages)
        body_size = font_weights.most_common(1)[0][0] if font_weights else 11
        repeated = {key for key, count in margin_counts.items()
                    if len(pages) >= 3 and count >= max(3, math.ceil(len(pages) * 0.6))}
        output = []
        for record in pages:
            items = [item for item in record['items'] if item.get('margin_key') not in repeated]
            lines = []
            for item in self._pdf_reading_order(items, record['width']):
                value = item['text']
                size = item.get('size', 0)
                if (not item.get('table') and not item.get('margin_key') and len(value) < 160
                        and (size >= body_size * 1.2 or (item.get('bold') and size >= body_size))):
                    level = 3 if size >= body_size * 1.4 else 4
                    value = '#' * level + ' ' + value
                lines.append(value)
            # Om all text är en upprepad sidfot ska den inte återinföras via fallback.
            value = '\n\n'.join(lines) if record['items'] else record['fallback']
            if value.strip():
                output.append(f"## Sida {record['number']}\n\n{value.strip()}")
        return '\n\n'.join(output)

    def _pdf_tables(self, page, native):
        detected = [(table, table.extract()) for table in page.find_tables().tables
                    if table.col_count >= 2 and table.row_count >= 2]

        def has_vat_legend(rows):
            return any(re.search(r'grå\s+ruta\s*=\s*exkl\.\s*moms.*vit\s+ruta\s*=\s*inkl\.\s*moms',
                                 ' '.join(value.split()), re.I)
                       for row in rows[:3] for value in row if value)

        strict = []
        if any(has_vat_legend(rows)
               or any(cell and cell[2] - cell[0] < 8 for row in table.rows for cell in row.cells)
               or sum(bool(cell and cell.strip()) for cell in rows[-1]) == 1
               for table, rows in detected if rows):
            try:
                strict = [(table, table.extract()) for table in page.find_tables(strategy='lines_strict').tables
                          if table.col_count >= 2 and table.row_count >= 2]
            except Exception:
                pass
        items = []
        for table, rows in detected:
            if not rows:
                continue
            if has_vat_legend(rows):
                matrix = next((self._pdf_vat_matrix(page, candidate, values, native, rows)
                               for candidate, values in strict
                               if has_vat_legend(values)
                               and all(abs(a - b) <= 3 for a, b in zip(table.bbox, candidate.bbox))), None)
                if matrix:
                    caption, header, body = matrix
                    items.append({'text': caption + '\n\n' + markdown_table(body, header),
                                  'bbox': tuple(table.bbox), 'table': True, 'columns': (),
                                  'header': header, 'rows': body, 'header_bbox': None})
                    continue
            table, rows = next(((candidate, values) for candidate, values in strict
                                if self._pdf_strict_table_matches(table, rows, candidate, values)),
                               (table, rows))
            geometry = table.rows
            rows = self._pdf_restore_cell_spans(geometry, rows, native)
            external = table.header.external
            if external:
                header, header_rows = table.header.names, 0
            else:
                header, header_rows = self._pdf_table_header(geometry, rows)
            body = rows[header_rows:]
            body = self._pdf_stacked_vat_rows(header, geometry[header_rows:], body, native)
            table_text = markdown_table(body, header)
            if not table_text:
                continue
            cells = max((row.cells for row in geometry), key=lambda row: sum(cell is not None for cell in row))
            columns = tuple(cell[0] for cell in cells) if all(cells) else ()
            items.append({'text': table_text, 'bbox': tuple(table.bbox), 'table': True,
                          'columns': columns, 'header': header, 'rows': body,
                          'extracted_rows': rows, 'header_rows': header_rows,
                          'header_bbox': tuple(table.header.bbox) if external else None})
        return items

    @staticmethod
    def _pdf_amount(value, *, currency=False):
        suffix = r'\s+kr' if currency else ''
        return bool(re.fullmatch(r'\d+(?:[ \u00a0]\d{3})*(?:,\d{1,2})?' + suffix, value))

    def _pdf_vat_matrix(self, page, table, rows, native, original):
        """Koppla varje beloppspar till de mätarklasser som cellen verkligen täcker."""
        if len(rows) < 5 or rows[-2][0] != 'Mätarinstallation' or rows[-1][0] != 'Mätarklass':
            return None
        installations, classes = rows[-2][2:], rows[-1][2:]
        foot = table.rows[-1].cells[2:]
        if (not all(foot) or not all(installations) or not all(classes)
                or len(foot) != len(installations) or len(foot) != len(classes)
                or len(set(classes)) != len(classes)):
            return None
        fills = [(path['rect'], path['fill']) for path in page.get_drawings()
                 if path.get('fill') and path['rect'].width > 3 and path['rect'].height > 3]
        spans = [span for line in native for span in line['spans']
                 if self._pdf_amount(' '.join(span['text'].split()), currency=True)]
        body, seen = [], []
        for geometry, row in zip(table.rows[2:-2], rows[2:-2]):
            if not row[0] or not row[0].isdigit() or not row[1] or not self._pdf_amount(row[1]):
                return None
            assigned = set()
            for cell, value in zip(geometry.cells[2:], row[2:]):
                if not value or not value.strip():
                    continue
                pair = [' '.join(line.split()) for line in value.splitlines() if line.strip()]
                if not cell or len(pair) != 2 or not all(self._pdf_amount(v, currency=True) for v in pair):
                    return None
                in_cell = []
                for span in spans:
                    x = (span['bbox'][0] + span['bbox'][2]) / 2
                    y = span['origin'][1] - span['size'] * 0.35
                    if cell[0] <= x < cell[2] and cell[1] <= y < cell[3]:
                        color = next((fill for rect, fill in reversed(fills) if rect.contains((x, y))), (1, 1, 1))
                        shade = ('grey' if min(color) >= 0.55 and max(color) <= 0.97
                                 and max(color) - min(color) <= 0.03 else
                                 'white' if min(color) >= 0.98 else '')
                        in_cell.append((y, ' '.join(span['text'].split()), shade))
                if [(value, shade) for _, value, shade in sorted(in_cell)] != [(pair[0], 'grey'), (pair[1], 'white')]:
                    return None
                covered = [i for i, rect in enumerate(foot)
                           if cell[0] - 2 <= rect[0] and rect[2] <= cell[2] + 2]
                if (not covered or assigned.intersection(covered)
                        or abs(foot[covered[0]][0] - cell[0]) > 2
                        or abs(foot[covered[-1]][2] - cell[2]) > 2):
                    return None
                assigned.update(covered)
                seen.extend(pair)
                body.extend([row[0], row[1], classes[i], installations[i], *pair] for i in covered)
            if not assigned:
                return None
        amounts = [' '.join(line.split()) for row in original[2:-2] for value in row if value
                   for line in value.splitlines() if self._pdf_amount(' '.join(line.split()), currency=True)]
        if not body or Counter(amounts) != Counter(seen):
            return None
        return rows[0][0], ['Prisklass', 'Max volym per år (m³)', 'Mätarklass',
                            'Mätarinstallation', 'Exkl. moms', 'Inkl. moms'], body

    def _pdf_stacked_vat_rows(self, header, geometry, rows, native):
        """Lägg till rader för säkra baslinjeträffar i celler med flera belopp."""
        if (len(header) != 4 or not re.match(r'ex(?:kl)?\.\s*moms\b', header[2] or '', re.I)
                or not re.match(r'in(?:k|kl)\.\s*moms\b', header[3] or '', re.I)):
            return rows
        output = []
        for row, shape in zip(rows, geometry):
            output.append(row)
            if len(row) != 4 or not all(shape.cells) or not all(row[2:]):
                continue
            prices = [[value.strip() for value in cell.splitlines() if value.strip()] for cell in row[2:]]
            if min(sum(self._pdf_amount(value) for value in values) for values in prices) < 2:
                continue
            columns = [[] for _ in row]
            for line in native:
                for index, cell in enumerate(shape.cells):
                    spans = [span for span in line['spans'] if span['text'].strip()
                             and cell[0] <= (span['bbox'][0] + span['bbox'][2]) / 2 < cell[2]
                             and cell[1] <= span['origin'][1] < cell[3]]
                    if spans:
                        main = max(spans, key=lambda span: span['size'])
                        columns[index].append((main['origin'][1], main['size'], self._pdf_line_text(spans)))
            for column in columns:
                column.sort()
            amounts = [[(y, size, value) for y, size, value in col if self._pdf_amount(value)]
                       for col in columns[2:]]
            if any([value for _, _, value in col] != [value for value in old if self._pdf_amount(value)]
                   for col, old in zip(amounts, prices)):
                continue
            def group_lines(entries):
                groups = []
                for entry in entries:
                    if not groups or entry[0] - groups[-1][-1][0] > 1.65 * entry[1]:
                        groups.append([])
                    groups[-1].append(entry)
                return groups

            groups = group_lines(columns[0])
            unit_groups = group_lines(columns[1])
            linked = []
            labels_used, left_used, right_used = set(), set(), set()
            for left_index, (y, size, ex) in enumerate(amounts[0]):
                rhs = [(index, inc) for index, (ry, _, inc) in enumerate(amounts[1]) if abs(ry - y) <= 1]
                labels = [(group, index) for group in groups for index, (ly, _, label) in enumerate(group)
                          if abs(ly - y) <= 0.6 * size and not label.startswith('*')]
                if len(rhs) != 1 or len(labels) != 1:
                    continue
                right_index, inc = rhs[0]
                group, label_index = labels[0]
                prefix_y, _, prefix = group[0]
                ly, _, label = group[label_index]
                if ly in labels_used or right_index in right_used:
                    continue
                if label.startswith('('):
                    # En beräkningsrad får ärva beskrivningen ovanför först när
                    # det utskrivna uttrycket verifierar beloppet utan moms.
                    calculation = re.fullmatch(
                        r'\(\s*(\d{1,9})\s*m[³3]\s*[x×]\s*(\d{1,9}(?:,\d{1,2})?)'
                        r'\s*kr\s*/\s*m[³3]\s*,\s*exkl\.?\s*moms\s*\)', label, re.I)
                    preceding = next((entry for entry in reversed(columns[0]) if entry[0] < ly), None)
                    if (not preceding or not calculation
                            or Decimal(calculation[1]) * Decimal(calculation[2].replace(',', '.'))
                            != Decimal(ex.replace(' ', '').replace('\u00a0', '').replace(',', '.'))):
                        continue
                    prefix_y, _, prefix = preceding
                    if ly - prefix_y > 1.8 * size or prefix_y in labels_used or prefix.startswith(('*', '(')):
                        continue
                    labels_used.add(prefix_y)
                elif prefix_y in labels_used or prefix.startswith(('*', '(')):
                    prefix = label
                labels_used.add(ly)
                left_used.add(left_index)
                right_used.add(right_index)
                unit = ''
                nearby = [group for group in unit_groups if any(abs(uy - y) <= 0.6 * size for uy, _, _ in group)]
                if len(nearby) == 1:
                    unit_lines = nearby[0]
                    if all(sum(abs(other - uy) <= abs(y - uy) for other, _, _ in amounts[0]) == 1
                           for uy, _, _ in unit_lines):
                        unit = '\n'.join(value for _, _, value in unit_lines)
                    else:
                        aligned = [value for uy, _, value in unit_lines if abs(uy - y) <= 0.6 * size]
                        if len(aligned) == 1:
                            unit = aligned[0]
                linked.append(['\n'.join(dict.fromkeys((prefix, label))), unit, ex, inc])
            if len(linked) >= 2:
                remaining = []
                for values, used in zip(prices, (left_used, right_used)):
                    kept = []
                    index = 0
                    for value in values:
                        if not self._pdf_amount(value):
                            kept.append(value)
                            continue
                        if index not in used:
                            kept.append(value)
                        index += 1
                    remaining.append('\n'.join(kept))
                note = 'Tabelltext och belopp utan entydig radkoppling'
                output[-1] = [note + '\n' + (row[0] or ''), row[1], *remaining]
                output.extend(linked)
        return output

    def _pdf_strict_table_matches(self, table, rows, candidate, values):
        """Välj bort falska kolumner bara när den striktare geometrin bevarar dataraderna."""
        if (candidate.col_count >= table.col_count or candidate.bbox[3] > table.bbox[3] + 3
                or any(abs(candidate.bbox[i] - table.bbox[i]) > 3 for i in range(3))
                or candidate.header.external != table.header.external):
            return False
        candidate_rows = candidate.rows
        bands = [row.bbox[1] for row in candidate_rows]
        matched = set()
        for geometry, row in zip(table.rows, rows):
            cells = [cell for cell in row if cell and cell.strip()]
            cy = (geometry.bbox[1] + geometry.bbox[3]) / 2
            numbers = [' '.join(cell.split()) for cell in cells if self._pdf_table_value(cell)]
            if len(cells) < 2 and not numbers:
                continue
            index = bisect_right(bands, cy) - 1
            if (index < 0 or index in matched or cy >= candidate_rows[index].bbox[3]
                    or sum(bool(cell and cell.strip()) for cell in values[index]) < 2):
                return False
            if numbers != [' '.join(cell.split()) for cell in values[index]
                           if cell and self._pdf_table_value(cell)]:
                return False
            matched.add(index)
        return True

    def _pdf_restore_cell_spans(self, geometry, rows, native):
        """Behåll en hel textsekvens när dess geometri entydigt hör till en cell."""
        bands = [row.bbox[1] for row in geometry]
        by_row = [{} for _ in rows]
        ambiguous = set()
        for line_index, line in enumerate(native):
            for span in line['spans']:
                if not span['text'].strip():
                    continue
                x0, y0, x1, y1 = span['bbox']
                cy = (y0 + y1) / 2
                index = bisect_right(bands, cy) - 1
                if index < 0:
                    continue
                row = geometry[index]
                if cy >= row.bbox[3] or not row.bbox[0] <= (x0 + x1) / 2 < row.bbox[2]:
                    continue
                cell_index = next((col for col, cell in enumerate(row.cells)
                                   if cell and cell[0] - 0.5 <= x0 and x1 <= cell[2] + 0.5
                                   and cell[1] <= cy < cell[3]), None)
                if cell_index is None:
                    ambiguous.add(index)
                else:
                    by_row[index].setdefault(cell_index, {}).setdefault(line_index, []).append(span)

        def normalized(value):
            return ' '.join((value or '').split())

        for index, groups in enumerate(by_row):
            original = rows[index]
            if index in ambiguous or not any(
                    len(normalized(span['text'])) >= len(normalized(original[col])) + 6
                    for col, lines in groups.items() for spans in lines.values() for span in spans):
                continue
            repaired = list(original)
            for col, lines in groups.items():
                repaired[col] = '\n'.join(self._pdf_line_text(sorted(spans, key=lambda span: span['bbox'][0]))
                                          for spans in lines.values())
            if all(not normalized(before) or (col in groups and normalized(after)
                    and (not self._pdf_table_value(before) or normalized(before) == normalized(after)))
                   for col, (before, after) in enumerate(zip(original, repaired))):
                rows[index] = repaired
        return rows

    def _pdf_table_header(self, geometry, rows):
        """Platta ut en bekräftad grupprubrik så varje kolumn bär hela rubrikvägen."""
        first = rows[0]
        if len(rows) < 3:
            return first, 1
        parents = [(cell, value) for cell, value in zip(geometry[0].cells, first) if cell]
        children = geometry[1].cells
        lower = rows[1]
        if (len(parents) < 2 or len(children) != len(first) or len(lower) != len(children) or not all(children)
                or sum(bool(value and value.strip()) for value in lower) < 2
                or any(self._pdf_table_value(value) for value in lower if value)):
            return first, 1
        owners = [next((index for index, (cell, _) in enumerate(parents)
                        if cell[0] <= child[0] + 0.5 and child[2] <= cell[2] + 0.5), None)
                  for child in children]
        if None in owners or len(set(owners)) == len(children):
            return first, 1
        spans = Counter(owners)
        if any(spans[owner] > 1 and not value for owner, value in zip(owners, lower)):
            return first, 1
        return ['\n'.join(dict.fromkeys(value for value in (parents[owner][1], child) if value))
                for owner, child in zip(owners, lower)], 2

    @staticmethod
    def _pdf_line_text(spans: List[Dict]) -> str:
        value, previous = '', None
        for span in spans:
            part = span.get('text', '')
            if (previous and value and part and not value[-1].isspace() and not part[0].isspace()
                    and span['bbox'][0] - previous['bbox'][2] > span.get('size', 10) * 0.15):
                value += ' '
            value += part
            previous = span
        return value.strip()

    @staticmethod
    def _pdf_margin_key(text: str, margin: str) -> str:
        if not margin or len(text) > 200:
            return ''
        key = re.sub(r'\s+', ' ', text).strip().lower()
        key = re.sub(r'\b(sida|page)\s+\d+(?:\s+(av|of)\s+\d+)?', r'\1 #', key)
        if re.fullmatch(r'\d+(?:\s*/\s*\d+)?', key):
            key = '#'
        return margin + ':' + key

    @staticmethod
    def _pdf_continue_tables(pages: List[Dict]):
        def table_at_edge(page, *, first):
            content = [item for item in page['items'] if not item.get('margin_key')]
            if not content:
                return None
            key = 1 if first else 3
            item = (min if first else max)(content, key=lambda item: item['bbox'][key])
            return item if item.get('table') and item['columns'] else None

        def normalized(row):
            return [' '.join((cell or '').lower().split()) for cell in row]

        def blank(cell):
            return normalized([cell])[0] in ('', 'blank cell')

        def numbers(row):
            return {i for i, cell in enumerate(row) if re.match(r'^\s*[+−-]?\d', cell or '')}

        for previous, page in zip(pages, pages[1:]):
            tail, head = table_at_edge(previous, first=False), table_at_edge(page, first=True)
            if (not tail or not head or head['bbox'][1] > page['height'] * 0.30
                    or tail['bbox'][3] < previous['height'] * 0.72
                    or len(tail['columns']) != len(head['columns'])):
                continue
            tolerance = max(3, page['width'] * 0.01)
            if (abs(tail['bbox'][2] - head['bbox'][2]) > tolerance
                    or any(abs(a - b) > tolerance for a, b in zip(tail['columns'], head['columns']))):
                continue

            header = tail['header']
            if normalized(header) == normalized(head['header']):
                body = head['rows']
            elif head['header_rows'] == 1:
                # En datarad får inte bli tabellhuvud och upprepas i varje chunk.
                first_row = head['extracted_rows'][0]
                tail_numbers = set().union(*(numbers(row) for row in tail['rows'][-6:]))
                evidence = (numbers(first_row) & tail_numbers) - numbers(header)
                if not any(not re.fullmatch(r'(?:19|20)\d{2}', first_row[i].strip()) for i in evidence):
                    continue
                body = head['extracted_rows']
            else:
                continue

            body = [list(row) for row in body]
            if body and not blank(header[0]) and any(blank(row[0]) for row in tail['rows'][-6:]):
                label = next((row[0] for row in reversed(tail['rows']) if not blank(row[0])), '')
                if label and not numbers([label]):
                    for row in body:
                        if not blank(row[0]) or not any(not blank(cell) for cell in row[1:]):
                            break
                        row[0] = label
            head.update(header=header, rows=body, text=markdown_table(body, header))

    @staticmethod
    def _pdf_column_gutters(items: List[Dict], width: float, max_line_width: float = 0.48) -> List[float]:
        # Breda rubriker och tabeller får dela sidan i band men bestämmer inte spalterna.
        intervals = sorted((item['bbox'][0], item['bbox'][2], item['bbox'][1], item['bbox'][3])
                           for item in items if not item.get('table') and not item.get('margin_key')
                           and item['bbox'][2] - item['bbox'][0] < width * max_line_width)
        groups = []
        for start, end, top, bottom in intervals:
            if groups and start <= groups[-1][1] + width * 0.008:
                group = groups[-1]
                group[1] = max(group[1], end)
                group[2] = min(group[2], top)
                group[3] = max(group[3], bottom)
                group[4] += 1
            else:
                groups.append([start, end, top, bottom, 1])
        columns = [group for group in groups if group[4] >= 3 and group[1] - group[0] >= width * 0.07]
        return [(left[1] + right[0]) / 2 for left, right in zip(columns, columns[1:])
                if right[0] - left[1] >= width * 0.012
                and min(left[3], right[3]) - max(left[2], right[2]) >= 12]

    @staticmethod
    def _pdf_order_column_bands(items: List[Dict], gutters: List[float]) -> List[Dict]:
        pending, ordered = [], []
        for item in sorted(items, key=lambda item: (item['bbox'][1], item['bbox'][0])):
            x0, _, x1, _ = item['bbox']
            if item.get('margin_key') or any(x0 < cut < x1 for cut in gutters):
                ordered.extend(entry for _, entry in sorted(pending, key=lambda pair: pair[0]))
                pending.clear()
                ordered.append(item)
            else:
                pending.append((sum(cut <= x0 for cut in gutters), item))
        ordered.extend(entry for _, entry in sorted(pending, key=lambda pair: pair[0]))
        return ordered

    @staticmethod
    def _pdf_table_value(text: str) -> bool:
        return bool(re.fullmatch(r'[\d\s.,/%€$:+−–-]+(?:\s*(?:kr|sek|eur|m²|m2|st|kg))?|ja|nej|yes|no',
                                 text.strip(), re.I))

    @staticmethod
    def _pdf_aligned_rows(items: List[Dict], gutters: List[float]) -> List[Dict]:
        # Kantlösa tabeller med flera värdekolumner ska också läsas radvis.
        bands = []
        for item in sorted(items, key=lambda item: item['bbox'][3]):
            x0, _, x1, baseline = item['bbox']
            if item.get('table') or item.get('margin_key') or any(x0 < cut < x1 for cut in gutters):
                continue
            if not bands or baseline - bands[-1][0] > 3:
                bands.append((baseline, []))
            bands[-1][1].append((sum(cut <= x0 for cut in gutters), item))
        rows = []
        for _, cells in bands:
            cells.sort(key=lambda cell: cell[0])
            columns = [col for col, _ in cells]
            if (len(columns) >= 3 and columns[0] == 0 and len(set(columns)) == len(columns)
                    and all(DocumentConverter._pdf_table_value(item['text']) for _, item in cells[1:])):
                rows.append([item for _, item in cells])
        if len(rows) < 3:
            return items
        used = {id(item) for row in rows for item in row}
        merged = [{'text': ' | '.join(item['text'] for item in row), 'table': True,
                   'bbox': (row[0]['bbox'][0], min(item['bbox'][1] for item in row),
                            row[-1]['bbox'][2], max(item['bbox'][3] for item in row))}
                  for row in rows]
        return merged + [item for item in items if id(item) not in used]

    @staticmethod
    def _pdf_reading_order(items: List[Dict], width: float) -> List[Dict]:
        """Läs spaltvis inom band, med breda rubriker och tabeller emellan."""
        gutters = DocumentConverter._pdf_column_gutters(items, width, max_line_width=0.30)
        if len(gutters) <= 1:
            gutters = DocumentConverter._pdf_column_gutters(items, width)
        if len(gutters) > 1:
            items = DocumentConverter._pdf_aligned_rows(items, gutters)
            return DocumentConverter._pdf_order_column_bands(items, gutters)
        mid = gutters[0] if gutters else width / 2
        left = [item for item in items if item['bbox'][2] < mid and not (item.get('margin_key') or item.get('table'))]
        right = [item for item in items if item['bbox'][0] > mid and not (item.get('margin_key') or item.get('table'))]
        normal = lambda values: sorted(values, key=lambda item: (item['bbox'][1], item['bbox'][0]))
        if len(left) < 3 or len(right) < 3:
            return normal(items)
        # Priser/etiketter på samma rad är en tabell utan linjer, inte två
        # självständiga textspalter. Bevara kopplingen också i chunkad text.
        pairs, unused = [], list(right)
        for label in normal(left):
            candidates = [value for value in unused
                          if abs(value['bbox'][3] - label['bbox'][3]) <= max(
                              3, (label['bbox'][3] - label['bbox'][1]) * 0.3)]
            if candidates:
                value = min(candidates, key=lambda item: abs(item['bbox'][3] - label['bbox'][3]))
                pairs.append((label, value))
                unused.remove(value)
        def tabular(label, value):
            short = len(label['text'].split()) <= 4 and len(value['text'].split()) <= 3
            return DocumentConverter._pdf_table_value(value['text']) or short
        table_pairs = [(label, value) for label, value in pairs if tabular(label, value)]
        if (len(pairs) >= min(len(left), len(right)) * 0.7 and pairs
                and len(table_pairs) >= 3):
            paired = {id(item) for pair in table_pairs for item in pair}
            merged = [{**label, 'text': label['text'] + ' | ' + value['text'],
                       'bbox': (label['bbox'][0], min(label['bbox'][1], value['bbox'][1]),
                                value['bbox'][2], max(label['bbox'][3], value['bbox'][3])),
                       'table': True} for label, value in table_pairs]
            return DocumentConverter._pdf_reading_order(
                merged + [item for item in items if id(item) not in paired], width)
        if max(min(item['bbox'][1] for item in left), min(item['bbox'][1] for item in right)) >= min(
                max(item['bbox'][3] for item in left), max(item['bbox'][3] for item in right)):
            return normal(items)
        return DocumentConverter._pdf_order_column_bands(items, [mid])

    def _extract_docx(self, filepath: str, metadata: Optional[Dict] = None) -> str:
        _load_parser('.docx')
        doc = DocxDocument(filepath)
        self._read_office_metadata(doc, 'core_properties', metadata)
        parts = []

        def para_to_md(para) -> Optional[str]:
            text = para.text.strip()
            if not text:
                return None
            style = para.style
            name = (style.name or '') if style else ''
            if name.startswith('Heading') or name.startswith('Rubrik'):
                digits = re.findall(r'\d+', name)
                level = int(digits[0]) if digits else 1
                return f"{'#' * min(level + 1, 6)} {text}"
            if name in ('Title', 'Titel'):
                return f"## {text}"
            return text

        def table_to_md(table) -> Optional[str]:
            return markdown_table([[cell.text.strip() for cell in row.cells] for row in table.rows]) or None

        if DocxParagraph is not None and DocxTable is not None:
            # Gå igenom dokumentet i ordning så att tabeller hamnar där de står
            for child in doc.element.body.iterchildren():
                if child.tag.endswith('}p'):
                    md = para_to_md(DocxParagraph(child, doc))
                elif child.tag.endswith('}tbl'):
                    md = table_to_md(DocxTable(child, doc))
                else:
                    continue
                if md:
                    parts.append(md)
        else:
            for para in doc.paragraphs:
                md = para_to_md(para)
                if md:
                    parts.append(md)
            for table in doc.tables:
                md = table_to_md(table)
                if md:
                    parts.append(md)
        return "\n\n".join(parts)

    def _extract_xlsx(self, filepath: str, metadata: Optional[Dict] = None) -> str:
        _load_parser('.xlsx')
        wb = xl_load_workbook(filepath, read_only=True, data_only=True)
        try:
            self._read_office_metadata(wb, 'properties', metadata)
            return self._xlsx_text(wb)
        finally:
            wb.close()

    def _xlsx_text(self, wb) -> str:
        parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue                      # hoppa över dolda blad
            parts.append(f"## {sheet_name}")
            rows_data = []
            truncated = False
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= self.MAX_XLSX_ROWS_PER_SHEET:
                    truncated = True
                    break
                if any(cell is not None and cell != '' for cell in row):
                    rows_data.append(row)
            if rows_data:
                parts.append(markdown_table(rows_data))
            if truncated:
                parts.append(f"_(Bladet avkortat efter {self.MAX_XLSX_ROWS_PER_SHEET} rader.)_")
        return "\n\n".join(parts)

    def _extract_pptx(self, filepath: str, metadata: Optional[Dict] = None) -> str:
        _load_parser('.pptx')
        prs = PptxPresentation(filepath)
        self._read_office_metadata(prs, 'core_properties', metadata)
        parts = []

        def shape_text(shape, depth=0):
            if depth > 50:
                raise ValueError('För många nästlade PowerPoint-grupper')
            if hasattr(shape, 'shapes'):
                return [part for child in sorted(shape.shapes, key=lambda s: (s.top, s.left))
                        for part in shape_text(child, depth + 1)]
            if shape.has_table:
                return [markdown_table([[cell.text.strip() if not cell.is_spanned else ''
                                         for cell in row.cells] for row in shape.table.rows])]
            if shape.has_text_frame:
                return [para.text.strip() for para in shape.text_frame.paragraphs if para.text.strip()]
            return []

        for i, slide in enumerate(prs.slides):
            slide_texts = []
            for shape in sorted(slide.shapes, key=lambda s: (s.top, s.left)):
                slide_texts.extend(shape_text(shape))
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                notes = frame.text.strip() if frame is not None else ''
                if notes:
                    slide_texts.append('### Talarnoteringar\n\n' + notes)
            if slide_texts:
                parts.append(
                    f"## Bild {i + 1}\n\n" + "\n\n".join(slide_texts))
        return "\n\n".join(parts)

_DOCUMENT_CONVERSION_LOCK = threading.Lock()

def convert_document_result(converter, filepath: str, url: str, ref: Dict) -> Dict:
    """Fångar resultat och diagnostik under samma lås, utan delad resultatstatus."""
    with _DOCUMENT_CONVERSION_LOCK:
        path = converter.convert(filepath, url,
                                 referer_url=ref.get('referer_url', ''),
                                 referer_title=ref.get('referer_title', ''),
                                 link_text=ref.get('link_text', ''),
                                 **({key: ref[key] for key in ('fetched_at', 'server_last_modified') if key in ref}))
        return {"path": path, "ocr": converter.last_used_ocr,
                "error": converter.last_error}
