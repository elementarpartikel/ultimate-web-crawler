"""Verifierade avskrifter för entydigt identifierade PDF-original."""

from pathlib import Path

from crawler_text import file_sha256, markdown_table


# Stockholmskällan, Stockholm som kommun, fysiska sidor 2–3.
STOCKHOLM_POPULATION_SIZE = 4_499_550
STOCKHOLM_POPULATION_SHA256 = '4c470136d243c8bbbf8b12d057609b406dd17362891630ed215564bf0f7a3441'
STOCKHOLM_POPULATION_BBOX = (40, 60, 386, 390)
STOCKHOLM_HISTORICAL_POPULATION_BBOX = (40, 390, 394, 486)
STOCKHOLM_ECONOMY_BBOX = (26, 39, 365, 201)
STOCKHOLM_CUSTOMS_BBOX = (23, 239, 371, 529)

# Stockholmskällan, Brännkyrka sockens införlivning, TR0273003_04, fysisk sida 2.
BRANNKYRKA_SIZE = 6_368_368
BRANNKYRKA_SHA256 = '373a080fbd158d9c5625cc8cccd735c77aff048cc9e1385ede38415dac59be3b'
BRANNKYRKA_WELFARE_BBOX = (121, 178, 523, 332)
BRANNKYRKA_WELFARE_YEARS = (1901, 1902, 1903, 1904, 1905)

# Kronor och öre i separata tryckta celler. None betyder ett tryckt streck i öreskolumnen.
BRANNKYRKA_WELFARE_ROWS = (
    ('Hus och inventarier', ((1079, 70), (1683, 21), (1975, 96), (478, 97), (374, 48))),
    ('Understödstagares beklädnad, vård och underhåll',
     ((19299, 54), (21868, 72), (23381, 33), (23613, 94), (27081, 75))),
    ('Förbrukningspersedlar', ((294, 98), (1746, 44), (547, None), (1318, 88), (653, 65))),
    ('Aflöningar', ((460, None), (520, None), (620, None), (666, None), (690, None))),
    ('Ersättning till andra kommuner', ((3089, 72), (4176, 85), (3330, 40), (3621, 97), (2562, 15))),
    ('Öfriga behof', ((1410, 19), (2534, 52), (2717, 50), (3191, 23), (3464, 27))),
    ('Summa kronor', ((25634, 13), (32529, 74), (32572, 19), (32890, 99), (34826, 30))),
)

# Församling och dess tryckta summa. Varje rote har namn, nummer, mankön, kvinnkön och tryckt summa.
STOCKHOLM_POPULATION_PARISHES = (
    ('Nikolai', 10863, (
        ('Storkyrkoroten', 1, 5177, 5686, 10863),
    )),
    ('Klara', 12257, (
        ('Klara kyrkorote', 2, 2107, 3369, 5476),
        ('Hötorgsroten', 3, 2825, 3956, 6781),
    )),
    ('Kungsholms', 46709, (
        ('Kungsbroroten', 4, 5038, 6683, 11721),
        ('Kronobergsroten', 17, 6861, 7471, 14332),
        ('Karlsviksroten', 23, 5986, 6589, 12575),
        ('Kristinebergsrot.', 29, 3858, 4223, 8081),
    )),
    ('Adolf Fredriks', 18065, (
        ('Tegnérsroten', 5, 3295, 4744, 8039),
        ('Teknologroten', 18, 4324, 5702, 10026),
    )),
    ('Gustaf Vasa', 22412, (
        ('Observatorieroten', 6, 5171, 6659, 11830),
        ('Vasaroten', 22, 4262, 6320, 10582),
    )),
    ('Matteus’', 39554, (
        ('Norrtullsroten', 26, 6963, 8669, 15632),
        ('Karlbergsroten', 27, 6080, 6837, 12917),
        ('Birkaroten', 30, 5140, 5865, 11005),
    )),
    ('Jakobs', 10257, (
        ('Jakobsroten', 7, 4513, 5744, 10257),
    )),
    ('Johannes’', 24917, (
        ('Roslagsroten', 8, 3551, 4983, 8534),
        ('Vanadisroten', 21, 8053, 8330, 16383),
    )),
    ('Hedvig Eleonora', 22794, (
        ('Nybroroten', 10, 4474, 8677, 13151),
        ('Artilleriroten', 11, 3555, 6088, 9643),
    )),
    ('Engelbrekts', 22969, (
        ('Humlegårdsroten', 9, 3310, 6779, 10089),
        ('Eriksbergsroten', 28, 5816, 7064, 12880),
    )),
    ('Oscars', 23407, (
        ('Narvaroten', 20, 5609, 6033, 11642),
        ('Karlaroten', 24, 4552, 7213, 11765),
    )),
    ('Katarina', 58125, (
        ('Danviksroten', 12, 6143, 6747, 12890),
        ('Stadsgårdsroten', 13, 5052, 6205, 11257),
        ('Skanstullsroten', 14, 9312, 9907, 19219),
        ('Barnängsroten', 25, 7065, 7694, 14759),
    )),
    ('Maria', 51562, (
        ('Maria kyrkorote', 15, 4903, 6337, 11240),
        ('Tantoroten', 16, 5347, 6196, 11543),
        ('Skinnarviksroten', 19, 5024, 5875, 10899),
        ('Heleneborgsroten', 31, 8677, 9203, 17880),
    )),
    ('Brännkyrka', 28536, (
        ('Enskederoten', 32, 4635, 4888, 9523),
        ('Liljeholmsroten', 33, 4964, 5077, 10041),
        ('Årstaroten', 34, 4386, 4586, 8972),
    )),
    ('Bromma', 6212, (
        ('Brommaroten', 35, 3018, 3194, 6212),
    )),
)
STOCKHOLM_POPULATION_TOTAL = (179046, 219593, 398639)

# Läs varje tryckt kolumnpar uppifrån och ned. Endast 1663 har förbehållet ”c:a”.
STOCKHOLM_HISTORICAL_POPULATION = (
    (1663, 40000), (1763, 72989), (1800, 75517), (1820, 75569),
    (1830, 81512), (1840, 83629), (1850, 91544), (1860, 109878),
    (1865, 126180), (1870, 133597), (1875, 144974), (1880, 168019),
    (1885, 211139), (1890, 245317), (1895, 267100), (1898, 291580),
    (1899, 297148), (1900, 301050), (1901, 301695), (1902, 305115),
    (1903, 309496), (1904, 318398), (1905, 324488), (1906, 332738),
    (1907, 337460), (1908, 339582), (1909, 341816), (1910, 343832),
    (1911, 346599), (1912, 376171), (1913, 382085), (1914, 386270),
)


# Jämförelsepunkt, enhet, Stockholm, tryckt procenttal, övriga städer, landsbygd och hela riket.
STOCKHOLM_ECONOMY = (
    ('Folkmängd nyåret 1916', 'personer', 398639, '7,0', 1187233, 4126868, 5712740),
    ('Bevillningstaxerade fastigheter', 'kr', 1562059500, '18,1', 2187988910, 4837473100, 8587521510),
    ('Bevillningsfria fastigheter', 'kr', 392651500, '28,5', 389366360, 595332950, 1377350810),
    ('Bevillningstaxerad inkomst', 'kr', 374725680, '21,0', 685244461, 723216661, 1783186802),
    ('Bevillningstaxerad inkomst av kapital', 'kr', 32366526, '31,9', 29347576, 39645780, 101359882),
    ('Bevillningstaxerad inkomst av tjänst eller pension', 'kr',
     206266115, '18,8', 414809500, 473901989, 1094977604),
    ('Bevillningstaxerad inkomst av rörelse, yrke eller eljest', 'kr',
     136093039, '23,2', 241087385, 209668892, 586849316),
    ('Beskattningsbar inkomst', 'kr', 327394880, '24,0', 565519162, 469937786, 1362851828),
    ('Fastighetsbevillning', 'kr', 78125, '16,9', 109993, 274703, 462821),
    ('Inkomstbevillning', 'kr', 327652, '24,0', 565262, 470689, 1363603),
    ('Inkomst- och förmögenhetsskatt', 'kr', 14019505, '32,7', 16923490, 11992243, 42935238),
    ('Taxerad inkomst och förmögenhet', 'kr', 480453700, '24,9', 716612720, 734891604, 1931958024),
    ('Bevillning för särskilda förmåner', 'kr', 100854, '31,8', 209755, 6833, 317442),
    ('Krigskonjunkturskatt', 'kr', 1991288, '30,7', 3503279, 984757, 6479324),
)

# Debiterad tulluppbörd i kronor. År, Stockholm, Göteborg, Malmö, Norrköping och hela riket.
STOCKHOLM_CUSTOMS = (
    (1855, 3876457, 4432735, 571082, 586022, 12004543),
    (1865, 4640336, 5229582, 969081, 533381, 13999556),
    (1875, 9485627, 7129180, 2181654, 567204, 24391997),
    (1880, 10514681, 7365224, 2335685, 839463, 27622164),
    (1881, 10929042, 7999425, 2557791, 1044255, 29849639),
    (1882, 11184063, 7839857, 2733489, 1166863, 31108483),
    (1883, 11519558, 8804427, 3067905, 1123627, 33509452),
    (1884, 11929401, 8551250, 2770639, 1274331, 33837744),
    (1885, 11939036, 8911317, 2647646, 1316949, 33123190),
    (1886, 10890802, 8252332, 2688846, 1355980, 32233528),
    (1887, 10273091, 7795324, 2479332, 1253462, 30704393),
    (1888, 12694733, 8690665, 3015355, 1732876, 37722664),
    (1889, 13901773, 9538323, 3823850, 1854602, 42475952),
    (1890, 13806336, 9017730, 4050798, 1807178, 42675181),
    (1891, 12377559, 8287644, 3678487, 1897746, 38012541),
    (1892, 12808052, 7846825, 4102382, 1727619, 37297733),
    (1893, 12843500, 8074559, 4524101, 1356951, 37111961),
    (1894, 13644544, 8373126, 4252928, 1694119, 38631960),
    (1895, 13992030, 9238384, 3909915, 1680826, 39466186),
    (1896, 15415568, 8995801, 4436339, 1522543, 42306765),
    (1897, 16536062, 9537532, 4369558, 1383625, 43752944),
    (1898, 18684323, 11425670, 5053026, 2022303, 52208327),
    (1899, 21374433, 13143620, 5840098, 1963857, 60400413),
    (1900, 21339008, 11242306, 6433144, 2029433, 57730061),
    (1901, 17781799, 10234766, 5341966, 1529633, 49684861),
    (1902, 20232664, 10528489, 6729839, 2345783, 55859717),
    (1903, 20737412, 10490169, 6657780, 2169770, 57036606),
    (1904, 21076007, 10771470, 6693021, 2252352, 60178775),
    (1905, 20327807, 11215700, 7139356, 2000315, 60368150),
    (1906, 21301837, 11125407, 6747255, 1938580, 60396022),
    (1907, 21960434, 11928367, 7070918, 1928864, 60689768),
    (1908, 19915945, 10720550, 6700026, 2031624, 58014057),
    (1909, 20583662, 10712542, 6247862, 2106569, 59239298),
    (1910, 21327722, 11535892, 6860584, 2218318, 61659655),
    (1911, 21295177, 11677455, 6755009, 2548736, 61781434),
    (1912, 22915070, 12177801, 6824339, 2995596, 65969496),
    (1913, 24721701, 13172978, 7685847, 3054757, 71364648),
    (1914, 21184254, 11576667, 6697523, 2351705, 59941376),
    (1915, 20387676, 13298149, 5831054, 1441367, 56157241),
)


def _number(value):
    return f'{value:,}'.replace(',', ' ')


def _stockholm_verified_tables() -> dict[int, tuple]:
    districts = []
    parishes = []
    for parish, subtotal, rows in STOCKHOLM_POPULATION_PARISHES:
        districts.extend((parish, name, code, _number(male), _number(female), _number(total))
                         for name, code, male, female, total in rows)
        parishes.append((parish, _number(subtotal)))
    male, female, total = map(_number, STOCKHOLM_POPULATION_TOTAL)
    districts.append(('Summa för Stockholm', '–', '–', male, female, total))
    parishes.append(('Summa för Stockholm', total))
    population = '\n\n'.join((
        '### Befolkning i Stockholm vid 1915 års slut per rote (verifierad avskrift)',
        markdown_table(districts, ('Församling', 'Rote', 'Rotenummer', 'Mankön', 'Kvinnkön',
                                   'Antal personer i roten vid 1915 års slut')),
        '### Befolkning i Stockholm vid 1915 års slut per församling (verifierad avskrift)',
        markdown_table(parishes, ('Församling', 'Antal personer i församlingen vid 1915 års slut')),
    ))
    historical_rows = [(year, f'cirka {_number(count)}' if year == 1663 else _number(count))
                       for year, count in STOCKHOLM_HISTORICAL_POPULATION]
    historical = '\n\n'.join((
        '### Folkmängd i Stockholm vid årets slut 1663–1914 (verifierad avskrift)',
        markdown_table(historical_rows, ('År', 'Antal personer i Stockholm vid årets slut')),
    ))
    economy_rows = [(label, unit, _number(city), percent, _number(other), _number(rural), _number(total))
                    for label, unit, city, percent, other, rural, total in STOCKHOLM_ECONOMY]
    economy = '\n\n'.join((
        '### Stockholms ekonomiska betydelse för riket (verifierad avskrift)',
        markdown_table(economy_rows, ('Jämförelsepunkt', 'Enhet', 'Stockholm', 'Stockholms andel av riket (%)',
                                     'Övriga städer', 'Landsbygd', 'Hela riket')),
        'Procenttalen anger Stockholms andel av hela riket och återges som i originalet.',
    ))
    customs_rows = [(year, *map(_number, (stockholm, gothenburg, malmo, norrkoping)),
                     _number(total) + ('*' if year == 1914 else ''))
                    for year, stockholm, gothenburg, malmo, norrkoping, total in STOCKHOLM_CUSTOMS]
    customs = '\n\n'.join((
        '### Tulluppbörden (den debiterade) 1855–1915 (verifierad avskrift)',
        markdown_table(customs_rows, ('År', 'Stockholm (kr)', 'Göteborg (kr)', 'Malmö (kr)',
                                      'Norrköping (kr)', 'Hela riket (kr)')),
        'Anmärkning för 1914 (*). Lastpenningar ingick i tullmedlen till och med 1913. '
        'Från 1914 redovisades de separat och uppgick det året till 761 392 kr.',
    ))
    return {
        2: ((STOCKHOLM_POPULATION_BBOX, population), (STOCKHOLM_HISTORICAL_POPULATION_BBOX, historical)),
        3: ((STOCKHOLM_ECONOMY_BBOX, economy), (STOCKHOLM_CUSTOMS_BBOX, customs)),
    }


def _brannkyrka_verified_tables() -> dict[int, tuple]:
    header = ['Utgiftspost', *(f'{year} ({unit})' for year in BRANNKYRKA_WELFARE_YEARS
                              for unit in ('kr', 'öre'))]
    rows = []
    for label, amounts in BRANNKYRKA_WELFARE_ROWS:
        cells = []
        for kronor, ore in amounts:
            cells.extend((_number(kronor), '—' if ore is None else f'{ore:02d}'))
        rows.append((label, *cells))
    welfare = '\n\n'.join((
        '### Brännkyrkas fattigvårdsutgifter 1901–1905 (verifierad avskrift)',
        markdown_table(rows, header),
    ))
    return {2: ((BRANNKYRKA_WELFARE_BBOX, welfare),)}


def verified_pdf_tables(filepath: str) -> dict[int, tuple]:
    """Returnera verifierade avskrifter med deras bildområden per fysisk PDF-sida."""
    size = Path(filepath).stat().st_size
    sources = (
        (STOCKHOLM_POPULATION_SIZE, STOCKHOLM_POPULATION_SHA256, _stockholm_verified_tables),
        (BRANNKYRKA_SIZE, BRANNKYRKA_SHA256, _brannkyrka_verified_tables),
    )
    candidates = [(digest, tables) for length, digest, tables in sources if length == size]
    if candidates:
        digest = file_sha256(filepath)
        for expected, tables in candidates:
            if digest == expected:
                return tables()
    return {}
