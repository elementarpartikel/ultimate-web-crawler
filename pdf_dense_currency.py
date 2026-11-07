"""Omläsning av skannade årstabeller med separata kronor- och öreskolumner."""

import re
from itertools import groupby
from statistics import median

from crawler_text import markdown_table
from pdf_scan_tables import _word_rows

_YEAR = re.compile(r'[12][0-9]{3}\.?')
_KRONOR = re.compile(r'(?:[0-9]{1,3}(?:[ ,.][0-9]{3})+|[0-9]{1,9})')
_ORE = re.compile(r'[0-9]{1,2}')
_DASHES = {'-', '–', '—', '−'}
_SCALE = 4


def _bands(indices):
    for _, group in groupby(enumerate(indices), key=lambda pair: pair[1] - pair[0]):
        positions = [position for _, position in group]
        yield positions[0], positions[-1] + 1


def _header_and_total(words):
    for row in _word_rows(word for word in words if word[3] > word[1] and word[4] != '|'):
        headings = [(left, right) for left, right in zip(row, row[1:])
                    if left[4].casefold() in ('år', 'ar', 'year') and _YEAR.fullmatch(right[4])
                    and 0 <= right[0] - left[2] <= 12]
        if not 2 <= len(headings) <= 7:
            continue
        years = [int(right[4].rstrip('.')) for _, right in headings]
        if years != list(range(years[0], years[0] + len(years))):
            continue
        centers = [(left[0] + right[2]) / 2 for left, right in headings]
        widths = [b - a for a, b in zip(centers, centers[1:])]
        width = median(widths)
        if not 28 <= width <= 100 or any(abs(w - width) > width * 0.16 for w in widths):
            continue
        top = median((left[1] + left[3]) / 2 for left, _ in headings)
        totals = [(a, b) for a in words if a[4].casefold() in ('summa', 'total')
                  and a[2] < centers[0] - width * 0.35
                  and 32 < (a[1] + a[3]) / 2 - top < 320
                  for b in _row_near(words, a)
                  if b[4].strip('|.:').casefold() in ('kronor', 'kr')
                  and a[2] <= b[0] <= a[2] + 16
                  and b[2] <= centers[0] - width * 0.35]
        if len(totals) == 1:
            return years, centers, width, top, min(left[1] for left, _ in headings), totals[0]
    return None


def _row_near(words, anchor):
    cy = (anchor[1] + anchor[3]) / 2
    return (word for word in words if abs((word[1] + word[3]) / 2 - cy) <= 4)


def _grid(page, fitz, header, words):
    _, centers, width, top, heading_top, total_words = header
    total_bottom = max(word[3] for word in total_words)
    scale = 2
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                          clip=fitz.Rect(0, top - 6, centers[-1] + width * 0.7, total_bottom + 6),
                          colorspace=fitz.csGRAY)
    data, w, h = pix.samples, pix.width, pix.height
    x_at = lambda x: round(x * scale - pix.x)
    y_at = lambda y: round(y * scale - pix.y)
    left = x_at(centers[0] - width * 0.35)
    right = x_at(centers[-1] + width * 0.35)
    if not 0 <= left < right <= w or h < 8:
        return None
    background = median(data[y*w+x] for y in range(4, h, 13) for x in range(left, right, 13))
    cutoff = min(185, background - 25)
    black = data.translate(bytes(int(value < cutoff) for value in range(256)))
    ink = data.translate(bytes(int(value < min(135, background - 62)) for value in range(256)))
    line_min = (right - left) * 0.53
    horizontal = list(_bands(y for y in range(max(0, y_at(top + 4)), h)
                             if sum(ink[y*w+left:y*w+right]) >= line_min))
    if len(horizontal) != 2:
        return None
    header_end, total_start = horizontal[0][1], horizontal[1][0]
    if total_start - header_end < 32:
        return None

    body = range(header_end + 4, total_start - 3)
    strength = [sum(black[y*w+x] for y in body) for x in range(w)]

    def rule(start, stop):
        candidates = [(a, b) for a, b in _bands(x for x in range(max(0, start), min(w, stop))
                                                if strength[x] >= len(body) * 0.46) if b - a <= 7]
        if len(candidates) != 1:
            return None
        a, b = candidates[0]
        return max(range(a, b), key=strength.__getitem__)

    midpoints = [centers[0] - width / 2, *((a+b)/2 for a, b in zip(centers, centers[1:])),
                 centers[-1] + width / 2]
    edges = [rule(x_at(mid - width * 0.14), x_at(mid + width * 0.14)) for mid in midpoints]
    if any(edge is None for edge in edges):
        return None
    left_edge, right_edge = (edges[0] + pix.x) / scale, (edges[-1] + pix.x) / scale
    header_end_y = (header_end + pix.y) / scale
    for x0, y0, x1, y1, value, *_ in words:
        if not (left_edge <= (x0+x1)/2 <= right_edge and heading_top <= (y0+y1)/2 <= header_end_y):
            continue
        title = value.strip(' |.[]').casefold()
        if title and title not in ('år', 'ar', 'year', 'kr', 'kronor', 'öre', 'ore') and not _YEAR.fullmatch(title):
            # En annan tryckt underrubrik kan betyda att den smala spalten inte är ören.
            return None
    dividers = [rule(round(a+(b-a)*0.62), round(a+(b-a)*0.87)) for a, b in zip(edges, edges[1:])]
    if any(divider is None for divider in dividers):
        return None
    label_rules = list(_bands(x for x in range(max(0, edges[0]-440), edges[0]-round(width))
                              if strength[x] >= len(body) * 0.46))
    if not label_rules or label_rules[-1][1] - label_rules[-1][0] > 7:
        return None
    label_left = label_rules[-1][1]
    columns = sorted(edges + dividers)
    slices = [(a+3, b-3) for a, b in zip(columns, columns[1:])]
    minimum = (columns[-1] - columns[0]) * 0.065

    def ink_rows(first, last):
        return list(_bands(y for y in range(first, last)
                           if sum(sum(ink[y*w+a:y*w+b]) for a, b in slices) >= minimum))

    bands = ink_rows(header_end + 2, total_start - 1)
    totals = ink_rows(horizontal[1][1] + 2, h - 3)
    if not 2 <= len(bands) <= 15 or len(totals) != 1:
        return None
    bands.extend(totals)
    if any(not 4 <= b-a <= 24 for a, b in bands):
        return None
    rows = []
    previous = header_end
    for i, (a, b) in enumerate(bands):
        if i == len(bands) - 1:
            previous = horizontal[1][1]
        labels = ((label_left + pix.x + 2)/scale, (previous + pix.y + 2)/scale,
                  (columns[0] + pix.x - 2)/scale, (b + pix.y + 2)/scale)
        y0, y1 = (a + pix.y - 2)/scale, (b + pix.y + 2)/scale
        numeric = [((x0 + pix.x + 2)/scale, y0, (x1 + pix.x - 2)/scale, y1)
                   for x0, x1 in zip(columns, columns[1:])]
        rows.append([labels, *numeric])
        previous = b + 2
    bbox = ((label_left + pix.x - 3)/scale, heading_top - 1,
            (columns[-1] + pix.x + 3)/scale, max(rows[-1][-1][3], total_bottom))
    return rows, bbox


def _recognize_cells(page, fitz, rows, read_ocr):
    """Packa två bildvarianter av varje cell i en begränsad OCR-yta."""
    numeric_count = len(rows[0])
    slots, tile_w, tile_h = 6, 244, 82
    w = slots * tile_w
    strip_h = ((len(rows) * numeric_count + slots - 1) // slots) * tile_h
    canvas = bytearray(b'\xff') * (w * strip_h * 2)
    transforms = (bytes(min(255, round(value * 1.38)) for value in range(256)),
                  bytes(0 if value < 145 else 255 for value in range(256)))
    with fitz.open() as markers:
        marker_page = markers.new_page(width=17, height=12)
        marker_page.insert_text((1, 9), 'Tal', fontsize=9)
        marker = marker_page.get_pixmap(matrix=fitz.Matrix(_SCALE, _SCALE), colorspace=fitz.csGRAY)
    marker_data = marker.samples
    boundaries = {}
    for i, row in enumerate(rows):
        for j, box in enumerate(row):
            pix = page.get_pixmap(matrix=fitz.Matrix(_SCALE, _SCALE), clip=fitz.Rect(box), colorspace=fitz.csGRAY)
            index = i * numeric_count + j
            x, y = index % slots * tile_w, index // slots * tile_h
            if pix.width + marker.width > tile_w - 12 or max(pix.height, marker.height) > tile_h - 10:
                return None
            x += (tile_w - pix.width - marker.width) // 2
            y += (tile_h - pix.height) // 2
            boundaries[i, j] = x + marker.width
            for variant, transform in enumerate(transforms):
                # Det konstgjorda ordet hindrar OCR från att slå ihop tal från grannceller.
                for dy in range(marker.height):
                    dest = (variant * strip_h + y + (pix.height-marker.height)//2 + dy)*w+x
                    canvas[dest:dest+marker.width] = marker_data[dy*marker.width:(dy+1)*marker.width]
                data = pix.samples.translate(transform)
                for dy in range(pix.height):
                    dest = (variant * strip_h + y + dy) * w + x + marker.width
                    canvas[dest:dest+pix.width] = data[dy*pix.width:(dy+1)*pix.width]

    readings = [[[[] for _ in row] for row in rows] for _ in transforms]
    with fitz.open() as temporary:
        sheet = temporary.new_page(width=w/_SCALE, height=strip_h*2/_SCALE)
        sheet.insert_image(sheet.rect, pixmap=fitz.Pixmap(fitz.csGRAY, w, strip_h*2, bytes(canvas), False))
        textpage = read_ocr(sheet)
        if textpage is None:
            return None
        words = sheet.get_text('words', textpage=textpage, sort=True)
    for x0, y0, x1, y1, value, *_ in words:
        x, y = (x0+x1)/2*_SCALE, (y0+y1)/2*_SCALE
        variant, y = divmod(int(y), strip_h)
        if not 0 <= variant < 2:
            continue
        column = int(x) // tile_w
        index = y // tile_h * slots + column
        i, j = divmod(index, numeric_count)
        if i >= len(rows):
            continue
        if x0*_SCALE < column*tile_w - 2 or x1*_SCALE > (column+1)*tile_w + 2:
            return None
        boundary = boundaries[i, j]
        if x0*_SCALE < boundary < x1*_SCALE and (fused := re.fullmatch(r'[‘\x27]?Tal([0-9]+)', value)):
            readings[variant][i][j].append(fused.group(1))
        elif x > boundary:
            readings[variant][i][j].append(value)
    return [[[' '.join(cell) for cell in row] for row in variant] for variant in readings]


def _amount(cell, *, ore):
    if ore and cell in _DASHES:
        return '—'
    if not (_ORE if ore else _KRONOR).fullmatch(cell):
        return None
    return int(re.sub(r'[ ,.]', '', cell))


def _verified_rows(readings, years, labels):
    first, second = readings
    if len(first) != len(second) or len(first) != len(labels) or not labels:
        return None
    rows, cents = [], []
    for label, a, b in zip(labels, first, second):
        if len(a) != 2*len(years) or len(b) != len(a):
            return None
        values = [_amount(cell, ore=j % 2 == 1) for j, cell in enumerate(a)]
        if None in values or values != [_amount(cell, ore=j % 2 == 1) for j, cell in enumerate(b)]:
            return None
        cents.append([kr * 100 + (ore if isinstance(ore, int) else 0)
                      for kr, ore in zip(values[::2], values[1::2])])
        rows.append([label, *(('—' if value == '—' else f'{value:02d}' if j % 2 else
                              f'{value:,}'.replace(',', ' ')) for j, value in enumerate(values))])
    if (not re.match(r'^(?:summa|total)\b', rows[-1][0], re.I)
            or any(sum(values[:-1]) != values[-1] for values in zip(*cents))):
        return None
    header = ['Post', *(f'{year} ({unit})' for year in years for unit in ('kr', 'öre'))]
    return markdown_table(rows, header)


def read_dense_currency_table(page, fitz, words, read_ocr):
    """Strukturera bara en tabell vars bildceller och tryckta summor ger entydigt stöd."""
    header = _header_and_total(words)
    if header is None or (grid := _grid(page, fitz, header, words)) is None:
        return None
    rows, bbox = grid
    labels = []
    total_words = header[-1]
    for (left, top, right, bottom), *_ in rows[:-1]:
        words_in_label = [word for word in words if word not in total_words
                          and left-2 <= word[0] < word[2] <= right+2
                          and top <= (word[1]+word[3])/2 <= bottom]
        label = ' '.join(word[4] for line in _word_rows(words_in_label) for word in line)
        label = re.sub(r'(?:\s|[.·…])+$', '', label).strip('| ')
        if len(label) < 3 or not any(char.isalpha() for char in label):
            return None
        labels.append(label)
    # Summans OCR-ruta kan vara hög och täcka den sista utgiftsposten.
    labels.append(' '.join(word[4].strip('| ') for word in total_words))
    if len(set(labels)) != len(labels):
        return None
    readings = _recognize_cells(page, fitz, [row[1:] for row in rows], read_ocr)
    if readings is not None and (text := _verified_rows(readings, header[0], labels)) is not None:
        return bbox, text
    return None
