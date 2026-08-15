#!/usr/bin/env bash
# ============================================================
#  AI-отдел продаж, установка скилл-пака для Claude Code
#  Запуск:  bash install.sh
#  Скрипт идемпотентный: можно запускать сколько угодно раз,
#  ваш config.json при повторном запуске не перезаписывается.
#
#  Куда что кладётся:
#    ~/.claude/skills/prodazhi-*/     семь скиллов, каждый со своим SKILL.md
#    ~/.claude/tools/prodazhi/        скрипты, стили и config.json
#    ~/.claude/commands/продажи.md    команда /продажи
#
#  Скрипты лежат ВНЕ папки скиллов намеренно: каталог внутри skills/
#  без SKILL.md загрузчик скиллов считает битым скиллом.
# ============================================================

set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
SKILLS_DIR="$CLAUDE_HOME/skills"
COMMANDS_DIR="$CLAUDE_HOME/commands"
PACK_DIR="$CLAUDE_HOME/tools/prodazhi"
SCRIPTS_DIR="$PACK_DIR/scripts"
CONFIG="$PACK_DIR/config.json"

BOLD=""; DIM=""; GREEN=""; RED=""; YELLOW=""; OFF=""
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"
  GREEN="$(printf '\033[32m')"; RED="$(printf '\033[31m')"
  YELLOW="$(printf '\033[33m')"; OFF="$(printf '\033[0m')"
fi

ok()   { printf "  %s+%s %s\n" "$GREEN" "$OFF" "$1"; }
skip() { printf "  %s=%s %s\n" "$DIM" "$OFF" "$1"; }
warn() { printf "  %s!%s %s\n" "$YELLOW" "$OFF" "$1"; }
fail() { printf "  %sX%s %s\n" "$RED" "$OFF" "$1"; }
head_() { printf "\n%s%s%s\n" "$BOLD" "$1" "$OFF"; }

printf "\n%sAI-ОТДЕЛ ПРОДАЖ%s, установка\n" "$BOLD" "$OFF"
printf "%sРепозиторий: %s%s\n" "$DIM" "$SRC" "$OFF"

# ------------------------------------------------------------
head_ "1. Проверка окружения"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 не найден."
  echo ""
  echo "  Поставьте Python 3.9 или новее и запустите install.sh снова."
  echo "  macOS:  brew install python3"
  echo "  Ubuntu: sudo apt install python3 python3-pip"
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  fail "нужен Python 3.9 или новее, у вас $PY_VER"
  exit 1
fi
ok "python3 версии $PY_VER"

# openpyxl не обязателен: в fns_rmsp.py есть свой парсер xlsx на zipfile
# и ElementTree, он идёт первым. openpyxl это страховка на случай смены формата.
if python3 -c 'import openpyxl' >/dev/null 2>&1; then
  if python3 - <<'PYCHECK' >/dev/null 2>&1
import sys, openpyxl
parts = []
for chunk in str(openpyxl.__version__).split(".")[:3]:
    digits = "".join(ch for ch in chunk if ch.isdigit())
    parts.append(int(digits) if digits else 0)
while len(parts) < 3:
    parts.append(0)
sys.exit(0 if tuple(parts) >= (3, 0, 6) else 1)
PYCHECK
  then
    ok "openpyxl на месте, версия подходящая"
  else
    OPXL_VER="$(python3 -c 'import openpyxl; print(openpyxl.__version__)' 2>/dev/null)"
    warn "openpyxl $OPXL_VER слишком старый, нужен 3.0.6 или новее."
    echo "      В файле от rmsp.nalog.ru неверно указан размер листа, и старый openpyxl"
    echo "      молча читает одну строку вместо нескольких сотен."
    echo "      Обновить:  python3 -m pip install -U -r \"$SRC/requirements.txt\""
  fi
else
  skip "openpyxl не установлен, это нормально: используется встроенный парсер xlsx"
  echo "      Поставить страховку:  python3 -m pip install -r \"$SRC/requirements.txt\""
fi

# ------------------------------------------------------------
head_ "2. Скиллы"

mkdir -p "$SKILLS_DIR"

# Наследство прошлых версий: скрипты клались в ~/.claude/skills/prodazhi/,
# то есть в папку без SKILL.md. Для загрузчика это битый скилл, убираем.
if [ -d "$SKILLS_DIR/prodazhi" ] && [ ! -f "$SKILLS_DIR/prodazhi/SKILL.md" ]; then
  if [ -f "$SKILLS_DIR/prodazhi/config.json" ]; then
    mkdir -p "$PACK_DIR"
    if [ ! -f "$CONFIG" ]; then
      cp "$SKILLS_DIR/prodazhi/config.json" "$CONFIG" && \
        ok "старый config.json перенесён в $CONFIG"
    fi
  fi
  rm -rf "${SKILLS_DIR:?}/prodazhi"
  ok "убрана битая папка $SKILLS_DIR/prodazhi, в ней не было SKILL.md"
fi

if [ -d "$SRC/skills" ]; then
  COPIED=0
  for skill_path in "$SRC"/skills/*/; do
    [ -d "$skill_path" ] || continue
    name="$(basename "$skill_path")"
    if [ ! -f "$skill_path/SKILL.md" ]; then
      warn "пропускаю $name: внутри нет SKILL.md"
      continue
    fi
    rm -rf "${SKILLS_DIR:?}/$name.tmp-install"
    cp -R "$skill_path" "$SKILLS_DIR/$name.tmp-install" || { fail "не смог скопировать $name"; continue; }
    rm -rf "${SKILLS_DIR:?}/$name"
    mv "$SKILLS_DIR/$name.tmp-install" "$SKILLS_DIR/$name"
    ok "скилл $name"
    COPIED=$((COPIED + 1))
  done
  [ "$COPIED" -eq 0 ] && warn "в $SRC/skills/ нет ни одной папки со скиллом"
else
  warn "папки $SRC/skills/ пока нет, скиллы не скопированы"
  echo "      Это нормально, если вы ставите репозиторий до того, как скиллы собраны."
fi

# ------------------------------------------------------------
head_ "3. Скрипты и стили"

mkdir -p "$SCRIPTS_DIR" || { fail "не смог создать $SCRIPTS_DIR"; exit 1; }

SCRIPT_COUNT=0
SCRIPT_FAILED=0
for f in "$SRC"/scripts/*.py; do
  [ -f "$f" ] || continue
  if cp "$f" "$SCRIPTS_DIR/"; then
    SCRIPT_COUNT=$((SCRIPT_COUNT + 1))
  else
    SCRIPT_FAILED=$((SCRIPT_FAILED + 1))
  fi
done
if [ "$SCRIPT_COUNT" -gt 0 ]; then
  chmod +x "$SCRIPTS_DIR"/*.py 2>/dev/null
  ok "скриптов скопировано: $SCRIPT_COUNT, в $SCRIPTS_DIR"
  # Скрипты, которых больше нет в репозитории, убираются: иначе рядом со
  # свежими файлами остаётся старый мусор и его кто-нибудь запустит.
  for old in "$SCRIPTS_DIR"/*.py; do
    [ -f "$old" ] || continue
    if [ ! -f "$SRC/scripts/$(basename "$old")" ]; then
      rm -f "$old" && skip "убран устаревший $(basename "$old")"
    fi
  done
elif [ "$SCRIPT_FAILED" -gt 0 ]; then
  fail "ни один скрипт скопировать не удалось, проверьте права на $SCRIPTS_DIR"
else
  warn "в $SRC/scripts/ нет ни одного .py"
fi

if [ -f "$SRC/assets/comandos.css" ]; then
  mkdir -p "$PACK_DIR/assets"
  CSS_OK=1
  cp "$SRC/assets/comandos.css" "$PACK_DIR/assets/comandos.css" || CSS_OK=0
  # копия рядом со скриптами, чтобы report.py нашёл стили без поиска
  cp "$SRC/assets/comandos.css" "$SCRIPTS_DIR/comandos.css" || CSS_OK=0
  if [ "$CSS_OK" -eq 1 ]; then
    ok "дизайн-система comandos.css на месте"
  else
    warn "comandos.css скопирован не полностью, отчёты могут собраться на запасных стилях"
  fi
else
  warn "assets/comandos.css не найден, отчёты соберутся на запасных стилях"
fi

# ------------------------------------------------------------
head_ "4. Проверка скриптов"

# Проверяются ВСЕ скопированные файлы, а не один. Проверка синтаксическая:
# ни один скрипт при установке не запускается и в сеть не ходит.
if [ "$SCRIPT_COUNT" -gt 0 ]; then
  SYNTAX_REPORT="$(python3 - "$SCRIPTS_DIR" <<'PYSYNTAX'
import ast
import os
import sys

folder = sys.argv[1]
bad = []
total = 0
for name in sorted(os.listdir(folder)):
    if not name.endswith(".py"):
        continue
    total += 1
    path = os.path.join(folder, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ast.parse(fh.read(), filename=path)
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        bad.append("%s: %s" % (name, exc))
print("TOTAL %d" % total)
for line in bad:
    print("BAD %s" % line)
PYSYNTAX
)"
  SYNTAX_TOTAL="$(printf '%s\n' "$SYNTAX_REPORT" | awk '/^TOTAL/ {print $2}')"
  SYNTAX_BAD="$(printf '%s\n' "$SYNTAX_REPORT" | grep -c '^BAD ' || true)"
  if [ "${SYNTAX_BAD:-0}" -eq 0 ]; then
    ok "синтаксис проверен у всех скриптов: ${SYNTAX_TOTAL:-0}"
  else
    fail "скриптов с ошибкой синтаксиса: $SYNTAX_BAD"
    printf '%s\n' "$SYNTAX_REPORT" | sed -n 's/^BAD /      /p'
    echo "      Установка не завершена, эти файлы работать не будут."
  fi

  # report.py единственный, у кого запуск заведомо безопасен: argparse и офлайн.
  if python3 "$SCRIPTS_DIR/report.py" --help >/dev/null 2>&1; then
    ok "report.py запускается"
  else
    warn "report.py не запустился, проверьте вручную: python3 \"$SCRIPTS_DIR/report.py\" --help"
  fi
  if python3 "$SCRIPTS_DIR/stoplist.py" --help >/dev/null 2>&1; then
    ok "stoplist.py запускается, стоп-лист доступен"
  else
    warn "stoplist.py не запустился, отписка работать не будет"
  fi
fi

# ------------------------------------------------------------
head_ "5. Команда /продажи"

if [ -d "$SRC/commands" ]; then
  mkdir -p "$COMMANDS_DIR"
  CMD_COUNT=0
  for cmd in "$SRC"/commands/*.md; do
    [ -f "$cmd" ] || continue
    if cp "$cmd" "$COMMANDS_DIR/"; then
      CMD_COUNT=$((CMD_COUNT + 1))
      ok "команда $(basename "$cmd" .md)"
    else
      warn "не смог скопировать $(basename "$cmd")"
    fi
  done
  [ "$CMD_COUNT" -eq 0 ] && warn "в $SRC/commands/ нет ни одного файла .md"
else
  warn "папки $SRC/commands/ нет, команда /продажи не установлена"
fi

# ------------------------------------------------------------
head_ "6. Конфиг"

# Конфиг ровно один, и он здесь. Скрипты ищут его сами: сначала рядом с собой,
# потом в рабочей папке, потом в этом стандартном месте.
if [ -f "$CONFIG" ]; then
  skip "config.json уже есть, оставляю как есть: $CONFIG"
elif [ -f "$SRC/config.example.json" ]; then
  if python3 - "$SRC/config.example.json" "$CONFIG" <<'PYCONFIG'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as fh:
    cfg = json.load(fh)

# Подпись и доступы обнуляются намеренно. Иначе первый же прогон разошлёт
# письма, подписанные чужим именем из примера.
for block, fields in (
    ("pisma", ("ot_kogo_imya", "ot_kogo_kompaniya", "ot_kogo_telefon", "podpis")),
    ("pochta", ("smtp_host", "imap_host", "login", "parol", "ot_kogo",
                "papka_chernovikov")),
):
    node = cfg.get(block)
    if not isinstance(node, dict):
        continue
    for field in fields:
        if field in node:
            node[field] = None if field == "papka_chernovikov" else ""

with open(dst, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PYCONFIG
  then
    ok "создан config.json из примера, подпись и почтовые доступы пустые"
    warn "ПОКА ПОДПИСЬ ПУСТАЯ, ПИСЬМА НЕ СОБИРАЮТСЯ. Это защита от чужой подписи."
    echo "      Заполните в $CONFIG блок pisma:"
    echo "      ot_kogo_imya, ot_kogo_kompaniya, ot_kogo_telefon."
  else
    fail "не получилось собрать config.json из примера"
  fi
else
  warn "config.example.json не найден, конфиг не создан"
fi

# Второй config.json в репозитории не создаётся намеренно: два конфига это
# гарантированная путаница, человек заполнит не тот. Если вы работаете прямо
# из клона репозитория, скопируйте пример руками и заполните его:
#   cp config.example.json config.json

# ------------------------------------------------------------
head_ "Готово"

cat <<TXT
  Скиллы:   $SKILLS_DIR/prodazhi-*
  Скрипты:  $SCRIPTS_DIR
  Команда:  $COMMANDS_DIR/продажи.md
  Конфиг:   $CONFIG   (он один, второго нет)

  Что дальше:
  1. Заполните config.json: имя и телефон для подписи, регион, коды ОКВЭД,
     вилки по численности и выручке, почтовые доступы.
  2. Проверьте, что отчёты собираются:
       python3 "$SCRIPTS_DIR/report.py" --demo --out ./out
     Появятся 5 html-файлов в папке out, это демо на вымышленных компаниях.
  3. Запустите Claude Code в рабочей папке и наберите /продажи портрет.

  Важно: часть источников открывается только с российского IP.
  Подробности в разделе «Ограничения» файла README.md.
TXT
printf "\n"
