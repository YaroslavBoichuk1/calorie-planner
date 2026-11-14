"""Налаштування Django для «Раціону».

Усе, що відрізняється між твоїм комп'ютером і сервером, береться зі змінних середовища.
Локально їх зручно тримати у файлі .env у корені проєкту (він не потрапляє в git; приклад — .env.example).
Без жодних змінних застосунок працює в безпечному «серверному» режимі: DEBUG вимкнено, і потрібен секретний ключ.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path):
    """Читає рядки КЛЮЧ=значення з файлу .env у змінні середовища. Те, що вже задано в середовищі, не перезаписує."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_list(name, default=''):
    """Змінна зі списком через кому → список без порожніх елементів."""
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


load_env_file(BASE_DIR / '.env')

# Режим розробки вмикається лише явно: DJANGO_DEBUG=1. На сервері його нема.
DEBUG = os.environ.get('DJANGO_DEBUG') == '1'

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('Задай DJANGO_SECRET_KEY: без нього сервер не запускається (див. .env.example).')
    SECRET_KEY = 'django-insecure-local-development-only'

# Адреси сайту, наприклад: racion.example.com. Локально Django в режимі розробки й так пускає localhost.
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS')
# Звідки приймати форми через HTTPS, наприклад: https://racion.example.com
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')
# Пошта для питань про дані — показується на сторінці приватності. Перед викладанням обов'язкова.
CONTACT_EMAIL = os.environ.get('DJANGO_CONTACT_EMAIL', '').strip()


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'nutrition.apps.NutritionConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',      # роздає CSS і JS; одразу після SecurityMiddleware
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'nutrition.context_processors.navigation',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# База — SQLite. Для кількох процесів gunicorn: журнал WAL (читання не чекають на запис),
# запис починається одразу (IMMEDIATE), а зайнята база чекає до 20 с замість помилки.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DJANGO_DB_PATH') or BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'init_command': 'PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;',
            'transaction_mode': 'IMMEDIATE',
            'timeout': 20,
        },
    }
}

# Кеш у базі: спільний для всіх процесів gunicorn (у ньому лічильники спроб входу).
# Таблицю створює команда: python manage.py createcachetable
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache',
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LOGIN_URL = 'login'
# Посилання для відновлення пароля діє годину і стає недійсним, щойно пароль змінено.
PASSWORD_RESET_TIMEOUT = 60 * 60


# Пошта (листи для відновлення пароля). На сервері — через поштовий сервіс за SMTP (DJANGO_EMAIL_*).
# Локально листи не відправляються, а друкуються в терміналі, де запущено сервер.
DEFAULT_FROM_EMAIL = os.environ.get('DJANGO_FROM_EMAIL', 'Раціон <noreply@localhost>')
EMAIL_HOST = os.environ.get('DJANGO_EMAIL_HOST', '')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.environ.get('DJANGO_EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('DJANGO_EMAIL_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('DJANGO_EMAIL_PASSWORD', '')
    EMAIL_USE_SSL = EMAIL_PORT == 465          # порт 465 — одразу зашифроване з'єднання
    EMAIL_USE_TLS = not EMAIL_USE_SSL          # 587 — шифрування вмикається після підключення (STARTTLS)
    EMAIL_TIMEOUT = 10
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


# Мова й час. Українською — повідомлення Django й адмінка, а дробові числа пишуться з комою («12,5 грн»).
# Тому числа, які йдуть у код (SVG, стилі), у шаблонах виводяться через |unlocalize.
LANGUAGE_CODE = 'uk'
TIME_ZONE = 'Europe/Kyiv'
USE_I18N = True
USE_TZ = True


# CSS і JS. collectstatic збирає їх у staticfiles/; на сервері до назв файлів додається хеш вмісту
# (style.3f2a….css), тож після оновлення браузер одразу бере новий файл, а старий можна кешувати хоч рік.
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Сайт стоїть за проксі (Caddy), який приймає HTTPS і передає запит далі.
# Лише тоді віримо заголовкам X-Forwarded-*: інакше їх міг би підробити будь-хто.
BEHIND_PROXY = os.environ.get('DJANGO_BEHIND_PROXY') == '1'
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Безпека на сервері
if not DEBUG:
    SECURE_SSL_REDIRECT = True                  # http:// → https://
    SESSION_COOKIE_SECURE = True                # кукі входу — лише через HTTPS
    CSRF_COOKIE_SECURE = True
    # HSTS: браузер запам'ятовує, що сайт працює тільки через HTTPS. Починаємо з години;
    # коли все стабільно, збільшимо до року (31536000) через DJANGO_HSTS_SECONDS.
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '3600'))
    # Піддомени й список попереднього завантаження браузерів — зобов'язання, яке важко скасувати.
    # Вмикаємо свідомо, коли домен і HTTPS стабільні; доти Django про це не нагадує.
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get('DJANGO_HSTS_SUBDOMAINS') == '1'
    SECURE_HSTS_PRELOAD = os.environ.get('DJANGO_HSTS_PRELOAD') == '1'
    SILENCED_SYSTEM_CHECKS = [
        check for check, enabled in (('security.W005', SECURE_HSTS_INCLUDE_SUBDOMAINS), ('security.W021', SECURE_HSTS_PRELOAD))
        if not enabled
    ]
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = 'DENY'


# Журнал: помилки й попередження — у стандартний вивід, звідки їх збере сервер (journalctl).
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '{asctime} {levelname} {name}: {message}', 'style': '{'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
}
