"""Власні перевірки Django: запускаються командою check (а з --deploy — перед викладанням на сервер)."""
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def contact_email_check(app_configs, **kwargs):
    """На сторінці приватності має бути пошта, куди людина може написати про свої дані."""
    if settings.CONTACT_EMAIL:
        return []
    return [Warning(
        'Не задано DJANGO_CONTACT_EMAIL: на сторінці «Приватність» нема пошти для звернень.',
        hint='Додай DJANGO_CONTACT_EMAIL=... у .env на сервері.',
        id='nutrition.W001',
    )]


@register(Tags.security, deploy=True)
def email_check(app_configs, **kwargs):
    """Без поштового сервісу листи для відновлення пароля просто друкуються в журнал і до людей не доходять."""
    if settings.EMAIL_HOST:
        return []
    return [Warning(
        'Не задано DJANGO_EMAIL_HOST: листи для відновлення пароля не відправлятимуться.',
        hint='Додай у .env на сервері DJANGO_EMAIL_HOST, DJANGO_EMAIL_USER, DJANGO_EMAIL_PASSWORD і DJANGO_FROM_EMAIL.',
        id='nutrition.W002',
    )]
