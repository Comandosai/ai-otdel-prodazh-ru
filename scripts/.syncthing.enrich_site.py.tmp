# -*- coding: utf-8 -*-
"""
enrich_site.py: от записи реестра к сайту и контактам компании.

Что делает:
  find_site(name, inn)        подбирает домен компании
  fetch_html(url)             читает страницу ЦЕЛИКОМ
  extract_contacts(html, url) достаёт почту, телефон, ИНН, ОГРН
  enrich_company(...)         обходит страницы в заданном порядке и сводит результат

Порядок обхода страниц (важен, проверен на живых сайтах):
  /contacts/, /kontakty/, футер главной, /policy/, /oferta/, /rekvizity/

Почему такой порядок. Живая проверка 13.08.2026:
  chelny-hleb.ru/          почты НЕТ ни одной
  chelny-hleb.ru/contacts/ sales@, job@, esz@
Почта живёт на странице контактов. Страница политики нужна ради ИНН,
чтобы связать сайт с записью реестра, и срабатывает она заметно реже.

Регулярки взяты из разведки SIGNALY.md 4.1 и перепроверены живым запросом.

Python 3.9, только стандартная библиотека.
"""

import os
import re
import ssl
import sys
import time
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TIMEOUT = 25
POLITE_DELAY = 1.0          # секунда между запросами к одному хосту
MAX_BYTES = 20 * 1024 * 1024  # предохранитель, обычная страница в 30 раз меньше

# Порядок страниц ровно как в конвейере. Ранг это приоритет значения при слиянии.
PAGE_ORDER = [
    ("contacts", "/contacts/"),
    ("kontakty", "/kontakty/"),
    ("main", "/"),
    ("policy", "/policy/"),
    ("oferta", "/oferta/"),
    ("rekvizity", "/rekvizity/"),
]

# ----------------------------------------------------------------------------
# Регулярки. SIGNALY.md 4.1, столбец «Сайтов с попаданием» из корпуса 7 сайтов.
# В markdown-таблице вертикальная черта экранирована как \| , здесь она обычная.
# ----------------------------------------------------------------------------

RX_EMAIL = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?:ru|com|рф|net|org|su)\b')            # 4/7
RX_PHONE = re.compile(
    r'(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}')           # 6/7
# Дополнение к проверенной регулярке, в разведке его не было.
# Проверенная выше рассчитана на трёхзначный код: +7 (999) 123-45-67.
# В регионах код города четырёхзначный: Набережные Челны 8552, Нижнекамск 8555,
# и такой номер она пропускает. Эта версия берёт любые 10 цифр после +7 или 8.
# Хвостовой (?!\d) не даёт склеить номер из середины длинного числа, например из ИНН.
RX_PHONE_LOOSE = re.compile(r'(?:\+7|\b8)[\s\-()]*(?:\d[\s\-()]*){10}(?!\d)')
# В разведке порядок альтернатив был (\d{10}|\d{12}) и (\d{13}|\d{15}).
# На 12-значном ИНН предпринимателя короткая альтернатива срабатывает первой
# и обрезает номер до 10 знаков: 860800724395 превращается в 8608007243.
# Поэтому длинная альтернатива стоит первой, а (?!\d) не даёт взять кусок числа.
RX_INN = re.compile(r'ИНН\D{0,10}(\d{12}|\d{10})(?!\d)')                        # 1/7
RX_OGRN = re.compile(r'ОГРН\D{0,10}(\d{15}|\d{13})(?!\d)')                      # 1/7
RX_POLICY_LINK = re.compile(
    r'href=["\']([^"\']*(?:policy|politic|politika|privacy|personal|konfiden'
    r'|confiden|oferta|rekvizit)[^"\']*)["\']', re.I)                           # 5/7
RX_CONTACT_LINK = re.compile(
    r'href=["\']([^"\']*(?:contact|kontakt|svyaz|about|o-kompanii)[^"\']*)["\']', re.I)
RX_META_CHARSET = re.compile(rb'charset\s*=\s*["\']?([\w\-]+)', re.I)
RX_FOOTER = re.compile(r'<footer[^>]*>', re.I)

# Почтовые адреса, которые ловятся регуляркой, но к компании отношения не имеют.
NOISE_MAIL_DOMAINS = (
    "example.com", "example.ru", "sentry.io", "wixpress.com", "domain.ru",
    "mysite.ru", "site.ru", "test.ru", "email.com", "your-mail.ru",
)
# Ящики для писем о работе, для холодного письма в отдел закупок они бесполезны.
MAIL_LOW_PRIORITY = ("job", "hr", "vacancy", "rabota", "resume", "cv", "career",
                     "noreply", "no-reply", "webmaster", "abuse", "postmaster",
                     "spam", "robot")
# Ящики, которые читает человек, принимающий коммерческие предложения.
MAIL_HIGH_PRIORITY = ("info", "sales", "sale", "zakaz", "zakupki", "opt", "sbyt",
                      "office", "mail", "market", "kp", "manager", "secretar",
                      "priemnaya", "post")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Организационно-правовые формы, они не участвуют в подборе домена.
OPF_WORDS = (
    "общество", "с", "ограниченной", "ответственностью", "акционерное",
    "публичное", "непубличное", "закрытое", "открытое", "производственный",
    "кооператив", "товарищество", "унитарное", "предприятие", "ооо", "оао",
    "зао", "пао", "ао", "нао", "ип", "тд", "торговый", "дом", "компания",
    "фирма", "групп", "group", "холдинг",
)

_LAST_HIT = {}


def translit(text):
    """Кириллица в латиницу для подбора домена по названию."""
    out = []
    for ch in (text or "").lower():
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def name_core(name):
    """Смысловое ядро названия без кавычек и без ОПФ.

    'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЧЕЛНЫ-ХЛЕБ"' -> 'ЧЕЛНЫ-ХЛЕБ'
    """
    if not name:
        return ""
    quoted = re.findall(r'[«"\']([^«»"\']{2,})[»"\']', name)
    core = quoted[0] if quoted else name
    words = [w for w in re.split(r'[^\wа-яА-ЯёЁ-]+', core, flags=re.UNICODE) if w]
    kept = [w for w in words if w.lower() not in OPF_WORDS]
    return " ".join(kept if kept else words)


# ----------------------------------------------------------------------------
# Сеть
# ----------------------------------------------------------------------------

def _throttle(host):
    prev = _LAST_HIT.get(host)
    if prev is not None:
        wait = POLITE_DELAY - (time.time() - prev)
        if wait > 0:
            time.sleep(wait)
    _LAST_HIT[host] = time.time()


def http_get(url, timeout=TIMEOUT, allow_insecure=True):
    """Один GET с честными замерами и живучестью к битым сертификатам.

    Возвращает словарь. Тело читается ЦЕЛИКОМ, без обрезки.
    ssl_ok=False означает, что сертификат не прошёл проверку, но страницу
    мы всё равно забрали. Это частый случай у сайтов малого бизнеса:
    у kzsk.ru сертификат не совпадает с доменом, проверено 13.08.2026.
    """
    res = {"url": url, "final_url": None, "status": None, "body": b"",
           "content_type": "", "ttfb": None, "total": None, "ssl_ok": None,
           "error": None, "insecure_retry": False}
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname:
        _throttle(parsed.hostname)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        "Connection": "close",
    })

    def _open(ctx):
        t0 = time.time()
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        ttfb = time.time() - t0
        body = resp.read(MAX_BYTES)
        return resp, ttfb, body, time.time() - t0

    try:
        resp, ttfb, body, total = _open(None)
        res["ssl_ok"] = url.lower().startswith("https")
    except urllib.error.HTTPError as e:
        res["status"] = e.code
        res["error"] = "HTTP %s" % e.code
        try:
            res["body"] = e.read(MAX_BYTES)
            res["content_type"] = e.headers.get("Content-Type", "")
        except Exception:
            pass
        return res
    except urllib.error.URLError as e:
        is_ssl = isinstance(getattr(e, "reason", None), ssl.SSLError)
        if is_ssl and allow_insecure:
            try:
                ctx = ssl._create_unverified_context()
                resp, ttfb, body, total = _open(ctx)
                res["ssl_ok"] = False
                res["insecure_retry"] = True
            except Exception as e2:
                res["error"] = "%s: %s" % (type(e2).__name__, e2)
                res["ssl_ok"] = False
                return res
        else:
            res["error"] = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
            if is_ssl:
                res["ssl_ok"] = False
            return res
    except (socket.timeout, ssl.SSLError, ConnectionError) as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
        return res
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
        return res

    res["status"] = getattr(resp, "status", None) or resp.getcode()
    res["final_url"] = resp.geturl()
    res["content_type"] = resp.headers.get("Content-Type", "") or ""
    res["body"] = body
    res["ttfb"] = round(ttfb, 3)
    res["total"] = round(total, 3)
    return res


def decode_html(body, content_type=""):
    """Раскодировать тело страницы. Заголовок, потом meta, потом cp1251.

    Сайты на Битриксе часто отдают windows-1251, без этого текст превращается
    в мусор и регулярки по русским словам ИНН и ОГРН перестают срабатывать.
    """
    if not body:
        return ""
    enc = None
    m = re.search(r'charset\s*=\s*["\']?([\w\-]+)', content_type or "", re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m2 = RX_META_CHARSET.search(body[:8192])
        if m2:
            try:
                enc = m2.group(1).decode("ascii", "ignore")
            except Exception:
                enc = None
    for candidate in [enc, "utf-8", "windows-1251"]:
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def fetch_html(url):
    """Прочитать страницу ЦЕЛИКОМ и вернуть текст.

    Обрезать нельзя. В разведке первый прогон читал 500 КБ и терял футеры:
    у segezha-group.com страница 1 324 133 байта, у kzsk.ru 769 115.
    Копирайт, ИНН и почта лежат именно в футере, обрезка даёт ложный вывод
    «у компании нет реквизитов».
    """
    page = fetch_page(url)
    return page.get("html", "")


def fetch_page(url):
    """То же, что fetch_html, но с метаданными запроса для диагностики."""
    res = http_get(url)
    res["html"] = decode_html(res.get("body") or b"", res.get("content_type", ""))
    res["bytes"] = len(res.get("body") or b"")
    res.pop("body", None)
    return res


# ----------------------------------------------------------------------------
# Разбор страницы
# ----------------------------------------------------------------------------

def footer_slice(html):
    """Кусок страницы, который считаем футером."""
    if not html:
        return ""
    starts = [m.start() for m in RX_FOOTER.finditer(html)]
    if starts:
        return html[starts[-1]:]
    return html[int(len(html) * 0.7):]


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def find_phones(html):
    """Телефоны с защитой от ложных срабатываний внутри длинных чисел.

    Проверенная регулярка не смотрит, чем окружено совпадение, и на строке
    'ИНН 860800724395' выдаёт номер 8 608 007 24 39. Поэтому совпадение
    отбрасывается, если сразу перед ним или сразу после него стоит цифра.
    """
    found = []
    for rx in (RX_PHONE, RX_PHONE_LOOSE):
        for m in rx.finditer(html or ""):
            start, end = m.span()
            before = html[start - 1] if start > 0 else ""
            after = html[end] if end < len(html) else ""
            if before.isdigit() or after.isdigit():
                continue
            phone = normalize_phone(m.group(0))
            if phone:
                found.append((start, phone))
    out = []
    for _, phone in sorted(found, key=lambda x: x[0]):
        if phone not in out:
            out.append(phone)
    return out


def clean_email(raw):
    mail = (raw or "").strip().strip(".,;:()<>\"'").lower()
    if mail.startswith("mailto:"):
        mail = mail[7:]
    if "@" not in mail:
        return None
    local, _, dom = mail.partition("@")
    if not local or not dom:
        return None
    if dom in NOISE_MAIL_DOMAINS:
        return None
    if any(bad in mail for bad in ("@2x", ".png", ".jpg", ".svg", ".webp")):
        return None
    return mail


def rank_email(mail):
    """Чем меньше, тем интереснее ящик для холодного письма.

    Мусорные ящики проверяются ПЕРВЫМИ, и это принципиально: postmaster
    начинается на post, а post стоит среди приоритетных. Если сначала пройти
    по приоритетным, холодное письмо уедет на технический адрес.
    """
    local = mail.split("@", 1)[0].strip().lower()
    for key in MAIL_LOW_PRIORITY:
        if local == key or local.startswith(key):
            return 500
    for i, key in enumerate(MAIL_HIGH_PRIORITY):
        if local == key or local.startswith(key):
            return i
    return 100


def has_mx(domain, timeout=5):
    """Есть ли у домена почты MX-запись.

    Лёгкая проверка через системный nslookup, без единой pip-зависимости.
    Домен без MX-записи почти наверняка не примет письмо: либо адрес
    склеился неверно при разборе страницы, либо домен вообще не настроен
    принимать почту. Это не проверка конкретного ящика, а проверка того,
    что почта на этом домене в принципе может существовать.

    Возвращает True/False. Если nslookup в системе не найден (урезанный
    контейнер, чужая ОС), возвращает None: это значит «не проверяли»,
    а не «плохая почта», вызывающий код не должен путать эти два случая.
    """
    if not domain:
        return None
    if shutil.which("nslookup") is None:
        return None
    try:
        proc = subprocess.run(["nslookup", "-type=MX", domain],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    output = ((proc.stdout or "") + (proc.stderr or "")).lower()
    return "mail exchanger" in output


# SMTP-верификацию (соединение на 25 порт и RCPT TO без реальной отправки)
# сюда сознательно не добавляем. У большинства провайдеров РФ на обычных
# подключениях исходящий 25 порт закрыт, а почтовые сервера банят SMTP-
# хендшейк с динамического IP почти сразу. MX плюс синтаксис адреса это
# реалистичный потолок проверки в этой среде, дальше только ложное чувство
# точности. Будущему разработчику: не пытайтесь, это тупик.


def extract_contacts(html, base_url=""):
    """Почта, телефон, ИНН, ОГРН и ссылки на страницы с реквизитами."""
    out = {"emails": [], "email": None, "phones": [], "phone": None,
           "inn": None, "inns": [], "ogrn": None, "ogrns": [],
           "policy_links": [], "contact_links": []}
    if not html:
        return out

    seen = set()
    for raw in RX_EMAIL.findall(html):
        mail = clean_email(raw)
        if mail and mail not in seen:
            seen.add(mail)
            out["emails"].append(mail)
    if out["emails"]:
        out["emails"].sort(key=rank_email)
        out["email"] = out["emails"][0]

    for phone in find_phones(html):
        if phone not in out["phones"]:
            out["phones"].append(phone)
    if out["phones"]:
        out["phone"] = out["phones"][0]

    out["inns"] = list(dict.fromkeys(RX_INN.findall(html)))
    out["ogrns"] = list(dict.fromkeys(RX_OGRN.findall(html)))
    if out["inns"]:
        out["inn"] = out["inns"][0]
    if out["ogrns"]:
        out["ogrn"] = out["ogrns"][0]

    for rx, key in ((RX_POLICY_LINK, "policy_links"), (RX_CONTACT_LINK, "contact_links")):
        links = []
        for href in rx.findall(html):
            full = absolutize(href, base_url)
            if full and full not in links:
                links.append(full)
        out[key] = links[:8]
    return out


def absolutize(href, base_url):
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    if not base_url:
        return href if href.startswith("http") else None
    try:
        return urllib.parse.urljoin(base_url, href)
    except Exception:
        return None


def host_of(url):
    try:
        h = urllib.parse.urlsplit(url if "//" in url else "http://" + url).hostname or ""
        return h.lower().lstrip(".")
    except Exception:
        return ""


def same_site(url_a, url_b):
    a, b = host_of(url_a), host_of(url_b)
    if not a or not b:
        return False
    return a.replace("www.", "") == b.replace("www.", "")


# ----------------------------------------------------------------------------
# Подбор сайта
# ----------------------------------------------------------------------------

def normalize_site_url(value):
    """Привести значение колонки WWW реестра к рабочему адресу."""
    v = (value or "").strip()
    if not v or v.lower() in ("нет", "-", "отсутствует", "none", "null"):
        return None
    v = v.split()[0].strip().strip(",;")
    if v.startswith("//"):
        v = "https:" + v
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    host = host_of(v)
    if not host or "." not in host:
        return None
    return v


def domain_candidates(name, limit=8):
    """Кандидаты домена из названия компании.

    Приём работает лучше, чем кажется: ЧЕЛНЫ-ХЛЕБ -> chelny-hleb.ru,
    КЗСК -> kzsk.ru, ТАТСПИРТПРОМ -> tatspirtprom.ru. Все три живые.
    Кандидат принимается только после проверки, см. find_site.
    """
    core = name_core(name)
    lat = translit(core)
    words = [w for w in lat.split() if w]
    if not words:
        return []
    joined = "".join(words)
    hyphened = "-".join(words)
    initials = "".join(w[0] for w in words if w)
    bases = []
    for b in (joined, hyphened, initials if len(initials) >= 3 else None):
        if b and 2 < len(b) <= 30 and b not in bases:
            bases.append(b)
    out = []
    for b in bases:
        for tld in (".ru", ".com"):
            out.append("https://" + b + tld)
    return out[:limit]


# Слова, которые есть в названии половины производств страны. Они НЕ опознают
# компанию. Живой пример из прогона: для «Нижнекамского молочного комбината»
# домен nmk.ru дал два попадания, «молочный» и «комбинат», и выглядел своим.
# На деле это чужой молочный комбинат: слова «Нижнекамск» на нём нет вообще.
# Опознавать компанию можно только по отличающей части названия.
GENERIC_NAME_STEMS = (
    "комбин", "завод", "фабрик", "молоч", "мясн", "хлеб", "пищев", "торгов",
    "произв", "строит", "промыш", "группа", "центр", "сервис", "компан",
    "предпр", "холдин", "трейд", "снаб", "объеди", "управл", "стройм",
)


def _is_generic_token(token):
    low = (token or "").lower()
    return any(low.startswith(stem) for stem in GENERIC_NAME_STEMS)


def looks_like_company_page(html, name, inn):
    """Насколько страница похожа на сайт именно этой компании.

      1.0  на странице стоит наш ИНН, сомнений нет
      0.6  совпала отличающая часть названия
      0.45 совпало одно отличающее слово
      0.25 совпали только общие слова вроде «молочный комбинат»
    """
    if not html:
        return 0.0
    found = extract_contacts(html, "")
    if inn and inn in found["inns"]:
        return 1.0

    low = html.lower()
    tokens = [t for t in re.split(r'[^\wа-яА-ЯёЁ]+', name_core(name), flags=re.UNICODE)
              if len(t) >= 4]
    distinctive, generic = 0, 0
    for token in tokens:
        ru = token.lower()
        stem = ru[:6] if len(ru) > 6 else ru
        lat = translit(ru)
        lat_stem = lat[:8] if len(lat) > 8 else lat
        hit = stem in low or (len(lat_stem) >= 4 and lat_stem in low)
        if not hit:
            continue
        if _is_generic_token(ru):
            generic += 1
        else:
            distinctive += 1

    if distinctive >= 2 or (distinctive and generic):
        return 0.6
    if distinctive == 1:
        return 0.45
    if generic:
        return 0.25
    return 0.2


# Порог приёма кандидата по происхождению. Догадка по названию принимается
# строже, чем адрес, который компания сама указала в реестре: трёхбуквенный
# домен вроде nmk.ru почти наверняка принадлежит кому-то другому, и написать
# по нему письмо «про ваш сайт» это позор на всю рассылку.
ACCEPT_THRESHOLD = {"registry_www": 0.45, "guess": 0.6, "search": 1.0}


def find_site(name, inn, www=None, record=None, use_search=False, verbose=False):
    """Подобрать сайт компании. Возвращает URL или None."""
    url, _score, _origin = find_site_detailed(
        name, inn, www=www, record=record, use_search=use_search, verbose=verbose)
    return url


def find_site_detailed(name, inn, www=None, record=None, use_search=False,
                       verbose=False):
    """То же, что find_site, но возвращает (url, уверенность, происхождение).

    Порядок:
      1) колонка WWW из выгрузки реестра МСП (report.xlsx, 17-я колонка)
      2) подбор домена по названию с проверкой ИНН на самой странице
      3) поиск в интернете, по умолчанию выключен, см. ниже

    Про поиск честно. Живая проверка 13.08.2026 из этой среды:
      html.duckduckgo.com  HTTP 202 и страница антибота, ссылок в выдаче нет
      lite.duckduckgo.com  то же самое
      yandex.ru/search     200, но в теле SmartCaptcha
      mojeek.com           403
      search.brave.com     429
    Ни один поисковик скриптом не отдал результатов. Поэтому use_search=False
    по умолчанию, и обещать этот путь как рабочий нельзя.
    """
    record = record or {}
    hint = www or record.get("www") or record.get("WWW") or record.get("site")
    candidates = []

    hint_url = normalize_site_url(hint)
    if hint_url:
        candidates.append(("registry_www", hint_url))
    for guess in domain_candidates(name):
        candidates.append(("guess", guess))
    if use_search:
        for url in _search_candidates(name, inn):
            candidates.append(("search", url))

    best = None
    tried = set()
    for origin, url in candidates:
        host = host_of(url)
        if not host or host in tried:
            continue
        tried.add(host)
        page = fetch_page(url)
        if page.get("error") and not page.get("html"):
            if verbose:
                print("   нет ответа: %s (%s)" % (url, page.get("error")))
            continue
        score = looks_like_company_page(page.get("html", ""), name, inn)
        if origin == "registry_www":
            score = max(score, 0.7)   # компания сама указала адрес в реестре
        if verbose:
            print("   кандидат %s: %s, совпадение %.2f" % (origin, url, score))
        final = page.get("final_url") or url
        if score >= 1.0:
            return final, score, origin
        if best is None or score > best[0]:
            best = (score, final, origin)
    if best and best[0] >= ACCEPT_THRESHOLD.get(best[2], 1.0):
        return best[1], best[0], best[2]
    if verbose and best:
        print("   лучший кандидат %s отвергнут: совпадение %.2f ниже порога %.2f"
              % (best[1], best[0], ACCEPT_THRESHOLD.get(best[2], 1.0)))
    return None, 0.0, None


def _search_candidates(name, inn, limit=5):
    """Резервный путь через поисковик. Из этой среды не подтверждён, см. find_site."""
    out = []
    queries = []
    if inn:
        queries.append("ИНН %s" % inn)
    if name:
        queries.append("%s официальный сайт" % name_core(name))
    for q in queries:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
        page = fetch_page(url)
        html = page.get("html", "")
        for raw in re.findall(r'uddg=([^"&]+)', html):
            link = urllib.parse.unquote(raw)
            host = host_of(link)
            if host and host not in ("duckduckgo.com",) and link not in out:
                out.append("https://" + host + "/")
        if len(out) >= limit:
            break
    return out[:limit]


# ----------------------------------------------------------------------------
# Полное обогащение
# ----------------------------------------------------------------------------

def enrich_company(name, inn, www=None, record=None, site=None, verbose=False,
                   use_search=False):
    """Найти сайт и собрать с него контакты в заданном порядке страниц.

    Возвращает словарь с фактами и источником каждого факта, чтобы
    verify.py мог посчитать доверие, а письмо ссылалось на конкретную страницу.
    """
    result = {"inn": inn, "name": name, "site": None, "pages": [],
              "email": None, "emails": [], "phone": None, "phones": [],
              "inn_on_site": None, "ogrn_on_site": None,
              "site_confidence": 0.0, "site_origin": None,
              "sources": {}, "errors": []}

    if site:
        result["site_origin"] = "задан вручную"
        result["site_confidence"] = 0.7
    else:
        site, score, origin = find_site_detailed(name, inn, www=www, record=record,
                                                 use_search=use_search,
                                                 verbose=verbose)
        result["site_confidence"] = score
        result["site_origin"] = origin
    if not site:
        result["errors"].append("сайт не найден")
        return result
    result["site"] = site

    base = "%s://%s" % (urllib.parse.urlsplit(site).scheme or "https", host_of(site))
    plan = [(rank, key, urllib.parse.urljoin(base + "/", path.lstrip("/")))
            for rank, (key, path) in enumerate(PAGE_ORDER)]

    collected = []
    main_html = ""
    for rank, key, url in plan:
        page = fetch_page(url)
        ok = page.get("status") == 200 and page.get("html")
        if verbose:
            print("   %-9s %s -> %s, %s байт" % (
                key, url, page.get("status"), page.get("bytes")))
        if not ok:
            continue
        html = page["html"]
        if key == "main":
            main_html = html
            # футер главной, тот самый кусок, который теряется при обрезке чтения
            data = extract_contacts(footer_slice(html), page.get("final_url") or url)
            whole = extract_contacts(html, page.get("final_url") or url)
            for field in ("emails", "phones", "inns", "ogrns"):
                for v in whole[field]:
                    if v not in data[field]:
                        data[field].append(v)
            data["email"] = data["emails"][0] if data["emails"] else None
            data["phone"] = data["phones"][0] if data["phones"] else None
            data["inn"] = data["inns"][0] if data["inns"] else None
            data["ogrn"] = data["ogrns"][0] if data["ogrns"] else None
            data["policy_links"] = whole["policy_links"]
            data["contact_links"] = whole["contact_links"]
        else:
            data = extract_contacts(html, page.get("final_url") or url)
        collected.append((rank, key, page.get("final_url") or url, data))
        result["pages"].append({"key": key, "url": url,
                                "status": page.get("status"),
                                "bytes": page.get("bytes")})

    # Ссылки, найденные на главной, пробуем после списка из плана.
    extra = []
    if main_html:
        found = extract_contacts(main_html, site)
        for link in (found["contact_links"] + found["policy_links"]):
            if same_site(link, site) and link not in [c[2] for c in collected]:
                extra.append(link)
    for i, url in enumerate(extra[:4]):
        page = fetch_page(url)
        if page.get("status") == 200 and page.get("html"):
            data = extract_contacts(page["html"], page.get("final_url") or url)
            collected.append((90 + i, "link", page.get("final_url") or url, data))
            result["pages"].append({"key": "link", "url": url,
                                    "status": page.get("status"),
                                    "bytes": page.get("bytes")})

    collected.sort(key=lambda x: x[0])
    for rank, key, url, data in collected:
        for mail in data["emails"]:
            if mail not in result["emails"]:
                result["emails"].append(mail)
                result["sources"].setdefault("email:" + mail, url)
        for phone in data["phones"]:
            if phone not in result["phones"]:
                result["phones"].append(phone)
                result["sources"].setdefault("phone:" + phone, url)
        if data["inn"] and not result["inn_on_site"]:
            result["inn_on_site"] = data["inn"]
            result["sources"]["inn_on_site"] = url
        if data["ogrn"] and not result["ogrn_on_site"]:
            result["ogrn_on_site"] = data["ogrn"]
            result["sources"]["ogrn_on_site"] = url

    if result["emails"]:
        result["emails"].sort(key=rank_email)
        result["email"] = result["emails"][0]
        result["sources"]["email"] = result["sources"].get("email:" + result["email"])
    if result["phones"]:
        result["phone"] = result["phones"][0]
        result["sources"]["phone"] = result["sources"].get("phone:" + result["phone"])
    result["sources"]["site"] = site
    return result


# ----------------------------------------------------------------------------
# Демо
# ----------------------------------------------------------------------------

SAMPLE_HTML = u"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><form action="/send"><input name="phone"></form>
<script>ym(44398735, "init", {});</script>
<script src="https://mc.yandex.ru/metrika/tag.js"></script>
<footer>&copy; 2020 ОАО «Челны-хлеб». Телефон: +7 (8552) 30-70-00,
почта sales@chelny-hleb.ru, для резюме job@chelny-hleb.ru.
ИНН 1650027925, ОГРН 1021602013762.
<a href="/politika-konfidencialnosti/">Политика</a>
<a href="/contacts/">Контакты</a></footer></body></html>"""


def _demo():
    print("=" * 72)
    print("enrich_site.py: демо")
    print("=" * 72)

    print("\n[1] Проверка регулярок на образце с реальными значениями")
    contacts = extract_contacts(SAMPLE_HTML, "https://chelny-hleb.ru/")
    checks = [
        ("почта", contacts["email"], "sales@chelny-hleb.ru"),
        ("телефон", contacts["phone"], "+78552307000"),
        ("ИНН", contacts["inn"], "1650027925"),
        ("ОГРН", contacts["ogrn"], "1021602013762"),
    ]
    ok_all = True
    for label, got, want in checks:
        ok = (got == want)
        ok_all = ok_all and ok
        print("    %-8s %-22s %s" % (label, got, "верно" if ok else "ждали " + want))
    print("    ящик для резюме отодвинут в конец списка:",
          contacts["emails"][-1] == "job@chelny-hleb.ru")
    print("    ссылка на политику:", contacts["policy_links"][:1])
    print("    итог самопроверки:", "все регулярки живы" if ok_all else "есть расхождение")

    print("\n[2] Подбор домена по названию, без сети")
    for nm in ['ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЧЕЛНЫ-ХЛЕБ"',
               'АКЦИОНЕРНОЕ ОБЩЕСТВО "КАЗАНСКИЙ ЗАВОД СИНТЕТИЧЕСКОГО КАУЧУКА"',
               'ООО "НИЖНЕКАМСКИЙ МОЛОЧНЫЙ КОМБИНАТ"']:
        print("    %-46s -> %s" % (name_core(nm),
                                   [host_of(u) for u in domain_candidates(nm)][:4]))

    if "--offline" in sys.argv:
        print("\n[3] Живой прогон пропущен, задан ключ --offline")
        return

    print("\n[3] Живой прогон: chelny-hleb.ru")
    try:
        data = enrich_company('ОАО "ЧЕЛНЫ-ХЛЕБ"', "1650027925",
                              site="https://chelny-hleb.ru/", verbose=True)
        print("    сайт:      ", data["site"])
        print("    почта:     ", data["email"], "со страницы", data["sources"].get("email"))
        print("    все ящики: ", ", ".join(data["emails"][:5]))
        print("    телефон:   ", data["phone"])
        print("    ИНН с сайта:", data["inn_on_site"], "источник",
              data["sources"].get("inn_on_site"))
        print("    ОГРН с сайта:", data["ogrn_on_site"])
    except Exception as e:
        print("    сеть недоступна, это не ошибка скрипта: %s: %s" % (type(e).__name__, e))


def fetch_director(inn):
    """
    ФИО руководителя из ЕГРЮЛ по ИНН, в виде 'ДОЛЖНОСТЬ: Фамилия Имя Отчество'.

    Именно отсюда в письмо попадает обращение по имени. Если ЕГРЮЛ не ответил,
    обогащение продолжается: письмо просто поздоровается общей фразой.
    """
    if not inn:
        return None
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import fns_egrul
        return fns_egrul.director_line(fns_egrul.get_director(inn))
    except Exception as exc:                            # noqa: BLE001
        print("    %-13s ЕГРЮЛ не ответил: %s"
              % (inn, str(exc).split("\n")[0]))
        return None


def _cli(argv):
    """Пакетное обогащение из базы конвейера.

    python3 scripts/enrich_site.py --db data/leads.db --inn-file /tmp/chunk_01.txt
    """
    import argparse
    import verify

    parser = argparse.ArgumentParser(
        description="Поиск сайта и контактов для компаний из базы")
    parser.add_argument("--db", default=verify.DB_PATH)
    parser.add_argument("--inn-file", help="файл со списком ИНН, по одному в строке")
    parser.add_argument("--inn", help="один ИНН")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--only-missing", action="store_true",
                        help="только те, у кого сайт ещё не найден")
    parser.add_argument("--search", action="store_true",
                        help="разрешить поиск в интернете, из-за антиботов почти "
                             "всегда бесполезно")
    parser.add_argument("--no-director", dest="no_director", action="store_true",
                        help="не спрашивать ФИО руководителя в ЕГРЮЛ, "
                             "тогда письмо будет здороваться общей фразой")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("базы %s нет. Сначала соберите выборку через fns_rmsp.py" % args.db)
        return 1

    con = verify.open_db(args.db)
    for column in ("inn_on_site", "ogrn_on_site", "phone"):
        verify.ensure_column(con, "enrichment", column)
    verify.ensure_company_columns(con)
    inns = []
    if args.inn:
        inns.append(verify.norm_digits(args.inn))
    if args.inn_file:
        inns.extend(verify.read_inn_file(args.inn_file))
    companies = verify.db_companies(con, inns=inns or None, limit=args.limit,
                                    need_site=args.only_missing)
    if not companies:
        print("нечего обогащать: подходящих записей в базе не нашлось")
        con.close()
        return 0

    print("обогащаю %d компаний" % len(companies))
    if not args.no_director:
        print("ФИО руководителя беру в ЕГРЮЛ, это примерно 2 секунды на компанию")
    found, with_fio = 0, 0
    for row in companies:
        record = verify.canonical_record(row)
        data = enrich_company(record.get("name"), record.get("inn"),
                              www=record.get("www"), record=record,
                              use_search=args.search)
        director = record.get("director_fio")
        if not director and not args.no_director:
            director = fetch_director(record.get("inn"))
        if director:
            with_fio += 1
        inn_match = verify.verify_inn_match(data.get("inn_on_site"), record)
        if inn_match >= 1.0:
            source = "сайт компании, ИНН на сайте совпал с реестром"
            confidence = 1.0
        elif data.get("site"):
            source = "сайт компании, опознан по названию домена"
            confidence = min(0.5, data.get("site_confidence") or 0.5)
        else:
            source = "сайт не найден"
            confidence = 0.0
        if not data.get("site"):
            print("    %-13s сайт не найден" % record.get("inn"))
        else:
            found += 1
            print("    %-13s %-34s %s" % (record.get("inn"), data["site"],
                                          data.get("email") or "почты нет"))
        # db_replace, а не db_insert: повторный прогон должен обновлять строку,
        # а не класть рядом вторую с теми же фактами
        verify.db_replace(con, "enrichment", {
            "inn": record.get("inn"), "name": record.get("name"),
            "website": data.get("site"), "email": data.get("email"),
            "director": director, "revenue": record.get("revenue"),
            "source": source,
            "confidence": "%.2f" % confidence,
            "checked_at": verify.now_stamp(),
            "inn_on_site": data.get("inn_on_site"),
            "ogrn_on_site": data.get("ogrn_on_site"),
            "phone": data.get("phone"),
        })
        verify.db_upsert_company(con, {
            "inn": record.get("inn"), "website": data.get("site"),
            "email": data.get("email"), "director": director})
        con.commit()
    con.close()
    print("сайт найден у %d из %d, ФИО руководителя есть у %d"
          % (found, len(companies), with_fio))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    flags = ("--db", "--inn", "--inn-file", "--limit", "--only-missing", "--search",
             "--no-director")
    # справка обязана печататься мгновенно, без единого запроса в сеть
    if "--help" in argv or "-h" in argv:
        return _cli(argv)
    if any(a.split("=")[0] in flags for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
