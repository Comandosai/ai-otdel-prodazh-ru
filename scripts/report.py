#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py: генератор HTML-отчётов конвейера «AI-отдел продаж».

Читает sqlite-базу конвейера и складывает в out/ пять самодостаточных
HTML-файлов. CSS вставляется прямо в <head>, картинок и внешних скриптов нет,
поэтому каждый файл открывается двойным кликом и работает офлайн.

Отчёты:
    sobrano.html        что собрали из реестров (ядро выборки)
    otsev.html          воронка бесплатного отсева до обогащения
    obogashchenie.html  что нашли: сайт, почта, ФИО, выручка + уверенность
    signaly.html        диагностика боли по каждой компании
    pisma.html          черновики писем и факты, на которых они построены

ОЖИДАЕМАЯ СХЕМА БАЗЫ
--------------------
Скрипт не требует точных имён. Он читает PRAGMA table_info и подбирает
колонки по списку синонимов (см. COL_ALIASES), пропущенные поля показывает
как «нет данных», отсутствующие таблицы как честный пустой блок.
Базовый вариант, на который всё рассчитано:

    companies(inn, ogrn, name, okved, okved_name, region, city, area,
              employees, revenue, has_contracts, has_licenses,
              category, is_new, phone, email, www, collected_at)
        поля 1:1 с выгрузкой rmsp.nalog.ru/report.xlsx,
        employees = od2_sschr, has_contracts = признак госконтрактов,
        revenue = gainSum из bo.nalog.gov.ru, в тысячах рублей.

    otsev(inn, name, reason, rule, dropped_at)
        по строке на каждую отсеянную компанию, reason = текст причины
        («есть ОКВЭД 49.41, свой автопарк»), rule = кодовое имя правила.

    enrichment(inn, name, website, email, director, position,
               revenue, source, confidence, checked_at)
        source = откуда взято (сайт, egrul, bo, rmsp),
        confidence = высокая / средняя / низкая, либо high/medium/low,
        либо число 0..1.

    signals(inn, name, signal, detail, severity, source, checked_at)
        severity = высокая / средняя / низкая.

    letters(inn, name, to_email, subject, body, facts, status, created_at)
        facts = JSON-массив строк, либо строки через перевод строки,
        либо через «;».

ЗАПУСК
------
    python3 report.py --db data/leads.db --out out
    python3 report.py --db data/leads.db --out out --only otsev
    python3 report.py --demo --out out        # демо-данные, база не нужна

Python 3.9, только стандартная библиотека.
"""

import argparse
import datetime
import html
import json
import os
import re
import sqlite3
import sys

VERSION = "1.0"

# ---------------------------------------------------------------- CSS

FALLBACK_CSS = """
:root{--lime:#d9fc67;--lime-deep:#8fae1f;--ink:#131313;--ink-soft:#4a4a4a;
--muted:#6f6f6f;--paper:#f6f7f3;--card:#fff;--line:#e3e5dc;--error:#972e2e;--ok:#2e7d4f}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-size:16px;line-height:1.6;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
main{padding:48px 0 120px}
h1{font-size:44px;line-height:1.05;text-transform:uppercase;margin:0 0 16px}
h2{font-size:30px;margin:0 0 10px;padding-bottom:10px;border-bottom:3px solid var(--ink)}
.tbl{width:100%;border-collapse:collapse;font-size:14px;background:#fff;border:1px solid var(--line)}
.tbl th{background:#131313;color:#fff;text-align:left;padding:10px 13px}
.tbl td{padding:10px 13px;border-top:1px solid var(--line)}
.stat,.co-card,.mail,.note,.empty{background:#fff;border:1px solid var(--line);
border-radius:12px;padding:18px 20px;margin-bottom:14px}
.stat .v{font-size:38px;font-weight:800;display:block}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px;background:#eceee4}
.badge--high{background:var(--lime)}
.badge--mid{background:#fdf7e6;color:#8a6a12}
.badge--low{background:#fdf3f3;color:var(--error)}
.funnel__row{display:grid;grid-template-columns:minmax(150px,260px) minmax(0,1fr) 96px;
gap:16px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.funnel__track{background:#eceee4;border-radius:6px;height:30px;overflow:hidden}
.funnel__bar{height:100%;background:#c9ccbe}
.funnel__num{font-size:26px;font-weight:800;text-align:right}
.mail__head{background:#131313;color:#fff;padding:16px 22px}
.mail__subj{color:var(--lime);font-size:22px;margin:0}
.mail__body{padding:22px;white-space:pre-wrap}

/* Ниже классы, без которых отчёт разваливается: шапка, воронка, карточки
   компаний, письма и служебные пометки в таблицах. Запасные стили покрывают
   ВСЕ классы, которые встречаются в этом файле, иначе отчёт «собран успешно»
   выглядел бы как голый текст. Проверяется тем же способом, каким считались
   пропуски: список class= из report.py против списка правил здесь. */
.hero{padding:44px 0 26px;border-bottom:3px solid var(--ink);margin-bottom:34px}
.kicker{font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:var(--muted);margin-bottom:10px}
.lead{font-size:19px;color:var(--ink-soft);max-width:820px;margin:0 0 16px}
.sub{color:var(--muted);font-size:15px;margin:0 0 16px;max-width:820px}
.num{display:inline-block;min-width:52px;color:var(--muted);font-weight:800}
.meta-row{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-block;font-size:13px;padding:5px 12px;border-radius:99px;
background:#eceee4;color:var(--ink-soft)}
.chip.hot{background:var(--lime);color:var(--ink);font-weight:700}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;
margin-bottom:26px}
.stat .k{display:block;font-size:13px;color:var(--muted);text-transform:uppercase;
letter-spacing:.06em}
.scroller{overflow-x:auto;margin-bottom:22px}
.tbl th.r,.tbl td.n{text-align:right}
.tbl td.co{font-weight:700}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.nowrap{white-space:nowrap}
.wrapcell{max-width:280px;word-break:break-word}
.dim{color:var(--muted)}
.hot{color:var(--ink);font-weight:800}
.ok{color:var(--ok);font-weight:700}
.bad{color:var(--error);font-weight:700}
.badge--none{background:#f2f2ef;color:var(--muted)}
.badge--src{background:#eef2fb;color:#31507f;font-weight:600}
.sig{display:inline-block;font-size:13px;padding:3px 10px;border-radius:8px;
background:#eceee4;margin:0 6px 6px 0}
.sig--hot{background:#fdf3f3;color:var(--error);font-weight:700}
.sig--mid{background:#fdf7e6;color:#8a6a12}
.sig--soft{background:#f2f2ef;color:var(--muted)}
.funnel{margin-bottom:26px}
.funnel__label{font-weight:600}
.funnel__row--start .funnel__bar{background:var(--ink)}
.funnel__row--keep .funnel__bar{background:var(--lime-deep)}
.funnel__row--drop .funnel__bar{background:#d8b3b3}
.stat.accent{border:2px solid var(--ink)}
.stat.good .v{color:var(--ok)}
tr.warn td,.warn{background:#fdf7e6}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.co-card.top,.co-card--top{border-color:var(--ink);border-width:2px}
.co-card__rank{font-size:13px;font-weight:800;color:var(--muted)}
.co-card__name{font-size:19px;font-weight:800;margin:2px 0 4px}
.co-card__inn{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:13px;color:var(--muted)}
.co-card__meta{font-size:14px;color:var(--ink-soft);margin:8px 0}
.co-card__facts{font-size:14px;margin:8px 0;padding-left:18px}
.co-card__foot{font-size:13px;color:var(--muted);border-top:1px solid var(--line);
padding-top:10px;margin-top:10px}
.mails{display:grid;gap:18px}
.mail{padding:0;overflow:hidden}
.mail__to{font-size:13px;color:#c9ccbe;margin:6px 0 0}
.mail__facts{padding:0 22px 18px;font-size:14px;color:var(--ink-soft)}
.mail__foot{padding:14px 22px;border-top:1px solid var(--line);font-size:13px;
color:var(--muted)}
.warnbox{background:#fdf7e6;border:1px solid #e6d9ae;border-radius:12px;
padding:16px 20px;margin-bottom:22px}
.rep-foot{border-top:3px solid var(--ink);padding:22px 0 60px;font-size:13px;
color:var(--muted)}
.brand{font-weight:800;letter-spacing:.14em;color:var(--ink)}
"""

CSS_WARNING = (
    "Файл assets/comandos.css не найден, использованы запасные стили. "
    "Положите comandos.css рядом с report.py или в ../assets/."
)


def find_css():
    """Ищет comandos.css рядом со скриптом и в ../assets/. Возвращает (css, ok)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "comandos.css"),
        os.path.join(here, "assets", "comandos.css"),
        os.path.join(here, os.pardir, "assets", "comandos.css"),
        os.path.join(here, os.pardir, os.pardir, "assets", "comandos.css"),
        os.path.join(os.getcwd(), "assets", "comandos.css"),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read(), True
            except OSError:
                continue
    return FALLBACK_CSS, False


# ---------------------------------------------------------------- утилиты

def esc(value):
    """Безопасный текст для HTML. None превращается в пустую строку."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def dim(value, placeholder="нет данных"):
    """Значение или серый плейсхолдер."""
    if value is None or str(value).strip() == "":
        return '<span class="dim">%s</span>' % esc(placeholder)
    return esc(value)


def num(value):
    """Число с разделителем разрядов.

    Разделитель, это неразрывный пробел U+00A0: по правилам русской
    типографики число не должно переноситься по разрядам.
    Написан явно через escape, чтобы его было видно в исходнике.
    """
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    sign = "-" if n < 0 else ""
    body = "{:,}".format(abs(n)).replace(",", "\u00a0")
    return sign + body


def numd(value, placeholder="нет данных"):
    out = num(value)
    if out is None:
        return '<span class="dim">%s</span>' % esc(placeholder)
    return out


def pct(part, whole):
    if not whole:
        return 0.0
    return 100.0 * float(part) / float(whole)


def plural(n, one, few, many):
    """Русская форма множественного числа."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def now_human():
    return datetime.datetime.now().strftime("%d.%m.%Y %H:%M")


def clean_html_tags(value):
    """Чистит <strong> из ответов bo.nalog.gov.ru и прочий мусор разметки."""
    if value is None:
        return None
    return re.sub(r"<[^>]{1,40}>", "", str(value))


# ---------------------------------------------------------------- доступ к базе

TABLE_ALIASES = {
    "companies": ["companies", "company", "sobrano", "leads", "lead", "organizations", "vyborka"],
    "otsev": ["otsev", "rejected", "filtered", "otseyannye", "drops", "excluded"],
    "enrichment": ["enrichment", "enriched", "obogashchenie", "obogaschenie", "contacts", "enrich"],
    "signals": ["signals", "signaly", "diagnostics", "diagnostika", "pain", "boli"],
    "letters": ["letters", "pisma", "emails", "drafts", "chernoviki", "mail"],
}

COL_ALIASES = {
    "inn": ["inn", "innul", "inn_ul", "innorg", "i"],
    "ogrn": ["ogrn", "ogrnip", "o"],
    "name": ["name", "name_ex", "nameex", "shortname", "short_name", "namec", "fullname",
             "full_name", "namep", "company", "company_name", "title", "naimenovanie", "c", "n"],
    "okved": ["okved", "okved1", "okved2", "okved_code", "okvedmain", "okved2main"],
    "okved_name": ["okved_name", "okved1name", "okved2name", "okved_text", "vid_deyatelnosti"],
    "region": ["region", "regioncode", "region_code", "regionname", "rn"],
    "city": ["city", "cityname", "gorod", "settlement", "localityname"],
    "area": ["area", "areaname", "district", "rayon"],
    "employees": ["employees", "od2_sschr", "sschr", "chislennost", "staff", "headcount", "emp"],
    "revenue": ["revenue", "gainsum", "gain_sum", "vyruchka", "sumdohod", "turnover"],
    "has_contracts": ["has_contracts", "hascontracts", "goskontrakty", "contracts", "gz"],
    "has_licenses": ["has_licenses", "haslicenses", "licenses", "licenzii"],
    "category": ["category", "msp_category", "mspcategory", "kategoriya"],
    "is_new": ["is_new", "isnew", "novaya", "new"],
    "phone": ["phone", "tel", "telefon", "phones"],
    "email": ["email", "mail", "e_mail", "pochta", "emails"],
    "website": ["website", "site", "www", "url", "domain", "sajt"],
    "director": ["director", "fio", "ceo", "head", "rukovoditel", "g", "director_fio"],
    "position": ["position", "dolzhnost", "post", "role"],
    "source": ["source", "istochnik", "src", "origin", "from_source"],
    "confidence": ["confidence", "uverennost", "conf", "trust", "score", "quality"],
    "reason": ["reason", "prichina", "cause", "why", "reason_text"],
    "rule": ["rule", "pravilo", "rule_id", "filter", "code", "rule_name"],
    "signal": ["signal", "signal_name", "signal_type", "type", "priznak", "problem"],
    "detail": ["detail", "details", "value", "detal", "evidence", "fact", "comment"],
    "severity": ["severity", "level", "weight", "priority", "vazhnost", "confidence"],
    "subject": ["subject", "tema", "title", "subj", "theme"],
    "body": ["body", "text", "telo", "message", "content", "letter"],
    "facts": ["facts", "fakty", "evidence", "basis", "osnovaniya", "reasons"],
    "to_email": ["to_email", "to", "recipient", "email", "mail", "komu", "pochta"],
    "status": ["status", "sostoyanie", "state"],
    "created_at": ["created_at", "created", "dt", "date", "checked_at", "collected_at", "ts", "dropped_at"],
    "checked_at": ["checked_at", "checked", "dt", "date", "collected_at", "created_at", "ts"],
    "collected_at": ["collected_at", "collected", "dt", "date", "created_at", "ts"],
}


class Db(object):
    """Тонкая обёртка над sqlite: терпит другие имена таблиц и колонок."""

    def __init__(self, path):
        self.path = path
        self.con = None
        self.tables = {}
        if path and os.path.isfile(path):
            self.con = sqlite3.connect(path)
            self.con.row_factory = sqlite3.Row
            rows = self.con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
            for row in rows:
                self.tables[row["name"].lower()] = row["name"]

    # -- служебное -------------------------------------------------

    def resolve(self, logical):
        for alias in TABLE_ALIASES.get(logical, [logical]):
            if alias.lower() in self.tables:
                return self.tables[alias.lower()]
        return None

    def columns(self, table):
        if not self.con:
            return {}
        out = {}
        for row in self.con.execute('PRAGMA table_info("%s")' % table.replace('"', '')):
            out[str(row["name"]).lower()] = row["name"]
        return out

    # -- чтение ----------------------------------------------------

    def load(self, logical, fields, order_by=None, limit=None):
        """Возвращает (rows, table_name). rows = список словарей по fields.

        Если таблицы нет, вернёт (None, None). Отсутствующие поля будут None.
        """
        table = self.resolve(logical)
        if not table:
            return None, None
        have = self.columns(table)
        mapping = {}
        for field in fields:
            for alias in COL_ALIASES.get(field, [field]):
                if alias.lower() in have:
                    mapping[field] = have[alias.lower()]
                    break
        if not mapping:
            return [], table
        select = ", ".join(
            '"%s" AS "%s"' % (src.replace('"', ''), dst) for dst, src in mapping.items()
        )
        query = 'SELECT %s FROM "%s"' % (select, table.replace('"', ''))
        if order_by and order_by in mapping:
            query += ' ORDER BY "%s" DESC' % order_by
        if limit:
            query += " LIMIT %d" % int(limit)
        try:
            raw = self.con.execute(query).fetchall()
        except sqlite3.Error:
            return [], table
        rows = []
        for item in raw:
            record = dict((f, None) for f in fields)
            for field in mapping:
                record[field] = item[field]
            rows.append(record)
        return rows, table

    def count(self, logical):
        table = self.resolve(logical)
        if not table:
            return None
        try:
            row = self.con.execute('SELECT COUNT(*) AS c FROM "%s"' % table.replace('"', '')).fetchone()
            return int(row["c"])
        except sqlite3.Error:
            return None


# ---------------------------------------------------------------- нормализация

CONF_HIGH = ("высокая", "высокое", "high", "точно", "verified", "podtverzhdeno", "1")
CONF_MID = ("средняя", "среднее", "medium", "mid", "veroyatno", "probable", "2")
CONF_LOW = ("низкая", "низкое", "low", "guess", "predpolozhenie", "3")

CONF_LABEL = {
    "high": "высокая",
    "mid": "средняя",
    "low": "низкая",
    "none": "не указана",
}


def conf_level(value):
    """Приводит уверенность к high / mid / low / none."""
    if value is None or str(value).strip() == "":
        return "none"
    text = str(value).strip().lower()
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        number = None
    if number is not None:
        if number > 1.0:
            number = number / 100.0 if number <= 100 else 1.0
        if number >= 0.8:
            return "high"
        if number >= 0.5:
            return "mid"
        return "low"
    for token in CONF_HIGH:
        if text.startswith(token):
            return "high"
    for token in CONF_MID:
        if text.startswith(token):
            return "mid"
    for token in CONF_LOW:
        if text.startswith(token):
            return "low"
    return "none"


def conf_badge(value):
    level = conf_level(value)
    label = CONF_LABEL[level]
    if level == "none":
        return '<span class="badge badge--none">%s</span>' % esc(label)
    return '<span class="badge badge--%s">%s</span>' % (level, esc(label))


SEV_CLASS = {"high": "sig--hot", "mid": "sig--mid", "low": "sig--soft", "none": "sig"}


def sev_chip(text, severity=None):
    level = conf_level(severity)
    css = SEV_CLASS.get(level, "sig")
    if css == "sig":
        return '<span class="sig">%s</span>' % esc(text)
    return '<span class="sig %s">%s</span>' % (css, esc(text))


def as_bool(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "да", "yes", "y", "t"):
        return True
    if text in ("0", "false", "нет", "no", "n", "f", ""):
        return False
    return None


def yesno(value):
    flag = as_bool(value)
    if flag is None:
        return '<span class="dim">нет данных</span>'
    if flag:
        return '<span class="ok">да</span>'
    return '<span class="dim">нет</span>'


CATEGORY_LABEL = {"1": "микро", "2": "малое", "3": "среднее", "0": "не МСП"}


def category_label(value):
    if value is None or str(value).strip() == "":
        return None
    key = str(value).strip()
    return CATEGORY_LABEL.get(key, key)


def split_facts(value):
    """Факты письма: JSON-массив, перевод строки или точка с запятой."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except ValueError:
            pass
    parts = re.split(r"[\n\r]+|(?<!\d);(?!\d)|\s•\s", text)
    return [p.strip(" -•\t") for p in parts if p.strip(" -•\t")]


# ---------------------------------------------------------------- каркас страницы

def page(css, title, kicker, heading, lead, chips, body, css_ok=True):
    """Собирает самодостаточный HTML: стили инлайном, внешних файлов нет."""
    chip_html = "".join(
        '<span class="chip%s">%s</span>' % ((" hot" if hot else ""), esc(text))
        for text, hot in chips
    )
    warn = ""
    if not css_ok:
        warn = '<div class="warnbox"><h3>Стили не найдены</h3><p>%s</p></div>' % esc(CSS_WARNING)
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
%(css)s
</style>
</head>
<body>
<div class="wrap">
<main>
<header class="hero">
  <div class="kicker">%(kicker)s</div>
  <h1>%(heading)s</h1>
  <p class="lead">%(lead)s</p>
  <div class="meta-row">%(chips)s</div>
</header>
%(warn)s
%(body)s
<footer class="rep-foot">
  <span class="brand">COMANDOS</span>
  <span>AI-отдел продаж, отчёт собран %(stamp)s</span>
  <span>report.py v%(version)s</span>
</footer>
</main>
</div>
</body>
</html>
""" % {
        "title": esc(title),
        "css": css,
        "kicker": esc(kicker),
        "heading": heading,
        "lead": esc(lead),
        "chips": chip_html,
        "warn": warn,
        "body": body,
        "stamp": esc(now_human()),
        "version": esc(VERSION),
    }


def stat(value, key, mod=""):
    css = "stat" + ((" " + mod) if mod else "")
    return '<div class="%s"><span class="v">%s</span><span class="k">%s</span></div>' % (
        css, value, esc(key))


def stats_block(items):
    return '<div class="stats">%s</div>' % "".join(items)


def empty_block(title, text):
    return '<div class="empty"><b>%s</b>%s</div>' % (esc(title), esc(text))


def no_table(logical, hint):
    return empty_block(
        "нет таблицы %s" % logical,
        "Запустите соответствующий шаг конвейера. %s" % hint,
    )


def write(out_dir, filename, content):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def dedupe_by_inn(rows):
    """Одна строка на компанию: последняя проверка вытесняет предыдущие.

    Отчёт считает КОМПАНИИ, а не строки таблицы. База, собранная до появления
    db_replace, могла накопить по несколько строк на один ИНН, и без этой
    свёртки отчёт завышал бы и «компаний обогащено», и «черновиков готово».
    """
    seen, without_inn = {}, []
    for row in rows or []:
        inn = str(row.get("inn") or "").strip()
        if inn:
            seen[inn] = row
        else:
            without_inn.append(row)
    return list(seen.values()) + without_inn


def top_counter(rows, field, limit=12):
    """Частотный список значений поля, отсортированный по убыванию."""
    counts = {}
    for row in rows:
        key = row.get(field)
        key = "не указано" if key is None or str(key).strip() == "" else str(key).strip()
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# ---------------------------------------------------------------- 1. собрано

SOBRANO_FIELDS = ["inn", "ogrn", "name", "okved", "okved_name", "region", "city", "area",
                  "employees", "revenue", "has_contracts", "has_licenses", "category",
                  "is_new", "phone", "email", "website", "collected_at"]


def render_sobrano(db, out_dir, css, css_ok=True, limit=400):
    """Отчёт по ядру выборки: что пришло из реестра МСП и ГИР БО."""
    rows, table = db.load("companies", SOBRANO_FIELDS)
    if rows is None:
        body = no_table("companies", "Ядро выборки собирается из rmsp.nalog.ru/report.xlsx.")
        return write(out_dir, "sobrano.html", page(
            css, "Собрано", "шаг 1 из 5", "Что <em>собрали</em>",
            "Таблица с компаниями пока не создана.", [], body, css_ok))

    total = len(rows)
    with_emp = [r for r in rows if num(r.get("employees")) is not None]
    emp_values = [int(float(r["employees"])) for r in with_emp]
    avg_emp = int(round(sum(emp_values) / float(len(emp_values)))) if emp_values else None
    contracts = sum(1 for r in rows if as_bool(r.get("has_contracts")) is True)
    fresh = sum(1 for r in rows if as_bool(r.get("is_new")) is True)
    with_rev = sum(1 for r in rows if num(r.get("revenue")) is not None)
    with_site = sum(1 for r in rows if r.get("website"))
    with_mail = sum(1 for r in rows if r.get("email"))

    cards = [
        stat(numd(total), "компаний в выборке", "accent"),
        stat(numd(avg_emp) if avg_emp is not None else '<span class="dim">нет</span>',
             "средняя численность"),
        stat(numd(contracts), "с госконтрактами", "good" if contracts else ""),
        stat(numd(fresh), "вновь созданных"),
        stat(numd(with_rev), "с выручкой из ГИР БО"),
        stat(numd(with_site) + '<small>из %s</small>' % num(total), "с сайтом в реестре"),
    ]

    okved_rows = "".join(
        '<tr><td class="mono">%s</td><td>%s</td><td class="n hot">%s</td></tr>'
        % (esc(code), esc(_okved_name(rows, code)), num(count))
        for code, count in top_counter(rows, "okved", 15)
    )
    city_rows = "".join(
        '<tr><td>%s</td><td class="n">%s</td><td class="dim">%.1f%%</td></tr>'
        % (esc(city), num(count), pct(count, total))
        for city, count in top_counter(rows, "city", 12)
    )

    table_rows = []
    for row in rows[:limit]:
        table_rows.append(
            '<tr>'
            '<td class="mono">%s</td>'
            '<td class="co">%s</td>'
            '<td class="mono">%s</td>'
            '<td>%s</td>'
            '<td class="n">%s</td>'
            '<td class="n">%s</td>'
            '<td class="nowrap">%s</td>'
            '<td class="nowrap">%s</td>'
            '<td class="nowrap">%s</td>'
            '</tr>' % (
                dim(row.get("inn")),
                dim(clean_html_tags(row.get("name")), "без названия"),
                dim(row.get("okved")),
                dim(row.get("city")),
                numd(row.get("employees")),
                numd(row.get("revenue")),
                yesno(row.get("has_contracts")),
                dim(category_label(row.get("category"))),
                yesno(row.get("is_new")),
            )
        )
    caption = ""
    if total > limit:
        caption = '<caption>Показаны первые %s из %s. Полный список лежит в базе, таблица %s.</caption>' % (
            num(limit), num(total), esc(table))

    body = """
<section>
  <h2><span class="num">01</span>Ядро выборки</h2>
  <p class="sub">Источник: реестр МСП, выгрузка report.xlsx одним запросом. Численность,
  признак госконтрактов и категория МСП приходят там же, платить за них не нужно.</p>
  %(stats)s
</section>

<section>
  <h2><span class="num">02</span>Срез по нише</h2>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>ОКВЭД</th><th>Вид деятельности</th><th class="r">Компаний</th></tr></thead>
    <tbody>%(okved)s</tbody>
  </table></div>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>Город</th><th class="r">Компаний</th><th>Доля</th></tr></thead>
    <tbody>%(cities)s</tbody>
  </table></div>
</section>

<section>
  <h2><span class="num">03</span>Компании</h2>
  <p class="sub">Почта в реестре заполняется предпринимателем добровольно, поэтому чаще
  всего пустая. Найдено с почтой: %(mails)s. Это нормально, контакты добываются на шаге обогащения.</p>
  <div class="scroller"><table class="tbl">
    %(caption)s
    <thead><tr>
      <th>ИНН</th><th>Компания</th><th>ОКВЭД</th><th>Город</th>
      <th class="r">Людей</th><th class="r">Выручка, тыс. руб.</th><th>Госконтракты</th>
      <th>Категория</th><th>Новая</th>
    </tr></thead>
    <tbody>%(rows)s</tbody>
  </table></div>
</section>
""" % {
        "stats": stats_block(cards),
        "okved": okved_rows or '<tr><td colspan="3" class="dim">нет данных</td></tr>',
        "cities": city_rows or '<tr><td colspan="3" class="dim">нет данных</td></tr>',
        "rows": "".join(table_rows) or '<tr><td colspan="9" class="dim">нет строк</td></tr>',
        "caption": caption,
        "mails": num(with_mail),
    }

    return write(out_dir, "sobrano.html", page(
        css,
        "Собрано: ядро выборки",
        "шаг 1 из 5",
        "Что <em>собрали</em>",
        "Полная выборка по нише из открытых реестров ФНС. Без ключей, без оплаты, без капчи.",
        [("компаний: %s" % num(total), True),
         ("с госконтрактами: %s" % num(contracts), False),
         ("вновь созданных: %s" % num(fresh), False),
         ("таблица: %s" % table, False)],
        body, css_ok))


def _okved_name(rows, code):
    for row in rows:
        if str(row.get("okved") or "").strip() == code and row.get("okved_name"):
            return clean_html_tags(row["okved_name"])
    return "нет расшифровки"


# ---------------------------------------------------------------- 2. отсев

OTSEV_FIELDS = ["inn", "name", "reason", "rule", "created_at"]


def render_otsev(db, out_dir, css, css_ok=True, limit=400):
    """Отчёт по бесплатному отсеву до обогащения. Главное здесь: воронка с числами."""
    drops, drop_table = db.load("otsev", OTSEV_FIELDS)
    total_collected = db.count("companies")

    if drops is None:
        body = no_table("otsev", "Отсев идёт до обогащения и не стоит ни одного запроса к сети.")
        return write(out_dir, "otsev.html", page(
            css, "Отсев", "шаг 2 из 5", "Кого <em>отсеяли</em>",
            "Таблица отсева пока не создана.", [], body, css_ok))

    dropped_inns = set(str(r.get("inn")) for r in drops if r.get("inn"))
    dropped_total = len(dropped_inns) if dropped_inns else len(drops)

    if total_collected is None:
        was = dropped_total
        left = None
        base_note = ("Таблицы companies нет, поэтому «было» посчитано по строкам отсева. "
                     "Соберите выборку, чтобы воронка стала полной.")
    else:
        was = total_collected
        left = max(total_collected - dropped_total, 0)
        base_note = ""

    by_reason = top_counter(drops, "reason", 50)

    funnel = ['<div class="funnel__row funnel__row--start">'
              '<div class="funnel__label">Было после выборки из реестра</div>'
              '<div class="funnel__track"><div class="funnel__bar" style="width:100%%"></div></div>'
              '<div class="funnel__num">%s<small>компаний</small></div></div>' % num(was)]

    for reason, count in by_reason:
        funnel.append(
            '<div class="funnel__row funnel__row--drop">'
            '<div class="funnel__label">%s</div>'
            '<div class="funnel__track"><div class="funnel__bar" style="width:%.1f%%"></div></div>'
            '<div class="funnel__num">−%s<small>%.1f%%</small></div></div>'
            % (esc(reason), max(pct(count, was), 1.0), num(count), pct(count, was))
        )

    if left is not None:
        funnel.append(
            '<div class="funnel__row funnel__row--keep">'
            '<div class="funnel__label">Осталось в работе</div>'
            '<div class="funnel__track"><div class="funnel__bar" style="width:%.1f%%"></div></div>'
            '<div class="funnel__num">%s<small>%.1f%% от старта</small></div></div>'
            % (max(pct(left, was), 1.0), num(left), pct(left, was))
        )

    reason_rows = "".join(
        '<tr><td>%s</td><td class="n bad">%s</td><td class="dim">%.1f%%</td></tr>'
        % (esc(reason), num(count), pct(count, was))
        for reason, count in by_reason
    )

    rule_rows = "".join(
        '<tr><td class="mono">%s</td><td class="n">%s</td></tr>' % (esc(rule), num(count))
        for rule, count in top_counter(drops, "rule", 20)
    )

    detail_rows = []
    for row in drops[:limit]:
        detail_rows.append(
            '<tr><td class="mono">%s</td><td class="co">%s</td><td>%s</td><td class="mono dim">%s</td></tr>'
            % (dim(row.get("inn")),
               dim(clean_html_tags(row.get("name")), "без названия"),
               dim(row.get("reason"), "причина не записана"),
               dim(row.get("rule"), "")))

    cards = [
        stat(numd(was), "было на входе"),
        stat(numd(dropped_total), "отсеяли бесплатно", "bad"),
        stat(numd(left) if left is not None else '<span class="dim">?</span>',
             "осталось в работе", "accent"),
        stat("%.0f%%" % pct(dropped_total, was) if was else "0%", "доля отсева"),
    ]

    note = ('<div class="note">%s</div>' % esc(base_note)) if base_note else ""

    body = """
<section>
  <h2><span class="num">01</span>Воронка отсева</h2>
  <p class="sub">Весь этот отсев происходит до единого платного или медленного запроса.
  Обогащать имеет смысл только тех, кто дошёл до низа.</p>
  %(stats)s
  %(note)s
  <div class="funnel">%(funnel)s</div>
</section>

<section>
  <h2><span class="num">02</span>Причины</h2>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>Причина</th><th class="r">Отсеяно</th><th>Доля от старта</th></tr></thead>
    <tbody>%(reasons)s</tbody>
  </table></div>
  %(rules)s
</section>

<section>
  <h2><span class="num">03</span>Кого отсеяли</h2>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>ИНН</th><th>Компания</th><th>Причина</th><th>Правило</th></tr></thead>
    <tbody>%(details)s</tbody>
  </table></div>
</section>
""" % {
        "stats": stats_block(cards),
        "note": note,
        "funnel": "".join(funnel),
        "reasons": reason_rows or '<tr><td colspan="3" class="dim">нет данных</td></tr>',
        "rules": ('<h3>По правилам</h3><div class="scroller"><table class="tbl">'
                  '<thead><tr><th>Правило</th><th class="r">Срабатываний</th></tr></thead>'
                  '<tbody>%s</tbody></table></div>' % rule_rows) if rule_rows else "",
        "details": "".join(detail_rows) or '<tr><td colspan="4" class="dim">нет строк</td></tr>',
    }

    chips = [("было: %s" % num(was), False),
             ("отсеяли: %s" % num(dropped_total), False)]
    if left is not None:
        chips.append(("осталось: %s" % num(left), True))
    chips.append(("причин: %s" % num(len(by_reason)), False))

    return write(out_dir, "otsev.html", page(
        css,
        "Отсев: воронка",
        "шаг 2 из 5",
        "Кого <em>отсеяли</em>",
        "Бесплатный отсев до обогащения. Сначала выбрасываем всех, кто не может быть клиентом, и только потом тратим запросы.",
        chips, body, css_ok))


# ---------------------------------------------------------------- 3. обогащение

ENRICH_FIELDS = ["inn", "name", "website", "email", "director", "position",
                 "revenue", "phone", "source", "confidence", "checked_at"]


def render_obogashchenie(db, out_dir, css, css_ok=True, limit=400):
    """Отчёт по обогащению: что нашли, откуда взяли и насколько этому верить."""
    rows, table = db.load("enrichment", ENRICH_FIELDS)
    if rows is None:
        body = no_table("enrichment", "Обогащение идёт по сайту компании, ЕГРЮЛ и ГИР БО.")
        return write(out_dir, "obogashchenie.html", page(
            css, "Обогащение", "шаг 3 из 5", "Что <em>нашли</em>",
            "Таблица обогащения пока не создана.", [], body, css_ok))

    rows = dedupe_by_inn(rows)
    total = len(rows)
    with_site = sum(1 for r in rows if r.get("website"))
    with_mail = sum(1 for r in rows if r.get("email"))
    with_fio = sum(1 for r in rows if r.get("director"))
    levels = {"high": 0, "mid": 0, "low": 0, "none": 0}
    for row in rows:
        levels[conf_level(row.get("confidence"))] += 1

    cards = [
        stat(numd(total), "компаний обогащено", "accent"),
        stat(numd(with_site), "нашли сайт"),
        stat(numd(with_mail), "нашли почту", "good" if with_mail else ""),
        stat(numd(with_fio), "нашли ФИО директора"),
        stat(numd(levels["high"]), "высокая уверенность", "good"),
        stat(numd(levels["low"] + levels["none"]), "низкая или без оценки", "bad"),
    ]

    src_rows = "".join(
        '<tr><td>%s</td><td class="n">%s</td><td class="dim">%.1f%%</td></tr>'
        % (esc(source), num(count), pct(count, total))
        for source, count in top_counter(rows, "source", 15)
    )

    data_rows = []
    for row in rows[:limit]:
        site = row.get("website")
        if site:
            href = site if str(site).startswith("http") else "https://" + str(site)
            site_html = '<a href="%s" rel="noreferrer noopener">%s</a>' % (esc(href), esc(site))
        else:
            site_html = '<span class="dim">нет сайта</span>'
        mail = row.get("email")
        mail_html = ('<a href="mailto:%s">%s</a>' % (esc(mail), esc(mail))) if mail \
            else '<span class="dim">нет почты</span>'
        source = row.get("source")
        source_html = ('<span class="badge badge--src">%s</span>' % esc(source)) if source \
            else '<span class="dim">не указан</span>'
        data_rows.append(
            '<tr>'
            '<td class="mono">%s</td>'
            '<td class="co">%s</td>'
            '<td class="wrapcell">%s</td>'
            '<td class="wrapcell">%s</td>'
            '<td class="wrapcell">%s</td>'
            '<td class="n">%s</td>'
            '<td>%s</td>'
            '<td>%s</td>'
            '</tr>' % (
                dim(row.get("inn")),
                dim(clean_html_tags(row.get("name")), "без названия"),
                site_html,
                mail_html,
                dim(row.get("director"), "нет ФИО"),
                numd(row.get("revenue")),
                source_html,
                conf_badge(row.get("confidence")),
            )
        )

    caption = ""
    if total > limit:
        caption = '<caption>Показаны первые %s из %s, таблица %s.</caption>' % (
            num(limit), num(total), esc(table))

    body = """
<section>
  <h2><span class="num">01</span>Что удалось достать</h2>
  <p class="sub">Каждая строка помечена источником и уверенностью. Письмо на данных с низкой
  уверенностью отправлять нельзя: один неверный факт убивает всё письмо.</p>
  %(stats)s
</section>

<section>
  <h2><span class="num">02</span>Источники</h2>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>Источник</th><th class="r">Записей</th><th>Доля</th></tr></thead>
    <tbody>%(sources)s</tbody>
  </table></div>
</section>

<section>
  <h2><span class="num">03</span>Таблица обогащения</h2>
  <div class="scroller"><table class="tbl">
    %(caption)s
    <thead><tr>
      <th>ИНН</th><th>Компания</th><th>Сайт</th><th>Почта</th>
      <th>Директор</th><th class="r">Выручка, тыс. руб.</th><th>Источник</th><th>Уверенность</th>
    </tr></thead>
    <tbody>%(rows)s</tbody>
  </table></div>
</section>
""" % {
        "stats": stats_block(cards),
        "sources": src_rows or '<tr><td colspan="3" class="dim">нет данных</td></tr>',
        "rows": "".join(data_rows) or '<tr><td colspan="8" class="dim">нет строк</td></tr>',
        "caption": caption,
    }

    return write(out_dir, "obogashchenie.html", page(
        css,
        "Обогащение: контакты и факты",
        "шаг 3 из 5",
        "Что <em>нашли</em>",
        "Сайт, почта, ФИО руководителя и выручка. У каждой находки виден источник и уровень доверия.",
        [("обогащено: %s" % num(total), True),
         ("с почтой: %s" % num(with_mail), False),
         ("высокая уверенность: %s" % num(levels["high"]), False)],
        body, css_ok))


# ---------------------------------------------------------------- 4. сигналы

SIGNAL_FIELDS = ["inn", "name", "signal", "detail", "severity", "source", "checked_at"]
SIGNAL_CO_FIELDS = ["inn", "name", "city", "employees", "website", "revenue"]


def render_signaly(db, out_dir, css, css_ok=True, limit=90):
    """Отчёт по диагностике боли: какие сигналы нашлись у каждой компании."""
    rows, table = db.load("signals", SIGNAL_FIELDS)
    if rows is None:
        body = no_table("signals", "Сигналы снимаются одним HTTP-запросом к сайту компании.")
        return write(out_dir, "signaly.html", page(
            css, "Сигналы", "шаг 4 из 5", "Что <em>сломано</em>",
            "Таблица сигналов пока не создана.", [], body, css_ok))

    companies, _ = db.load("companies", SIGNAL_CO_FIELDS)
    co_index = {}
    for row in (companies or []):
        if row.get("inn"):
            co_index[str(row["inn"])] = row

    grouped = {}
    for row in rows:
        key = str(row.get("inn") or "без ИНН")
        grouped.setdefault(key, []).append(row)

    total_signals = len(rows)
    total_co = len(grouped)
    hot = sum(1 for r in rows if conf_level(r.get("severity")) == "high")

    cards = [
        stat(numd(total_co), "компаний с сигналами", "accent"),
        stat(numd(total_signals), "сигналов всего"),
        stat(numd(hot), "сильных сигналов", "bad" if hot else ""),
        stat("%.1f" % (float(total_signals) / total_co) if total_co else "0",
             "сигналов на компанию"),
    ]

    freq_rows = "".join(
        '<tr><td>%s</td><td class="n hot">%s</td><td class="dim">%.1f%%</td></tr>'
        % (esc(signal), num(count), pct(count, total_co))
        for signal, count in top_counter(rows, "signal", 20)
    )

    ordered = sorted(
        grouped.items(),
        key=lambda kv: (-sum(1 for r in kv[1] if conf_level(r.get("severity")) == "high"),
                        -len(kv[1])))

    cards_html = []
    for index, (inn, items) in enumerate(ordered[:limit]):
        company = co_index.get(inn, {})
        name = clean_html_tags(items[0].get("name") or company.get("name")) or "без названия"
        chips = "".join(sev_chip(item.get("signal") or "сигнал", item.get("severity"))
                        for item in items)
        facts = []
        for item in items:
            detail = item.get("detail")
            if not detail:
                continue
            css_class = " class=\"warn\"" if conf_level(item.get("severity")) == "high" else ""
            facts.append("<li%s>%s</li>" % (css_class, esc(clean_html_tags(detail))))
        foot = []
        if company.get("city"):
            foot.append("<span>%s</span>" % esc(company["city"]))
        if num(company.get("employees")) is not None:
            foot.append("<span>%s %s</span>" % (
                num(company["employees"]),
                plural(company["employees"], "человек", "человека", "человек")))
        site = company.get("website")
        if site:
            href = site if str(site).startswith("http") else "https://" + str(site)
            foot.append('<a href="%s" rel="noreferrer noopener">%s</a>' % (esc(href), esc(site)))
        sources = sorted(set(str(i.get("source")) for i in items if i.get("source")))
        if sources:
            foot.append("<span>источник: %s</span>" % esc(", ".join(sources)))

        cards_html.append(
            '<div class="co-card%(top)s">%(rank)s'
            '<div class="co-card__name">%(name)s</div>'
            '<div class="co-card__inn">ИНН %(inn)s</div>'
            '<div class="co-card__meta">%(chips)s</div>'
            '<ul class="co-card__facts">%(facts)s</ul>'
            '<div class="co-card__foot">%(foot)s</div>'
            '</div>' % {
                "top": " top" if index < 3 else "",
                "rank": ('<div class="co-card__rank">%d место</div>' % (index + 1)) if index < 3 else "",
                "name": esc(name),
                "inn": esc(inn),
                "chips": chips,
                "facts": "".join(facts) or '<li>подробности не записаны</li>',
                "foot": "".join(foot) or "<span>нет карточки в companies</span>",
            })

    tail = ""
    if len(ordered) > limit:
        tail = '<div class="note">Показаны %s компаний из %s, остальные лежат в таблице %s.</div>' % (
            num(limit), num(len(ordered)), esc(table))

    body = """
<section>
  <h2><span class="num">01</span>Диагностика</h2>
  <p class="sub">Сигнал, это не «нам кажется». Это то, что видно снаружи одним запросом:
  нет Яндекс.Метрики, протухший копирайт, нет адаптива, нет формы, медленный ответ.</p>
  %(stats)s
</section>

<section>
  <h2><span class="num">02</span>Частота сигналов</h2>
  <div class="scroller"><table class="tbl">
    <thead><tr><th>Сигнал</th><th class="r">Компаний</th><th>Доля</th></tr></thead>
    <tbody>%(freq)s</tbody>
  </table></div>
</section>

<section>
  <h2><span class="num">03</span>Компании</h2>
  <div class="cards">%(cards)s</div>
  %(tail)s
</section>
""" % {
        "stats": stats_block(cards),
        "freq": freq_rows or '<tr><td colspan="3" class="dim">нет данных</td></tr>',
        "cards": "".join(cards_html) or empty_block("пусто", "Ни одного сигнала не записано."),
        "tail": tail,
    }

    return write(out_dir, "signaly.html", page(
        css,
        "Сигналы: диагностика боли",
        "шаг 4 из 5",
        "Что <em>сломано</em>",
        "Проверяемые признаки, из которых потом собирается письмо. Каждый снят снаружи и его можно показать клиенту.",
        [("компаний: %s" % num(total_co), True),
         ("сигналов: %s" % num(total_signals), False),
         ("сильных: %s" % num(hot), False)],
        body, css_ok))


# ---------------------------------------------------------------- 5. письма

LETTER_FIELDS = ["inn", "name", "to_email", "subject", "body", "facts", "status", "created_at"]


def render_pisma(db, out_dir, css, css_ok=True, limit=60):
    """Отчёт по письмам: тема, тело и факты, на которых письмо построено."""
    rows, table = db.load("letters", LETTER_FIELDS)
    if rows is None:
        body = no_table("letters", "Письма собираются из сигналов и уходят в черновики почты.")
        return write(out_dir, "pisma.html", page(
            css, "Письма", "шаг 5 из 5", "Черновики <em>писем</em>",
            "Таблица писем пока не создана.", [], body, css_ok))

    rows = dedupe_by_inn(rows)
    total = len(rows)
    with_mail = sum(1 for r in rows if r.get("to_email"))
    fact_counts = [len(split_facts(r.get("facts"))) for r in rows]
    avg_facts = (sum(fact_counts) / float(len(fact_counts))) if fact_counts else 0
    no_facts = sum(1 for c in fact_counts if c == 0)

    cards = [
        stat(numd(total), "черновиков готово", "accent"),
        stat(numd(with_mail), "с адресом получателя"),
        stat("%.1f" % avg_facts, "фактов на письмо", "good" if avg_facts >= 2 else ""),
        stat(numd(no_facts), "писем без фактов", "bad" if no_facts else ""),
    ]

    status_rows = "".join(
        '<tr><td>%s</td><td class="n">%s</td></tr>' % (esc(status), num(count))
        for status, count in top_counter(rows, "status", 10)
    )

    mails_html = []
    for row in rows[:limit]:
        facts = split_facts(row.get("facts"))
        facts_html = "".join("<li>%s</li>" % esc(clean_html_tags(f)) for f in facts)
        if not facts_html:
            facts_html = '<li>факты не записаны, письмо отправлять рано</li>'
        foot = []
        if row.get("inn"):
            foot.append("<span>ИНН %s</span>" % esc(row["inn"]))
        if row.get("status"):
            foot.append("<span>статус: %s</span>" % esc(row["status"]))
        if row.get("created_at"):
            foot.append("<span>%s</span>" % esc(row["created_at"]))
        foot.append("<span>%s %s</span>" % (
            len(facts), plural(len(facts), "факт", "факта", "фактов")))

        mails_html.append(
            '<article class="mail">'
            '<div class="mail__head">'
            '<div class="mail__to">кому: %(to)s &nbsp;|&nbsp; %(co)s</div>'
            '<h3 class="mail__subj">%(subj)s</h3>'
            '</div>'
            '<div class="mail__body">%(body)s</div>'
            '<div class="mail__facts"><h4>На чём построено письмо</h4><ul>%(facts)s</ul></div>'
            '<div class="mail__foot">%(foot)s</div>'
            '</article>' % {
                "to": esc(row.get("to_email") or "адрес не найден"),
                "co": esc(clean_html_tags(row.get("name")) or "компания не указана"),
                "subj": esc(row.get("subject") or "без темы"),
                "body": esc(row.get("body") or "тело письма пустое"),
                "facts": facts_html,
                "foot": "".join(foot),
            })

    tail = ""
    if total > limit:
        tail = '<div class="note">Показаны %s писем из %s, остальные в таблице %s.</div>' % (
            num(limit), num(total), esc(table))

    body_html = """
<section>
  <h2><span class="num">01</span>Пачка черновиков</h2>
  <p class="sub">Ни одно письмо не уходит само. Скрипт кладёт черновики в почту,
  отправляет человек, глазами проверив факты.</p>
  %(stats)s
  %(statuses)s
</section>

<section>
  <h2><span class="num">02</span>Письма</h2>
  <div class="mails">%(mails)s</div>
  %(tail)s
</section>
""" % {
        "stats": stats_block(cards),
        "statuses": ('<div class="scroller"><table class="tbl">'
                     '<thead><tr><th>Статус</th><th class="r">Писем</th></tr></thead>'
                     '<tbody>%s</tbody></table></div>' % status_rows) if status_rows else "",
        "mails": "".join(mails_html) or empty_block("пусто", "Ни одного черновика не создано."),
        "tail": tail,
    }

    return write(out_dir, "pisma.html", page(
        css,
        "Письма: черновики",
        "шаг 5 из 5",
        "Черновики <em>писем</em>",
        "Каждое письмо построено на проверяемых фактах и написано под конкретную компанию. Отправку подтверждает человек.",
        [("писем: %s" % num(total), True),
         ("с адресом: %s" % num(with_mail), False),
         ("фактов на письмо: %.1f" % avg_facts, False)],
        body_html, css_ok))


# ---------------------------------------------------------------- демо-данные

DEMO_COMPANIES = [
    # (инн, огрн, название, оквэд, расшифровка, город, людей, выручка, ГК, категория, новая, сайт)
    ("1600000101", "1160000000101", 'ООО «ДЕМО МЕБЕЛЬ ПЛЮС»', "16.29",
     "Производство изделий из дерева", "Казань", 64, 380000, 0, 2, 0, "demo-mebel.example"),
    ("1600000102", "1160000000102", 'ООО «ДЕМО ГОФРОТАРА»', "22.22",
     "Производство пластмассовых изделий для упаковки", "Набережные Челны", 118, 910000, 1, 3, 0, "demo-gofra.example"),
    ("1600000103", "1160000000103", 'АО «ДЕМО КИРПИЧНЫЙ ЗАВОД»', "23.32",
     "Производство кирпича и керамических материалов", "Альметьевск", 210, 1450000, 1, 3, 0, "demo-kirpich.example"),
    ("1600000104", "1160000000104", 'ООО «ДЕМО МЕТИЗЫ»', "25.94",
     "Производство крепёжных изделий", "Зеленодольск", 41, 190000, 0, 2, 0, "demo-metiz.example"),
    ("1600000105", "1160000000105", 'ООО «ДЕМО ОПТ ПРОДУКТЫ»', "46.31",
     "Торговля оптовая фруктами и овощами", "Казань", 87, 640000, 0, 2, 0, None),
    ("1600000106", "1160000000106", 'ООО «ДЕМО ХЛЕБ»', "10.71",
     "Производство хлеба и хлебобулочных изделий", "Нижнекамск", 76, 310000, 0, 2, 1, "demo-hleb.example"),
    ("1600000107", "1160000000107", 'ООО «ДЕМО ТРАНС ЛОГИСТИКА»', "49.41",
     "Деятельность автомобильного грузового транспорта", "Казань", 150, 720000, 0, 3, 0, "demo-trans.example"),
    ("1600000108", "1160000000108", 'ИП ДЕМОВ ДЕМО ДЕМОВИЧ', "46.90",
     "Торговля оптовая неспециализированная", "Чистополь", 2, 8000, 0, 1, 1, None),
    ("1600000109", "1160000000109", 'ООО «ДЕМО ХИМЗАВОД»', "20.30",
     "Производство красок и лаков", "Казань", 320, 2900000, 1, 3, 0, "demo-him.example"),
    ("1600000110", "1160000000110", 'ООО «ДЕМО ПРОФИЛЬ»', "25.11",
     "Производство строительных металлических конструкций", "Елабуга", 95, 830000, 0, 2, 0, "demo-profil.example"),
]

DEMO_OTSEV = [
    ("1600000107", 'ООО «ДЕМО ТРАНС ЛОГИСТИКА»', "есть ОКВЭД 49.41, свой автопарк", "okved_49_41"),
    ("1600000108", "ИП ДЕМОВ ДЕМО ДЕМОВИЧ", "численность 2 человека, меньше нижней вилки", "chislennost_min"),
    ("1600000109", 'ООО «ДЕМО ХИМЗАВОД»', "выручка 2.9 млрд, выше верхней вилки", "vyruchka_max"),
    ("1600000104", 'ООО «ДЕМО МЕТИЗЫ»', "вакансии водителей категории E на hh, вероятно свой автопарк", "vakansii_voditel"),
    ("1600000105", 'ООО «ДЕМО ОПТ ПРОДУКТЫ»', "сайт не найден, писать некуда", "net_sajta"),
]

DEMO_ENRICH = [
    ("1600000101", 'ООО «ДЕМО МЕБЕЛЬ ПЛЮС»', "demo-mebel.example", "zakaz@demo-mebel.example",
     "Директор: Демов Игорь Петрович", 380000, "сайт, страница /contacts/", "высокая"),
    ("1600000102", 'ООО «ДЕМО ГОФРОТАРА»', "demo-gofra.example", "sales@demo-gofra.example",
     "Генеральный директор: Демова Анна Сергеевна", 910000, "egrul.nalog.ru + сайт", "высокая"),
    ("1600000103", "АО «ДЕМО КИРПИЧНЫЙ ЗАВОД»", "demo-kirpich.example", "info@demo-kirpich.example",
     "Генеральный директор: Демин Рустам Ильдарович", 1450000, "egrul.nalog.ru", "средняя"),
    ("1600000106", 'ООО «ДЕМО ХЛЕБ»', "demo-hleb.example", None,
     "Директор: Демьянов Марат Наилевич", 310000, "egrul.nalog.ru", "средняя"),
    ("1600000110", 'ООО «ДЕМО ПРОФИЛЬ»', "demo-profil.example", "opt@demo-profil.example",
     None, 830000, "почта угадана по шаблону домена", "низкая"),
]

DEMO_SIGNALS = [
    ("1600000101", 'ООО «ДЕМО МЕБЕЛЬ ПЛЮС»', "нет Яндекс.Метрики",
     "На главной нет ни mc.yandex.ru, ни вызова ym(). Заявки никто не считает.", "высокая", "html сайта"),
    ("1600000101", 'ООО «ДЕМО МЕБЕЛЬ ПЛЮС»', "копирайт 2021",
     "В футере стоит © 2021, сайт не обновляли три года.", "средняя", "html сайта"),
    ("1600000102", 'ООО «ДЕМО ГОФРОТАРА»', "нет формы заявки",
     "На сайте нет ни одного тега form, оставить заявку нельзя.", "высокая", "html сайта"),
    ("1600000102", 'ООО «ДЕМО ГОФРОТАРА»', "медленный ответ",
     "Первый байт пришёл за 4.1 секунды при замере с обычного канала.", "средняя", "http-замер"),
    ("1600000103", "АО «ДЕМО КИРПИЧНЫЙ ЗАВОД»", "нет адаптива",
     "Отсутствует meta viewport, на телефоне страница не масштабируется.", "высокая", "html сайта"),
    ("1600000103", "АО «ДЕМО КИРПИЧНЫЙ ЗАВОД»", "есть госконтракты",
     "Флаг has_contracts в реестре МСП равен 1, компания работает с госзаказом.", "низкая", "реестр МСП"),
    ("1600000106", 'ООО «ДЕМО ХЛЕБ»', "нет SSL",
     "Сертификат не отдаётся, браузер показывает предупреждение.", "высокая", "tcp/tls-замер"),
    ("1600000110", 'ООО «ДЕМО ПРОФИЛЬ»', "нет Яндекс.Метрики",
     "Счётчик не найден в исходном html.", "высокая", "html сайта"),
    ("1600000110", 'ООО «ДЕМО ПРОФИЛЬ»', "копирайт 2020",
     "В футере © 2020, за пять лет страницу не трогали.", "средняя", "html сайта"),
]

# Это тексты одной конкретной демо-ниши (транспортная компания ищет
# производителей и оптовиков, которым нужны перевозки), а не шаблон
# продукта. Продукт универсальный: конвейер холодной лидогенерации для
# любого B2B. Реальный шаблон письма живёт в scripts/write_letter.py
# (DEFAULT_TEMPLATE), а оффер и вопрос в конце подставляются из
# config.json, блок pisma, поля offer и cta.
DEMO_LETTERS = [
    ("1600000101", 'ООО «ДЕМО МЕБЕЛЬ ПЛЮС»', "zakaz@demo-mebel.example",
     "Игорь Петрович, про доставку мебели из Казани",
     "Игорь Петрович, здравствуйте.\n\n"
     "Смотрел сайт demo-mebel.example. Вижу производство мебели в Казани, 64 человека в штате.\n\n"
     "Заметил две вещи. На сайте нет счётчика Метрики, то есть заявки с сайта никто не считает. "
     "И копирайт в футере стоит 2021 года.\n\n"
     "Мы возим сборные и отдельные машины по Татарстану и в Москву. Если сейчас доставку тянет "
     "своя газель или наёмные частники, могу прислать расчёт по вашим типовым маршрутам.\n\n"
     "Нужен расчёт? Отвечу цифрами в одном письме.\n\n"
     "Дмитрий, транспортная компания",
     json.dumps(["производство мебели, ОКВЭД 16.29",
                 "64 человека по данным реестра МСП",
                 "нет Яндекс.Метрики на сайте",
                 "копирайт в футере 2021",
                 "нет собственного ОКВЭД 49.41, автопарка нет"], ensure_ascii=False),
     "черновик"),
    ("1600000102", 'ООО «ДЕМО ГОФРОТАРА»', "sales@demo-gofra.example",
     "Анна Сергеевна, гофротара и логистика по РТ",
     "Анна Сергеевна, здравствуйте.\n\n"
     "У вас упаковочное производство в Набережных Челнах, 118 человек, по реестру МСП среднее предприятие.\n\n"
     "На сайте demo-gofra.example нет формы заявки, а страница отдаётся за 4.1 секунды. "
     "Клиент с телефона до заявки просто не доходит.\n\n"
     "По логистике: возим паллеты и объёмный груз, у гофры главная проблема это объём при малом весе. "
     "Считаем по кубам, а не по тоннам, так дешевле.\n\n"
     "Прислать расчёт по двум вашим типовым направлениям?\n\n"
     "Дмитрий, транспортная компания",
     json.dumps(["упаковочное производство, ОКВЭД 22.22",
                 "118 человек, категория среднее",
                 "нет формы заявки на сайте",
                 "время до первого байта 4.1 секунды"], ensure_ascii=False),
     "черновик"),
    ("1600000103", "АО «ДЕМО КИРПИЧНЫЙ ЗАВОД»", "info@demo-kirpich.example",
     "Рустам Ильдарович, перевозки кирпича из Альметьевска",
     "Рустам Ильдарович, здравствуйте.\n\n"
     "Ваш сайт не открывается нормально с телефона: в коде нет meta viewport, страница не масштабируется. "
     "Для завода это не критично, но заявки с мобильных вы теряете.\n\n"
     "Пишу по другому поводу. Вы работаете с госзаказом, значит есть жёсткие сроки поставки. "
     "Мы возим стройматериалы по РТ и в соседние регионы, подаём машину в день заявки.\n\n"
     "Скинуть тариф на самосвал и на тентованную фуру по вашим маршрутам?\n\n"
     "Дмитрий, транспортная компания",
     json.dumps(["производство кирпича, ОКВЭД 23.32",
                 "210 человек в штате",
                 "признак госконтрактов в реестре МСП",
                 "нет meta viewport, сайт не адаптивен"], ensure_ascii=False),
     "черновик"),
]

DEMO_SCHEMA = """
CREATE TABLE companies(
  inn TEXT PRIMARY KEY, ogrn TEXT, name TEXT, okved TEXT, okved_name TEXT,
  region TEXT, city TEXT, employees INTEGER, revenue INTEGER,
  has_contracts INTEGER, category INTEGER, is_new INTEGER, website TEXT,
  email TEXT, collected_at TEXT);
CREATE TABLE otsev(inn TEXT, name TEXT, reason TEXT, rule TEXT, dropped_at TEXT);
CREATE TABLE enrichment(inn TEXT, name TEXT, website TEXT, email TEXT, director TEXT,
  revenue INTEGER, source TEXT, confidence TEXT, checked_at TEXT);
CREATE TABLE signals(inn TEXT, name TEXT, signal TEXT, detail TEXT,
  severity TEXT, source TEXT, checked_at TEXT);
CREATE TABLE letters(inn TEXT, name TEXT, to_email TEXT, subject TEXT,
  body TEXT, facts TEXT, status TEXT, created_at TEXT);
"""


def build_demo_db(path):
    """Создаёт демо-базу с вымышленными компаниями. Реальных данных здесь нет."""
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    if os.path.isfile(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(DEMO_SCHEMA)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    con.executemany(
        "INSERT INTO companies(inn,ogrn,name,okved,okved_name,region,city,employees,"
        "revenue,has_contracts,category,is_new,website,email,collected_at) "
        "VALUES(?,?,?,?,?,'16',?,?,?,?,?,?,?,NULL,?)",
        [(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11], stamp)
         for c in DEMO_COMPANIES])
    con.executemany(
        "INSERT INTO otsev(inn,name,reason,rule,dropped_at) VALUES(?,?,?,?,?)",
        [(d[0], d[1], d[2], d[3], stamp) for d in DEMO_OTSEV])
    con.executemany(
        "INSERT INTO enrichment(inn,name,website,email,director,revenue,source,confidence,checked_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], stamp) for e in DEMO_ENRICH])
    con.executemany(
        "INSERT INTO signals(inn,name,signal,detail,severity,source,checked_at) VALUES(?,?,?,?,?,?,?)",
        [(s[0], s[1], s[2], s[3], s[4], s[5], stamp) for s in DEMO_SIGNALS])
    con.executemany(
        "INSERT INTO letters(inn,name,to_email,subject,body,facts,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(l[0], l[1], l[2], l[3], l[4], l[5], l[6], stamp) for l in DEMO_LETTERS])
    con.commit()
    con.close()
    return path


# ---------------------------------------------------------------- запуск

RENDERERS = [
    ("sobrano", render_sobrano),
    ("otsev", render_otsev),
    ("obogashchenie", render_obogashchenie),
    ("signaly", render_signaly),
    ("pisma", render_pisma),
]


def render_all(db_path, out_dir, only=None):
    """Собирает все отчёты. Возвращает список путей."""
    css, css_ok = find_css()
    if not css_ok:
        sys.stderr.write("ВНИМАНИЕ: " + CSS_WARNING + "\n")
    db = Db(db_path)
    if db.con is None:
        sys.stderr.write("ВНИМАНИЕ: база %s не найдена, отчёты будут пустыми.\n" % db_path)
    paths = []
    for name, func in RENDERERS:
        if only and name != only:
            continue
        paths.append(func(db, out_dir, css, css_ok))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Генератор HTML-отчётов конвейера «AI-отдел продаж».")
    parser.add_argument("--db", default="data/leads.db", help="путь к sqlite-базе конвейера")
    parser.add_argument("--out", default="out", help="папка для готовых HTML")
    parser.add_argument("--only", choices=[n for n, _ in RENDERERS],
                        help="собрать только один отчёт")
    parser.add_argument("--demo", action="store_true",
                        help="создать демо-базу с вымышленными компаниями и собрать отчёты по ней")
    args = parser.parse_args(argv)

    db_path = args.db
    if args.demo:
        db_path = os.path.join(args.out, "demo.db")
        build_demo_db(db_path)
        print("Демо-база собрана: %s" % db_path)

    paths = render_all(db_path, args.out, args.only)
    print("Готово, отчётов: %d" % len(paths))
    for path in paths:
        print("  " + os.path.abspath(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
