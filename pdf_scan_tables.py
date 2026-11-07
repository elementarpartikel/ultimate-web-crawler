"""Geometrisk läsning av tydliga numeriska tabeller i ett OCR-lager."""

import re
from bisect import bisect_right
from statistics import median

from crawler_text import markdown_table

_YEAR = re.compile(r'(?:[12][0-9]{3})')
_YEAR_LABEL = re.compile(r'(?:år|ar|årtal|year)s?[.:]?', re.I)
_YEAR_HEADING = re.compile(r'(?:(?:år|ar|year) )?([12][0-9]{3})[.:]?', re.I)
_NUMBER = re.compile(
    r'(?:(?:ca\.?|c:a|cirka|about) )?(?:[+−–-] ?)?(?:[0-9]{1,3}(?:[ .,\x27’][0-9]{3})+|[0-9]+)'
    r'(?:[,:.][0-9]{1,6})?(?: ?(?:kr\.?|SEK|%|‰))?\*?', re.I)
_UNITS = {'kr', 'kr.', 'kronor', 'sek', 'tkr', 'mnkr', 'mkr', '%', 'procent'}
_DASHES = {'–', '—', '-'}


def _center(word):
    return (word[1] + word[3]) / 2


def _word_rows(words):
    rows = []
    for word in sorted(words, key=_center):
        height = word[3] - word[1]
        if not rows or _center(word) - _center(rows[-1][0]) > min(height, rows[-1][0][3] - rows[-1][0][1]) * 0.48:
            rows.append([])
        rows[-1].append(word)
    return [sorted(row, key=lambda word: word[0]) for row in rows]


def _cells(row):
    height = median(word[3] - word[1] for word in row)
    cells = []
    for word in row:
        if not cells or word[0] - cells[-1][-1][2] >= max(4, height * 0.72):
            cells.append([])
        cells[-1].append(word)
    return cells


def _cell_text(words):
    return ' '.join(word[4] for word in words)


def _body_row(row):
    if any(left[2] > right[0] + 1.5 for left, right in zip(row, row[1:])):
        return None
    cells = _cells(row)
    if not 2 <= len(cells) <= 12:
        return None
    values = [_cell_text(cell) for cell in cells]
    if (not all(_NUMBER.fullmatch(value) or value in _DASHES for value in values[1:])
            or not any(_NUMBER.fullmatch(value) for value in values[1:])):
        return None
    first = values[0]
    if not (_YEAR.fullmatch(first) or (any(c.isalpha() for c in first) and len(first) <= 95)):
        return None
    gutters = [(left[-1][2], right[0][0]) for left, right in zip(cells, cells[1:])]
    return values, gutters


def _header_cells(row, cuts):
    cells = [[] for _ in range(len(cuts) + 1)]
    for word in row:
        if any(word[0] + 1 < cut < word[2] - 1 for cut in cuts):
            return None
        cells[bisect_right(cuts, (word[0] + word[2]) / 2)].append(word)
    return [_cell_text(cell) for cell in cells]


def _header(rows, start, end, cuts, values, height):
    units = None
    for index in range(start - 1, max(-1, start - 3), -1):
        row = rows[index]
        if _center(rows[start][0]) - _center(row[0]) > height * 4.0:
            break
        labels = _header_cells(row, cuts)
        if labels is None:
            break
        if index == start - 1 and all(not label or label.lower() in _UNITS for label in labels):
            units = labels
            continue
        if _YEAR_LABEL.fullmatch(labels[0]) and all(labels):
            year_columns = [i for i, label in enumerate(labels) if _YEAR_LABEL.fullmatch(label)]
            for column in year_columns:
                years = [value[column] for value in values]
                if not all(_YEAR.fullmatch(year) for year in years):
                    return None
                if len(set(years)) != len(years) or years not in (sorted(years), sorted(years, reverse=True)):
                    return None
        else:
            # I vissa tabeller står åren som kolumnrubriker och varje rad har en saketikett.
            matches = [_YEAR_HEADING.fullmatch(label) for label in labels[1:]]
            if (len(matches) < 2 or not all(matches)
                    or not all(any(c.isalpha() for c in value[0]) for value in values)):
                return None
            if (len(matches) == 2 and labels[0]
                    and labels[0].strip('()').lower() not in _UNITS
                    and all(_YEAR.fullmatch(label) for label in labels[1:])):
                return None
            years = [int(match.group(1)) for match in matches]
            steps = [right - left for left, right in zip(years, years[1:])]
            # Lösa fyrsiffriga belopp kan annars feltolkas som ett nytt tabellhuvud.
            if not (all(1 <= step <= 10 for step in steps)
                    or all(-10 <= step <= -1 for step in steps)):
                return None
            labels[0] = labels[0] or 'Post'
        # Prosa i en angränsande spalt får inte smyga in som rubrik.
        if any(len(label.split()) > 8 for label in labels):
            return None
        if units is not None:
            labels = [f'{label} ({unit})' if unit and unit.lower() not in label.lower().split() else label
                      for label, unit in zip(labels, units)]
        region = [word for body in rows[index:end] for word in body]
        bbox = (min(word[0] for word in region), min(word[1] for word in region),
                max(word[2] for word in region), max(word[3] for word in region))
        return {'bbox': bbox, 'text': markdown_table(values, labels), 'table': True, 'columns': ()}
    return None


def scan_table_items(words):
    """Knyt befintliga tecken till rader och kolumner utan att rätta eller fylla i tal."""
    # Linjer kan läsas som fristående pipe-tecken. Behåll tecken som sitter ihop med ett tal.
    rows = _word_rows(word for word in words if word[4] and word[4] != '|' and word[3] > word[1])
    bodies = [_body_row(row) for row in rows]
    tables = []
    start = 0
    while start < len(rows):
        # En möjlig rubrik i de två närmast föregående raderna begränsar sökningen.
        if (bodies[start] is None or not any(
                any(_YEAR_LABEL.fullmatch(word[4]) for word in row)
                or sum(bool(_YEAR_HEADING.fullmatch(word[4])) for word in row) >= 2
                for row in rows[max(0, start - 2):start])):
            start += 1
            continue
        values, gutters = bodies[start]
        height = median(word[3] - word[1] for word in rows[start])
        end = start + 1
        table_values = [values]
        while end < len(bodies):
            body = bodies[end]
            if (body is None or len(body[1]) != len(gutters)
                    or _center(rows[end][0]) - _center(rows[end - 1][0]) > height * 2.8):
                break
            row_values, row_gutters = body
            shared = [(max(left, row_left), min(right, row_right))
                      for (left, right), (row_left, row_right) in zip(gutters, row_gutters)]
            if any(right - left < 2 for left, right in shared):
                break
            table_values.append(row_values)
            gutters = shared
            end += 1
        if end - start >= 3:
            cuts = [(left + right) / 2 for left, right in gutters]
            table = _header(rows, start, end, cuts, table_values, height)
            if table is not None:
                tables.append(table)
                start = end
                continue
        start += 1
    return tables
