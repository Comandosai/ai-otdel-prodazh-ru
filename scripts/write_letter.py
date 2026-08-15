# -*- coding: utf-8 -*-
"""
write_letter.py: сборка контекста для письма и проверка готового письма.

Текст письма пишет Claude через скилл. Этот модуль отвечает за две вещи,
которые нельзя доверять языковой модели:

  build_letter_context(inn)  собрать ВСЕ проверенные факты с источниками
  validate_letter(text, ctx) не выпустить письмо с плейсхолдером или выдумкой

Правило простое: в контекст попадают только факты, прошедшие фильтр из
verify.py. Что не прошло, лежит рядом в flagged и в промпт не подставляется.
Модель физически не видит непроверенных фактов, поэтому не может их упомянуть.

Python 3.9, только стандартная библиотека.
"""

import os
import re
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify  # noqa: E402
from verify import (  # noqa: E402
    canonical_record, load_company, registry_facts, make_fact, gate_facts,
    conflicts_report, verify_inn_match, verify_domain_match,
    first_name_patronymic, director_name, norm_digits,
    SOURCE_SITE, DEFAULT_THRESHOLD,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Конфиг ищется по цепочке: переменная AIOP_CONFIG, потом config.json рядом
# со скриптами (корень репозитория или установленный пак), потом стандартное
# место установки. Так один и тот же скрипт работает и из клона репозитория,
# и после install.sh из любой рабочей папки.
INSTALLED_PACK = os.path.join(os.path.expanduser("~"), ".claude", "tools", "prodazhi")
CONFIG_CANDIDATES = (
    os.path.join(ROOT, "config.json"),
    os.path.join(os.getcwd(), "config.json"),
    os.path.join(INSTALLED_PACK, "config.json"),
)
CONFIG_PATH = CONFIG_CANDIDATES[0]


def find_config():
    """Путь к рабочему config.json. Файла может не быть, это не падение."""
    env = os.environ.get("AIOP_CONFIG")
    if env:
        return env
    for path in CONFIG_CANDIDATES:
        if os.path.isfile(path):
            return path
    return CONFIG_CANDIDATES[0]

# Штампы, по которым письмо сразу читается как рассылка.
CLICHES = (
    "динамично развива", "индивидуальный подход", "взаимовыгодн",
    "гибкая система скидок", "широкий спектр", "команда профессионалов",
    "лидер рынка", "надеемся на плодотворное", "коммерческое предложение",
    "не упустите", "спешите", "уникальное предложение",
)

# Следы незаполненного шаблона.
PLACEHOLDER_PATTERNS = (
    (r'\[[^\]\n]{1,60}\]', "квадратные скобки"),
    (r'\{\{[^}\n]{1,60}\}\}', "двойные фигурные скобки"),
    (r'(?<!\{)\{[A-Za-zА-Яа-я_][\w.]{0,40}\}(?!\})', "фигурные скобки"),
    (r'<[А-Яа-я][^>\n]{0,40}>', "угловые скобки"),
    (r'\bXXX+\b', "XXX"),
    (r'_{3,}', "подчёркивания"),
    (r'\bИМЯ\b|\bНАЗВАНИЕ\b|\bКОМПАНИЯ\b', "заглушка в верхнем регистре"),
)

DEFAULT_TEMPLATE = u"""Тема: {{subject}}

Здравствуйте, {{greeting}}.

Смотрел реестр по вашей отрасли: {{company.okved_name}}, {{company.city}}, \
в штате {{company.employees}} человек.

{{pain_line}}

Мы возим грузы по Татарстану и в соседние регионы, машины свои. \
Если сейчас логистику закрывает подрядчик, могу посчитать ваш маршрут \
и прислать цифры для сравнения, без обязательств.

Ответьте одной строкой, какое направление возите чаще всего, и я пришлю расчёт.

{{sender.signature}}

{{sender.unsubscribe}}
"""

# Отписка. Она обязана быть в каждом письме: ответ словом «стоп» заносит ИНН
# в таблицу stoplist через scripts/stoplist.py, и компания больше не попадёт
# ни в письма, ни в черновики.
DEFAULT_UNSUBSCRIBE = (
    u"Если писать не нужно, ответьте одним словом «стоп», и я больше не напишу.")

DEFAULT_SENDER = {
    "name": "",
    "company": "",
    "phone": "",
    "email": "",
    "site": "",
    "signature": "",
    "unsubscribe": DEFAULT_UNSUBSCRIBE,
}


# ----------------------------------------------------------------------------
# Контекст
# ----------------------------------------------------------------------------

def normalize_sender(sender=None):
    """Достроить подпись и отписку, чтобы в письме не осталось пустых слотов.

    Шаблон подставляет ровно два поля: signature и unsubscribe. Отдельные
    name, company, phone нужны только для того, чтобы собрать из них подпись,
    если готовая подпись в конфиге не задана.
    """
    out = dict(DEFAULT_SENDER)
    for key, value in (sender or {}).items():
        if value not in (None, "", []):
            out[key] = value
    if not out.get("signature"):
        parts = [out.get("name"), out.get("company"), out.get("phone")]
        out["signature"] = "\n".join(
            str(p).strip() for p in parts if p not in (None, "", []))
    if not out.get("unsubscribe"):
        out["unsubscribe"] = DEFAULT_UNSUBSCRIBE
    return out


def load_sender(config_path=None):
    """Подпись отправителя из config.json.

    Основной вид конфига описан в config.example.json, блок «pisma»:
    ot_kogo_imya, ot_kogo_kompaniya, ot_kogo_telefon, podpis, otpiska.
    Короткий английский блок «sender» тоже понимается, он удобнее в тестах.

    Если подпись в конфиге не заполнена, signature останется пустой, слот
    в шаблоне не закроется и валидатор не выпустит письмо. Это нарочно:
    лучше остановить прогон, чем разослать письма с чужой подписью.
    """
    path = config_path or os.environ.get("AIOP_CONFIG") or find_config()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return dict(DEFAULT_SENDER)
    sender = dict(DEFAULT_SENDER)
    pisma = cfg.get("pisma") or {}
    if pisma.get("ot_kogo_imya"):
        sender["name"] = pisma["ot_kogo_imya"]
    if pisma.get("ot_kogo_kompaniya"):
        sender["company"] = pisma["ot_kogo_kompaniya"]
    if pisma.get("ot_kogo_telefon"):
        sender["phone"] = pisma["ot_kogo_telefon"]
    if pisma.get("podpis"):
        sender["signature"] = str(pisma["podpis"]).replace("\\n", "\n")
    if pisma.get("otpiska"):
        sender["unsubscribe"] = str(pisma["otpiska"]).replace("\\n", "\n")
    pochta = cfg.get("pochta") or {}
    if pochta.get("login"):
        sender["email"] = pochta["login"]
    sender.update({k: v for k, v in (cfg.get("sender") or {}).items()
                   if v not in (None, "")})
    return normalize_sender(sender)


def build_letter_context(inn, record=None, pains=None, threshold=DEFAULT_THRESHOLD,
                         live=False, db_path=None, data_dir=None, sender=None,
                         config_path=None):
    """Собрать всё, что можно подставить в промпт письма.

    Возвращает словарь с проверенными фактами, их источниками, болями,
    расхождениями и готовыми текстовыми блоками для промпта.

    live=True дополнительно сходит на сайт компании и снимет диагностику.
    По умолчанию False, чтобы сборка контекста была быстрой и повторяемой.
    """
    inn = norm_digits(inn)
    stored = load_company(inn, db_path=db_path, data_dir=data_dir)
    merged = dict(stored)
    for key, value in (record or {}).items():
        if value not in (None, "", []):
            merged[key] = value
    company = canonical_record(merged)
    company.setdefault("inn", inn)

    site = company.get("site") or company.get("www")
    pains = list(pains or company.get("pains") or [])

    if live:
        enriched, diag = _live_lookup(company)
        for key, value in (enriched or {}).items():
            if value not in (None, "", []) and key not in ("sources",):
                company.setdefault(key, value)
        if enriched and enriched.get("site"):
            site = enriched["site"]
            company["site"] = site
        if diag:
            company["metrics"] = diag["metrics"]
            pains = diag["pains"] + pains

    if not pains and company.get("metrics"):
        pains = _pains_from_metrics(company["metrics"], company)
    if not pains:
        pains = load_pains_from_db(inn, db_path=db_path)

    # ---- факты
    facts = registry_facts(company)
    site_source = company.get("_sources", {}) if isinstance(
        company.get("_sources"), dict) else {}
    if site:
        facts.append(make_fact("site", site, SOURCE_SITE, source_url=site,
                               note="сайт компании"))
    if company.get("email"):
        facts.append(make_fact("email", company["email"], SOURCE_SITE,
                               source_url=site_source.get("email") or site,
                               note="почта с сайта"))
    if company.get("phone"):
        facts.append(make_fact("phone", company["phone"], SOURCE_SITE,
                               source_url=site_source.get("phone") or site,
                               note="телефон с сайта"))
    if company.get("inn_on_site"):
        facts.append(make_fact("inn_on_site", company["inn_on_site"], SOURCE_SITE,
                               source_url=site, note="ИНН, найденный на сайте"))
    for pain in pains:
        facts.append(make_fact(
            pain.get("field") or ("pain:" + str(pain.get("code"))),
            pain.get("evidence") or pain.get("text"),
            pain.get("source_type") or SOURCE_SITE,
            source_url=pain.get("source_url"),
            note=pain.get("text"), confidence=pain.get("confidence"),
            text=pain.get("text")))

    usable, flagged = gate_facts(facts, threshold=threshold)
    usable_fields = set(f["field"] for f in usable)
    usable_pains = [p for p in pains
                    if (p.get("field") or ("pain:" + str(p.get("code")))) in usable_fields]

    # ---- проверки
    inn_conf = verify_inn_match(company.get("inn_on_site"), company)
    domain_ok = verify_domain_match(site, company) if site else False
    conflicts = conflicts_report(company)
    blockers = [c for c in conflicts if c["severity"] == "high"]

    # Стоп-лист сильнее любых фактов: попросили не писать, значит не пишем.
    stop_reason = verify.stoplist_reason(
        inn, email=company.get("email"), db_path=db_path,
        config_path=config_path or find_config())
    if stop_reason:
        blockers = list(blockers) + [{
            "severity": "high", "code": "stoplist",
            "text": "ИНН в стоп-листе: %s" % stop_reason,
        }]

    fio = director_name(company.get("director_fio"))
    greeting = first_name_patronymic(fio) or "коллеги"

    context = {
        "inn": inn,
        "generated_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "threshold": threshold,
        "company": _public_company(company),
        "director": {"fio": fio, "post": company.get("director_post"),
                     "greeting": greeting},
        "greeting": greeting,
        "contacts": {"email": company.get("email"), "phone": company.get("phone"),
                     "site": site},
        "site": site,
        "facts": usable,
        "flagged_facts": flagged,
        "all_facts": facts,
        "pains": usable_pains,
        "pain_line": _as_sentence(usable_pains[0]["text"]) if usable_pains else "",
        "conflicts": conflicts,
        "blockers": blockers,
        "blocked": bool(blockers),
        "verification": {
            "inn_match": inn_conf,
            "domain_match": domain_ok,
            "identified_by": ("ИНН на сайте совпал с реестром" if inn_conf >= 1.0
                              else "только по названию домена" if domain_ok
                              else "сайт не подтверждён"),
        },
        "sender": normalize_sender(sender) if sender else load_sender(config_path),
        "stoplist": stop_reason,
        "subject": _subject(company, usable_pains),
    }
    context["facts_block"] = facts_block(usable)
    context["pains_block"] = pains_block(usable_pains)
    context["flagged_block"] = facts_block(flagged, show_reason=True)
    return context


def _public_company(company):
    """Поля компании, которые можно подставлять в текст."""
    keys = ("inn", "ogrn", "name", "okved", "okved_name", "city", "area",
            "region", "employees", "revenue", "revenue_period", "has_contracts",
            "category", "dtregistry", "is_new", "address")
    out = {}
    for key in keys:
        value = company.get(key)
        if value not in (None, "", []):
            out[key] = value
    out["short_name"] = short_name(company.get("name"))
    return out


def short_name(name):
    """'АКЦИОНЕРНОЕ ОБЩЕСТВО "ЧЕЛНЫ-ХЛЕБ"' -> 'Челны-хлеб'."""
    if not name:
        return None
    quoted = re.findall(r'[«"\']([^«»"\']{2,})[»"\']', str(name))
    core = quoted[0] if quoted else str(name)
    core = core.strip()
    if core.isupper():
        core = core.capitalize()
    return core


def load_pains_from_db(inn, db_path=None):
    """Боли, которые уже записала диагностика в таблицу signals."""
    import sqlite3
    path = db_path or verify.DB_PATH
    if not path or not os.path.exists(path):
        return []
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT rowid, signal, detail, severity, source FROM signals "
            "WHERE CAST(inn AS TEXT) = ? ORDER BY rowid",
            (verify.norm_digits(inn),)).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    pains = []
    for row in rows:
        if not row["signal"]:
            continue
        pains.append({
            "code": "signal-%s" % row["rowid"],
            "field": "pain:signal-%s" % row["rowid"],
            "text": row["signal"], "evidence": row["detail"],
            "severity": row["severity"], "source_type": SOURCE_SITE,
            "source_url": row["source"], "confidence": 0.7,
        })
    return pains


def _live_lookup(company):
    try:
        import enrich_site
        import diagnose as diagnose_mod
    except ImportError:
        return None, None
    enriched = enrich_site.enrich_company(
        company.get("name"), company.get("inn"),
        www=company.get("www"), record=company, site=company.get("site"))
    diag = None
    if enriched.get("site"):
        diag = diagnose_mod.diagnose(enriched["site"], company)
    return enriched, diag


def _pains_from_metrics(metrics, company):
    try:
        import diagnose as diagnose_mod
    except ImportError:
        return []
    return diagnose_mod.pains_from(metrics, company)


def _subject(company, pains):
    """Тема письма. Коротко, иначе в списке писем она обрежется."""
    name = short_name(company.get("name")) or ""
    subject = "Перевозки для %s" % name if name else "Вопрос по перевозкам"
    if len(subject) > 60:
        subject = "Вопрос по перевозкам"
    return subject


def capitalize_ru(text):
    """Первая буква заглавная, остальной текст не трогаем."""
    s = str(text or "").strip()
    return s[0].upper() + s[1:] if s else s


def _as_sentence(text):
    """Боль в готовое предложение: заглавная буква и точка в конце."""
    s = capitalize_ru(text)
    if s and s[-1] not in ".!?:":
        s += "."
    return s


def facts_block(facts, show_reason=False):
    """Факты в текст для промпта. Каждая строка со ссылкой на источник."""
    lines = []
    for f in facts or []:
        value = f.get("text") or f.get("value")
        note = f.get("note")
        label = note if note and not f.get("text") else f.get("field")
        line = "- %s: %s [источник: %s, доверие %.2f]" % (
            label, value, f.get("source_url") or f.get("source_type"),
            f.get("confidence") or 0)
        if show_reason and f.get("flag_reason"):
            line += " ОТБРАКОВАН: %s" % f["flag_reason"]
        lines.append(line)
    return "\n".join(lines)


def pains_block(pains):
    lines = []
    for p in pains or []:
        lines.append("- %s (основание: %s)" % (p.get("text"), p.get("evidence")))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Шаблон
# ----------------------------------------------------------------------------

RX_SLOT = re.compile(r'\{\{\s*([A-Za-z_][\w.]*)\s*\}\}')


def render_letter(template, context):
    """Подставить значения в шаблон. Неизвестные слоты остаются на месте.

    Так и задумано: незакрытый слот увидит валидатор и не выпустит письмо.
    Пустой pain_line это не сбой шаблона, а сигнал, что писать пока не о чем:
    у компании не нашлось ни одной боли, и повода для письма тоже нет.
    """
    def repl(match):
        value = _dig(context, match.group(1))
        if value in (None, "", []):
            return match.group(0)
        return str(value)
    return RX_SLOT.sub(repl, template or "")


def _dig(context, path):
    node = context
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def split_subject(text):
    """Отделить тему от тела. Возвращает (subject, body)."""
    lines = (text or "").strip().splitlines()
    if lines and lines[0].lower().startswith(("тема:", "subject:")):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        return subject, body
    return None, (text or "").strip()


# ----------------------------------------------------------------------------
# Валидатор
# ----------------------------------------------------------------------------

def validate_letter(text, context, threshold=None, min_len=200, max_len=2500):
    """Проверить готовое письмо. Возвращает (ok, problems).

    ok=False, если есть хоть одна проблема уровня error. Предупреждения
    не блокируют отправку, но их видно в отчёте.
    """
    threshold = threshold if threshold is not None else context.get(
        "threshold", DEFAULT_THRESHOLD)
    problems = []

    def add(severity, code, message, sample=None):
        problems.append({"severity": severity, "code": code,
                         "text": message, "sample": sample})

    body = (text or "").strip()
    if not body:
        add("error", "empty", "письмо пустое")
        return False, problems

    subject, letter = split_subject(body)

    for pattern, label in PLACEHOLDER_PATTERNS:
        found = re.findall(pattern, body)
        if found:
            add("error", "placeholder",
                "в письме остался незаполненный плейсхолдер (%s)" % label,
                found[0])

    if "—" in body or "–" in body:
        add("error", "em_dash",
            "в тексте есть длинное тире, по стилю проекта его быть не должно",
            _around(body, "—") or _around(body, "–"))

    for fact in context.get("flagged_facts", []):
        value = fact.get("value")
        if value in (None, "", []):
            continue
        value = str(value).strip()
        if len(value) < 4:
            continue
        if value.lower() in body.lower():
            add("error", "unverified_fact",
                "в письме упомянут непроверенный факт «%s» (%s, доверие %.2f)"
                % (value, fact.get("field"), fact.get("confidence") or 0), value)

    if context.get("blocked"):
        for blocker in context.get("blockers", []):
            add("error", "blocked", "письмо отправлять нельзя: %s" % blocker["text"])

    if context.get("stoplist"):
        add("error", "stoplist",
            "компания в стоп-листе, писать ей нельзя: %s" % context["stoplist"])

    sender = context.get("sender") or {}
    unsubscribe = str(sender.get("unsubscribe") or DEFAULT_UNSUBSCRIBE).strip()
    marker = unsubscribe.split(",")[0].strip().lower()
    if marker and marker not in body.lower() and u"стоп" not in body.lower():
        add("error", "no_unsubscribe",
            "в письме нет строки об отписке. Последняя строка должна давать "
            "человеку простой способ отказаться: «%s»" % unsubscribe)

    low = body.lower()
    for cliche in CLICHES:
        if cliche in low:
            add("warning", "cliche", "штамп из рассылок: «%s»" % cliche, cliche)

    if len(letter) < min_len:
        add("warning", "too_short",
            "письмо короче %d знаков, фактов в нём почти нет" % min_len)
    if len(letter) > max_len:
        add("warning", "too_long",
            "письмо длиннее %d знаков, до конца его не дочитают" % max_len)

    if subject is None:
        add("warning", "no_subject", "в письме нет строки Тема:")
    elif len(subject) > 70:
        add("warning", "long_subject", "тема длиннее 70 знаков, в списке писем обрежется")

    for number in re.findall(r'\b\d{3,}\b', letter):
        if not _number_is_backed(number, context):
            add("warning", "unbacked_number",
                "число %s в письме не найдено среди проверенных фактов" % number,
                number)

    ok = not any(p["severity"] == "error" for p in problems)
    return ok, problems


def _number_is_backed(number, context):
    haystack = []
    for fact in context.get("facts", []):
        haystack.append(str(fact.get("value")))
        haystack.append(str(fact.get("text")))
        haystack.append(str(fact.get("note")))
    sender = context.get("sender") or {}
    haystack.extend([str(v) for v in sender.values()])
    haystack.append(str(datetime.date.today().year))
    blob = " ".join(haystack)
    return number in blob


def _around(text, needle, width=30):
    pos = text.find(needle)
    if pos < 0:
        return None
    return text[max(0, pos - width):pos + width].replace("\n", " ")


def report_problems(problems):
    if not problems:
        print("    замечаний нет")
        return
    for p in problems:
        mark = "ОШИБКА " if p["severity"] == "error" else "внимание"
        print("    [%s] %s" % (mark, p["text"]))
        if p.get("sample"):
            print("              фрагмент: %s" % str(p["sample"])[:70])


# ----------------------------------------------------------------------------
# Демо
# ----------------------------------------------------------------------------

DEMO_RECORD = {
    "inn": "1650027925",
    "ogrn": "1021602013762",
    "name_ex": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ЧЕЛНЫ-ХЛЕБ"',
    "okved1": "10.71.1",
    "okved1name": "производство хлеба и хлебобулочных изделий",
    "regioncode": "16",
    "cityname": "Набережные Челны",
    "od2_sschr": 76,
    "has_contracts": 1,
    "category": 3,
    "site": "https://chelny-hleb.ru/",
    "inn_on_site": "1650027925",
    "email": "sales@chelny-hleb.ru",
    "g": "ДИРЕКТОР: Гатиятуллин Ильшат Рафикович",
    "metrics": {
        "url": "https://chelny-hleb.ru/", "reachable": True, "has_metrika": False,
        "metrika_id": None, "has_viewport": True, "has_form": False,
        "copyright_year": 2021, "has_ssl": True, "ttfb_seconds": 3.8,
        "cms": None, "has_email": True, "has_phone": True,
    },
}

DEMO_SENDER = {
    "name": "Дмитрий",
    "company": "транспортная компания, Казань",
    "phone": "+7 900 000-00-00",
    "email": "d@example.ru",
}


def _demo():
    print("=" * 72)
    print("write_letter.py: демо")
    print("=" * 72)

    ctx = build_letter_context(DEMO_RECORD["inn"], record=DEMO_RECORD,
                               sender=DEMO_SENDER)

    print("\n[1] Контекст собран, база и сеть не нужны")
    print("    компания:     ", ctx["company"].get("short_name"))
    print("    опознана:     ", ctx["verification"]["identified_by"])
    print("    обращение:    ", ctx["greeting"])
    print("    фактов в дело:", len(ctx["facts"]), "отбраковано:", len(ctx["flagged_facts"]))
    print("    болей:        ", len(ctx["pains"]))
    print("    блокировка:   ", "да" if ctx["blocked"] else "нет")

    print("\n[2] Блок фактов, который уходит в промпт")
    print("\n".join("    " + line for line in ctx["facts_block"].splitlines()))
    print("\n[3] Боли, найденные на сайте")
    print("\n".join("    " + line for line in ctx["pains_block"].splitlines()))

    print("\n[4] Письмо по шаблону")
    letter = render_letter(DEFAULT_TEMPLATE, ctx)
    print("\n".join("    | " + line for line in letter.strip().splitlines()))

    print("\n[5] Проверка этого письма")
    ok, problems = validate_letter(letter, ctx)
    print("    вердикт:", "можно отправлять" if ok else "отправлять нельзя")
    report_problems(problems)

    print("\n[6] Проверка испорченного письма")
    bad = (u"Тема: Предложение\n\nЗдравствуйте, [имя]!\n\n"
           u"Наша динамично развивающаяся компания предлагает вам сотрудничество.\n"
           u"Знаю, что вашей компанией руководит Петров Пётр Петрович.\n"
           u"Оборот вашей компании 999999 рублей, а мы работаем с 2003 года.")
    ctx_bad = dict(ctx)
    ctx_bad["flagged_facts"] = list(ctx["flagged_facts"]) + [
        verify.make_fact("director_fio", "Петров Пётр Петрович",
                         verify.SOURCE_SEARCH, "поисковая выдача")]
    ok2, problems2 = validate_letter(bad, ctx_bad)
    print("    вердикт:", "можно отправлять" if ok2 else "отправлять нельзя")
    report_problems(problems2)

    print("\n[7] Что будет, если реестр говорит, что клиент нам не подходит")
    blocked_record = dict(DEMO_RECORD)
    blocked_record["okved1"] = "49.41"
    ctx3 = build_letter_context(blocked_record["inn"], record=blocked_record,
                                sender=DEMO_SENDER)
    print("    блокировка:", "да" if ctx3["blocked"] else "нет")
    for b in ctx3["blockers"]:
        print("    причина:", b["text"])


def _cli(argv):
    """Контекст для письма по компаниям из базы.

    По умолчанию печатает контекст в JSON: его подставляет в свой промпт
    скилл, который пишет текст. С ключом --render дополнительно собирает
    письмо по встроенному шаблону и кладёт его в таблицу letters.

    python3 scripts/write_letter.py --db data/leads.db --inn 1650027925
    python3 scripts/write_letter.py --db data/leads.db --render --limit 20
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Сборка контекста письма из проверенных фактов")
    parser.add_argument("--db", default=verify.DB_PATH)
    parser.add_argument("--inn")
    parser.add_argument("--inn-file")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--render", action="store_true",
                        help="собрать письмо по встроенному шаблону и записать в базу")
    parser.add_argument("--json", action="store_true", help="только JSON, без пояснений")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("базы %s нет. Сначала соберите выборку и обогащение" % args.db)
        return 1

    con = verify.open_db(args.db)
    sender = load_sender()
    if args.render and not args.json and not sender.get("signature"):
        print("подпись не настроена, письма собраны не будут: заполните в "
              "config.json блок pisma, поля ot_kogo_imya, ot_kogo_kompaniya "
              "и ot_kogo_telefon. Конфиг: %s" % find_config())
    # Стоп-лист из конфига переносится в базу при каждом прогоне: человек
    # правит config.json, а проверка идёт по таблице.
    added = verify.stoplist_sync_config(con, config_path=find_config())
    if added and not args.json:
        print("в стоп-лист из config.json добавлено ИНН: %d" % added)
    inns = []
    if args.inn:
        inns.append(verify.norm_digits(args.inn))
    if args.inn_file:
        inns.extend(verify.read_inn_file(args.inn_file))
    if not inns:
        inns = [c["inn"] for c in verify.db_companies(con, limit=args.limit)]

    contexts, written, blocked, stopped = [], 0, 0, 0
    for inn in inns[:args.limit]:
        ctx = build_letter_context(inn, db_path=args.db)
        if not ctx["company"]:
            continue
        contexts.append(ctx)
        if ctx.get("stoplist"):
            stopped += 1
            if not args.json:
                print("    %-13s стоп-лист, письмо не собирается: %s"
                      % (inn, ctx["stoplist"]))
            continue
        if ctx["blocked"]:
            blocked += 1
            if not args.json:
                print("    %-13s письмо запрещено: %s"
                      % (inn, ctx["blockers"][0]["text"]))
            continue
        if not args.render:
            continue
        already = con.execute(
            "SELECT status FROM letters WHERE inn = ? ORDER BY rowid DESC LIMIT 1",
            (inn,)).fetchone()
        if already and already["status"] == verify.STATUS_IN_DRAFTS:
            # письмо уже лежит в ящике: переписать его значит залить дубль
            if not args.json:
                print("    %-13s письмо уже в черновиках ящика, пропускаю" % inn)
            continue
        letter = render_letter(DEFAULT_TEMPLATE, ctx)
        ok, problems = validate_letter(letter, ctx)
        subject, body = split_subject(letter)
        # db_replace, а не db_insert: второй прогон должен обновлять письмо
        # по ИНН, а не класть рядом второе с теми же фактами
        verify.db_replace(con, "letters", {
            "inn": inn, "name": ctx["company"].get("name"),
            "to_email": ctx["contacts"].get("email"),
            "subject": subject, "body": body,
            "facts": json.dumps([f.get("text") or "%s: %s" % (f.get("note"), f.get("value"))
                                 for f in ctx["facts"]], ensure_ascii=False),
            "status": verify.STATUS_DRAFT if ok else verify.STATUS_REVIEW,
            "created_at": verify.now_stamp(),
        })
        con.commit()
        written += 1
        if not args.json:
            mark = verify.STATUS_DRAFT if ok else verify.STATUS_REVIEW
            print("    %-13s %-13s %s" % (inn, mark, subject))
    con.close()

    if args.json:
        print(json.dumps(contexts, ensure_ascii=False, indent=2, default=str))
    else:
        print("\nконтекстов собрано: %d, писем записано: %d, запрещено: %d, "
              "в стоп-листе: %d"
              % (len(contexts), written, blocked, stopped))
        if not args.render:
            print("текст письма пишет скилл: возьмите блок facts_block из контекста")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    flags = ("--db", "--inn", "--inn-file", "--limit", "--render", "--json")
    # справка обязана печататься мгновенно, без сети и без записи файлов
    if "--help" in argv or "-h" in argv:
        return _cli(argv)
    if any(a.split("=")[0] in flags for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
