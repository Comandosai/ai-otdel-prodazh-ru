# -*- coding: utf-8 -*-
"""
fns_egrul.py — ФИО руководителя из ЕГРЮЛ (egrul.nalog.ru).

Единственный бесплатный источник ФИО директора. Ключ не нужен, капчи нет.
Именно отсюда берётся имя, к которому потом обращается письмо.

Схема двухфазная, обе фазы обязательны:

    POST https://egrul.nalog.ru/
         query=<ИНН>&region=&page=&vyp3CaptchaToken=&PreventChromeAutocomplete=
      -> {"t": "<токен>", "captchaRequired": false}

    GET  https://egrul.nalog.ru/search-result/<токен>
      -> {"rows": [...]}

Второй запрос отдаёт пустой ответ, пока поиск ещё идёт на сервере, поэтому
его надо опрашивать с интервалом 0.5 с. Признак готовности — появление ключа
rows, даже если список пустой (значит, просто ничего не нашлось).

Замеры 13.08.2026: 25 разных поисков подряд, все успешные, captchaRequired
всегда false. Примерно 2 секунды на поиск, из которых большая часть — это
ожидание готовности результата. То есть темп 1 запрос в 2 секунды получается
сам собой, специально тормозить не нужно, но троттлинг тут всё равно есть.

Поиск по ИНН даёт ТОЧНОЕ попадание (tot = 1). Поиск по названию нечёткий:
«Кайбицкий хлебокомбинат» находит 1327 записей, поэтому связывать компании
по названию нельзя, только по ИНН или ОГРН.

ЮЛ и ИП различаются полем k:
    k = "ul"  юрлицо: есть c (краткое имя), p (КПП) и g (должность и ФИО)
    k = "fl"  ИП: полей c, p, g НЕТ, ФИО предпринимателя лежит в n

Требования: Python 3.9, только стандартная библиотека.
"""

import json
import os
import re
import socket
import ssl
import sys
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

from typing import Any, Dict, List, Optional, Sequence, Union

BASE = "https://egrul.nalog.ru"
URL_INDEX = BASE + "/index.html"
URL_POST = BASE + "/"
URL_RESULT = BASE + "/search-result/%s"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TIMEOUT = 30
RETRIES = 3
THROTTLE_SEC = 2.0        # вежливый темп, он же естественный для сервиса
POLL_INTERVAL = 0.5       # как часто спрашивать готов ли результат
POLL_TIMEOUT = 15.0       # сколько всего ждать результат

_last_call = [0.0]


class EgrulError(Exception):
    """Понятная человеку ошибка при работе с ЕГРЮЛ."""
    pass


class EgrulCaptcha(EgrulError):
    """Сервис потребовал ввести капчу."""
    pass


# --------------------------------------------------------------------------
# сеть
# --------------------------------------------------------------------------

def get_cookies() -> urllib.request.OpenerDirector:
    """
    Забирает стартовые куки со страницы поиска.

    Возвращает opener с cookie-jar. Его надо переиспользовать: создавать
    новую сессию на каждый ИНН и дороже, и подозрительнее для сервиса.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = []
    req = urllib.request.Request(URL_INDEX, headers={"User-Agent": UA})
    _open(opener, req).read()
    return opener


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < THROTTLE_SEC:
        time.sleep(THROTTLE_SEC - delta)
    _last_call[0] = time.time()


def _open(opener: urllib.request.OpenerDirector,
          req: urllib.request.Request,
          timeout: int = TIMEOUT,
          retries: int = RETRIES):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if err.code >= 500 and attempt < retries:
                last_err = err
                time.sleep(1.5 * attempt)
                continue
            raise EgrulError("ЕГРЮЛ ответил HTTP %d на %s.\n  Ответ: %s"
                             % (err.code, req.full_url, body or "(пусто)"))
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(1.5 * attempt)
                continue
    raise EgrulError(
        "Не смог достучаться до egrul.nalog.ru за %d попытки. Причина: %s.\n"
        "  Проверьте интернет и доступность домена." % (retries, last_err))


# --------------------------------------------------------------------------
# поиск
# --------------------------------------------------------------------------

def search(query: Union[str, int],
           region: str = "",
           page: str = "",
           opener: Optional[urllib.request.OpenerDirector] = None,
           poll_timeout: float = POLL_TIMEOUT) -> List[Dict[str, Any]]:
    """
    Полный цикл поиска: POST за токеном, затем опрос результата.

    Возвращает список сырых записей rows. Пустой список означает, что поиск
    отработал и ничего не нашёл, а не что что-то сломалось.
    """
    if opener is None:
        opener = get_cookies()

    _throttle()
    body = urllib.parse.urlencode({
        "query": str(query),
        "region": region,
        "page": page,
        "vyp3CaptchaToken": "",
        "PreventChromeAutocomplete": "",
    }).encode("utf-8")

    req = urllib.request.Request(URL_POST, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": URL_INDEX,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    raw = _open(opener, req).read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise EgrulError("ЕГРЮЛ вернул не JSON на запрос токена. Начало:\n  %s"
                         % raw[:300])

    if payload.get("captchaRequired"):
        raise EgrulCaptcha(
            "ЕГРЮЛ требует капчу для запроса '%s'.\n"
            "  За разведку такого ни разу не случилось (25 поисков подряд), "
            "но если случилось: сделайте паузу и снизьте темп." % query)

    token = payload.get("t")
    if not token:
        raise EgrulError("ЕГРЮЛ не выдал токен поиска. Ответ: %s"
                         % json.dumps(payload, ensure_ascii=False)[:300])

    deadline = time.time() + poll_timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        res_req = urllib.request.Request(URL_RESULT % token, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": URL_INDEX,
        })
        text = _open(opener, res_req).read().decode("utf-8", "replace").strip()
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            # готовность определяется наличием ключа rows, даже пустого
            if isinstance(data, dict) and "rows" in data:
                return data.get("rows") or []
        time.sleep(POLL_INTERVAL)

    raise EgrulError(
        "ЕГРЮЛ не отдал результат по запросу '%s' за %.0f секунд "
        "(%d попыток опроса).\n  Обычно поиск занимает около 2 секунд, "
        "попробуйте позже." % (query, poll_timeout, attempts))


# --------------------------------------------------------------------------
# разбор
# --------------------------------------------------------------------------

def parse_director(g_value: Optional[str]) -> Dict[str, Optional[str]]:
    """
    Разбирает поле g формата 'ДОЛЖНОСТЬ: Фамилия Имя Отчество'.

    Сырая строка возвращается всегда: сервис умеет класть в это поле
    несколько лиц через запятую, и такой случай живыми данными НЕ проверялся.
    Поэтому доверять можно raw, а разобранные post и name считать
    удобным, но не абсолютным упрощением.
    """
    raw = (g_value or "").strip()
    if not raw:
        return {"director_post": None, "director_name": None, "director_raw": None}
    match = re.match(r"^\s*([^:]+?)\s*:\s*(.+?)\s*$", raw)
    if not match:
        return {"director_post": None, "director_name": raw, "director_raw": raw}
    return {
        "director_post": match.group(1).strip(),
        "director_name": match.group(2).strip(),
        "director_raw": raw,
    }


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводит сырую запись rows к плоскому словарю.

    Различает ЮЛ и ИП: у ИП (k = 'fl') полей c, p и g нет вообще,
    ФИО предпринимателя лежит в n.
    """
    kind = (row.get("k") or "").strip().lower()
    end_date = (row.get("e") or "").strip() or None

    rec: Dict[str, Any] = {
        "inn": (row.get("i") or "").strip() or None,
        "ogrn": (row.get("o") or "").strip() or None,
        "kpp": (row.get("p") or "").strip() or None,
        "name": (row.get("n") or "").strip() or None,
        "short_name": (row.get("c") or "").strip() or None,
        "kind": kind or None,
        "is_ip": kind == "fl",
        "reg_date": (row.get("r") or "").strip() or None,
        "end_date": end_date,
        "region_name": (row.get("rn") or "").strip() or None,
        "status": "прекратила деятельность" if end_date else "действующая",
    }

    if kind == "fl":
        # у ИП руководителя нет, распоряжается он сам
        rec.update({
            "director_post": "Индивидуальный предприниматель",
            "director_name": rec["name"],
            "director_raw": None,
        })
    else:
        rec.update(parse_director(row.get("g")))
    return rec


def get_director(inn: Union[str, int],
                 opener: Optional[urllib.request.OpenerDirector] = None
                 ) -> Dict[str, Any]:
    """
    Главная функция модуля: по ИНН отдаёт карточку с ФИО руководителя.

    Ключи ответа: found, inn, ogrn, kpp, name, short_name, kind, is_ip,
    reg_date, end_date, region_name, status, director_name, director_post,
    director_raw.

    Если по ИНН ничего не нашлось, возвращается {'found': False, 'inn': ...},
    а не исключение: в конвейере это штатная ситуация.
    """
    key = str(inn).strip()
    rows = search(key, opener=opener)
    if not rows:
        return {"found": False, "inn": key,
                "director_name": None, "director_post": None,
                "ogrn": None, "reg_date": None, "status": None}

    # поиск по ИНН точный, но подстрахуемся и возьмём ровно нужную запись
    chosen = None
    for row in rows:
        if (row.get("i") or "").strip() == key:
            chosen = row
            break
    if chosen is None:
        chosen = rows[0]

    rec = normalize_row(chosen)
    rec["found"] = True
    rec["matched_exactly"] = (rec.get("inn") == key)
    return rec


def get_directors(inns: Sequence[Union[str, int]],
                  verbose: bool = True) -> Dict[str, Dict[str, Any]]:
    """
    Пачкой по списку ИНН, одна сессия на всех, троттлинг 2 секунды.

    Считайте время заранее: 100 ИНН это примерно 4 минуты, 1000 ИНН
    примерно 40 минут. Ошибка по одному ИНН не роняет весь прогон.
    """
    opener = get_cookies()
    out: Dict[str, Dict[str, Any]] = {}
    total = len(inns)
    for i, inn in enumerate(inns, 1):
        key = str(inn).strip()
        try:
            out[key] = get_director(key, opener=opener)
        except EgrulError as exc:
            if verbose:
                print("      %s: пропускаю, %s"
                      % (key, str(exc).split("\n")[0]))
            out[key] = {"found": False, "inn": key,
                        "error": str(exc).split("\n")[0]}
        if verbose and (i % 10 == 0 or i == total):
            print("      обработано %d из %d" % (i, total))
    return out


# --------------------------------------------------------------------------
# демо
# --------------------------------------------------------------------------

def _demo() -> None:
    samples = [
        ("1621000372", "ЮЛ, потребобщество из разведки"),
        ("1650027925", "ЮЛ, АО ЧЕЛНЫ-ХЛЕБ, крупнейший по выручке из ГИР БО"),
        ("160800724395", "ИП, 12-значный ИНН"),
        ("9999999999", "заведомо несуществующий ИНН"),
    ]

    print("=" * 74)
    print("ЕГРЮЛ. Демо: ФИО руководителя по ИНН")
    print("Темп %.0f с между запросами, ожидание результата до %.0f с"
          % (THROTTLE_SEC, POLL_TIMEOUT))
    print("=" * 74)

    print("\n[1/2] Открываю сессию")
    opener = get_cookies()
    print("      куки получены, капчи нет")

    print("\n[2/2] Спрашиваю по каждому ИНН")
    started = time.time()
    for inn, comment in samples:
        print("\n  ИНН %s  (%s)" % (inn, comment))
        try:
            rec = get_director(inn, opener=opener)
        except EgrulCaptcha as exc:
            print("      КАПЧА: %s" % str(exc).split("\n")[0])
            continue
        except EgrulError as exc:
            print("      ошибка: %s" % str(exc).split("\n")[0])
            continue

        if not rec.get("found"):
            print("      не найдено (это штатный ответ, не ошибка)")
            continue

        print("      наименование : %s" % (rec.get("short_name") or rec.get("name")))
        print("      ОГРН         : %s" % rec.get("ogrn"))
        print("      тип          : %s" % ("ИП" if rec.get("is_ip") else "юрлицо"))
        print("      должность    : %s" % rec.get("director_post"))
        print("      ФИО          : %s" % rec.get("director_name"))
        print("      дата рег.    : %s" % rec.get("reg_date"))
        print("      статус       : %s" % rec.get("status"))
        if rec.get("director_raw"):
            print("      сырое поле g : %s" % rec.get("director_raw"))

    elapsed = time.time() - started
    print("\n  Итого %d запросов за %.1f с, в среднем %.1f с на запрос."
          % (len(samples), elapsed, elapsed / len(samples)))
    print("\nГотово. ФИО директора это то самое обращение по имени в письме.")


# --------------------------------------------------------------------------
# запись ФИО директора в базу конвейера
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))


def _verify():
    """Модуль verify лежит рядом. Импорт ленивый, чтобы --help ничего не грузил."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import verify
    return verify


def director_line(rec: Dict[str, Any]) -> Optional[str]:
    """
    Строка для колонки director: 'ДОЛЖНОСТЬ: Фамилия Имя Отчество'.

    Должность сохраняется вместе с ФИО, потому что письмо здоровается по имени,
    а человек в отчёте хочет видеть, кто это: директор, управляющий или ИП.
    """
    if not rec or not rec.get("found"):
        return None
    raw = rec.get("director_raw")
    if raw:
        return raw
    name = rec.get("director_name")
    if not name:
        return None
    post = rec.get("director_post")
    return "%s: %s" % (post, name) if post else name


def save_directors(results: Dict[str, Dict[str, Any]], db_path: str,
                   verbose: bool = True) -> Dict[str, int]:
    """
    Кладёт ФИО руководителя в companies.director и в enrichment.director.

    Повторный прогон ничего не дублирует: companies обновляется по ИНН,
    строка enrichment заменяется через db_replace.
    """
    verify = _verify()
    con = verify.open_db(db_path)
    verify.ensure_company_columns(con)
    stamp = verify.now_stamp()
    saved, empty = 0, 0
    for inn, rec in results.items():
        line = director_line(rec)
        if not line:
            empty += 1
            continue
        key = verify.norm_digits(inn)
        verify.db_upsert_company(con, {"inn": key, "director": line})
        cur = con.execute("UPDATE enrichment SET director = ? WHERE inn = ?",
                          (line, key))
        if not cur.rowcount:
            verify.db_replace(con, "enrichment", {
                "inn": key,
                "name": rec.get("short_name") or rec.get("name"),
                "director": line,
                "source": "ЕГРЮЛ, поиск по ИНН",
                "confidence": "1.00",
                "checked_at": stamp,
            })
        saved += 1
    con.commit()
    con.close()
    if verbose:
        print("      ФИО записано: %d, не нашлось: %d" % (saved, empty))
    return {"saved": saved, "empty": empty}


def _inns_from_db(db_path: str, region: Optional[str], okveds: Sequence[str],
                  limit: Optional[int], refresh: bool) -> List[str]:
    """ИНН из таблицы companies. По умолчанию только те, у кого ФИО ещё нет."""
    verify = _verify()
    if not os.path.exists(db_path):
        return []
    con = verify.open_db(db_path, create=False)
    verify.ensure_company_columns(con)
    where, params = [], []
    if not refresh:
        where.append("(director IS NULL OR director = '')")
    if region:
        where.append("CAST(region AS TEXT) = ?")
        params.append(str(region))
    if okveds:
        where.append("(%s)" % " OR ".join("okved LIKE ?" for _ in okveds))
        params.extend([str(o) + "%" for o in okveds])
    sql = "SELECT inn FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY inn"
    if limit:
        sql += " LIMIT %d" % int(limit)
    try:
        rows = [r[0] for r in con.execute(sql, params).fetchall()]
    except Exception:                                  # таблицы ещё нет
        rows = []
    con.close()
    return [verify.norm_digits(r) for r in rows if verify.norm_digits(r)]


# --------------------------------------------------------------------------
# командная строка
# --------------------------------------------------------------------------

def _cli(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="fns_egrul.py",
        description="ФИО руководителя из ЕГРЮЛ по компаниям из базы конвейера",
        epilog="Без ключа --live скрипт в сеть не ходит. Пример: fns_egrul.py "
               "--db data/leads.db --inn-file /tmp/chunk_01.txt --live")
    parser.add_argument("--config", help="конфиг ниши, по умолчанию config.json")
    parser.add_argument("--db", help="файл sqlite, по умолчанию data/leads.db")
    parser.add_argument("--region", help="брать из базы только этот код региона")
    parser.add_argument("--okved", help="брать только эти ОКВЭД, через запятую")
    parser.add_argument("--inn", help="один ИНН")
    parser.add_argument("--inn-file", dest="inn_file",
                        help="файл со списком ИНН, по одному в строке")
    parser.add_argument("--limit", type=int, default=100,
                        help="сколько компаний обработать за прогон, по умолчанию 100")
    parser.add_argument("--refresh", action="store_true",
                        help="спрашивать и тех, у кого ФИО уже записано")
    parser.add_argument("--live", action="store_true",
                        help="разрешить обращение к egrul.nalog.ru")
    parser.add_argument("--offline", action="store_true",
                        help="без сети: показать, сколько компаний ждёт ФИО")
    parser.add_argument("--demo", action="store_true",
                        help="показательный прогон по четырём ИНН, требует --live")
    args = parser.parse_args(list(argv))

    verify = _verify()
    profile = verify.config_profile(verify.load_config(args.config))
    db_path = verify.resolve_db_path(args.db, profile)
    region = args.region or profile.get("region")
    okveds = verify.cfg_list(args.okved) or profile.get("okved") or []

    inns: List[str] = []
    if args.inn:
        key = verify.norm_digits(args.inn)
        if key:
            inns.append(key)
    if args.inn_file:
        inns.extend(verify.read_inn_file(args.inn_file))
    from_db = not inns
    if from_db:
        inns = _inns_from_db(db_path, region, okveds, args.limit, args.refresh)
    inns = list(dict.fromkeys(inns))[:args.limit] if args.limit else list(dict.fromkeys(inns))

    print("ЕГРЮЛ. Параметры прогона:")
    print("      конфиг  : %s" % (profile.get("path") or "не найден, беру ключи"))
    print("      база    : %s" % db_path)
    print("      источник: %s" % ("таблица companies" if from_db else "список ИНН"))
    print("      компаний: %d" % len(inns))
    print("      темп    : %.0f с на запрос, это примерно %.0f мин на прогон"
          % (THROTTLE_SEC, (len(inns) * THROTTLE_SEC) / 60.0))

    if args.demo:
        if not args.live:
            print("\nдемо ходит в сеть, поэтому запускается только с ключом --live")
            return 0
        _demo()
        return 0

    if not args.live and not args.offline:
        print("\nрежим не выбран. Добавьте --live, чтобы сходить в ЕГРЮЛ, "
              "или --offline, чтобы просто посмотреть план.")
        return 0

    if args.offline:
        print("\nрежим --offline: в сеть не иду. Запустите то же самое с --live, "
              "и ФИО лягут в колонку director.")
        return 0

    if not inns:
        print("\nсписок пуст: соберите выборку через fns_rmsp.py "
              "или передайте --inn-file")
        return 0

    print("\n[1/2] Спрашиваю ЕГРЮЛ по каждому ИНН")
    results = get_directors(inns, verbose=True)
    found = sum(1 for r in results.values() if r.get("found"))
    print("      найдено карточек: %d из %d" % (found, len(inns)))

    print("\n[2/2] Кладу ФИО в базу %s" % db_path)
    save_directors(results, db_path)
    print("\nГотово. Теперь письмо здоровается по имени, а не с коллегами.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return _cli(argv)
    except EgrulError as exc:
        print("\nОШИБКА: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
