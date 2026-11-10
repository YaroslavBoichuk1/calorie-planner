"""Віддає раціони без власника (збережені ще до акаунтів) акаунту з указаною поштою.

Запуск: python manage.py claim_plans пошта@приклад.com
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from nutrition.forms import normalize_email
from nutrition.models import MealPlan


class Command(BaseCommand):
    help = 'Віддає раціони без власника акаунту з указаною поштою.'

    def add_arguments(self, parser):
        parser.add_argument('email')

    def handle(self, *args, email, **options):
        user = get_user_model().objects.filter(username=normalize_email(email)).first()
        if user is None:
            raise CommandError('Акаунта з такою поштою немає. Спершу зареєструйся на сайті.')
        count = MealPlan.objects.filter(owner__isnull=True).update(owner=user)
        self.stdout.write(f'Раціонів передано: {count}.')
