"""Gemensamma texthjälpare för crawler och isolerad dokumentkonvertering."""
from __future__ import annotations
import hashlib
import logging
import os
import re
from enum import Enum
from typing import Dict, List, Optional

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

def file_sha256(path: str) -> str:
    """Hashar en fil strömmande, även på Python 3.11."""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()

def markdown_table(rows: List[List], header: Optional[List] = None) -> str:
    """Bevara kolumner, radbrytningar och pipe-tecken i dokumenttabeller."""
    if not rows:
        return ''
    width = max(len(row) for row in rows)
    if header is not None:
        width = max(width, len(header))
        values = [header] + rows
    else:
        values = rows
    output = []
    for i, row in enumerate(values):
        cells = [str(value if value is not None else '').replace('\\', '\\\\')
                 .replace('|', '\\|').replace('\r', '').replace('\n', '<br>') for value in row]
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
    r'(ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts)|'
    r'disregard (the )?(previous|above|system)|'
    r'ignorera (alla )?(tidigare|föregående|ovanstående) (instruktioner|anvisningar)|'
    r'you are now (a|an|in)\b|reveal (your|the) system prompt|'
    r'nya instruktioner:|new instructions:)', re.IGNORECASE)

def detect_prompt_injection(*texts: str) -> bool:
    return any(t and _INJECTION_RE.search(t) for t in texts)

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
        text = re.sub(pnr_pattern, '[PERSONNUMMER]', text)
    if config.get("remove_phone"):
        phone_pattern = (
            r'(?<!\d)(?<!\d[\-\/:])(?<![a-zA-Z])(?:\(0[1-9]\)?[\s\-]*|(?:\+|00)46[\s\-]*(?:\(?0\)?)?[\s\-]*[1-9]|'
            r'0[\s\-]*\(?[1-9]\)?)[\s\-]*\d(?:[\s\-]*\d){4,8}\b'
        )
        text = re.sub(phone_pattern, '[TELEFON]', text)
    if config.get("remove_ip"):
        text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP-ADRESS]', text)
    return text


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith('|') and s.endswith('|') and len(s) > 1


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(_is_table_row(s) and re.match(r'^\|(?:\s*:?-+:?\s*\|)+\s*$', s))


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
