# -*- coding: utf-8 -*-
"""
diagnose.py: что у компании сломано на сайте.

Снимает восемь показателей одним HTTP-запросом, без ключей и без Google:
  has_metrika, metrika_id, has_viewport, has_form,
  copyright_year, has_ssl, ttfb_seconds, cms

и превращает их в список болей человеческим языком, которые можно
дословно поставить в письмо.

Чего здесь намеренно НЕТ:
  YCLIENTS и DIKIDI. Это онлайн-запись для салонов и клиник. У завода
  или оптовой базы онлайн-записи не бывает, для нашей ниши это шум.
  Google Analytics как боль. В разведке GA не нашёлся ни на одном из семи
  производственных сайтов. После ухода Google из России отсутствие GA это
  норма, а не проблема. Проверять надо отсутствие Яндекс.Метрики.
  PageSpeed Insights. Без ключа он отдаёт 429, проверено шесть раз подряд
  на шести доменах. Скорость меряем сами, временем до первого байта.

Python 3.9, только стандартная библиотека.
"""

import os
import re
import ssl
import sys
import socket
import datetime
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from enrich_site import (  # noqa: E402
    fetch_page, host_of, footer_slice, extract_contacts, normalize_site_url,
)

# ----------------------------------------------------------------------------
# Детекторы. Числа в комментариях это попадания на корпусе из 7 живых
# сайтов производств и оптовиков, замеры 13.08.2026.
# ----------------------------------------------------------------------------

RX_METRIKA = re.compile(r'mc\.yandex\.ru/(?:metrika|watch)|ym\s*\(\s*\d{5,9}\s*,')   # 5/7
RX_METRIKA_ID = re.compile(r'(?:ym\s*\(\s*|mc\.yandex\.ru/watch/)(\d{5,9})')          # 5/7
RX_VIEWPORT = re.compile(r'<meta[^>]+name\s*=\s*["\']viewport["\']', re.I)            # 7/7
RX_FORM = re.compile(r'<form\b[^>]*>', re.I)                                         # 5/7
RX_COPYRIGHT = re.compile(r'(?:©|&copy;|&#169;)\s*(?:\d{4}\s*[–—−-]\s*)?(20\d{2})')  # 4/7

# CMS. Битрикс подтверждён на 5 сайтах из 7, остальные добавлены по структуре
# загрузчика и на корпусе разведки не встречались.
CMS_RULES = [
    ("1C-Битрикс", re.compile(r'/bitrix/(?:js|templates|cache)/')),                   # 5/7
    ("Tilda", re.compile(r'tildacdn\.(?:com|net)|tilda-blocks')),                     # 0/7
    ("WordPress", re.compile(r'/wp-(?:content|includes)/')),
    ("Joomla", re.compile(r'/media/(?:jui|system)/js/|Joomla!')),
    ("MODX", re.compile(r'/assets/(?:components|templates)/|MODX')),
    ("OpenCart", re.compile(r'index\.php\?route=common/|catalog/view/theme')),
    ("Drupal", re.compile(r'/sites/(?:default|all)/(?:files|themes|modules)/')),
    ("InSales", re.compile(r'assets\.insales\.ru|static\.insales')),
    ("Wix", re.compile(r'static\.parastorage\.com|wix\.com')),
]

SLOW_TTFB = 3.0        # порог «медленно», в разведке живые сайты давали 1.5 до 3.2 с
STALE_YEARS = 2        # копирайт старше этого числа лет считаем протухшим


def check_ssl(host, port=443, timeout=6):
    """Проверка сертификата ровно так, как это делает браузер.

    Возвращает (ok, текст ошибки). Ошибка нужна в письме: у kzsk.ru сертификат
    выписан на другое имя, и посетитель видит красное предупреждение.
    """
    if not host:
        return False, "нет домена"
    try:
        ctx = ssl.create_default_context()
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            wrapped = ctx.wrap_socket(sock, server_hostname=host)
            wrapped.close()
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return True, None
    except ssl.SSLCertVerificationError as e:
        return False, "сертификат не проходит проверку: %s" % getattr(e, "verify_message", e)
    except ssl.SSLError as e:
        return False, "ошибка TLS: %s" % e
    except (socket.timeout, socket.gaierror, OSError) as e:
        return False, "нет соединения по 443: %s" % type(e).__name__


def detect_cms(html):
    for name, rx in CMS_RULES:
        if rx.search(html or ""):
            return name
    return None


def copyright_year(html):
    """Год из копирайта. Берём максимум, в подвале часто несколько дат."""
    years = [int(y) for y in RX_COPYRIGHT.findall(html or "") if y.isdigit()]
    years = [y for y in years if 2000 <= y <= datetime.date.today().year + 1]
    return max(years) if years else None


def diagnose_site(url, html=None, page=None, skip_ssl=None):
    """Снять показатели сайта. Возвращает словарь метрик, без оценок.

    Если html или page переданы снаружи, разбор идёт БЕЗ единого обращения
    к сети: проверка сертификата тоже пропускается, иначе «разбор без сети»
    втихую делает DNS-запрос и TLS-рукопожатие.
    """
    url = normalize_site_url(url) or url
    host = host_of(url)
    metrics = {
        "url": url, "host": host, "final_url": None, "status": None,
        "bytes": None, "reachable": False, "error": None,
        "has_metrika": False, "metrika_id": None,
        "has_viewport": False, "has_form": False, "forms_count": 0,
        "copyright_year": None, "has_ssl": False, "ssl_error": None,
        "ttfb_seconds": None, "total_seconds": None, "cms": None,
        "has_email": False, "has_phone": False, "checked_at": _now(),
    }
    if not url:
        metrics["error"] = "адрес сайта не задан"
        return metrics

    if skip_ssl is None:
        skip_ssl = html is not None or page is not None
    if not skip_ssl:
        ok, ssl_err = check_ssl(host)
        metrics["has_ssl"] = ok
        metrics["ssl_error"] = ssl_err

    if html is None:
        page = page or fetch_page(url)
        html = page.get("html", "")
        metrics["status"] = page.get("status")
        metrics["final_url"] = page.get("final_url")
        metrics["bytes"] = page.get("bytes")
        metrics["ttfb_seconds"] = page.get("ttfb")
        metrics["total_seconds"] = page.get("total")
        metrics["error"] = page.get("error")
    metrics["reachable"] = bool(html)
    if not html:
        return metrics

    metrics["has_metrika"] = bool(RX_METRIKA.search(html))
    ids = RX_METRIKA_ID.findall(html)
    metrics["metrika_id"] = ids[0] if ids else None
    metrics["has_viewport"] = bool(RX_VIEWPORT.search(html))
    forms = RX_FORM.findall(html)
    metrics["forms_count"] = len(forms)
    metrics["has_form"] = len(forms) > 0
    metrics["copyright_year"] = copyright_year(html) or copyright_year(footer_slice(html))
    metrics["cms"] = detect_cms(html)
    contacts = extract_contacts(html, url)
    metrics["has_email"] = bool(contacts["emails"])
    metrics["has_phone"] = bool(contacts["phones"])
    return metrics


def pains_from(metrics, company=None):
    """Метрики в боли человеческим языком.

    Каждая боль это факт с источником, чтобы verify.py мог её взвесить,
    а письмо сослалось на конкретную страницу. Уверенность 0.7 это вес
    факта, снятого с сайта компании, реестр весит 1.0.
    """
    company = company or {}
    url = metrics.get("final_url") or metrics.get("url")
    year_now = datetime.date.today().year
    pains = []

    def add(code, text, evidence):
        pains.append({
            "code": code, "text": text, "evidence": evidence,
            "field": "pain:" + code, "source_type": "company_site",
            "source_url": url, "confidence": 0.7,
        })

    if not metrics.get("url"):
        pains.append({
            "code": "no_site",
            "text": "у компании нет сайта, найти её в интернете можно только по справочникам",
            "evidence": "сайт не найден ни в реестре, ни по названию",
            "field": "pain:no_site", "source_type": "registry",
            "source_url": None, "confidence": 1.0,
        })
        return pains

    if not metrics.get("reachable"):
        add("site_down",
            "сайт не открывается, а значит заявки с него не приходят вообще",
            "ответ сервера: %s" % (metrics.get("error") or metrics.get("status")))
        return pains

    if not metrics.get("has_metrika"):
        add("no_metrika",
            "на сайте нет счётчика Яндекс.Метрики, заявки и посетителей никто не считает",
            "в коде страницы нет ни mc.yandex.ru, ни вызова ym()")

    year = metrics.get("copyright_year")
    if year and year <= year_now - STALE_YEARS:
        add("stale_copyright",
            "сайт не обновлялся с %d года, в подвале до сих пор стоит копирайт %d" % (year, year),
            "год копирайта %d, сейчас %d" % (year, year_now))

    if not metrics.get("has_viewport"):
        add("no_viewport",
            "сайт не адаптирован под телефон, с мобильного им пользоваться неудобно",
            "в коде нет мета-тега viewport")

    if not metrics.get("has_form"):
        add("no_form",
            "на сайте нет ни одной формы заявки, оставить запрос можно только звонком",
            "тег form на странице не найден")

    if not metrics.get("has_ssl"):
        add("bad_ssl",
            "браузер помечает сайт как небезопасный, часть посетителей уходит с предупреждения",
            metrics.get("ssl_error") or "сертификат не отдаётся")

    ttfb = metrics.get("ttfb_seconds")
    if ttfb is not None and ttfb > SLOW_TTFB:
        add("slow",
            "сайт отвечает медленно, до первого байта проходит %.1f секунды" % ttfb,
            "замер времени ответа: %.2f с" % ttfb)

    # Почту ищем не только здесь: enrich_site обходит ещё и /contacts/, где она
    # чаще всего и лежит. Если адрес уже добыт, это не боль, а решённый вопрос.
    if not metrics.get("has_email") and not company.get("email"):
        add("no_email_on_site",
            "на сайте не указана почта, написать в компанию можно только через форму",
            "адрес электронной почты на проверенной странице не найден")

    return pains


def diagnose(url, company=None, verbose=False):
    """Полный проход: метрики плюс боли."""
    metrics = diagnose_site(url)
    pains = pains_from(metrics, company)
    if verbose:
        _print_report(metrics, pains)
    return {"metrics": metrics, "pains": pains}


def _now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _print_report(metrics, pains):
    print("    адрес:        ", metrics.get("final_url") or metrics.get("url"))
    print("    ответ:        ", metrics.get("status"), "%s байт" % metrics.get("bytes"),
          "ttfb %s с" % metrics.get("ttfb_seconds"))
    print("    Метрика:      ", "есть, счётчик %s" % metrics["metrika_id"]
          if metrics["has_metrika"] else "нет")
    print("    адаптив:      ", "есть" if metrics["has_viewport"] else "нет")
    print("    формы:        ", metrics["forms_count"])
    print("    копирайт:     ", metrics["copyright_year"])
    print("    сертификат:   ", "в порядке" if metrics["has_ssl"]
          else "битый, %s" % metrics["ssl_error"])
    print("    CMS:          ", metrics["cms"] or "не определилась")
    if pains:
        print("    боли (%d):" % len(pains))
        for p in pains:
            print("      - %s" % p["text"])
            print("        основание: %s" % p["evidence"])
    else:
        print("    боли: не нашлось, сайт в порядке")


# ----------------------------------------------------------------------------
# Демо
# ----------------------------------------------------------------------------

BROKEN_SAMPLE = u"""<!doctype html><html><head><title>Завод</title></head>
<body><h1>Продукция</h1><p>Звоните: 8 (8552) 12-34-56</p>
<footer>&copy; 2020 Казанский завод</footer></body></html>"""

HEALTHY_SAMPLE = u"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><form action="/order"><input name="tel"></form>
<script>ym(44398735, "init", {});</script>
<script src="/bitrix/js/main/core/core.js"></script>
<footer>&copy; 2026 info@zavod.ru</footer></body></html>"""


def _demo():
    print("=" * 72)
    print("diagnose.py: демо")
    print("=" * 72)

    print("\n[1] Разбор без сети: сломанный сайт")
    m = diagnose_site("https://example-broken.ru/", html=BROKEN_SAMPLE)
    m["has_ssl"], m["ssl_error"] = False, "сертификат выписан на другое имя"
    m["ttfb_seconds"] = 4.2
    _print_report(m, pains_from(m))

    print("\n[2] Разбор без сети: здоровый сайт")
    m2 = diagnose_site("https://example-ok.ru/", html=HEALTHY_SAMPLE)
    m2["has_ssl"], m2["ttfb_seconds"] = True, 0.4
    _print_report(m2, pains_from(m2))

    if "--offline" in sys.argv:
        print("\n[3] Живой прогон пропущен, задан ключ --offline")
        return

    print("\n[3] Живой прогон: kzsk.ru")
    print("    образец заброшенного сайта: сертификат не совпадает с доменом,")
    print("    копирайт 2020, ни Метрики, ни формы заявки")
    try:
        diagnose("https://kzsk.ru/", verbose=True)
    except Exception as e:
        print("    сеть недоступна, это не ошибка скрипта: %s: %s" % (type(e).__name__, e))


SEVERITY = {
    "no_site": "high", "site_down": "high", "no_email_on_site": "high",
    "no_metrika": "medium", "no_form": "medium", "stale_copyright": "medium",
    "bad_ssl": "medium", "no_viewport": "low", "slow": "low",
}


def _cli(argv):
    """Пакетная диагностика сайтов из базы конвейера.

    python3 scripts/diagnose.py --db data/leads.db --limit 100
    """
    import argparse
    import verify

    parser = argparse.ArgumentParser(description="Диагностика сайтов компаний")
    parser.add_argument("--db", default=verify.DB_PATH)
    parser.add_argument("--inn", help="один ИНН")
    parser.add_argument("--inn-file")
    parser.add_argument("--url", help="разово проверить один адрес, база не нужна")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    if args.url:
        diagnose(args.url, verbose=True)
        return 0

    if not os.path.exists(args.db):
        print("базы %s нет. Сначала соберите выборку и обогащение" % args.db)
        return 1

    con = verify.open_db(args.db)
    inns = []
    if args.inn:
        inns.append(verify.norm_digits(args.inn))
    if args.inn_file:
        inns.extend(verify.read_inn_file(args.inn_file))
    companies = verify.db_companies(con, inns=inns or None, limit=args.limit)
    checked, total_pains = 0, 0
    for row in companies:
        record = verify.canonical_record(row)
        site = record.get("www")
        if not site:
            continue
        result = diagnose(site, record)
        checked += 1
        total_pains += len(result["pains"])
        print("    %-13s %-34s болей: %d"
              % (record.get("inn"), site[:34], len(result["pains"])))
        # на одну компанию приходится несколько сигналов, поэтому перед записью
        # чистим её строки целиком: иначе повторный прогон продублирует боли,
        # и одна и та же фраза уедет в письмо дважды
        verify.db_delete(con, "signals", record.get("inn"))
        for pain in result["pains"]:
            verify.db_insert(con, "signals", {
                "inn": record.get("inn"), "name": record.get("name"),
                "signal": pain["text"], "detail": pain["evidence"],
                "severity": SEVERITY.get(pain["code"], "medium"),
                "source": pain.get("source_url") or site,
                "checked_at": verify.now_stamp(),
            })
        con.commit()
    con.close()
    print("проверено сайтов: %d, найдено сигналов: %d" % (checked, total_pains))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    flags = ("--db", "--inn", "--inn-file", "--limit", "--url")
    # справка обязана печататься мгновенно, без единого запроса в сеть
    if "--help" in argv or "-h" in argv:
        return _cli(argv)
    if any(a.split("=")[0] in flags for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
