"""python manage.py backup_db — копія бази SQLite у папку backups/ поруч із базою.

Запускається з deploy.sh перед міграціями: якщо міграція щось зіпсує, базу можна повернути з копії.
Лишаємо KEEP найновіших копій, старіші видаляємо, щоб диск не заповнився.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

KEEP = 10


def backup_database(db_path, keep=KEEP, now=None):
    """Копіює базу → шлях до копії; None, якщо бази ще нема (перше викладання)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    folder = db_path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    stamp = (now or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
    target = folder / f'{db_path.stem}-{stamp}{db_path.suffix}'

    # Не просто копіюємо файл: якщо сайт саме щось записує, копія файлу може вийти битою.
    # backup() з самого SQLite робить цілісну копію навіть під час запису.
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()

    # Назви з датою на кшталт 2026-11-18_14-05-00 за абеткою йдуть від старих до нових
    copies = sorted(folder.glob(f'{db_path.stem}-*{db_path.suffix}'))
    for old in copies[:-keep]:
        old.unlink()
    return target


class Command(BaseCommand):
    help = 'Робить копію бази SQLite у папку backups/ поруч із базою (лишає 10 найновіших).'

    def handle(self, *args, **options):
        database = settings.DATABASES['default']
        if not database['ENGINE'].endswith('sqlite3'):
            raise CommandError('backup_db уміє копіювати лише SQLite.')
        target = backup_database(database['NAME'])
        if target is None:
            self.stdout.write('Бази ще нема — копіювати нічого (перше викладання).')
        else:
            self.stdout.write(f'Копія бази: {target}')
