from django.db import transaction

from .models import MealPlan, PlanItem


def default_plan_name(period, days_count):
    """Назва раціону, якщо користувач її не ввів."""
    if period == 'day':
        return 'Раціон на день'
    if period == 'week':
        return 'Раціон на тиждень'
    return f'Раціон, днів: {days_count}'


@transaction.atomic
def save_plan(name, choices, plan_days, owner=None):
    """Зберігає раціон у базу: один MealPlan і по PlanItem на кожен продукт → MealPlan."""
    plan = MealPlan.objects.create(
        owner=owner,
        name=name.strip()[:100] or default_plan_name(choices['period'], len(plan_days)),
        period=choices['period'],
        days_count=len(plan_days),
        calories=choices['target_calories'],
        protein_pct=choices['protein_pct'],
        fat_pct=choices['fat_pct'],
        carbs_pct=choices['carbs_pct'],
    )
    items = []
    for day_number, day_info in enumerate(plan_days):
        for meal_number, meal_info in enumerate(day_info['day']):
            for item in meal_info['meal']:
                items.append(PlanItem(
                    plan=plan,
                    product=item['product'],
                    day_number=day_number,
                    day_name=day_info['name'],
                    meal_number=meal_number,
                    meal_name=meal_info['name'],
                    role=item.get('role', ''),
                    recipe_name=meal_info.get('recipe_name', ''),
                    grams=item['grams'],
                ))
    PlanItem.objects.bulk_create(items)
    return plan


def load_plan_days(plan):
    """Відновлює з бази ту саму форму, що й build_period → список днів {name, day}."""
    days = {}
    for item in plan.items.select_related('product'):
        day_info = days.setdefault(item.day_number, {'name': item.day_name, 'meals': {}})
        meal_info = day_info['meals'].setdefault(
            item.meal_number, {'name': item.meal_name, 'recipe_name': item.recipe_name, 'calories': 0, 'meal': []},
        )
        meal_info['calories'] += item.product.calories_for(item.grams)
        meal_info['meal'].append({'role': item.role, 'product': item.product, 'grams': item.grams})
    return [
        {'name': day_info['name'], 'day': list(day_info['meals'].values())}
        for day_info in days.values()
    ]
