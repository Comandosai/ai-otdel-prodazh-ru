#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stoplist.py: стоп-лист конвейера. Попросили не писать, значит не пишем.

Отписка в письме это обещание. Выполняет его эта таблица: ИНН попадает
в stoplist, и дальше он не проходит ни в письмо, ни в черновики. Проверка
стоит в двух местах: перед сборкой письма (write_letter.py) и перед
раскладкой черновиков (drafts.py).

Сеть не трогается никогда, ключей не нужно, работает только с локальной базой.

    python3 scripts/stoplist.py --db data/leads.db --list
    python3 scripts/stoplist.py --db data/leads.db --add 1650027925 --reason "ответили стоп"
    python3 scripts/stoplist.py --db data/leads.db --add 1650027925 --email info@zavod.ru
    python3 scripts/stoplist.py --db data/leads.db --check 1650027925
    python3 scripts/stoplist.py --db data/leads.db --remove 1650027925
    python3 scripts/stoplist.py --db data/leads.db --import-config

Python 3.9, только стандартная библиотека.
"""

import os
import sys
import argparse
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify  # noqa: E402

# Статус, который получают уже написанные письма при попадании ИНН в стоп-лист.
# Черновики берутся по другим статусам, поэтому такое письмо никуда не уйдёт.
WITHDRAWN_STATUS = "stop_list"


def withdraw_letters(con, inn):
    """Снять со сборки уже написанные письма этой компании."""
    inn = verify.norm_digits(inn)
    if not inn:
        return 0
    try:
        cur = con.execute(
            "UPDATE letters SET status = ? WHERE CAST(inn AS TEXT) = ? "
            "AND status <> ?", (WITHDRAWN_STATUS, inn, WITHDRAWN_STATUS))
        return cur.rowcount or 0
    except sqlite3.Error:
        return 0


def print_row(row):
    print("  %-13s %-26s %-10s %s" % (
        row.get("inn") or "", (row.get("email") or "")[:26],
        (row.get("source") or "")[:10], row.get("reason") or ""))


def cmd_list(con):
    rows = verify.stoplist_all(con)
    if not rows:
        print("стоп-лист пуст")
        return 0
    print("в стоп-листе записей: %d" % len(rows))
    print("  %-13s %-26s %-10s %s" % ("ИНН", "почта", "источник", "причина"))
    for row in rows:
        print_row(row)
    return 0


def cmd_add(con, inns, reason, email):
    added = 0
    withdrawn = 0
    for raw in inns:
        inn = verify.norm_digits(raw)
        if not inn:
            print("  %-13s пропущен: это не похоже на ИНН" % raw)
            continue
        if verify.stoplist_add(con, inn, email=email, reason=reason,
                               source="ruchnoy"):
            added += 1
            withdrawn += withdraw_letters(con, inn)
            print("  %-13s в стоп-листе: %s" % (inn, reason))
        else:
            print("  %-13s записать не получилось" % inn)
    con.commit()
    print("\nдобавлено: %d, снято с раскладки писем: %d" % (added, withdrawn))
    return 0 if added else 1


def cmd_remove(con, inns):
    removed = 0
    for raw in inns:
        inn = verify.norm_digits(raw)
        if not inn:
            continue
        try:
            cur = con.execute("DELETE FROM stoplist WHERE inn = ?", (inn,))
            if cur.rowcount:
                removed += 1
                print("  %-13s убран из стоп-листа" % inn)
            else:
                print("  %-13s в стоп-листе его и не было" % inn)
        except sqlite3.Error as exc:
            print("  %-13s ошибка: %s" % (inn, exc))
    con.commit()
    print("\nубрано записей: %d" % removed)
    return 0


def cmd_check(con, inns, config_path=None):
    bad = 0
    for raw in inns:
        inn = verify.norm_digits(raw)
        reason = verify.stoplist_reason(inn, con=con, config_path=config_path)
        if reason:
            bad += 1
            print("  %-13s ПИСАТЬ НЕЛЬЗЯ: %s" % (inn, reason))
        else:
            print("  %-13s чисто, письмо собирать можно" % inn)
    return 1 if bad else 0


def cmd_import_config(con, config_path=None):
    found = verify.find_config(config_path)
    if not found:
        print("конфига не нашёл, брать список неоткуда")
        return 1
    added = verify.stoplist_sync_config(con, config_path=found)
    total = len(verify.config_stoplist(found))
    print("конфиг: %s" % found)
    print("в поле otsev.isklyuchit_inn записей: %d, добавлено новых: %d"
          % (total, added))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Стоп-лист: кому конвейер писать не будет")
    parser.add_argument("--db", default=verify.DB_PATH,
                        help="база конвейера, по умолчанию data/leads.db")
    parser.add_argument("--config", help="путь к config.json")
    parser.add_argument("--add", nargs="+", metavar="ИНН",
                        help="добавить ИНН в стоп-лист")
    parser.add_argument("--remove", nargs="+", metavar="ИНН",
                        help="убрать ИНН из стоп-листа")
    parser.add_argument("--check", nargs="+", metavar="ИНН",
                        help="проверить, можно ли писать")
    parser.add_argument("--list", action="store_true", help="показать стоп-лист")
    parser.add_argument("--import-config", action="store_true",
                        dest="import_config",
                        help="перенести otsev.isklyuchit_inn из конфига в базу")
    parser.add_argument("--reason", default="просили не писать",
                        help="причина, попадёт в отчёт")
    parser.add_argument("--email", help="адрес, с которого пришла просьба")
    args = parser.parse_args(argv)

    con = verify.open_db(args.db)
    try:
        if args.add:
            return cmd_add(con, args.add, args.reason, args.email)
        if args.remove:
            return cmd_remove(con, args.remove)
        if args.check:
            return cmd_check(con, args.check, config_path=args.config)
        if args.import_config:
            return cmd_import_config(con, config_path=args.config)
        return cmd_list(con)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
