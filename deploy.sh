#!/usr/bin/env bash
# Оновлення сайту на сервері однією командою:  ./deploy.sh
#
# Що робить, по черзі:
#   1. забирає новий код з GitHub;
#   2. ставить бібліотеки з requirements.txt (якщо треба — створює .venv);
#   3. перевіряє налаштування сервера (check --deploy) — якщо чогось бракує, зупиняється, нічого не змінивши;
#   4. робить копію бази (backup_db);
#   5. оновлює будову бази (migrate) і каталог продуктів і страв (load_catalog);
#   6. збирає CSS і JS (collectstatic);
#   7. перезапускає сайт (systemd-сервіс, назва — у RACION_SERVICE, типово racion).
#
# Будь-яка помилка зупиняє скрипт: наступні кроки не виконуються.

# -e: зупинитись на першій помилці; -u: помилка, якщо змінна не задана; pipefail: помилка в будь-якій частині конвеєра
set -euo pipefail

SERVICE="${RACION_SERVICE:-racion}"

step() { printf '\n▶ %s\n' "$1"; }

main() {
    # Працюємо з папки, де лежить сам скрипт, звідки б його не запустили
    cd "$(dirname "$0")"

    if [ ! -f .env ]; then
        echo "Нема файлу .env. Скопіюй .env.example у .env і заповни серверну частину." >&2
        exit 1
    fi
    if grep -Eq '^[[:space:]]*DJANGO_DEBUG[[:space:]]*=[[:space:]]*1' .env; then
        echo "У .env увімкнено DJANGO_DEBUG=1 — на сервері так не можна (видно помилки з кодом, нема HTTPS-захисту)." >&2
        exit 1
    fi

    step "Новий код з GitHub"
    # Правки прямо на сервері загубились би або зламали б pull — краще зупинитись і розібратись
    if ! git diff --quiet HEAD; then
        echo "На сервері є незакомічені зміни у файлах проєкту (git status). Прибери їх і запусти знову." >&2
        exit 1
    fi
    git pull --ff-only

    step "Бібліотеки"
    if [ ! -x .venv/bin/python ]; then
        python3 -m venv .venv
    fi
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements.txt

    step "Перевірка налаштувань сервера"
    # Попередження теж зупиняють: напр. без пошти користувач не зможе відновити пароль
    .venv/bin/python manage.py check --deploy --fail-level WARNING

    step "Копія бази"
    .venv/bin/python manage.py backup_db

    step "Будова бази (migrate)"
    .venv/bin/python manage.py migrate --noinput

    step "Каталог продуктів і страв (load_catalog)"
    .venv/bin/python manage.py load_catalog

    step "CSS і JS (collectstatic)"
    .venv/bin/python manage.py collectstatic --noinput

    step "Перезапуск сайту"
    if command -v systemctl >/dev/null && systemctl cat "$SERVICE" >/dev/null 2>&1; then
        sudo systemctl restart "$SERVICE"
        systemctl --no-pager --lines=0 status "$SERVICE"
    else
        echo "Сервісу «$SERVICE» не знайшов — перезапусти gunicorn вручну (або задай RACION_SERVICE=назва)."
    fi

    printf '\n✅ Готово: %s\n' "$(git log --oneline -1)"
}

# Увесь код — у функції main, яку викликаємо в самому кінці. Bash читає скрипт по ходу виконання,
# а git pull може підмінити цей самий файл. Так bash спершу прочитає функцію цілком, і підміна її не зламає.
main "$@"
exit
