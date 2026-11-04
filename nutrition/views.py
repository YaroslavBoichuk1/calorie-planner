import math

from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import MealPlan, Recipe
from .plans import load_plan_days, save_plan
from .planner import (
    MIN_DAY_CALORIES, MAX_DAY_CALORIES, WEEK_DAYS,
    build_recipe_period, meal_totals, period_totals, shopping_list,
)

PERIODS = ('day', 'week', 'custom')
DEFAULT_DAYS = 3
MAX_DAYS = 31
MAX_DB_ID = 2 ** 63 - 1   # найбільший id, який вміщує база; більше число дало б помилку в запиті


def parse_int(raw_value, default):
    """Перетворює текст з адреси на ціле число; якщо там порожньо або не число — default."""
    try:
        return int(float(raw_value))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_days_count(raw_days):
    """Повертає кількість днів з адреси в межах 1…MAX_DAYS; якщо там не число — DEFAULT_DAYS."""
    return min(max(parse_int(raw_days, DEFAULT_DAYS), 1), MAX_DAYS)


def add_card_info(day):
    """Доповнює прийоми й продукти дня даними для карток: підсумки прийому, калорії, вартість і штуки продукту."""
    for meal_info in day:
        meal_info['totals'] = meal_totals(meal_info['meal'])
        for item in meal_info['meal']:
            item['calories'] = item['product'].calories_for(item['grams'])
            item['cost'] = item['product'].cost_for(item['grams'])
            item['pieces'] = item['product'].pieces_to_use(item['grams'])


def shopping_rows(days):
    """Рядки списку покупок для шаблону: продукт, грами, штуки й вартість."""
    rows = []
    for product, grams in shopping_list(days).items():
        rows.append({
            'product': product,
            'grams': grams,
            'pieces': product.pieces_for(grams),
            'cost': product.cost_for(grams),
        })
    return rows


def split_into_weeks(days):
    """Ділить дні на тижні по 7 для показу → список {name, days}; останній тиждень може бути неповним."""
    weeks = []
    for start in range(0, len(days), len(WEEK_DAYS)):
        weeks.append({
            'name': f'Тиждень {len(weeks) + 1}',
            'days': days[start:start + len(WEEK_DAYS)],
        })
    return weeks


def read_choices(data):
    """Читає вибір користувача з параметрів адреси → словник для побудови раціону."""
    period = data.get('period') or 'day'
    if period not in PERIODS:
        period = 'day'

    if period == 'week':
        days_count = len(WEEK_DAYS)
    else:
        days_count = parse_days_count(data.get('days'))

    return {
        'period': period,
        'days_count': days_count,
        'target_calories': parse_int(data.get('calories'), 2000),  # мета калорій на весь день.
        'protein_pct': parse_int(data.get('protein_pct'), 30),
        'fat_pct': parse_int(data.get('fat_pct'), 30),
        'carbs_pct': parse_int(data.get('carbs_pct'), 40),
    }


def build_plan_days(choices):
    """Будує раціон зі страв за вибором користувача → список днів {name, day}. Кидає ValueError, якщо ціль чи страви не годяться."""
    days_count = 1 if choices['period'] == 'day' else choices['days_count']
    return build_recipe_period(
        Recipe.objects.prefetch_related('ingredients__product'),
        days_count,
        choices['target_calories'],
        choices['protein_pct'] / 100,
        choices['fat_pct'] / 100,
        choices['carbs_pct'] / 100,
    )


def result_context(plan_days, period, protein_pct, fat_pct, carbs_pct):
    """Дані для показу раціону: картки, тижні, список покупок, підсумки, БЖВ і кільце."""
    context = {'period': period}

    # підсумки й картки: для кількох днів або для одного дня
    totals = period_totals(plan_days)
    for day_info in plan_days:
        add_card_info(day_info['day'])

    if period == 'day':
        context['day'] = plan_days[0]['day']
    else:
        context['plan_days'] = plan_days
        if len(plan_days) > len(WEEK_DAYS):
            context['plan_weeks'] = split_into_weeks(plan_days)
    context['shopping'] = shopping_rows(plan_days)

    # для темної панелі: розклад БЖВ (грами, фактичний % від калорій, ціль %)
    total_cal = totals['calories'] or 1
    macros = [
        {'name': 'Білок',     'grams': totals['protein'], 'actual': totals['protein'] * 4 / total_cal * 100, 'target': protein_pct, 'color': 'protein'},
        {'name': 'Жир',       'grams': totals['fat'],     'actual': totals['fat'] * 9 / total_cal * 100,     'target': fat_pct,     'color': 'fat'},
        {'name': 'Вуглеводи', 'grams': totals['carbs'],   'actual': totals['carbs'] * 4 / total_cal * 100,   'target': carbs_pct,   'color': 'carbs'},
    ]

    # для кільця: довжина кольорової дуги кожного макросу
    circumference = 2 * math.pi * 70          # довжина кола (радіус 70)
    offset = 0.0
    for m in macros:
        length = m['actual'] / 100 * circumference   # частка макросу × коло
        m['dash'] = f"{length:.1f} {circumference - length:.1f}"
        m['offset'] = round(-offset, 1)
        offset += length

    context['totals'] = totals
    context['macros'] = macros
    return context


def planner_view(request):
    """Головна сторінка: читає форму, будує раціон на день або кілька днів і показує його (або повідомлення про помилку)."""
    choices = read_choices(request.GET)

    # Базовий context: те, що потрібно ФОРМІ (є завжди — і при успіху, і при помилці)
    context = {
        **choices,
        'max_days': MAX_DAYS,
        'min_calories': MIN_DAY_CALORIES,
        'max_calories': MAX_DAY_CALORIES,
        'query': request.GET.urlencode(),
    }

    try:
        plan_days = build_plan_days(choices)
    except ValueError as error:
        context['error'] = str(error)
        return render(request, 'nutrition/planner.html', context)

    context.update(result_context(
        plan_days, choices['period'], choices['protein_pct'], choices['fat_pct'], choices['carbs_pct'],
    ))
    return render(request, 'nutrition/planner.html', context)


@require_POST
def plan_save_view(request):
    """Зберігає показаний раціон у базу й переходить на його сторінку."""
    query = QueryDict(request.POST.get('query', ''))
    choices = read_choices(query)
    try:
        plan_days = build_plan_days(choices)
    except ValueError:
        # головна сторінка сама покаже, що не так
        return redirect(f"{reverse('planner')}?{query.urlencode()}")
    plan = save_plan(request.POST.get('name', ''), choices, plan_days)
    return redirect('plan_detail', pk=plan.pk)


def plan_list_view(request):
    """Сторінка зі списком збережених раціонів."""
    plans = MealPlan.objects.prefetch_related('items__product')
    return render(request, 'nutrition/plan_list.html', {'plans': plans})


def plan_detail_view(request, pk):
    """Сторінка одного збереженого раціону."""
    if pk > MAX_DB_ID:
        raise Http404
    plan = get_object_or_404(MealPlan, pk=pk)
    context = result_context(load_plan_days(plan), plan.period, plan.protein_pct, plan.fat_pct, plan.carbs_pct)
    context['plan'] = plan
    return render(request, 'nutrition/plan_detail.html', context)
