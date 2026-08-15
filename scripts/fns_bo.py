# -*- coding: utf-8 -*-
"""
fns_bo.py — выручка и адрес из ГИР БО (bo.nalog.gov.ru).

Чистый REST JSON: без ключа, без капчи, без авторизации. Здесь лежит ВЫРУЧКА,
которой нет ни в реестре МСП, ни в ЕГРЮЛ.

=============================================================================
ГЛАВНОЕ ПРЕДУПРЕЖДЕНИЕ: gainSumFrom / gainSumTo НЕ ФИЛЬТРУЮТ
=============================================================================
Разведка проекта записала, что выручку можно фильтровать на стороне сервера.
Контрольный замер это ОПРОВЕРГ. Живые запросы 13.08.2026,
okved=10.71, address=ТАТАРСТАН, period=2025:

    без фильтра выручки                        119 записей
    gainSumFrom=10000  gainSumTo=500000        119 записей
    gainSumFrom=0      gainSumTo=1             119 записей
    gainSumFrom=999999999 gainSumTo=1000000000 119 записей

Три взаимоисключающих диапазона дают один и тот же результат, а в выдаче по
запросу «от 10 000» спокойно приходят компании с выручкой 0 и 27 тыс. руб.
Сужение 159 -> 119, которое разведка приписала фильтру выручки, на самом деле
даёт параметр period=2025.

Имена параметров при этом верные: в бандле /static/js/main.e815beb0.js есть
поля gainSumFrom и gainSumTo с подписями «Выручка (от)» и «Выручка (до)».
Публичный эндпоинт их просто игнорирует. Мы их всё равно отправляем, чтобы
всё заработало само, если ФНС однажды починит фильтр, но ГАРАНТИЮ по выручке
даёт только клиентский фильтр в этом модуле.

Что фильтрует на сервере по-настоящему (проверено):
    okved    да   10.71 -> 5431, 10.7 -> 6986, 10.71.1 -> 572
    address  да   49.41 вся РФ 65424, +ТАТАРСТАН 3402, +КАЗАНЬ 1598
    period   да   49.41: 2023 -> 45049, 2024 -> 47222, 2025 -> 45654
    inn      да   точное попадание, totalElements = 1
Не фильтруют вообще: gainSumFrom, gainSumTo, mspFlg, largest, hasAz,
published, notPublished, okopf, deadlineViolation, hasKS, bfoPeriodType, knd.
Параметр mspCategory отвечает HTTP 400, не передавайте его.

Остальные ограничения:
  1. size максимум 2000, значение 5000 сервер молча урезает до 2000.
  2. Глубина выдачи ровно 10 000: при size * page >= 10000 приходит HTTP 500.
     Дробить приходится по address и okved, см. iterate_all().
  3. address ищет подстроку по ВСЕМУ адресу, включая улицу: address=ТАТАРСТАН
     тянет компании с улицы Татарстан в Набережных Челнах. Регион чистить
     на клиенте по полю region.
  4. ГИР БО знает только тех, кто СДАВАЛ отчётность. По ОКВЭД 10.71 в
     Татарстане тут 159 организаций, а в реестре МСП 290. Это разные
     множества, и это нормально.

Требования: Python 3.9, только стандартная библиотека.
"""

import json
import math
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

BASE = "https://bo.nalog.gov.ru"
URL_SEARCH = BASE + "/advanced-search/organizations"
URL_QUICK = BASE + "/advanced-search/organizations/search"
URL_BFO = BASE + "/nbo/organizations/%s/bfo/"
URL_CARD = BASE + "/nbo/organizations/%s"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TIMEOUT = 60
RETRIES = 3
THROTTLE_SEC = 0.4

MAX_SIZE = 2000           # больше сервер всё равно урежет
DEPTH_LIMIT = 10000       # size * page должно быть строго меньше

_last_call = [0.0]


class BoError(Exception):
    """Понятная человеку ошибка при работе с ГИР БО."""
    pass


class BoDepthLimit(BoError):
    """Запрос упёрся в предел глубины выдачи 10 000 записей."""
    pass


# --------------------------------------------------------------------------
# сеть
# --------------------------------------------------------------------------

def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < THROTTLE_SEC:
        time.sleep(THROTTLE_SEC - delta)
    _last_call[0] = time.time()


def _get_json(url: str, timeout: int = TIMEOUT, retries: int = RETRIES) -> Any:
    """GET с ретраями. Возвращает разобранный JSON, попутно чистит <strong>."""
    _throttle()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": BASE + "/",
    })

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            raw = resp.read().decode("utf-8", "replace")
            try:
                return clean_strong(json.loads(raw))
            except ValueError:
                raise BoError("ГИР БО вернул не JSON. Начало ответа:\n  %s"
                              % raw[:300])
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if err.code == 500:
                # почти всегда это предел глубины, а не поломка сервера
                raise BoDepthLimit(
                    "ГИР БО ответил HTTP 500 на %s.\n"
                    "  Самая частая причина: превышен предел глубины выдачи "
                    "(size * page должно быть меньше %d).\n  Ответ: %s"
                    % (url, DEPTH_LIMIT, body or "(пусто)"))
            if err.code >= 502 and attempt < retries:
                last_err = err
                time.sleep(1.5 * attempt)
                continue
            raise BoError("ГИР БО ответил HTTP %d на %s.\n  Ответ: %s"
                          % (err.code, url, body or "(пусто)"))
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(1.5 * attempt)
                continue

    raise BoError(
        "Не смог достучаться до bo.nalog.gov.ru за %d попытки. Причина: %s.\n"
        "  Проверьте интернет и доступность домена." % (retries, last_err))


def clean_strong(obj: Any) -> Any:
    """
    Убирает подсветку совпадений <strong>...</strong> из ответа.

    Сервис оборачивает найденные подстроки в теги прямо внутри значений
    (в названии и в адресе). Если не вычистить, эти теги уедут в базу,
    а потом и в текст письма клиенту.
    """
    if isinstance(obj, str):
        return obj.replace("<strong>", "").replace("</strong>", "")
    if isinstance(obj, list):
        return [clean_strong(v) for v in obj]
    if isinstance(obj, dict):
        return dict((k, clean_strong(v)) for k, v in obj.items())
    return obj


# --------------------------------------------------------------------------
# поиск
# --------------------------------------------------------------------------

def search_organizations(okved: Optional[str] = None,
                         address: Optional[str] = None,
                         gain_from: Optional[int] = None,
                         gain_to: Optional[int] = None,
                         period: Optional[Union[str, int]] = None,
                         page: int = 0,
                         size: int = 100,
                         name: Optional[str] = None,
                         inn: Optional[str] = None,
                         ogrn: Optional[str] = None,
                         **extra) -> Dict[str, Any]:
    """
    Один запрос расширенного поиска. Возвращает разобранный JSON целиком:
    content (список организаций), totalElements, totalPages, pageable.

    gain_from / gain_to отправляются как gainSumFrom / gainSumTo, но сервер их
    ИГНОРИРУЕТ (см. шапку модуля). Отбор по выручке делайте клиентским
    filter_by_revenue или через iterate_all(revenue_from=..., revenue_to=...).

    Проверки лимитов делаются ДО обращения к сети, чтобы не ловить невнятный
    HTTP 500 в середине долгого прогона.
    """
    if size > MAX_SIZE:
        raise ValueError(
            "size=%d слишком большой. Сервер молча урезает всё, что больше %d, "
            "поэтому просите не больше %d." % (size, MAX_SIZE, MAX_SIZE))
    if size < 1:
        raise ValueError("size должен быть больше нуля")
    if page < 0:
        raise ValueError("page не может быть отрицательным")
    if size * page >= DEPTH_LIMIT:
        raise BoDepthLimit(
            "Запрошено смещение %d, а ГИР БО отдаёт максимум %d записей на один "
            "набор фильтров.\n  Дробите выборку по address или okved: "
            "используйте iterate_all()." % (size * page, DEPTH_LIMIT))

    params: List[Tuple[str, str]] = []
    if name:
        params.append(("name", name))
    if inn:
        params.append(("inn", str(inn)))
    if ogrn:
        params.append(("ogrn", str(ogrn)))
    if okved:
        params.append(("okved", str(okved)))
    if address:
        params.append(("address", address))
    # отправляем, хотя сервер их не применяет: починят, и всё заработает само
    if gain_from is not None:
        params.append(("gainSumFrom", str(int(gain_from))))
    if gain_to is not None:
        params.append(("gainSumTo", str(int(gain_to))))
    if period is not None:
        params.append(("period", str(period)))
    for key, value in extra.items():
        if value is not None:
            params.append((key, str(value)))
    params.append(("page", str(page)))
    params.append(("size", str(size)))

    url = URL_SEARCH + "?" + urllib.parse.urlencode(params)
    payload = _get_json(url)
    if not isinstance(payload, dict):
        raise BoError("Ожидался объект в ответе поиска, пришло %s"
                      % type(payload).__name__)
    return payload


def count_organizations(**kwargs) -> int:
    """Сколько всего под фильтр. Один дешёвый запрос с size=1."""
    kwargs.pop("page", None)
    kwargs.pop("size", None)
    payload = search_organizations(page=0, size=1, **kwargs)
    return int(payload.get("totalElements") or 0)


def get_bfo(org_id: Union[str, int]) -> List[Dict[str, Any]]:
    """
    Вся бухотчётность организации по годам.

    org_id — это ВНУТРЕННИЙ id ГИР БО (поле id из результата поиска),
    не ИНН и не ОГРН.

    Возвращает список записей по годам: period, actualBfoDate, actives,
    organizationInfo с полным адресом, ОКОПФ и ОКФС.
    """
    payload = _get_json(URL_BFO % org_id)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


def get_card(org_id: Union[str, int]) -> Dict[str, Any]:
    """Полная карточка организации по внутреннему id ГИР БО."""
    payload = _get_json(URL_CARD % org_id)
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# нормализация и клиентские фильтры
# --------------------------------------------------------------------------

def normalize(org: Dict[str, Any]) -> Dict[str, Any]:
    """Приводит организацию к плоскому виду, годному для записи в базу."""
    bfo = org.get("bfo") or {}
    parts = [org.get("index"), org.get("region"), org.get("district"),
             org.get("city"), org.get("settlement"), org.get("street"),
             org.get("house"), org.get("building"), org.get("office")]
    address = ", ".join(str(p).strip() for p in parts
                        if p not in (None, "", " "))
    return {
        "bo_id": org.get("id"),
        "inn": (org.get("inn") or "").strip() or None,
        "ogrn": (org.get("ogrn") or "").strip() or None,
        "name": (org.get("shortName") or org.get("fullName") or "").strip() or None,
        "okved": org.get("okved2"),
        "region_name": org.get("region"),
        "city": org.get("city"),
        "address": address or None,
        "status": org.get("statusCode"),
        "status_date": org.get("statusDate"),
        "revenue": bfo.get("gainSum"),          # тыс. руб.
        "revenue_period": bfo.get("period"),
        "bfo_date": bfo.get("actualBfoDate"),
        "has_audit": bfo.get("hasAz"),
    }


def filter_by_revenue(records: Sequence[Dict[str, Any]],
                      revenue_from: Optional[int] = None,
                      revenue_to: Optional[int] = None,
                      drop_unknown_revenue: bool = True) -> List[Dict[str, Any]]:
    """
    КЛИЕНТСКИЙ фильтр по выручке, в тыс. руб. Именно он даёт гарантию,
    потому что серверный gainSumFrom / gainSumTo не работает.

    drop_unknown_revenue решает судьбу компаний без сданной отчётности
    (revenue = None). Таких много: в живом замере 22 из 100. Выбор явный,
    потому что оба варианта осмысленны:
      True  — строгий отбор, в выборке только подтверждённая выручка
      False — оставить их как лидов с неизвестной выручкой
    """
    out = []
    for rec in records:
        revenue = rec.get("revenue")
        if revenue is None:
            if drop_unknown_revenue and (revenue_from is not None
                                         or revenue_to is not None):
                continue
            out.append(rec)
            continue
        if revenue_from is not None and revenue < revenue_from:
            continue
        if revenue_to is not None and revenue > revenue_to:
            continue
        out.append(rec)
    return out


def _region_matches(org: Dict[str, Any], region: str) -> bool:
    return region.strip().lower() in (org.get("region") or "").strip().lower()


def _as_list(value: Union[None, str, Sequence]) -> List[Optional[str]]:
    if value is None:
        return [None]
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or [None]
    return [str(value)]


# --------------------------------------------------------------------------
# полная выгрузка
# --------------------------------------------------------------------------

def iterate_all(okved: Union[None, str, Sequence] = None,
                address: Union[None, str, Sequence] = None,
                region: Optional[str] = None,
                period: Optional[Union[str, int]] = None,
                gain_from: Optional[int] = None,
                gain_to: Optional[int] = None,
                revenue_from: Optional[int] = None,
                revenue_to: Optional[int] = None,
                drop_unknown_revenue: bool = True,
                size: int = MAX_SIZE,
                max_records: Optional[int] = None,
                verbose: bool = True,
                **extra) -> List[Dict[str, Any]]:
    """
    Забирает выборку целиком, обходя предел глубины в 10 000 записей.

    Дробление идёт по тем осям, которые сервер РЕАЛЬНО фильтрует: okved и
    address. Оба принимают список, и тогда выборка идёт по всем сочетаниям:

        iterate_all(okved=['10.71', '10.72'],
                    address=['ТАТАРСТАН'], region='ТАТАРСТАН',
                    period=2025, revenue_from=50000)

    Дробления по выручке здесь НЕТ и быть не может: серверный фильтр
    gainSumFrom / gainSumTo не работает (см. шапку модуля), поэтому резать
    диапазон выручки бессмысленно, счётчик от этого не меняется.

    revenue_from / revenue_to — клиентский фильтр, применяется к уже
    полученным записям. gain_from / gain_to передаются серверу как есть,
    на всякий случай.

    region фильтруется НА КЛИЕНТЕ по полю region, потому что серверный address
    ищет подстроку по всему адресу вместе с улицей.

    Дедупликация по ИНН обязательна и делается всегда: адресные срезы могут
    пересекаться, например КАЗАНЬ целиком лежит внутри ТАТАРСТАН.

    Если отдельный срез всё равно больше 10 000, печатается внятное
    предупреждение и берутся первые 10 000: молча терять данные нельзя.
    """
    if size > MAX_SIZE:
        size = MAX_SIZE

    collected: Dict[str, Dict[str, Any]] = {}
    dropped_by_region = [0]
    requests_made = [0]
    truncated: List[str] = []

    def log(message: str) -> None:
        if verbose:
            print("      " + message)

    slices = [(o, a) for o in _as_list(okved) for a in _as_list(address)]

    for okved_value, address_value in slices:
        label = "окв=%s адрес=%s" % (okved_value or "любой",
                                     address_value or "любой")
        total = count_organizations(okved=okved_value, address=address_value,
                                    period=period, gain_from=gain_from,
                                    gain_to=gain_to, **extra)
        requests_made[0] += 1
        if total == 0:
            log("%s: пусто" % label)
            continue

        if total > DEPTH_LIMIT:
            truncated.append(label)
            log("%s: %d записей, это больше предела %d."
                % (label, total, DEPTH_LIMIT))
            log("  беру первые %d. Чтобы забрать всё, передайте более узкий "
                "список okved или address (например по городам)." % DEPTH_LIMIT)
        else:
            log("%s: %d записей, забираю" % (label, total))

        pages = int(math.ceil(float(min(total, DEPTH_LIMIT)) / size))
        stop = False
        for page in range(pages):
            if size * page >= DEPTH_LIMIT:
                break
            payload = search_organizations(
                okved=okved_value, address=address_value, period=period,
                gain_from=gain_from, gain_to=gain_to,
                page=page, size=size, **extra)
            requests_made[0] += 1
            chunk = payload.get("content") or []
            if not chunk:
                break
            for org in chunk:
                if region and not _region_matches(org, region):
                    dropped_by_region[0] += 1
                    continue
                rec = normalize(org)
                if rec["inn"]:
                    collected[rec["inn"]] = rec
            if max_records is not None and len(collected) >= max_records:
                stop = True
                break
        if stop:
            break

    result = list(collected.values())
    before_revenue = len(result)
    result = filter_by_revenue(result, revenue_from, revenue_to,
                               drop_unknown_revenue)

    if verbose:
        print("      уникальных ИНН получено: %d, запросов к серверу: %d"
              % (before_revenue, requests_made[0]))
        if region:
            print("      отсеяно клиентским фильтром по региону '%s': %d"
                  % (region, dropped_by_region[0]))
        if revenue_from is not None or revenue_to is not None:
            print("      отсеяно клиентским фильтром по выручке: %d, осталось %d"
                  % (before_revenue - len(result), len(result)))
        if truncated:
            print("      ВНИМАНИЕ: срезы обрезаны по пределу глубины: %s"
                  % ", ".join(truncated))

    if max_records is not None:
        result = result[:max_records]
    return result


def revenue_by_inn(inns: Sequence[str],
                   period: Optional[Union[str, int]] = None,
                   verbose: bool = True) -> Dict[str, Dict[str, Any]]:
    """
    Точечное обогащение выручкой по списку ИНН, один запрос на ИНН.

    Фильтр inn на сервере работает точно: totalElements = 1.
    Годится для десятков и сотен компаний. Если их тысячи, дешевле выгрузить
    нишу целиком через iterate_all и сопоставить локально по ИНН.
    """
    out: Dict[str, Dict[str, Any]] = {}
    total = len(inns)
    for i, inn in enumerate(inns, 1):
        key = str(inn).strip()
        try:
            payload = search_organizations(inn=key, period=period,
                                           page=0, size=10)
        except BoError as exc:
            if verbose:
                print("      %s: пропускаю, ошибка (%s)"
                      % (key, str(exc).split("\n")[0]))
            continue
        for org in (payload.get("content") or []):
            if (org.get("inn") or "").strip() == key:
                out[key] = normalize(org)
                break
        if verbose and i % 25 == 0:
            print("      обработано %d из %d" % (i, total))
    return out


# --------------------------------------------------------------------------
# демо
# --------------------------------------------------------------------------

def _money(value: Optional[int]) -> str:
    if value is None:
        return "нет данных"
    return "{:,}".format(int(value)).replace(",", " ") + " тыс."


def _demo() -> None:
    okved = "10.71"
    address = "ТАТАРСТАН"
    period = 2025

    print("=" * 74)
    print("ГИР БО. Демо: ОКВЭД %s, адрес содержит '%s', период %s"
          % (okved, address, period))
    print("=" * 74)

    print("\n[1/5] Что сервер фильтрует по-настоящему")
    print("      okved=%s по всей РФ:          %d" % (okved, count_organizations(okved=okved)))
    print("      + address=%s:          %d" % (address, count_organizations(okved=okved, address=address)))
    print("      + period=%s:                %d" % (period, count_organizations(okved=okved, address=address, period=period)))

    print("\n[2/5] ЛОВУШКА: фильтр по выручке gainSumFrom/gainSumTo НЕ РАБОТАЕТ")
    base = dict(okved=okved, address=address, period=period)
    probes = [
        ("без фильтра выручки          ", {}),
        ("выручка 10 000 .. 500 000    ", {"gain_from": 10000, "gain_to": 500000}),
        ("выручка 0 .. 1               ", {"gain_from": 0, "gain_to": 1}),
        ("выручка 999 999 999 .. 10^9  ", {"gain_from": 999999999, "gain_to": 1000000000}),
    ]
    for title, extra in probes:
        print("      %s -> %d" % (title, count_organizations(**dict(base, **extra))))
    print("      Три несовместимых диапазона дают одно и то же число.")
    print("      Сужение 159 -> 119 даёт period, а не выручка.")

    payload = search_organizations(size=100, **dict(base, gain_from=10000, gain_to=500000))
    revenues = [(o.get("bfo") or {}).get("gainSum") for o in payload.get("content") or []]
    known = [r for r in revenues if r is not None]
    below = [r for r in known if r < 10000]
    print("      В ответе на «выручка от 10 000» пришло %d компаний ниже порога,"
          % len(below))
    print("      самые маленькие значения: %s" % sorted(known)[:6])

    print("\n[3/5] Клиентский фильтр по выручке, он и даёт гарантию")
    orgs = [normalize(o) for o in (payload.get("content") or [])]
    strict = filter_by_revenue(orgs, revenue_from=10000, revenue_to=500000)
    soft = filter_by_revenue(orgs, revenue_from=10000, revenue_to=500000,
                             drop_unknown_revenue=False)
    print("      из %d записей страницы:" % len(orgs))
    print("        строго в диапазоне 10 000..500 000        %d" % len(strict))
    print("        плюс компании без сданной отчётности      %d" % len(soft))
    for rec in strict[:3]:
        print("        %-12s %-34s %s"
              % (rec["inn"], (rec["name"] or "")[:34], _money(rec["revenue"])))

    print("\n[4/5] Предел глубины выдачи: size * page < %d" % DEPTH_LIMIT)
    try:
        search_organizations(okved="49.41", page=5, size=2000)
        print("      неожиданно: сервер отдал ответ на смещении 10000")
    except BoDepthLimit as exc:
        print("      защита сработала ДО запроса: %s" % str(exc).split("\n")[0])

    print("\n[5/5] Полная выгрузка: iterate_all с чисткой региона и выручки")
    records = iterate_all(okved=okved, address=address, region="ТАТАРСТАН",
                          period=period, revenue_from=50000,
                          drop_unknown_revenue=True, size=MAX_SIZE)
    print("      компаний с выручкой от 50 млн руб.: %d" % len(records))

    records.sort(key=lambda r: r.get("revenue") or 0, reverse=True)
    print("\n      топ-3 по выручке:")
    for rec in records[:3]:
        print("        %-12s %-32s %s"
              % (rec["inn"], (rec["name"] or "")[:32], _money(rec["revenue"])))

    if records:
        top = records[0]
        print("\n      бухотчётность по годам для %s (внутренний id %s):"
              % (top["inn"], top["bo_id"]))
        for row in get_bfo(top["bo_id"])[:4]:
            info = row.get("organizationInfo") or {}
            print("        период %s, сдано %s, активы %s"
                  % (row.get("period"), row.get("actualBfoDate"), row.get("actives")))
            print("          адрес: %s" % (info.get("address") or "")[:60])

    print("\nГотово. Выручка приклеивается к ИНН из fns_rmsp.py, "
          "дальше ФИО директора берём в fns_egrul.py.")


# --------------------------------------------------------------------------
# запись выручки в базу конвейера
# --------------------------------------------------------------------------

import os  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _verify():
    """Модуль verify лежит рядом. Импорт ленивый, чтобы --help ничего не грузил."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import verify
    return verify


def company_row(rec: Dict[str, Any], stamp: str) -> Dict[str, Any]:
    """Запись ГИР БО в строку таблицы companies."""
    return {
        "inn": rec.get("inn"),
        "ogrn": rec.get("ogrn"),
        "name": rec.get("name"),
        "okved": rec.get("okved"),
        "city": rec.get("city"),
        "address": rec.get("address"),
        "revenue": rec.get("revenue"),
        "revenue_period": rec.get("revenue_period"),
        "status": rec.get("status"),
        "status_date": rec.get("status_date"),
        "collected_at": stamp,
    }


def save_to_db(records: Sequence[Dict[str, Any]], db_path: str,
               verbose: bool = True) -> Dict[str, int]:
    """
    Кладёт выручку и адрес в таблицу companies по ИНН.

    Запись идёт через db_upsert_company: повторный прогон обновляет строку,
    а не добавляет вторую, и не затирает пустотой уже собранные поля.
    """
    verify = _verify()
    con = verify.open_db(db_path)
    verify.ensure_company_columns(con)
    stamp = verify.now_stamp()
    written, with_revenue = 0, 0
    for rec in records:
        inn = verify.norm_digits(rec.get("inn"))
        if not inn:
            continue
        row = company_row(rec, stamp)
        row["inn"] = inn
        # реестр МСП это источник с весом 1.0, поэтому название и ОКВЭД из него
        # не перебиваем: ГИР БО тут только дозаполняет пустые места
        known = con.execute("SELECT name, okved FROM companies WHERE inn = ?",
                            (inn,)).fetchone()
        if known:
            for field in ("name", "okved"):
                if known[field]:
                    row.pop(field, None)
        if verify.db_upsert_company(con, row):
            written += 1
            if rec.get("revenue") is not None:
                with_revenue += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    con.close()
    if verbose:
        print("      обновлено компаний: %d, из них с выручкой: %d"
              % (written, with_revenue))
        print("      всего уникальных компаний в базе: %d" % total)
    return {"written": written, "with_revenue": with_revenue, "total": total}


def _inns_from_db(db_path: str, region: Optional[str], okveds: Sequence[str],
                  limit: Optional[int], refresh: bool) -> List[str]:
    """ИНН из companies. По умолчанию только те, у кого выручка ещё не собрана."""
    verify = _verify()
    if not os.path.exists(db_path):
        return []
    con = verify.open_db(db_path, create=False)
    verify.ensure_company_columns(con)
    where, params = [], []
    if not refresh:
        where.append("(revenue IS NULL OR revenue = '')")
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
        prog="fns_bo.py",
        description="Выручка из ГИР БО для компаний из базы конвейера",
        epilog="Без ключа --live скрипт в сеть не ходит. Пример: fns_bo.py "
               "--config config.json --db data/leads.db --limit 200 --live")
    parser.add_argument("--config", help="конфиг ниши, по умолчанию config.json")
    parser.add_argument("--db", help="файл sqlite, по умолчанию data/leads.db")
    parser.add_argument("--region", help="код региона ФНС для отбора из базы, "
                                         "либо название региона для режима --niche")
    parser.add_argument("--okved", help="коды ОКВЭД через запятую")
    parser.add_argument("--inn", help="один ИНН")
    parser.add_argument("--inn-file", dest="inn_file",
                        help="файл со списком ИНН, по одному в строке")
    parser.add_argument("--period", help="отчётный год, например 2025")
    parser.add_argument("--limit", type=int, default=200,
                        help="сколько компаний обработать за прогон, по умолчанию 200")
    parser.add_argument("--refresh", action="store_true",
                        help="спрашивать и тех, у кого выручка уже записана")
    parser.add_argument("--niche", action="store_true",
                        help="выгрузить нишу целиком по ОКВЭД и региону, "
                             "а не добирать выручку по списку ИНН")
    parser.add_argument("--live", action="store_true",
                        help="разрешить обращение к bo.nalog.gov.ru")
    parser.add_argument("--offline", action="store_true",
                        help="без сети: показать, сколько компаний ждёт выручку")
    parser.add_argument("--demo", action="store_true",
                        help="показательный прогон по Татарстану, требует --live")
    args = parser.parse_args(list(argv))

    verify = _verify()
    profile = verify.config_profile(verify.load_config(args.config))
    db_path = verify.resolve_db_path(args.db, profile)
    okveds = verify.cfg_list(args.okved) or profile.get("okved") or []
    period = args.period or profile.get("period")
    revenue_from = profile.get("revenue_from")
    revenue_to = profile.get("revenue_to")

    region_arg = args.region or profile.get("region")
    region_name = profile.get("region_name")
    if args.region and not str(args.region).isdigit():
        region_name = args.region
    region_code = str(region_arg) if region_arg and str(region_arg).isdigit() else None

    inns: List[str] = []
    if not args.niche:
        if args.inn:
            key = verify.norm_digits(args.inn)
            if key:
                inns.append(key)
        if args.inn_file:
            inns.extend(verify.read_inn_file(args.inn_file))
        if not inns:
            inns = _inns_from_db(db_path, region_code, okveds, args.limit,
                                 args.refresh)
        inns = list(dict.fromkeys(inns))[:args.limit]

    print("ГИР БО. Параметры прогона:")
    print("      конфиг : %s" % (profile.get("path") or "не найден, беру ключи"))
    print("      база   : %s" % db_path)
    print("      период : %s" % (period or "любой"))
    print("      режим  : %s" % ("выгрузка ниши целиком"
                                 if args.niche else "добор выручки по ИНН"))
    if args.niche:
        print("      ОКВЭД  : %s" % (", ".join(okveds) or "любой"))
        print("      адрес  : %s" % (region_name or "не задан"))
    else:
        print("      компаний: %d" % len(inns))
        print("      темп   : %.1f с на запрос, это примерно %.0f мин на прогон"
              % (THROTTLE_SEC, (len(inns) * THROTTLE_SEC) / 60.0))

    if args.demo:
        if not args.live:
            print("\nдемо ходит в сеть, поэтому запускается только с ключом --live")
            return 0
        _demo()
        return 0

    if not args.live and not args.offline:
        print("\nрежим не выбран. Добавьте --live, чтобы сходить в ГИР БО, "
              "или --offline, чтобы просто посмотреть план.")
        return 0

    if args.offline:
        print("\nрежим --offline: в сеть не иду. Запустите то же самое с --live, "
              "и выручка ляжет в колонку revenue.")
        return 0

    if args.niche:
        if region_code and not region_name:
            print("\nГИР БО ищет по тексту адреса, а не по коду региона. "
                  "Укажите название: --region ТАТАРСТАН")
            return 1
        print("\n[1/2] Выгружаю нишу из ГИР БО")
        records = iterate_all(okved=okveds or None,
                              address=region_name or None,
                              region=region_name or None,
                              period=period,
                              revenue_from=revenue_from, revenue_to=revenue_to,
                              drop_unknown_revenue=False,
                              size=profile.get("bo_page_size") or MAX_SIZE,
                              max_records=args.limit)
    else:
        if not inns:
            print("\nсписок пуст: соберите выборку через fns_rmsp.py "
                  "или передайте --inn-file")
            return 0
        print("\n[1/2] Спрашиваю выручку по каждому ИНН")
        found = revenue_by_inn(inns, period=period, verbose=True)
        records = list(found.values())
        print("      карточек получено: %d из %d" % (len(records), len(inns)))

    print("\n[2/2] Кладу в базу %s" % db_path)
    save_to_db(records, db_path)
    print("\nГотово. Выручка в тысячах рублей, поле revenue таблицы companies.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        return _cli(argv)
    except BoError as exc:
        print("\nОШИБКА: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
