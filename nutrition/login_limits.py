"""Захист від підбору пароля, масової реєстрації й засипання чужої пошти листами: рахуємо невдалі спроби й тимчасово блокуємо.

Лічильники лежать у кеші Django. Кожен лічильник живе LOCKOUT_SECONDS від першої спроби, потім зникає сам.
"""
import hashlib

from django.conf import settings
from django.core.cache import cache

EMAIL_FAILURE_LIMIT = 5        # невдалих входів в одну пошту, після яких вхід у неї блокується
IP_FAILURE_LIMIT = 30          # невдалих входів з однієї адреси (хтось перебирає різні пошти)
REGISTER_LIMIT = 10            # нових акаунтів з однієї адреси
LOCKOUT_SECONDS = 15 * 60      # на скільки блокуємо
REGISTER_WINDOW_SECONDS = 60 * 60
RESET_EMAIL_LIMIT = 3          # листів для відновлення пароля на одну пошту за годину
RESET_IP_LIMIT = 10            # запитів на відновлення з однієї адреси за годину
RESET_WINDOW_SECONDS = 60 * 60
LOCKOUT_MESSAGE = 'Забагато невдалих спроб входу. Спробуй ще раз за 15 хвилин.'
REGISTER_LIMIT_MESSAGE = 'Забагато нових акаунтів з цієї мережі. Спробуй ще раз за годину.'
RESET_LIMIT_MESSAGE = 'Забагато запитів на відновлення пароля. Спробуй ще раз за годину.'


def client_ip(request):
    """Адреса, з якої прийшов запит.

    За проксі (Caddy) REMOTE_ADDR — це адреса самого проксі, а справжню він дописує в кінець X-Forwarded-For.
    Беремо саме останню: усе, що лівіше, міг підставити сам відвідувач.
    """
    if getattr(settings, 'BEHIND_PROXY', False):
        forwarded = [part.strip() for part in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.META.get('REMOTE_ADDR', '')


def _key(kind, value):
    """Ключ лічильника. Пошту й адресу хешуємо: так ключ короткий і без дивних символів."""
    return f'limits:{kind}:{hashlib.sha256(value.encode()).hexdigest()}'


def _count(kind, value):
    return cache.get(_key(kind, value), 0)


def _bump(kind, value, seconds):
    """Додає 1 до лічильника; перша спроба запускає відлік часу."""
    key = _key(kind, value)
    cache.add(key, 0, seconds)
    try:
        cache.incr(key)
    except ValueError:              # лічильник саме закінчився між add і incr
        cache.set(key, 1, seconds)


def login_blocked(email, ip):
    """Чи заблоковано зараз вхід у цю пошту або з цієї адреси."""
    return _count('login-email', email) >= EMAIL_FAILURE_LIMIT or _count('login-ip', ip) >= IP_FAILURE_LIMIT


def record_login_failure(email, ip):
    _bump('login-email', email, LOCKOUT_SECONDS)
    _bump('login-ip', ip, LOCKOUT_SECONDS)


def clear_login_failures(email):
    """Після вдалого входу помилки цієї пошти забуваються (лічильник адреси лишається)."""
    cache.delete(_key('login-email', email))


def register_blocked(ip):
    return _count('register-ip', ip) >= REGISTER_LIMIT


def record_registration(ip):
    _bump('register-ip', ip, REGISTER_WINDOW_SECONDS)


def reset_blocked(email, ip):
    """Чи вичерпано ліміт листів для відновлення. Рахуємо й неіснуючі пошти — так не видно, які зареєстровані."""
    return _count('reset-email', email) >= RESET_EMAIL_LIMIT or _count('reset-ip', ip) >= RESET_IP_LIMIT


def record_reset(email, ip):
    _bump('reset-email', email, RESET_WINDOW_SECONDS)
    _bump('reset-ip', ip, RESET_WINDOW_SECONDS)
