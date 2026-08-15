# -*- coding: utf-8 -*-
"""
verify.py: контроль достоверности. Самый важный модуль конвейера.

Письмо на фактах работает ровно до первой выдумки. Один неверный факт про
чужую компанию убивает не одно письмо, а всю рассылку и репутацию домена.
Поэтому между обогащением и письмом стоит этот фильтр:

  verify_inn_match(inn_from_site, company_record) -> 0..1
  verify_domain_match(site_url, company_record)   -> True/False
  score_fact(field, source_type)                  -> вес источника
  gate_facts(facts, threshold=0.7)                -> (usable, flagged)
  conflicts_report(company)                       -> расхождения для человека

Веса источников:
  реестр ФНС    1.0   государственный источник, сверять не с чем
  сайт компании 0.7   компания пишет о себе сама, но это её слова
  поиск         0.4   связь «нашли по названию» ничем не подтверждена

Факты ниже порога в письмо НЕ ПОПАДАЮТ. Они уходят в flagged, человек
может посмотреть их глазами, но генератор письма их не видит.

Контрольные суммы ИНН и ОГРН проверены на 15 реальных номерах из выдачи
ЕГРЮЛ, реестра МСП и ГИР БО, включая 12-значный ИНН предпринимателя
и 15-значный ОГРНИП. Все 15 прошли, случайный номер 1234567890 отбит.

Python 3.9, только стандартная библиотека.
"""

import os
import re
import csv
import sys
import json
import sqlite3
import datetime
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "leads.db")

# ----------------------------------------------------------------------------
# Веса источников
# ----------------------------------------------------------------------------

SOURCE_REGISTRY = "registry"        # ФНС: реестр МСП, ЕГРЮЛ, ГИР БО
SOURCE_SITE = "company_site"        # сайт самой компании
SOURCE_SEARCH = "search"            # поисковая выдача, связь не подтверждена
SOURCE_MANUAL = "manual"            # человек проверил руками
SOURCE_DERIVED = "derived"          # вывод из других фактов

SOURCE_WEIGHTS = {
    SOURCE_REGISTRY: 1.0,
    SOURCE_MANUAL: 0.9,
    SOURCE_SITE: 0.7,
    SOURCE_DERIVED: 0.6,
    SOURCE_SEARCH: 0.4,
}
UNKNOWN_SOURCE_WEIGHT = 0.3

# Поля, которые из некоторых источников особенно ненадёжны.
# ФИО директора из поисковой выдачи это чистая догадка, в письмо такое нельзя.
FIELD_PENALTY = {
    ("director_fio", SOURCE_SEARCH): 0.15,
    ("director_fio", SOURCE_SITE): 0.6,
    ("email", SOURCE_SEARCH): 0.25,
    ("phone", SOURCE_SEARCH): 0.25,
    ("revenue", SOURCE_SITE): 0.4,
    ("employees", SOURCE_SITE): 0.4,
}

DEFAULT_THRESHOLD = 0.7

# ----------------------------------------------------------------------------
# Контрольные суммы
# ----------------------------------------------------------------------------

_INN10 = [2, 4, 10, 3, 5, 9, 4, 6, 8]
_INN12_A = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
_INN12_B = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]


def norm_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def inn_checksum_ok(inn):
    """Контрольная сумма ИНН. 10 знаков для юрлица, 12 для предпринимателя."""
    s = norm_digits(inn)
    d = [int(c) for c in s]
    if len(s) == 10:
        return sum(_INN10[i] * d[i] for i in range(9)) % 11 % 10 == d[9]
    if len(s) == 12:
        first = sum(_INN12_A[i] * d[i] for i in range(10)) % 11 % 10 == d[10]
        second = sum(_INN12_B[i] * d[i] for i in range(11)) % 11 % 10 == d[11]
        return first and second
    return False


def ogrn_checksum_ok(ogrn):
    """Контрольная сумма ОГРН, 13 знаков, или ОГРНИП, 15 знаков."""
    s = norm_digits(ogrn)
    if len(s) == 13:
        return int(s[:12]) % 11 % 10 == int(s[12])
    if len(s) == 15:
        return int(s[:14]) % 13 % 10 == int(s[14])
    return False


# ----------------------------------------------------------------------------
# Нормализация записи компании
# ----------------------------------------------------------------------------

FIELD_ALIASES = {
    "inn": ["inn", "ИНН", "i", "inn_registry"],
    "ogrn": ["ogrn", "ОГРН", "o", "ogrnip", "ОГРНИП"],
    "name": ["name_ex", "name", "shortName", "short_name", "fullName", "namep",
             "namec", "c", "n", "Наименование / ФИО", "Наименование"],
    "okved": ["okved1", "okved2", "okved", "okved2main",
              "Основной вид деятельности"],
    "okved_name": ["okved1name", "okved2name", "okved_name"],
    "region": ["regioncode", "region", "Регион", "rn"],
    "city": ["cityname", "city", "Город"],
    "area": ["areaname", "district", "Район"],
    "employees": ["od2_sschr", "employees", "sschr",
                  "Среднесписочная численность работников за предшествующий "
                  "календарный год"],
    "has_contracts": ["has_contracts", "hasContracts", "Госконтракты"],
    "category": ["category", "mspCategory", "Категория"],
    "is_active": ["is_active", "isActive"],
    "is_new": ["isnew", "isNew", "Вновь созданный"],
    "dtregistry": ["dtregistry", "dtRegistry", "Дата включения в реестр", "r"],
    "dtregistryout": ["dtregistryout", "Дата исключения из реестра", "e"],
    "revenue": ["gainSum", "gain_sum", "revenue", "vyruchka", "СумДоход"],
    "revenue_period": ["period", "revenue_period", "yearcode"],
    "director_fio": ["director_fio", "director", "g", "ruk", "fio"],
    "www": ["www", "WWW", "site", "url", "website"],
    "email": ["email", "E-mail", "e-mail", "mail"],
    "phone": ["phone", "Телефон", "tel"],
    "address": ["address", "Адрес", "adres"],
}


def canonical_record(raw):
    """Свести запись из любого источника к одним именам полей.

    Разные части конвейера пишут по-разному: report.xlsx русскими заголовками,
    search-proc.json полями okved1 и od2_sschr, ЕГРЮЛ буквами i, o, g, c.
    Здесь всё это приводится к одному виду, а исходные ключи сохраняются.
    """
    if not raw:
        return {}
    out = dict(raw)
    lower = {}
    for key, value in raw.items():
        lower[str(key).strip().lower()] = value
    for canon, aliases in FIELD_ALIASES.items():
        if out.get(canon) not in (None, ""):
            continue
        for alias in aliases:
            value = raw.get(alias)
            if value in (None, ""):
                value = lower.get(alias.strip().lower())
            if value not in (None, ""):
                out[canon] = value
                break
    if out.get("inn"):
        out["inn"] = norm_digits(out["inn"])
    if out.get("ogrn"):
        out["ogrn"] = norm_digits(out["ogrn"])
    if out.get("director_fio"):
        out["director_name"] = director_name(out["director_fio"])
        out["director_post"] = director_post(out["director_fio"])
    return out


def director_name(raw):
    """Из 'ДИРЕКТОР: Скобей Эдуард Львович' достать ФИО."""
    text = str(raw or "").strip()
    if not text:
        return None
    if ":" in text:
        text = text.split(":", 1)[1]
    text = text.split(",")[0].strip()
    return text or None


def director_post(raw):
    text = str(raw or "").strip()
    if ":" in text:
        return text.split(":", 1)[0].strip()
    return None


def _fix_case(word):
    """'ИЛЬШАТ' -> 'Ильшат', 'САЛИХ-ОГЛЫ' -> 'Салих-Оглы'. Обычный регистр не трогаем."""
    if not word.isupper():
        return word
    return "-".join(part.capitalize() for part in word.split("-"))


def first_name_patronymic(fio):
    """'Скобей Эдуард Львович' -> 'Эдуард Львович', обращение в письме.

    По предпринимателям ЕГРЮЛ отдаёт ФИО капсом, поэтому регистр приводится
    к обычному виду: 'ГАТИЯТУЛЛИН ИЛЬШАТ ИСМАГИЛОВИЧ' даёт 'Ильшат Исмагилович'.
    Письмо, которое здоровается капсом, читается как рассылка робота.
    """
    parts = [_fix_case(p) for p in str(fio or "").split() if p]
    if len(parts) >= 3:
        return " ".join(parts[1:3])
    if len(parts) == 2:
        return parts[1]
    return None


# ----------------------------------------------------------------------------
# Загрузка записи компании из того, что есть в проекте
# ----------------------------------------------------------------------------

TABLE_PREFERENCE = ("companies", "company", "leads", "lead", "rmsp", "registry",
                    "bo", "egrul", "enriched", "diagnosis", "pains")


def load_company(inn, db_path=None, data_dir=None):
    """Собрать всё, что проект знает про этот ИНН.

    Схему базы пишет другая часть конвейера, поэтому здесь ничего не
    предполагается: таблицы ищутся по наличию колонки inn, файлы по совпадению
    значения. Если базы нет вообще, функция вернёт пустой словарь, и это не
    ошибка, а сигнал вызвать обогащение.
    """
    inn = norm_digits(inn)
    merged = {}
    sources = {}

    def absorb(record, origin):
        for key, value in (record or {}).items():
            if value in (None, "", []):
                continue
            if key not in merged or merged[key] in (None, "", []):
                merged[key] = value
                sources[key] = origin

    for record, origin in _load_from_sqlite(inn, db_path or DB_PATH):
        absorb(record, origin)
    for record, origin in _load_from_files(inn, data_dir or DATA_DIR):
        absorb(record, origin)

    if not merged:
        return {}
    out = canonical_record(merged)
    out["_sources"] = sources
    out.setdefault("inn", inn)
    return out


def _load_from_sqlite(inn, db_path):
    out = []
    if not inn or not db_path or not os.path.exists(db_path):
        return out
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        tables = [r[0] for r in cur.fetchall()]
        tables.sort(key=lambda t: _table_rank(t))
        for table in tables:
            try:
                cur.execute('PRAGMA table_info("%s")' % table.replace('"', ''))
                cols = [r[1] for r in cur.fetchall()]
            except sqlite3.Error:
                continue
            inn_col = None
            for c in cols:
                if str(c).strip().lower() in ("inn", "инн"):
                    inn_col = c
                    break
            if not inn_col:
                continue
            try:
                cur.execute(
                    'SELECT * FROM "%s" WHERE CAST("%s" AS TEXT) = ? LIMIT 5'
                    % (table.replace('"', ''), inn_col.replace('"', '')), (inn,))
                for row in cur.fetchall():
                    out.append((dict(row), "sqlite:" + table))
            except sqlite3.Error:
                continue
        conn.close()
    except sqlite3.Error:
        return out
    return out


def _table_rank(name):
    low = str(name).lower()
    for i, key in enumerate(TABLE_PREFERENCE):
        if key in low:
            return i
    return 100


def _load_from_files(inn, data_dir):
    out = []
    if not inn or not data_dir or not os.path.isdir(data_dir):
        return out
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    names.sort(key=lambda n: _table_rank(n))
    for name in names:
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > 80 * 1024 * 1024:
                continue
        except OSError:
            continue
        if name.lower().endswith(".json"):
            out.extend(_scan_json(path, inn))
        elif name.lower().endswith(".csv"):
            out.extend(_scan_csv(path, inn))
    return out


def _scan_json(path, inn):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError, UnicodeDecodeError):
        return []
    origin = "json:" + os.path.basename(path)
    records = []
    if isinstance(data, dict):
        if inn in data and isinstance(data[inn], dict):
            records.append(data[inn])
        else:
            for value in data.values():
                if isinstance(value, list):
                    records.extend([v for v in value if isinstance(v, dict)])
            if not records and norm_digits(data.get("inn")) == inn:
                records.append(data)
    elif isinstance(data, list):
        records = [v for v in data if isinstance(v, dict)]
    return [(r, origin) for r in records if norm_digits(r.get("inn") or r.get("ИНН")) == inn]


def _scan_csv(path, inn):
    origin = "csv:" + os.path.basename(path)
    out = []
    for encoding in ("utf-8-sig", "windows-1251"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                delimiter = ";" if sample.count(";") > sample.count(",") else ","
                for row in csv.DictReader(fh, delimiter=delimiter):
                    value = row.get("inn") or row.get("ИНН") or row.get("Inn")
                    if norm_digits(value) == inn:
                        out.append((dict(row), origin))
                        if len(out) >= 3:
                            break
            return out
        except (UnicodeDecodeError, OSError, csv.Error):
            continue
    return out


# ----------------------------------------------------------------------------
# Проверки
# ----------------------------------------------------------------------------

def verify_inn_match(inn_from_site, company_record):
    """Насколько мы уверены, что найденный сайт принадлежит этой компании.

    1.0  на сайте стоит ровно тот же ИНН, что в реестре. Это железно.
    0.5  ИНН на сайте валиден, но в записи реестра ИНН нет, сверять не с чем.
    0.0  ИНН не найден, не проходит контрольную сумму или принадлежит другому.
    """
    record = canonical_record(company_record or {})
    site_inn = norm_digits(inn_from_site)
    if not site_inn:
        return 0.0
    if not inn_checksum_ok(site_inn):
        return 0.0
    reg_inn = norm_digits(record.get("inn"))
    if not reg_inn:
        return 0.5
    if site_inn == reg_inn:
        return 1.0
    return 0.0


def verify_ogrn_match(ogrn_from_site, company_record):
    """То же самое по ОГРН. Запасной путь, если на сайте нет ИНН."""
    record = canonical_record(company_record or {})
    site_ogrn = norm_digits(ogrn_from_site)
    if not site_ogrn or not ogrn_checksum_ok(site_ogrn):
        return 0.0
    reg_ogrn = norm_digits(record.get("ogrn"))
    if not reg_ogrn:
        return 0.5
    return 1.0 if site_ogrn == reg_ogrn else 0.0


def _domain_label(url):
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url if "//" in str(url) else "http://" + str(url))
    host = (parsed.hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and parts[-2] in ("com", "org", "net", "co", "gov", "edu"):
        return parts[-3]
    return parts[0] if len(parts) == 1 else parts[-2]


def verify_domain_match(site_url, company_record):
    """Похож ли домен на название компании. Возвращает True или False.

    Это мягкая проверка, она не заменяет сверку по ИНН. Нужна там, где
    ИНН на сайте не нашёлся, а решить, писать или не писать, всё равно надо.
    """
    record = canonical_record(company_record or {})
    label = _domain_label(site_url)
    if not label:
        return False

    declared = _domain_label(record.get("www"))
    if declared and declared == label:
        return True

    try:
        from enrich_site import translit, name_core
    except ImportError:
        return False

    core = name_core(record.get("name") or "")
    lat = translit(core)
    words = [w for w in lat.split() if w]
    if not words:
        return False

    flat_label = label.replace("-", "").replace("_", "")
    joined = "".join(words)
    initials = "".join(w[0] for w in words)

    if flat_label == joined or flat_label == initials:
        return True
    if len(flat_label) >= 4 and (flat_label in joined or joined in flat_label):
        return True
    strong = [w for w in words if len(w) >= 4]
    hits = sum(1 for w in strong if w in flat_label)
    if hits >= 2:
        return True
    if hits == 1 and len(strong) == 1:
        return True
    return False


def score_fact(field, source_type):
    """Вес факта: реестр 1.0, сайт компании 0.7, поиск 0.4."""
    base = SOURCE_WEIGHTS.get(source_type, UNKNOWN_SOURCE_WEIGHT)
    penalty = FIELD_PENALTY.get((field, source_type))
    if penalty is not None:
        return penalty
    return base


def make_fact(field, value, source_type, source_url=None, note=None,
              confidence=None, text=None):
    """Собрать факт в едином виде. Всё, что идёт в письмо, проходит отсюда."""
    return {
        "field": field,
        "value": value,
        "text": text,
        "source_type": source_type,
        "source_url": source_url,
        "note": note,
        "confidence": round(
            confidence if confidence is not None else score_fact(field, source_type), 3),
    }


def gate_facts(facts, threshold=DEFAULT_THRESHOLD):
    """Разделить факты на пригодные для письма и отбракованные.

    Порог 0.7 пропускает реестр и сайт компании и отсекает поиск.
    Отбракованные не удаляются: их смотрит человек в отчёте о расхождениях.
    """
    usable, flagged = [], []
    for fact in facts or []:
        conf = fact.get("confidence")
        if conf is None:
            conf = score_fact(fact.get("field"), fact.get("source_type"))
            fact = dict(fact)
            fact["confidence"] = round(conf, 3)
        if fact.get("value") in (None, "", []) and not fact.get("text"):
            flagged.append(_flag(fact, "пустое значение"))
        elif conf >= threshold:
            usable.append(fact)
        else:
            flagged.append(_flag(fact, "доверие %.2f ниже порога %.2f" % (conf, threshold)))
    return usable, flagged


def _flag(fact, reason):
    out = dict(fact)
    out["flag_reason"] = reason
    return out


# ----------------------------------------------------------------------------
# Отчёт о расхождениях
# ----------------------------------------------------------------------------

def conflicts_report(company):
    """Список расхождений для человека. Ничего не решает сам, только показывает."""
    record = canonical_record(company or {})
    out = []
    year_now = datetime.date.today().year

    def add(code, text, severity, left=None, right=None):
        out.append({"code": code, "text": text, "severity": severity,
                    "left": left, "right": right})

    reg_inn = norm_digits(record.get("inn"))
    site_inn = norm_digits(record.get("inn_on_site"))
    site = record.get("site") or record.get("www")

    if reg_inn and not inn_checksum_ok(reg_inn):
        add("bad_registry_inn",
            "ИНН из реестра не проходит контрольную сумму, запись битая",
            "high", reg_inn, None)

    if site_inn and reg_inn and site_inn != reg_inn:
        add("inn_mismatch",
            "на сайте указан ИНН %s, а в реестре %s, это разные компании"
            % (site_inn, reg_inn), "high", site_inn, reg_inn)
    elif site and not site_inn:
        if verify_domain_match(site, record):
            add("inn_not_found",
                "ИНН на сайте не найден, компания опознана только по названию домена",
                "medium", site, record.get("name"))
        else:
            add("site_unconfirmed",
                "сайт ничем не подтверждён: ИНН на нём нет, домен на название не похож",
                "high", site, record.get("name"))

    site_ogrn = norm_digits(record.get("ogrn_on_site"))
    reg_ogrn = norm_digits(record.get("ogrn"))
    if site_ogrn and reg_ogrn and site_ogrn != reg_ogrn:
        add("ogrn_mismatch",
            "ОГРН на сайте не совпадает с реестром", "high", site_ogrn, reg_ogrn)

    email = record.get("email")
    if email and site and "@" in str(email):
        mail_domain = str(email).split("@", 1)[1].lower()
        site_host = (urllib.parse.urlsplit(
            site if "//" in str(site) else "http://" + str(site)).hostname or "").lower()
        site_host = site_host[4:] if site_host.startswith("www.") else site_host
        free = ("mail.ru", "yandex.ru", "ya.ru", "gmail.com", "bk.ru", "inbox.ru",
                "list.ru", "rambler.ru", "internet.ru", "outlook.com")
        if mail_domain != site_host and mail_domain not in free:
            add("email_foreign_domain",
                "почта %s не с домена сайта %s, возможно это подрядчик или партнёр"
                % (email, site_host), "medium", email, site_host)

    if str(record.get("is_active", "1")) in ("0", "False", "false"):
        add("not_active",
            "компания исключена из реестра МСП, писать ей рано", "high",
            record.get("dtregistryout"), None)
    elif record.get("dtregistryout"):
        add("registry_out",
            "у компании проставлена дата исключения из реестра МСП", "high",
            record.get("dtregistryout"), None)

    okved = str(record.get("okved") or "")
    if okved.startswith("49.4"):
        add("own_fleet",
            "основной ОКВЭД %s это грузоперевозки, у компании свой автопарк "
            "и наши услуги ей не нужны" % okved, "high", okved, None)

    employees = record.get("employees")
    if employees in (None, "", 0, "0"):
        add("no_employees",
            "численность сотрудников неизвестна, размер компании оценить нечем",
            "low", employees, None)

    year = record.get("copyright_year")
    if year and int(year) > year_now:
        add("future_copyright",
            "год копирайта на сайте больше текущего, разбору не верим",
            "low", year, year_now)

    if not record.get("director_fio"):
        add("no_director",
            "ФИО руководителя не получено, обращение в письме придётся делать общим",
            "low", None, None)

    if not email:
        add("no_email",
            "адреса почты нет, письмо отправлять некуда", "high", None, None)

    return out


# ----------------------------------------------------------------------------
# Факты из записи реестра
# ----------------------------------------------------------------------------

REGISTRY_FACT_FIELDS = (
    ("name", "название"),
    ("inn", "ИНН"),
    ("ogrn", "ОГРН"),
    ("okved", "основной ОКВЭД"),
    ("okved_name", "вид деятельности"),
    ("city", "город"),
    ("employees", "среднесписочная численность"),
    ("revenue", "выручка, тыс. руб."),
    ("has_contracts", "признак госконтрактов"),
    ("category", "категория МСП"),
    ("dtregistry", "дата включения в реестр МСП"),
    ("director_fio", "руководитель"),
)


def registry_facts(company, source_url=None):
    """Превратить запись реестра в список фактов с весом 1.0."""
    record = canonical_record(company or {})
    facts = []
    for field, label in REGISTRY_FACT_FIELDS:
        value = record.get(field)
        if value in (None, "", []):
            continue
        origin = record.get("_sources", {}).get(field, "")
        source_type = SOURCE_REGISTRY
        if "site" in str(origin):
            source_type = SOURCE_SITE
        facts.append(make_fact(field, value, source_type,
                               source_url=source_url, note=label))
    return facts


# ----------------------------------------------------------------------------
# База конвейера
#
# Имена таблиц и колонок совпадают с теми, что читает report.py. Это один
# контракт на весь репозиторий: выборка пишет companies и otsev, обогащение
# пишет enrichment, диагностика пишет signals, письма пишут letters.
# ----------------------------------------------------------------------------

# Статусы письма. ОДИН словарь на весь проект: значения пишет write_letter.py,
# читает drafts.py, на них же ссылаются скиллы. Латиница выбрана намеренно:
# статус ездит в SQL-запросах скиллов, и ломаться из-за раскладки он не должен.
#
#   chernovik      текст собран, автопроверки прошли, ждёт глаз человека
#   na_proverke    письмо собрано, но автопроверки нашли замечания
#   v_chernovikah  письмо уже лежит в папке черновиков ящика, повторно не льём
STATUS_DRAFT = "chernovik"
STATUS_REVIEW = "na_proverke"
STATUS_IN_DRAFTS = "v_chernovikah"
LETTER_STATUSES = (STATUS_DRAFT, STATUS_REVIEW, STATUS_IN_DRAFTS)

PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  inn TEXT PRIMARY KEY, ogrn TEXT, name TEXT, okved TEXT, okved_name TEXT,
  region TEXT, city TEXT, employees INTEGER, revenue INTEGER,
  has_contracts INTEGER, category INTEGER, is_new INTEGER, website TEXT,
  email TEXT, collected_at TEXT);
CREATE TABLE IF NOT EXISTS otsev(
  inn TEXT, name TEXT, reason TEXT, rule TEXT, dropped_at TEXT);
CREATE TABLE IF NOT EXISTS enrichment(
  inn TEXT, name TEXT, website TEXT, email TEXT, director TEXT,
  revenue INTEGER, source TEXT, confidence TEXT, checked_at TEXT,
  inn_on_site TEXT, ogrn_on_site TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS signals(
  inn TEXT, name TEXT, signal TEXT, detail TEXT,
  severity TEXT, source TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS letters(
  inn TEXT, name TEXT, to_email TEXT, subject TEXT,
  body TEXT, facts TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stoplist(
  inn TEXT PRIMARY KEY, email TEXT, reason TEXT, source TEXT, added_at TEXT);
"""


def open_db(path=None, create=True):
    """Открыть базу конвейера и создать недостающие таблицы."""
    path = path or DB_PATH
    folder = os.path.dirname(os.path.abspath(path))
    if create and folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if create:
        con.executescript(PIPELINE_SCHEMA)
        con.commit()
    return con


def table_columns(con, table):
    try:
        return [r[1] for r in con.execute('PRAGMA table_info("%s")'
                                          % table.replace('"', '')).fetchall()]
    except sqlite3.Error:
        return []


def ensure_column(con, table, column, sql_type="TEXT"):
    """Добавить колонку, если её нет. База могла быть создана раньше."""
    cols = table_columns(con, table)
    if not cols or column in cols:
        return False
    try:
        con.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s'
                    % (table.replace('"', ''), column.replace('"', ''), sql_type))
        con.commit()
        return True
    except sqlite3.Error:
        return False


def db_insert(con, table, row):
    """Вставить строку, отбросив ключи, которых нет в таблице."""
    cols = table_columns(con, table)
    if not cols:
        return False
    data = {k: v for k, v in (row or {}).items() if k in cols}
    if not data:
        return False
    keys = list(data.keys())
    con.execute('INSERT INTO "%s" (%s) VALUES (%s)'
                % (table.replace('"', ''),
                   ",".join('"%s"' % k for k in keys),
                   ",".join("?" for _ in keys)),
                [data[k] for k in keys])
    return True


def db_delete(con, table, value, key="inn"):
    """Убрать из таблицы всё, что лежит по этому ключу.

    Нужно там, где на один ИНН штатно приходится несколько строк: у одной
    компании может быть и пять сигналов сразу, и все они её.
    """
    cols = table_columns(con, table)
    if not cols or key not in cols:
        return 0
    if key == "inn":
        value = norm_digits(value)
    if value in (None, ""):
        return 0
    cur = con.execute('DELETE FROM "%s" WHERE "%s" = ?'
                      % (table.replace('"', ''), key.replace('"', '')), (value,))
    return cur.rowcount or 0


def db_replace(con, table, row, key="inn"):
    """Записать строку, заменив собой предыдущую с тем же ключом.

    Повторный прогон шага не должен плодить дубли: иначе в письмо уходит одна
    и та же боль дважды, а отчёт считает компании по строкам и завышает цифры.
    """
    cols = table_columns(con, table)
    if not cols:
        return False
    if key in cols:
        db_delete(con, table, (row or {}).get(key), key=key)
    return db_insert(con, table, row)


def db_upsert_company(con, row):
    """Вставить или обновить компанию по ИНН, не затирая уже собранное."""
    cols = table_columns(con, "companies")
    data = {k: v for k, v in (row or {}).items() if k in cols and v not in (None, "")}
    inn = norm_digits(data.get("inn"))
    if not inn:
        return False
    data["inn"] = inn
    exists = con.execute("SELECT 1 FROM companies WHERE inn = ?", (inn,)).fetchone()
    if exists:
        fields = [k for k in data if k != "inn"]
        if fields:
            con.execute('UPDATE companies SET %s WHERE inn = ?'
                        % ",".join('"%s" = ?' % f for f in fields),
                        [data[f] for f in fields] + [inn])
        return True
    return db_insert(con, "companies", data)


# ----------------------------------------------------------------------------
# Стоп-лист
#
# Отписка это не вежливая фраза в письме, а строка в базе. Попросили не писать,
# значит ИНН уходит в таблицу stoplist, и дальше он не попадает ни в письмо,
# ни в черновики: проверка стоит перед сборкой письма и перед раскладкой.
# Ручной список из config.json (блок otsev, поле isklyuchit_inn) переносится
# в ту же таблицу, чтобы проверка была одна на весь конвейер.
# ----------------------------------------------------------------------------

def stoplist_add(con, inn, email=None, reason=None, source="ruchnoy"):
    """Добавить ИНН в стоп-лист. Повторное добавление обновляет причину."""
    inn = norm_digits(inn)
    if not inn:
        return False
    row = {"inn": inn, "email": (email or "").strip() or None,
           "reason": reason or "просили не писать", "source": source,
           "added_at": now_stamp()}
    try:
        con.execute("INSERT OR REPLACE INTO stoplist "
                    "(inn, email, reason, source, added_at) VALUES (?,?,?,?,?)",
                    (row["inn"], row["email"], row["reason"], row["source"],
                     row["added_at"]))
        return True
    except sqlite3.Error:
        return False


def stoplist_all(con):
    """Весь стоп-лист списком словарей."""
    try:
        return [dict(r) for r in con.execute(
            "SELECT inn, email, reason, source, added_at FROM stoplist "
            "ORDER BY added_at DESC, inn").fetchall()]
    except sqlite3.Error:
        return []


def stoplist_find(con, inn=None, email=None):
    """Строка стоп-листа по ИНН или по адресу почты. None, если чисто."""
    inn = norm_digits(inn)
    try:
        if inn:
            row = con.execute("SELECT * FROM stoplist WHERE inn = ?",
                              (inn,)).fetchone()
            if row:
                return dict(row)
        mail = (email or "").strip().lower()
        if mail:
            row = con.execute("SELECT * FROM stoplist WHERE lower(email) = ?",
                              (mail,)).fetchone()
            if row:
                return dict(row)
    except sqlite3.Error:
        return None
    return None


def config_stoplist(config_path=None):
    """Ручной стоп-лист из конфига: блок otsev, поле isklyuchit_inn."""
    cfg = load_config(config_path, verbose=False)
    otsev = cfg.get("otsev") or {}
    values = _cfg_first(otsev.get("isklyuchit_inn"), cfg.get("isklyuchit_inn"))
    return [norm_digits(v) for v in cfg_list(values) if norm_digits(v)]


def stoplist_sync_config(con, config_path=None):
    """Перенести ИНН из конфига в таблицу. Возвращает число добавленных."""
    added = 0
    for inn in config_stoplist(config_path):
        if stoplist_find(con, inn=inn):
            continue
        if stoplist_add(con, inn, reason="исключён в config.json",
                        source="config"):
            added += 1
    if added:
        con.commit()
    return added


def stoplist_reason(inn, email=None, db_path=None, config_path=None, con=None):
    """Почему этой компании писать нельзя. Пустая строка означает, что можно.

    Смотрит и в таблицу stoplist, и в конфиг, поэтому работает даже тогда,
    когда база ещё не создана.
    """
    inn = norm_digits(inn)
    if con is not None:
        row = stoplist_find(con, inn=inn, email=email)
        if row:
            return row.get("reason") or "просили не писать"
    else:
        path = db_path or DB_PATH
        if path and os.path.exists(path):
            try:
                local = sqlite3.connect(path)
                local.row_factory = sqlite3.Row
                row = stoplist_find(local, inn=inn, email=email)
                local.close()
            except sqlite3.Error:
                row = None
            if row:
                return row.get("reason") or "просили не писать"
    if inn and inn in config_stoplist(config_path):
        return "исключён в config.json"
    return ""


def db_companies(con, inns=None, limit=None, need_site=False, need_email=False):
    """Компании из базы. need_site оставит тех, у кого сайт ещё не найден."""
    cols = table_columns(con, "companies")
    if not cols:
        return []
    where, params = [], []
    if inns:
        clean = [norm_digits(i) for i in inns if norm_digits(i)]
        if not clean:
            return []
        where.append("inn IN (%s)" % ",".join("?" for _ in clean))
        params.extend(clean)
    if need_site and "website" in cols:
        where.append("(website IS NULL OR website = '')")
    if need_email and "email" in cols:
        where.append("(email IS NULL OR email = '')")
    sql = "SELECT * FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY inn"
    if limit:
        sql += " LIMIT %d" % int(limit)
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


# Колонки companies, которых нет в первоначальной схеме. Их дозаполняют шаги
# выручки (ГИР БО) и директора (ЕГРЮЛ), поэтому база могла быть создана раньше,
# чем эти колонки понадобились.
COMPANY_EXTRA_COLUMNS = (
    ("phone", "TEXT"),
    ("address", "TEXT"),
    ("status", "TEXT"),
    ("status_date", "TEXT"),
    ("dt_registry", "TEXT"),
    ("director", "TEXT"),
    ("revenue_period", "TEXT"),
)


def ensure_company_columns(con):
    """Довести таблицу companies до полной схемы конвейера."""
    for column, sql_type in COMPANY_EXTRA_COLUMNS:
        ensure_column(con, "companies", column, sql_type)
    return con


# ----------------------------------------------------------------------------
# Конфиг ниши
#
# Один конфиг читают все шаги конвейера. Файла может не быть вообще: тогда
# работают значения из ключей командной строки, и это не ошибка.
# ----------------------------------------------------------------------------

# Конфиг один на весь конвейер, и называется он config.json. Старое имя
# config/icp.json оставлено только для тех, у кого он уже лежит на диске.
CONFIG_NAMES = ("config.json", os.path.join("config", "icp.json"),
                "config.example.json")

# Где искать: переменная AIOP_CONFIG, потом рядом со скриптами (корень
# репозитория или установленный пак), потом рабочая папка, потом стандартное
# место установки. Так конфиг находится независимо от текущей папки.
CONFIG_ROOTS = (
    ROOT,
    os.getcwd,
    os.path.join(os.path.expanduser("~"), ".claude", "tools", "prodazhi"),
)


def find_config(path=None):
    """Найти конфиг: сначала переданный путь, потом обычные места.

    Если переданного файла нет, поиск продолжается по обычным местам, а не
    падает: скиллы зовут скрипты с путём config/icp.json, которого на диске
    может не быть, и это не повод обрывать прогон.
    """
    if path and os.path.exists(path):
        return path
    env = os.environ.get("AIOP_CONFIG")
    if env and os.path.exists(env):
        return env
    for base in CONFIG_ROOTS:
        base = base() if callable(base) else base
        for name in CONFIG_NAMES:
            full = os.path.join(base, name)
            if os.path.exists(full):
                return full
    return None


def load_config(path=None, verbose=True):
    """Прочитать конфиг ниши. Отсутствие файла возвращает пустой словарь."""
    found = find_config(path)
    if not found:
        if verbose and path:
            print("конфига %s нет, беру значения из ключей командной строки" % path)
        return {}
    if verbose and path and os.path.abspath(found) != os.path.abspath(path):
        print("конфига %s нет, беру %s" % (path, found))
    try:
        with open(found, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        print("конфиг %s прочитать не удалось (%s), беру значения из ключей"
              % (found, exc))
        return {}
    if not isinstance(data, dict):
        return {}
    data["_path"] = found
    return data


def _cfg_first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def cfg_list(value):
    """Значение конфига в список строк: принимает и строку, и список."""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def cfg_int(value):
    if value in (None, "", [], {}):
        return None
    try:
        return int(str(value).replace(" ", ""))
    except ValueError:
        return None


def config_profile(cfg):
    """Свести конфиг к параметрам выборки независимо от его схемы.

    Понимает и блоки config.example.json (portret_klienta, vilki, otsev,
    istochniki, puti), и плоскую схему, где те же поля лежат в корне файла.
    Всё, чего в конфиге нет, возвращается как None или пустой список.
    """
    cfg = cfg or {}
    portret = cfg.get("portret_klienta") or cfg.get("icp") or {}
    vilki = cfg.get("vilki") or cfg.get("razmer") or {}
    otsev = cfg.get("otsev") or {}
    istochniki = cfg.get("istochniki") or {}
    puti = cfg.get("puti") or {}

    return {
        "region": _cfg_first(portret.get("region"), cfg.get("region")),
        "region_name": _cfg_first(portret.get("region_nazvanie"),
                                  cfg.get("region_nazvanie")),
        "okved": cfg_list(_cfg_first(portret.get("okved"), cfg.get("okved"))),
        "np_type": cfg_list(_cfg_first(portret.get("tip_subekta"),
                                       cfg.get("tip_subekta"), "UL")),
        "category": cfg_list(_cfg_first(portret.get("kategoriya_msp"),
                                        cfg.get("kategoriya_msp"))),
        "employees_from": cfg_int(_cfg_first(vilki.get("chislennost_ot"),
                                             cfg.get("chislennost_ot"))),
        "employees_to": cfg_int(_cfg_first(vilki.get("chislennost_do"),
                                           cfg.get("chislennost_do"))),
        "revenue_from": cfg_int(_cfg_first(vilki.get("vyruchka_tys_rub_ot"),
                                           cfg.get("vyruchka_tys_rub_ot"))),
        "revenue_to": cfg_int(_cfg_first(vilki.get("vyruchka_tys_rub_do"),
                                         cfg.get("vyruchka_tys_rub_do"))),
        "period": _cfg_first(vilki.get("otchetnyj_god"), cfg.get("otchetnyj_god")),
        "exclude_okved": cfg_list(_cfg_first(otsev.get("isklyuchit_okved"),
                                             cfg.get("isklyuchit_okved"))),
        "exclude_inn": [norm_digits(i) for i in
                        cfg_list(_cfg_first(otsev.get("isklyuchit_inn"),
                                            cfg.get("isklyuchit_inn")))],
        "dt_category": _cfg_first(istochniki.get("rmsp_dt_category"),
                                  cfg.get("rmsp_dt_category")),
        "bo_page_size": cfg_int(_cfg_first(istochniki.get("bo_page_size"),
                                           cfg.get("bo_page_size"))),
        "db": _cfg_first(puti.get("baza"), cfg.get("baza")),
        "xlsx_dir": _cfg_first(puti.get("kesh_xlsx"), cfg.get("kesh_xlsx")),
        "path": cfg.get("_path"),
    }


def resolve_db_path(arg_value, profile=None):
    """Путь к базе: ключ командной строки, потом конфиг, потом data/leads.db."""
    if arg_value:
        return arg_value
    from_cfg = (profile or {}).get("db")
    if from_cfg:
        return from_cfg if os.path.isabs(from_cfg) else os.path.join(ROOT, from_cfg)
    return DB_PATH


def read_inn_file(path):
    """Список ИНН из файла: по одному в строке, лишнее отбрасывается."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                inn = norm_digits(line.split(";")[0].split(",")[0])
                if len(inn) in (10, 12):
                    out.append(inn)
    except OSError:
        return []
    return out


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------------------
# Демо
# ----------------------------------------------------------------------------

DEMO_COMPANY = {
    "inn": "1650027925",
    "ogrn": "1021602013762",
    "name_ex": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ЧЕЛНЫ-ХЛЕБ"',
    "okved1": "10.71.1",
    "okved1name": "Производство хлеба и хлебобулочных изделий недлительного хранения",
    "regioncode": "16", "cityname": "Набережные Челны",
    "od2_sschr": 76, "has_contracts": 1, "category": 3,
    "site": "https://chelny-hleb.ru/",
    "inn_on_site": "1650027925",
    "email": "sales@chelny-hleb.ru",
    "g": "ДИРЕКТОР: Иванов Иван Иванович",
}


def _demo():
    print("=" * 72)
    print("verify.py: демо")
    print("=" * 72)

    print("\n[1] Контрольные суммы на реальных номерах из ЕГРЮЛ и реестра МСП")
    for inn in ("1650027925", "1651080801", "160800724395", "1234567890"):
        print("    ИНН  %-13s %s" % (inn, "валиден" if inn_checksum_ok(inn) else "БИТЫЙ"))
    for ogrn in ("1021602013762", "309167228000012", "1021602013761"):
        print("    ОГРН %-16s %s" % (ogrn, "валиден" if ogrn_checksum_ok(ogrn) else "БИТЫЙ"))

    print("\n[2] Сверка сайта с записью реестра")
    print("    свой ИНН на сайте:       %.2f"
          % verify_inn_match("1650027925", DEMO_COMPANY))
    print("    чужой ИНН на сайте:      %.2f"
          % verify_inn_match("1651080801", DEMO_COMPANY))
    print("    ИНН с битой суммой:      %.2f"
          % verify_inn_match("1650027920", DEMO_COMPANY))
    print("    ИНН на сайте не найден:  %.2f" % verify_inn_match(None, DEMO_COMPANY))
    print("    домен chelny-hleb.ru:    %s"
          % verify_domain_match("https://chelny-hleb.ru/", DEMO_COMPANY))
    print("    домен sibur.ru:          %s"
          % verify_domain_match("https://sibur.ru/", DEMO_COMPANY))

    print("\n[3] Вес факта по источнику")
    for field, source in (("employees", SOURCE_REGISTRY), ("email", SOURCE_SITE),
                          ("director_fio", SOURCE_SEARCH), ("email", SOURCE_SEARCH)):
        print("    %-13s из %-13s -> %.2f" % (field, source, score_fact(field, source)))

    print("\n[4] Фильтр фактов, порог 0.7")
    facts = registry_facts(DEMO_COMPANY)
    facts.append(make_fact("email", "sales@chelny-hleb.ru", SOURCE_SITE,
                           "https://chelny-hleb.ru/contacts/"))
    facts.append(make_fact("director_fio", "Петров Пётр Петрович", SOURCE_SEARCH,
                           "поисковая выдача"))
    usable, flagged = gate_facts(facts)
    print("    в письмо пойдёт %d фактов:" % len(usable))
    for f in usable:
        print("      %.2f  %-14s %s" % (f["confidence"], f["field"], f["value"]))
    print("    отбраковано %d:" % len(flagged))
    for f in flagged:
        print("      %.2f  %-14s %s  (%s)"
              % (f["confidence"], f["field"], f["value"], f["flag_reason"]))

    print("\n[5] Расхождения для человека")
    broken = dict(DEMO_COMPANY)
    broken["inn_on_site"] = "1651080801"
    broken["okved1"] = "49.41"
    broken.pop("g")
    for c in conflicts_report(broken):
        print("    [%s] %s" % (c["severity"], c["text"]))

    print("\n[6] Загрузка записи из базы проекта")
    loaded = load_company("1650027925")
    if loaded:
        print("    нашлось полей: %d, источники: %s"
              % (len(loaded), sorted(set(loaded.get("_sources", {}).values()))))
    else:
        print("    базы data/leads.db пока нет, это нормально:")
        print("    выборку кладёт в неё шаг реестра, а модуль работает и без неё")


def _cli(argv):
    import argparse
    parser = argparse.ArgumentParser(
        description="Проверка достоверности данных по компании")
    parser.add_argument("--db", default=DB_PATH, help="путь к sqlite-базе конвейера")
    parser.add_argument("--inn", help="ИНН одной компании")
    parser.add_argument("--inn-file", help="файл со списком ИНН, по одному в строке")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    inns = []
    if args.inn:
        inns.append(norm_digits(args.inn))
    if args.inn_file:
        inns.extend(read_inn_file(args.inn_file))
    if not inns:
        con = open_db(args.db, create=False) if os.path.exists(args.db) else None
        if con is None:
            print("базы %s нет, укажите --inn или сначала соберите выборку" % args.db)
            return 1
        inns = [c["inn"] for c in db_companies(con, limit=args.limit)]
        con.close()

    total_conflicts = 0
    for inn in inns[:args.limit]:
        company = load_company(inn, db_path=args.db)
        if not company:
            print("%s: в базе нет" % inn)
            continue
        conflicts = conflicts_report(company)
        total_conflicts += len(conflicts)
        head = company.get("name") or ""
        print("%s %s" % (inn, head[:50]))
        if not conflicts:
            print("    расхождений нет")
        for c in conflicts:
            print("    [%s] %s" % (c["severity"], c["text"]))
    print("\nвсего расхождений: %d" % total_conflicts)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if any(a.split("=")[0] in ("--db", "--inn", "--inn-file", "--limit") for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
