import math
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import silpo
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
    context['shopping_has_cost'] = totals['cost'] is not None

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
        'silpo_connected': silpo.is_connected(request.session),
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


def safe_next_url(request):
    """Адреса повернення з форми: лише всередині нашого сайту, інакше — головна сторінка."""
    next_url = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return reverse('planner')


@require_POST
def silpo_login_view(request):
    """Починає вхід: перекидає людину на сторінку входу «Сільпо»."""
    redirect_uri = request.build_absolute_uri(reverse('silpo_callback'))
    try:
        return redirect(silpo.start_login(request.session, redirect_uri, safe_next_url(request)))
    except silpo.SilpoError as error:
        messages.error(request, str(error))
        return redirect('planner')


def silpo_callback_view(request):
    """Сюди «Сільпо» повертає людину після входу: завершує вхід і повертає її на сторінку, звідки вона прийшла."""
    try:
        next_url = silpo.finish_login(request.session, request.GET)
    except silpo.SilpoError as error:
        messages.error(request, str(error))
        return redirect('planner')
    messages.success(request, 'Акаунт «Сільпо» підключено.')
    return redirect(next_url)


def silpo_item_choices(data):
    """Вибір товарів на сторінці цін з параметрів адреси → {id продукту: id товару або 'skip'}."""
    choices = {}
    for key, value in data.items():
        if key.startswith('item_') and key[len('item_'):].isdecimal():
            choices[int(key[len('item_'):])] = value
    return choices


def plan_params(data):
    """Параметри адреси, що описують сам раціон, без вибору товарів і часу доставки."""
    return [(key, value) for key, value in data.items() if not key.startswith('item_') and key != 'slot']


def silpo_prices_view(request):
    """Сторінка реальних цін «Сільпо» для списку покупок раціону. Лише читає дані: кошик не змінює, ціни не зберігає."""
    back_url = f"{reverse('planner')}?{urlencode(plan_params(request.GET))}"
    if not silpo.is_connected(request.session):
        return redirect(back_url)

    try:
        plan_days = build_plan_days(read_choices(request.GET))
        prices = silpo.find_prices(
            silpo.McpConnection(request.session), shopping_list(plan_days),
            silpo_item_choices(request.GET), request.GET.get('slot', ''),
        )
    except (ValueError, silpo.SilpoError) as error:
        messages.error(request, str(error))
        return redirect(back_url)

    return render(request, 'nutrition/silpo_prices.html', {
        'prices': prices,
        'days_count': len(plan_days),
        'back_url': back_url,
        'plan_params': plan_params(request.GET),
        'query': request.GET.urlencode(),
    })


def add_plan_to_silpo_cart(request):
    """Натискання «Зібрати кошик»: заново шукає товари зі свіжими цінами й додає їх у кошик людини."""
    query = QueryDict(request.POST.get('query', ''))
    prices_url = f"{reverse('silpo_prices')}?{query.urlencode()}"
    try:
        plan_days = build_plan_days(read_choices(query))
        result = silpo.build_cart(
            silpo.McpConnection(request.session), shopping_list(plan_days),
            silpo_item_choices(query), query.get('slot', ''), request.session.get(silpo.ADDED_KEY),
        )
    except (ValueError, silpo.SilpoError) as error:
        messages.error(request, str(error))
        return redirect(prices_url)

    request.session[silpo.ADDED_KEY] = result['added']
    for problem in result['problems']:
        messages.warning(request, problem)
    if not result['problems']:
        messages.success(request, 'Кошик «Сільпо» зібрано під цей раціон. Замовлення ще не оформлено.')
    return redirect(f"{reverse('silpo_cart')}?{query.urlencode()}")


def silpo_cart_view(request):
    """Кошик «Сільпо»: POST додає товари раціону, а GET лише показує кошик і посилання на оформлення."""
    if not silpo.is_connected(request.session):
        return redirect('planner')
    if request.method == 'POST':
        return add_plan_to_silpo_cart(request)

    prices_url = f"{reverse('silpo_prices')}?{request.GET.urlencode()}"
    try:
        cart = silpo.read_cart(silpo.McpConnection(request.session))
    except silpo.SilpoError as error:
        messages.error(request, str(error))
        return redirect(prices_url)
    return render(request, 'nutrition/silpo_cart.html', {'cart': cart, 'prices_url': prices_url})


@require_POST
def silpo_logout_view(request):
    """Виходить з акаунта «Сільпо» в цьому браузері."""
    silpo.logout(request.session)
    messages.success(request, 'Акаунт «Сільпо» відключено.')
    return redirect(safe_next_url(request))
