"""Gemensamma texthjälpare för crawler och isolerad dokumentkonvertering."""
from __future__ import annotations
import hashlib
import ipaddress
import logging
import os
import re
import unicodedata
from datetime import date
from enum import Enum
from typing import Dict, List, Optional, Sequence

class LogLevel(Enum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR

def slugify(text: str) -> str:
    text = str(text).replace('å', 'a').replace('ä', 'a').replace('ö', 'o')
    text = text.replace('Å', 'A').replace('Ä', 'A').replace('Ö', 'O')
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '-', text).strip('-').lower()

def metadata_line(value) -> str:
    """Opålitlig metadata får inte skapa egna källfält eller separatorrader."""
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def pdf_page_url(url: str, page_number: int) -> str:
    return f'{url.partition("#")[0]}#page={page_number}'


def pdf_page_reference(url: str, page_number: int) -> str:
    """Synlig sidhänvisning även när mottagaren filtrerar bort rubriker."""
    return f'PDF-sida {page_number}. Sidlänk: <{pdf_page_url(url, page_number)}>'


_DIAGNOSTIC_URL_RE = re.compile(r'\b(https?://)([^\s<>"`\\]+)', re.IGNORECASE)


def redact_diagnostic_urls(text: str) -> str:
    """Ta bort query, fragment och användarinfo ur HTTP-adresser i diagnostik."""
    def redact(match):
        address = match[2].split('#', 1)[0].split('?', 1)[0]
        authority, separator, path = address.partition('/')
        return match[1] + authority.rsplit('@', 1)[-1] + separator + path

    return _DIAGNOSTIC_URL_RE.sub(redact, text)


def redact_diagnostic_data(value):
    """Maskera URL:er även i nästlade fält och fritext i JSON-diagnostik."""
    if isinstance(value, str):
        return redact_diagnostic_urls(value)
    if isinstance(value, dict):
        return {key: redact_diagnostic_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_diagnostic_data(item) for item in value]
    return value


def file_sha256(path: str) -> str:
    """Hashar en fil strömmande, även på Python 3.11."""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()

def markdown_table(rows: Sequence[Sequence], header: Optional[Sequence] = None) -> str:
    """Bevara kolumner, radbrytningar och pipe-tecken i tabeller."""
    if not rows:
        return ''
    width = max(len(row) for row in rows)
    if header is not None:
        width = max(width, len(header))
        values = [header, *rows]
    else:
        values = rows
    output = []
    for i, row in enumerate(values):
        cells = [str(value if value is not None else '').replace('\\', '\\\\')
                 .replace('|', '\\|').replace('\r\n', '\n').replace('\r', '\n')
                 .replace('\n', '<br>') for value in row]
        cells.extend([''] * (width - len(cells)))
        output.append('| ' + ' | '.join(cells) + ' |')
        if i == 0:
            output.append('|' + '|'.join(['---'] * width) + '|')
    return '\n'.join(output)

def downgrade_body_h1(text: str) -> str:
    """Ändra alla # (H1) till ## (H2) i brödtext.

    Crawlern sätter redan sin egen "# Titel" i filen, så H1-rubriker
    i den extraherade brödtexten blir dubbletter. Nedgradering till H2
    bevarar den visuella strukturen utan att förvirra RAG-chunkning.
    """
    return re.sub(r'^# ', '## ', text, flags=re.MULTILINE)

def atomic_write_text(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)

ROLE_MAILBOX_LOCALPARTS = {
    'info', 'kontakt', 'registrator', 'kommun', 'kommunen', 'vaxel', 'växel',
    'support', 'kundtjanst', 'kundservice', 'medborgarservice', 'miljo',
    'bygglov', 'socialtjanst', 'skola', 'bibliotek', 'press', 'media',
    'webb', 'webmaster', 'servicecenter', 'diarium', 'kansli', 'hr',
}

_INJECTION_RE = re.compile(
    r'(ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts|directions)|'
    # Kräv ett instruktionsobjekt. Annars träffas t.ex. undantag i blanketter.
    r'\bdisregard (all |any )?(the )?(previous|prior|above) (instructions?|prompts?|directions?)\b|'
    r'\bdisregard (the )?system (instructions?|prompts?|messages?)\b|'
    r'ignorera (alla )?(tidigare|föregående|ovanstående) (instruktioner|anvisningar)|'
    # Vanliga välkomstfraser och platsangivelser är för breda som egen signal.
    r'\byou are now (an? (ai|unrestricted|unfiltered) (assistant|model|chatbot)|'
    r'in (developer|unrestricted|jailbreak) mode)\b|reveal (your|the) system prompt|'
    r'nya instruktioner:|new instructions:)', re.IGNORECASE)


def _allow_format_markers(token: re.Match) -> str:
    if token.group() == ' ':
        return '[ \u200b]+'
    return '\u200b*'.join(token[1]) + ('\u200b*:' if token[2] else '')


# Samma ordmönster kan matcha markörer inne i ord och mellan ord samtidigt.
_OBFUSCATED_INJECTION_RE = re.compile(
    re.sub(r'([^\W\d_]{2,})(:)?| ', _allow_format_markers, _INJECTION_RE.pattern), re.IGNORECASE)


def detect_prompt_injection(*texts: str) -> bool:
    for text in texts:
        if not text:
            continue
        normalized = ' '.join(unicodedata.normalize('NFKC', text).split())
        if _INJECTION_RE.search(normalized):
            return True
        if not normalized.isascii() and any(unicodedata.category(char) == 'Cf' for char in normalized):
            candidate = ''.join('\u200b' if unicodedata.category(char) == 'Cf' else char
                                for char in normalized)
            if _OBFUSCATED_INJECTION_RE.search(candidate):
                return True
    return False


_PHONE_CONTEXT_RE = re.compile(r'(?:\btel:|\b(?:tfn|telefon|mobil|fax|vxl|växel)\s*:\s*)$', re.I)
_PNR_CONTEXT_RE = re.compile(r'\b(?:personnummer|samordningsnummer|pnr)\s*:?\s*$', re.I)
_IP_NUMBERING_RE = re.compile(r'(?:\b(?:avsnitt|kapitel|paragraf|punkt|version|versionen|ver\.?|'
                              r'section|chapter|paragraph)\s*[:§]?\s*|§\s*)$', re.I)


def _valid_person_number(number: str) -> bool:
    """Kontrollera datum (även samordningsnummer) och Luhn före maskering."""
    digits = re.sub(r'[-+]', '', number)
    short = digits[-10:]
    day = int(short[4:6])
    if day > 60:
        day -= 60
    year = int(digits[:4]) if len(digits) == 12 else 2000 + int(short[:2])
    try:
        date(year, int(short[2:4]), day)
    except ValueError:
        return False
    return sum(sum(divmod(int(c) * (2 if i % 2 == 0 else 1), 10))
               for i, c in enumerate(short)) % 10 == 0

def clean_pii_text(config: Dict, text: str) -> str:
    if not text:
        return text
    if config.get("remove_email"):
        keep_role = config.get("keep_role_emails", False)

        def _mask_email(m):
            local = m.group(0).split('@')[0].lower()
            if keep_role and local in ROLE_MAILBOX_LOCALPARTS:
                return m.group(0)
            return '[E-POST]'

        text = re.sub(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
            _mask_email, text
        )
    if config.get("remove_phone"):
        # 1. tel:-länkar (t.ex. [Ring](tel:0701234567)) maskeras direkt så att inte
        # personnummertvätten felaktigt tolkar 10-siffriga mobilnummer som födelsedatum
        text = re.sub(r'\btel:(?:\+|00)?[0-9\s\-]{5,20}', 'tel:[TELEFON]', text, flags=re.IGNORECASE)
        # 2. Telefonnummer med etikett (Tfn:, Mobil:, etc.)
        text = re.sub(
            r'(\b(?:tfn|telefon|mobil|fax|vxl|växel)\s*:\s*)(?:\(0[1-9]\)?[\s\-]*|(?:\+|00)46[\s\-]*(?:\(?0\)?)?[\s\-]*[1-9]|'
            r'0[\s\-]*\(?[1-9]\)?)[\s\-]*\d(?:[\s\-]*\d){4,8}\b',
            r'\g<1>[TELEFON]', text, flags=re.IGNORECASE
        )
    if config.get("remove_pnr"):
        pnr_pattern = (
            r'(?<!\d)(?:19|20)?\d{2}(?:0[1-9]|1[0-2])'
            r'(?:0[1-9]|[12]\d|3[01]|[6-9]\d)[\-\+]?\d{4}(?!\d)'
        )
        original = text
        def mask_person_number(match):
            prefix = original[max(0, match.start() - 60):match.start()]
            # Tydlig telefonkontext gäller också när telefonnummer ska bevaras.
            if _PHONE_CONTEXT_RE.search(prefix):
                return match.group(0)
            if _valid_person_number(match.group(0)) or _PNR_CONTEXT_RE.search(prefix):
                return '[PERSONNUMMER]'
            return match.group(0)
        text = re.sub(pnr_pattern, mask_person_number, text)
    if config.get("remove_phone"):
        phone_pattern = (
            r'(?<!\d)(?<!\d[\-\/:])(?<![a-zA-Z])(?:\(0[1-9]\)?[\s\-]*|(?:\+|00)46[\s\-]*(?:\(?0\)?)?[\s\-]*[1-9]|'
            r'0[\s\-]*\(?[1-9]\)?)[\s\-]*\d(?:[\s\-]*\d){4,8}\b'
        )
        text = re.sub(phone_pattern, '[TELEFON]', text)
    if config.get("remove_ip"):
        original = text
        def mask_ip(match):
            prefix = original[max(0, match.start() - 60):match.start()]
            if _IP_NUMBERING_RE.search(prefix):
                return match.group(0)
            try:
                ipaddress.IPv4Address(match.group(0))
            except ipaddress.AddressValueError:
                return match.group(0)
            return '[IP-ADRESS]'
        text = re.sub(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', mask_ip, text)
    return text


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith('|') and s.endswith('|') and len(s) > 1


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(_is_table_row(s) and re.match(r'^\|(?:\s*:?-+:?\s*\|)+\s*$', s))


_TABLE_CELL_TOKEN = re.compile(r'(\\[\\|]|\|)')
_TABLE_CELL_BREAK = re.compile(r'<br\s*/?>', re.IGNORECASE)
_MARKDOWN_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def _table_cells(line: str) -> List[str]:
    parts = _TABLE_CELL_TOKEN.split(line.strip()[1:-1])
    cells = []
    current = [parts[0]]
    for token, text in zip(parts[1::2], parts[2::2]):
        if token == '|':
            cells.append(''.join(current))
            current = [text]
        else:
            current.extend((token[1:], text))
    cells.append(''.join(current))
    return [metadata_line(_TABLE_CELL_BREAK.sub(' / ', cell)) for cell in cells]


def add_table_row_text(text: str, *, allow_headerless: bool = False) -> str:
    """Lägg läsbara radstycken efter varje extraherad Markdown-tabell."""
    if '|' not in text:
        return text

    lines = text.split('\n')
    output = []
    fence = ''
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = _MARKDOWN_FENCE.match(line)
        if marker:
            run, rest = marker.groups()
            if not fence:
                fence = run
            elif run.startswith(fence) and not rest.strip():
                fence = ''
        if fence or not _is_table_row(line) or line.startswith(('    ', '\t')):
            output.append(line)
            i += 1
            continue

        start = i
        while i < len(lines) and _is_table_row(lines[i]) and not lines[i].startswith(('    ', '\t')):
            i += 1
        block = lines[start:i]
        output.extend(block)
        if len(block) < 2:
            continue
        if _is_table_sep(block[1]):
            headers, rows = _table_cells(block[0]), block[2:]
        elif allow_headerless:
            # Trafilatura utelämnar avgränsaren för tabeller utan rubriker.
            headers, rows = [], block
        else:
            continue

        paragraphs = []
        for row in rows:
            fields = []
            for col, value in enumerate(_table_cells(row)):
                if value:
                    header = headers[col] if col < len(headers) else ''
                    fields.append(f'{header or f"Kolumn {col + 1}"}: {value}')
            if fields:
                # Fast inledning hindrar en cellrubrik från att bli en egen Markdown-rubrik.
                paragraphs.extend(('', 'Tabellrad. ' + '. '.join(fields) + '.'))
        if paragraphs:
            output.extend(paragraphs)
            if i < len(lines) and lines[i].strip():
                output.append('')
    return '\n'.join(output)


def _split_long_text(text: str, max_words: int, overlap_words: int) -> List[str]:
    """Delar en lång text radvis (bevarar tabeller, listor och stycken).

    Rader längre än `max_words` delas på ord. Överlappet består av hela rader
    från slutet av föregående del (upp till `overlap_words` ord).

    Om en tabell delas över flera delar propageras tabellhuvudet (rubrikrad
    och avgränsare) till varje efterföljande del för att bevara tabellstrukturen.
    """
    units: List[str] = []
    for line in text.split("\n"):
        words = line.split()
        if len(words) <= max_words:
            units.append(line)
        else:
            for i in range(0, len(words), max_words):
                units.append(" ".join(words[i:i + max_words]))

    active_table_header: Optional[List[str]] = None
    parts: List[str] = []
    cur: List[str] = []
    cur_words = 0
    new_units = 0          # enheter i `cur` som inte bara är överlapp

    for i, unit in enumerate(units):
        s = unit.strip()
        if _is_table_row(s) and i + 1 < len(units) and _is_table_sep(units[i + 1]):
            active_table_header = [unit, units[i + 1]]
        elif not _is_table_row(s):
            active_table_header = None

        n = len(unit.split())
        if cur and cur_words + n > max_words and new_units:
            parts.append("\n".join(cur).strip())
            overlap: List[str] = []
            ow = 0
            for prev in reversed(cur):
                pw = len(prev.split())
                if ow + pw > overlap_words:
                    break
                overlap.insert(0, prev)
                ow += pw
            cur = overlap
            cur_words = ow
            new_units = 0

            # Om vi befinner oss inuti en tabell och rubriken saknas i överlappet,
            # lägg till tabellhuvudet överst i nästa chunk så att kolumnerna förblir kända.
            if active_table_header and not (len(cur) >= 2 and cur[0] == active_table_header[0]):
                if cur and _is_table_sep(cur[0]):
                    cur.pop(0)
                cur = list(active_table_header) + cur
                cur_words += sum(len(h.split()) for h in active_table_header)

        cur.append(unit)
        cur_words += n
        new_units += 1

    if cur and new_units:
        parts.append("\n".join(cur).strip())
    return [p for p in parts if p]
