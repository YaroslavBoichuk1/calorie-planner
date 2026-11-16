"""python manage.py load_catalog — заносить продукти й страви з nutrition/data/catalog.json у базу.

Можна запускати скільки завгодно разів: продукти й страви шукаються за назвою, тож нічого не дублюється,
а змінене в каталозі оновлюється. Ціни продуктів не чіпаємо. Те, що додали в адмінці й чого нема в каталозі,
лишається як є.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from nutrition.models import Product, Recipe, RecipeIngredient

CATALOG_PATH = Path(__file__).resolve().parents[2] / 'data' / 'catalog.json'
PRODUCT_FIELDS = ('fdc_id', 'calories', 'proteins', 'fats', 'carbs', 'silpo_search', 'color')
MEAL_FLAGS = {'breakfast': 'for_breakfast', 'lunch': 'for_lunch', 'dinner': 'for_dinner'}


def load_catalog(path=CATALOG_PATH):
    """Заносить каталог у базу → {'products': (створено, оновлено), 'recipes': (створено, оновлено)}."""
    catalog = json.loads(Path(path).read_text(encoding='utf-8'))
    counts = {'products': [0, 0], 'recipes': [0, 0]}
    with transaction.atomic():
        products = {}
        for item in catalog['products']:
            defaults = {field: item[field] for field in PRODUCT_FIELDS}
            defaults['unit'] = item.get('unit', 'g')
            defaults['grams_per_piece'] = item.get('grams_per_piece')
            product, created = Product.objects.update_or_create(name=item['name'], defaults=defaults)
            products[product.name] = product
            counts['products'][0 if created else 1] += 1

        for item in catalog['recipes']:
            unknown = set(item['meals']) - set(MEAL_FLAGS)
            if unknown:
                raise CommandError(f"«{item['name']}»: невідомі прийоми {sorted(unknown)}")
            missing = set(item['ingredients']) - set(products)
            if missing:
                raise CommandError(f"«{item['name']}»: нема продуктів {sorted(missing)}")
            flags = {flag: meal in item['meals'] for meal, flag in MEAL_FLAGS.items()}
            recipe, created = Recipe.objects.update_or_create(name=item['name'], defaults=flags)
            recipe.ingredients.all().delete()
            RecipeIngredient.objects.bulk_create(
                RecipeIngredient(recipe=recipe, product=products[name], grams=grams)
                for name, grams in item['ingredients'].items()
            )
            counts['recipes'][0 if created else 1] += 1
    return {key: tuple(value) for key, value in counts.items()}


class Command(BaseCommand):
    help = 'Заносить продукти й страви з nutrition/data/catalog.json (повторний запуск безпечний).'

    def add_arguments(self, parser):
        parser.add_argument('--path', default=CATALOG_PATH, help='інший файл каталогу')

    def handle(self, *args, **options):
        counts = load_catalog(options['path'])
        (new_products, updated_products), (new_recipes, updated_recipes) = counts['products'], counts['recipes']
        self.stdout.write(self.style.SUCCESS(
            f'Продукти: нових {new_products}, оновлено {updated_products}. '
            f'Страви: нових {new_recipes}, оновлено {updated_recipes}.'
        ))
