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
