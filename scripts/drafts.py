# -*- coding: utf-8 -*-
"""
drafts.py: письма ложатся ЧЕРНОВИКАМИ в почтовый ящик, а не улетают сами.

Это принципиальный момент конвейера. Скрипт ничего не отправляет. Он кладёт
готовое письмо в папку черновиков по IMAP, а человек открывает почту,
читает, правит и жмёт «Отправить» сам. Так рассылка остаётся под контролем,
а ошибка в одном письме не превращается в ошибку в трёхстах.

  append_draft(...)  положить черновик в ящик по IMAP APPEND
  dry_run(...)       то же самое без сети, файлом .eml в out/drafts/
  find_drafts_folder имя папки черновиков у конкретного провайдера

Стоп-лист сильнее очереди писем: перед раскладкой каждый ИНН проверяется по
таблице stoplist, и тот, кто попросил не писать, черновика не получит.

Пароли и логины читаются из config.json и НЕ хранятся в коде. Файл ищется
по цепочке: переменная AIOP_CONFIG, потом рядом со скриптами, потом рабочая
папка, потом ~/.claude/tools/prodazhi/config.json. Ожидаемый вид файла
описан в config.example.json, блок «pochta». Короткий вид тоже понимается:

  {
    "imap": {
      "host": "imap.yandex.ru",
      "port": 993,
      "login": "you@example.ru",
      "app_password": "пароль приложения, не основной пароль от почты",
      "from": "Имя Фамилия <you@example.ru>",
      "drafts_folder": null
    },
    "sender": {"name": "Имя", "company": "Компания", "phone": "+7 900 000-00-00"}
  }

config.json должен быть в .gitignore.

Python 3.9, только стандартная библиотека.
"""

import os
import re
import ssl
import sys
import json
import time
import email.utils
import imaplib
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Конфиг ищется по цепочке: AIOP_CONFIG, потом рядом со скриптами (корень
# репозитория или установленный пак), потом рабочая папка, потом стандартное
# место установки. Так скрипт находит доступы к почте из любой папки.
INSTALLED_PACK = os.path.join(os.path.expanduser("~"), ".claude", "tools", "prodazhi")
CONFIG_PATH = os.path.join(ROOT, "config.json")
DRAFTS_DIR = os.path.join(ROOT, "out", "drafts")


def find_config():
    """Путь к рабочему config.json. Файла может не быть, это не падение."""
    env = os.environ.get("AIOP_CONFIG")
    if env:
        return env
    candidates = (CONFIG_PATH,
                  os.path.join(os.getcwd(), "config.json"),
                  os.path.join(INSTALLED_PACK, "config.json"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return CONFIG_PATH

# Имена папки черновиков у разных провайдеров, в порядке проверки.
DRAFT_FOLDER_CANDIDATES = ("[Gmail]/Drafts", "Drafts", "Черновики", "INBOX.Drafts",
                           "INBOX.Черновики", "[Gmail]/Черновики")

# Подсказка по хосту IMAP, чтобы не искать его руками.
IMAP_HOSTS = {
    "yandex.ru": "imap.yandex.ru", "ya.ru": "imap.yandex.ru",
    "mail.ru": "imap.mail.ru", "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru", "list.ru": "imap.mail.ru",
    "internet.ru": "imap.mail.ru",
    "gmail.com": "imap.gmail.com",
    "rambler.ru": "imap.rambler.ru",
}

RX_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')


# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------

def load_config(path=None):
    """Прочитать config.json. Отсутствие файла это не падение, а пустой словарь."""
    path = path or find_config()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def imap_settings(config=None, config_path=None):
    """Доступы к почте из конфигурации.

    Основной вид описан в config.example.json, блок «pochta»:
      imap_host, imap_port, login, parol, papka_chernovikov
    Короткий английский блок «imap» тоже понимается, он удобнее в тестах.
    Чего не хватает, достраивается по домену логина.
    """
    cfg = config if config is not None else load_config(config_path)
    pochta = cfg.get("pochta") or {}
    imap = {
        "host": pochta.get("imap_host") or None,
        "port": pochta.get("imap_port") or 993,
        "login": pochta.get("login") or None,
        "app_password": pochta.get("parol") or None,
        "from": pochta.get("ot_kogo") or pochta.get("login") or None,
        "drafts_folder": pochta.get("papka_chernovikov") or None,
    }
    for key, value in (cfg.get("imap") or {}).items():
        if value not in (None, ""):
            imap[key] = value
    imap = {k: (v if v != "" else None) for k, v in imap.items()}
    login = imap.get("login") or ""
    if not imap.get("host") and "@" in login:
        imap["host"] = IMAP_HOSTS.get(login.split("@", 1)[1].lower())
    if not imap.get("from"):
        imap["from"] = login
    imap["port"] = imap.get("port") or 993
    limits = cfg.get("pisma") or {}
    imap["daily_limit"] = limits.get("limit_v_den") or 50
    imap["pause"] = limits.get("pauza_mezhdu_pismami_sek") or 0
    return imap


def guess_imap_host(address):
    if "@" not in str(address or ""):
        return None
    return IMAP_HOSTS.get(str(address).split("@", 1)[1].lower())


# ----------------------------------------------------------------------------
# Письмо
# ----------------------------------------------------------------------------

def build_message(to_addr, subject, body, from_addr, reply_to=None, cc=None,
                  headers=None):
    """Собрать письмо в UTF-8.

    Тема с кириллицей кодируется по RFC 2047 автоматически, тело уходит
    в utf-8. Без этого Outlook и почта Mail.ru показывают кракозябры.
    """
    msg = EmailMessage()
    msg["Subject"] = subject or ""
    msg["From"] = from_addr or ""
    msg["To"] = to_addr or ""
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Date"] = email.utils.formatdate(localtime=True)
    domain = None
    addr = email.utils.parseaddr(from_addr or "")[1]
    if "@" in addr:
        domain = addr.split("@", 1)[1]
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    # RFC 8058: заголовок отписки в один клик. Mail.ru и Yandex 360 учитывают
    # его при оценке рассылок и показывают получателю кнопку «Отписаться» в
    # шапке письма. Текстовая строка отписки в теле письма этот заголовок не
    # заменяет, почтовые фильтры её не читают.
    if addr:
        msg["List-Unsubscribe"] = "<mailto:%s?subject=unsubscribe>" % addr
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body or "", subtype="plain", charset="utf-8")
    return msg


def message_bytes(msg):
    """Байты письма с переводом строки CRLF, как требует IMAP APPEND."""
    try:
        return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
    except Exception:
        return msg.as_bytes()


# ----------------------------------------------------------------------------
# Имя папки черновиков
# ----------------------------------------------------------------------------

def imap_utf7_encode(name):
    """Кириллица в модифицированный UTF-7 из RFC 3501.

    Папка «Черновики» в протоколе IMAP выглядит как &BCcENQRABD0EPgQyBDgEOgQ4-,
    и без этой конверсии сервер её просто не найдёт.
    """
    out = []
    buf = []

    def flush():
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        b64 = _b64(raw).replace("/", ",")
        out.append("&" + b64 + "-")
        del buf[:]

    for ch in name or "":
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name):
    """Обратное преобразование, чтобы показать имя папки человеку."""
    if not name:
        return name
    out = []
    i = 0
    text = name if isinstance(name, str) else name.decode("ascii", "replace")
    while i < len(text):
        ch = text[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = text.find("-", i)
        if end < 0:
            out.append(text[i:])
            break
        chunk = text[i + 1:end]
        if chunk == "":
            out.append("&")
        else:
            padded = chunk.replace(",", "/")
            padded += "=" * ((4 - len(padded) % 4) % 4)
            try:
                import base64
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except Exception:
                out.append(text[i:end + 1])
        i = end + 1
    return "".join(out)


def _b64(raw):
    import base64
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _parse_list_line(line):
    if isinstance(line, bytes):
        line = line.decode("ascii", "replace")
    m = RX_LIST_LINE.match(line.strip())
    if not m:
        return None, None
    flags = m.group("flags").split()
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return flags, name


def quote_mailbox(name):
    if not name:
        return name
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


def find_drafts_folder(imap, verbose=False):
    """Найти папку черновиков у конкретного сервера.

    Сначала честный путь: в ответе LIST у нужной папки стоит флаг \\Drafts,
    это RFC 6154, его отдают и Gmail, и Яндекс, и Mail.ru. Если флага нет,
    перебираем известные имена, включая русское «Черновики» в кодировке IMAP.
    """
    listed = []
    try:
        typ, data = imap.list()
        if typ == "OK":
            for line in data or []:
                flags, name = _parse_list_line(line)
                if not name:
                    continue
                listed.append((flags or [], name))
                if any(f.lower() == "\\drafts" for f in (flags or [])):
                    if verbose:
                        print("    папка найдена по флагу \\Drafts: %s"
                              % imap_utf7_decode(name))
                    return name
    except imaplib.IMAP4.error:
        pass

    names = [n for _, n in listed]
    for candidate in DRAFT_FOLDER_CANDIDATES:
        encoded = imap_utf7_encode(candidate)
        for name in names:
            if name == candidate or name == encoded:
                if verbose:
                    print("    папка найдена по имени: %s" % candidate)
                return name
    for candidate in DRAFT_FOLDER_CANDIDATES:
        encoded = imap_utf7_encode(candidate)
        try:
            typ, _ = imap.select(quote_mailbox(encoded), readonly=True)
            if typ == "OK":
                if verbose:
                    print("    папка отозвалась на SELECT: %s" % candidate)
                return encoded
        except imaplib.IMAP4.error:
            continue
    return None


# ----------------------------------------------------------------------------
# Запись черновика
# ----------------------------------------------------------------------------

def append_draft(imap_host, login, app_password, to_addr, subject, body,
                 from_addr, port=993, folder=None, verbose=False, msg=None):
    """Положить письмо в папку черновиков по IMAP.

    Возвращает словарь с результатом. Исключения наружу не летят: в конвейере
    из трёхсот писем одно упавшее не должно ронять весь прогон.
    """
    result = {"ok": False, "to": to_addr, "folder": None, "message_id": None,
              "error": None, "host": imap_host}
    if not imap_host:
        result["error"] = "не задан хост IMAP, проверьте config.json"
        return result
    if not to_addr:
        result["error"] = "не задан адрес получателя"
        return result

    msg = msg or build_message(to_addr, subject, body, from_addr)
    result["message_id"] = msg.get("Message-ID")
    raw = message_bytes(msg)

    imap = None
    try:
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL(imap_host, port, ssl_context=ctx)
        imap.login(login, app_password)
        box = folder or find_drafts_folder(imap, verbose=verbose)
        if not box:
            result["error"] = ("папка черновиков не найдена, укажите её вручную "
                               "в config.json полем drafts_folder")
            return result
        result["folder"] = imap_utf7_decode(box)
        typ, data = imap.append(quote_mailbox(box), "\\Draft",
                                imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            result["error"] = "сервер отказал: %s %s" % (typ, data)
            return result
        result["ok"] = True
        return result
    except imaplib.IMAP4.error as e:
        result["error"] = "IMAP: %s" % e
        return result
    except (ssl.SSLError, OSError) as e:
        result["error"] = "%s: %s" % (type(e).__name__, e)
        return result
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def dry_run(to_addr, subject, body, from_addr, out_dir=None, name_hint=None,
            msg=None):
    """Тот же черновик, но файлом .eml на диске, без подключения к почте.

    Файл открывается двойным щелчком в любом почтовом клиенте. Это режим
    для репетиции: посмотреть все триста писем глазами и только потом
    включать IMAP.
    """
    out_dir = out_dir or DRAFTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    msg = msg or build_message(to_addr, subject, body, from_addr)
    stem = _safe_name(name_hint or to_addr or "draft")
    path = os.path.join(out_dir, "%s.eml" % stem)
    data = message_bytes(msg)
    # Один черновик — один файл. Повторный dry-run не плодит stem-2.eml:
    # тот же байт-в-байт файл пропускаем, изменившееся письмо перезаписываем.
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                if fh.read() == data:
                    return path
        except OSError:
            pass
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _safe_name(text):
    stem = re.sub(r'[^\w.@-]+', "-", str(text or "draft"), flags=re.UNICODE)
    return stem.strip("-")[:80] or "draft"


def append_many(items, config=None, config_path=None, dry=True, verbose=True):
    """Пачка писем. dry=True складывает .eml, dry=False пишет в почту.

    items это список словарей с ключами to, subject, body и, по желанию, inn.
    """
    settings = imap_settings(config, config_path)
    from_addr = settings.get("from") or settings.get("login") or ""
    results = []
    for item in items or []:
        hint = item.get("inn") or item.get("to")
        if dry:
            path = dry_run(item.get("to"), item.get("subject"), item.get("body"),
                           from_addr, name_hint=hint)
            res = {"ok": True, "to": item.get("to"), "file": path, "error": None}
        else:
            res = append_draft(settings.get("host"), settings.get("login"),
                               settings.get("app_password"), item.get("to"),
                               item.get("subject"), item.get("body"), from_addr,
                               port=settings.get("port", 993),
                               folder=settings.get("drafts_folder"))
        results.append(res)
        if verbose:
            mark = "готово" if res["ok"] else "ошибка"
            print("    %-7s %-32s %s" % (mark, res.get("to"),
                                         res.get("file") or res.get("error") or
                                         res.get("folder") or ""))
    return results


# ----------------------------------------------------------------------------
# Демо
# ----------------------------------------------------------------------------

# Это письмо одной конкретной демо-ниши (транспортная компания ищет
# производителей, которым нужны перевозки). У вас будет своя ниша и свой
# текст: продукт не про перевозки, это конвейер холодной лидогенерации для
# любого B2B. Реальный текст письма собирается по шаблону DEFAULT_TEMPLATE
# из scripts/write_letter.py, а оффер и вопрос в конце берутся из
# config.json, блок pisma, поля offer и cta (см. config.example.json).
DEMO_LETTER = u"""Здравствуйте, Ильшат Рафикович.

Смотрел реестр по вашей отрасли: производство хлеба, Набережные Челны, \
в штате 76 человек.

Заодно посмотрел сайт: на нём нет счётчика Метрики, то есть заявки \
и посетителей никто не считает.

Мы возим грузы по Татарстану и в соседние регионы, машины свои. \
Могу посчитать ваш маршрут и прислать цифры для сравнения.

Дмитрий, транспортная компания, Казань
"""


def _demo():
    print("=" * 72)
    print("drafts.py: демо")
    print("=" * 72)

    print("\n[1] Имена папки черновиков в кодировке IMAP")
    for candidate in DRAFT_FOLDER_CANDIDATES[:4]:
        encoded = imap_utf7_encode(candidate)
        back = imap_utf7_decode(encoded)
        print("    %-16s -> %-28s -> %s %s"
              % (candidate, encoded, back, "" if back == candidate else "РАСХОЖДЕНИЕ"))

    print("\n[2] Определение папки на образцах ответа LIST разных провайдеров")

    class _FakeIMAP(object):
        def __init__(self, lines):
            self._lines = lines

        def list(self):
            return "OK", self._lines

        def select(self, box, readonly=False):
            return "NO", None

    samples = [
        ("Gmail", [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
                   b'(\\Noselect \\HasChildren) "/" "[Gmail]"']),
        ("Яндекс", [b'(\\HasNoChildren \\Drafts) "|" "&BCcENQRABD0EPgQyBDgEOgQ4-"']),
        ("сервер без флагов", [b'(\\HasNoChildren) "." "INBOX.Drafts"']),
        ("папки черновиков нет", [b'(\\HasNoChildren) "/" "INBOX"']),
    ]
    for label, lines in samples:
        found = find_drafts_folder(_FakeIMAP(lines))
        print("    %-20s -> %s" % (label, imap_utf7_decode(found) if found
                                   else "не нашлась, нужен drafts_folder в config.json"))

    print("\n[3] Черновик без сети, файл .eml")
    path = dry_run("sales@chelny-hleb.ru", "Перевозки для Челны-хлеб",
                   DEMO_LETTER, "Дмитрий <d@example.ru>", name_hint="1650027925")
    print("    файл:", path)
    with open(path, "rb") as fh:
        head = fh.read(400).decode("utf-8", "replace")
    print("    первые строки письма:")
    for line in head.splitlines()[:8]:
        print("      " + line)

    print("\n[4] Настройки почты из config.json")
    settings = imap_settings()
    if settings.get("host") and settings.get("login"):
        print("    хост:  ", settings["host"])
        print("    логин: ", settings["login"])
        print("    папка: ", settings.get("drafts_folder") or "определится сама по LIST")
    else:
        print("    config.json пока нет или в нём не заполнен блок imap")
        print("    это нормально: без него работает режим dry_run")
        print("    подсказка по хосту: %s" % guess_imap_host("you@yandex.ru"))

    if "--live" not in sys.argv:
        print("\n[5] Запись в почту пропущена. Чтобы попробовать по-настоящему,")
        print("    заполните config.json и запустите: python3 drafts.py --live")
        return

    print("\n[5] Живая запись черновика в почтовый ящик")
    if not (settings.get("host") and settings.get("login") and settings.get("app_password")):
        print("    в config.json нет хоста, логина или пароля приложения")
        return
    res = append_draft(settings["host"], settings["login"], settings["app_password"],
                       settings.get("login"), "Проверка конвейера",
                       DEMO_LETTER, settings.get("from"),
                       port=settings.get("port", 993),
                       folder=settings.get("drafts_folder"), verbose=True)
    if res["ok"]:
        print("    черновик лежит в папке:", res["folder"])
        print("    Message-ID:", res["message_id"])
    else:
        print("    не получилось:", res["error"])


def _cli(argv):
    """Черновики из таблицы letters.

    python3 scripts/drafts.py --db data/leads.db --dry-run --out out/drafts/
    python3 scripts/drafts.py --db data/leads.db --append --limit 300

    Без ключа --append скрипт в почту не ходит вообще.
    """
    import argparse
    import sqlite3
    sys.path.insert(0, HERE)
    import verify

    parser = argparse.ArgumentParser(
        description="Раскладка готовых писем по черновикам")
    parser.add_argument("--db", default=verify.DB_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="сложить .eml в папку, почту не трогать")
    parser.add_argument("--append", action="store_true",
                        help="положить письма в ящик по IMAP")
    parser.add_argument("--out", default=DRAFTS_DIR)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--status", default=verify.STATUS_DRAFT,
                        help="какие письма брать из таблицы letters: %s. "
                             "По умолчанию %s"
                             % (", ".join(verify.LETTER_STATUSES),
                                verify.STATUS_DRAFT))
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("базы %s нет. Сначала соберите письма через write_letter.py" % args.db)
        return 1
    if args.append and args.dry_run:
        print("выберите что-то одно: или --dry-run, или --append")
        return 1
    if not args.append and not args.dry_run:
        args.dry_run = True
        print("режим не указан, работаю в безопасном --dry-run")

    con = verify.open_db(args.db)
    # Стоп-лист из конфига переносится в базу перед выборкой, чтобы правка
    # config.json действовала сразу, без отдельной команды.
    verify.stoplist_sync_config(con, config_path=find_config())
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT rowid, inn, name, to_email, subject, body, status "
            "FROM letters WHERE status = ? AND to_email IS NOT NULL "
            "AND to_email <> '' ORDER BY rowid LIMIT ?",
            (args.status, args.limit)).fetchall()]
    except sqlite3.Error as e:
        print("не получилось прочитать таблицу letters: %s" % e)
        con.close()
        return 1

    # Последний рубеж отписки: письмо могло быть написано до того, как человек
    # попросил не писать. В черновики оно не попадёт.
    kept = []
    stopped = 0
    for row in rows:
        reason = verify.stoplist_reason(row["inn"], email=row["to_email"],
                                        con=con, config_path=find_config())
        if reason:
            stopped += 1
            print("    %-13s пропущено, стоп-лист: %s" % (row["inn"], reason))
            continue
        kept.append(row)
    rows = kept
    if stopped:
        print("из стоп-листа пропущено писем: %d" % stopped)
    if stopped and not rows:
        print("все письма отсеяны стоп-листом, раскладывать нечего")
        con.close()
        return 0

    if not rows:
        print("писем со статусом «%s» в базе нет" % args.status)
        try:
            have = con.execute("SELECT status, COUNT(*) FROM letters "
                               "GROUP BY status ORDER BY 2 DESC").fetchall()
        except sqlite3.Error:
            have = []
        if have:
            print("в таблице letters сейчас лежат статусы: %s"
                  % ", ".join("%s (%d)" % (r[0] or "пусто", r[1]) for r in have))
            print("рабочий словарь статусов: %s. Возьмите нужный ключом --status"
                  % ", ".join(verify.LETTER_STATUSES))
        else:
            print("таблица letters пустая: сначала соберите письма "
                  "через write_letter.py --render")
        con.close()
        return 0

    settings = imap_settings()
    from_addr = settings.get("from") or ""
    print("писем к раскладке: %d" % len(rows))

    if args.dry_run:
        os.makedirs(args.out, exist_ok=True)
        for row in rows:
            path = dry_run(row["to_email"], row["subject"], row["body"],
                           from_addr or "нет-адреса@example.ru",
                           out_dir=args.out, name_hint=row["inn"])
            print("    %-13s %-28s %s" % (row["inn"], row["to_email"], path))
        print("\nготово. Файлы .eml лежат в %s, в почту ничего не ушло" % args.out)
        con.close()
        return 0

    if not (settings.get("host") and settings.get("login")
            and settings.get("app_password")):
        print("в config.json не заполнен блок pochta: нужны imap_host, login, parol")
        con.close()
        return 1

    limit = min(len(rows), settings.get("daily_limit") or len(rows))
    if limit < len(rows):
        print("дневной лимит из config.json: %d писем за прогон" % limit)
    ok_count = 0
    for row in rows[:limit]:
        res = append_draft(settings["host"], settings["login"],
                           settings["app_password"], row["to_email"],
                           row["subject"], row["body"], from_addr,
                           port=settings.get("port", 993),
                           folder=settings.get("drafts_folder"))
        if res["ok"]:
            ok_count += 1
            con.execute("UPDATE letters SET status = ? WHERE rowid = ?",
                        (verify.STATUS_IN_DRAFTS, row["rowid"]))
            con.commit()
            print("    %-13s %-28s папка %s" % (row["inn"], row["to_email"],
                                                res["folder"]))
        else:
            print("    %-13s %-28s ошибка: %s" % (row["inn"], row["to_email"],
                                                  res["error"]))
        if settings.get("pause"):
            time.sleep(float(settings["pause"]))
    con.close()
    print("\nв черновиках: %d из %d. Отправляет человек, скрипт этого не делает."
          % (ok_count, limit))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    flags = ("--db", "--dry-run", "--append", "--out", "--limit", "--status")
    # справка обязана печататься мгновенно, без сети и без записи файлов
    if "--help" in argv or "-h" in argv:
        return _cli(argv)
    if any(a.split("=")[0] in flags for a in argv):
        return _cli(argv)
    _demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
