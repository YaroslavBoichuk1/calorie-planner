import math
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import silpo
from .models import MAX_LIST_KEY, BoughtList, MealPlan, Profile, Recipe
from .plans import load_plan_days, save_plan
from .planner import (
    MEAL_SCHEDULES, MIN_DAY_CALORIES, MAX_DAY_CALORIES, SNACKS, WEEK_DAYS,
    build_recipe_period, day_totals, meal_totals, period_totals, recipe_shares, recipe_totals,
    shopping_list, swap_meal,
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


PLAN_KEYS = ('period', 'days', 'calories', 'protein_pct', 'fat_pct', 'carbs_pct', 'variant', 'swaps')   # параметри, що описують сам раціон
MAX_SWAPS = 100               # більше замін в адресі не читаємо
MAX_VARIANT = 999999          # номер варіанта меню («Скласти заново»)
BOUGHT_KEY = 'bought'         # у сесії: що вже куплено {ключ списку: [id продуктів]}
MAX_BOUGHT_LISTS = 10         # скільки списків гостя пам'ятаємо в сесії; найстаріші забуваємо
MAX_ACCOUNT_BOUGHT_LISTS = 50 # скільки списків пам'ятаємо в акаунті
GOAL_KEYS = ('period', 'days', 'calories', 'protein_pct', 'fat_pct', 'carbs_pct')   # те, що зберігається як ціль
PERIOD_LABELS = {'day': 'День', 'week': 'Тиждень', 'custom': 'Свій'}
SHORT_DAY_NAMES = {
    'Понеділок': 'Пн', 'Вівторок': 'Вт', 'Середа': 'Ср', 'Четвер': 'Чт',
    "П'ятниця": 'Пт', 'Субота': 'Сб', 'Неділя': 'Нд',
}
MACROS = (('protein', 'Білок', 4), ('fat', 'Жир', 9), ('carbs', 'Вуглеводи', 4))   # ключ, назва, ккал в 1 г
PRESETS = (('Збалансовано', 30, 30, 40), ('Більше білка', 40, 30, 30), ('Менше вуглеводів', 35, 40, 25))
DEFAULT_COLOR = '#D9D9D4'      # колір продукту, якому його ще не задали
RING_RADIUS = 80               # радіус кільця БЖВ у SVG 200×200
RING_GAP = 26                  # проміжок між дугами кільця (з урахуванням заокруглених кінців)
PLATE_CENTER = 60              # центр тарілки в SVG 120×120
PLATE_FOOD_RADIUS = 40         # радіус їжі на тарілці


def plan_query(data, **changes):
    """Параметри раціону з адреси (без вибору дня, страви чи товарів) → рядок запиту; changes підміняють значення."""
    params = {key: data.get(key) for key in PLAN_KEYS if data.get(key) not in (None, '')}
    params.update({key: value for key, value in changes.items() if value is not None})
    return urlencode({key: value for key, value in params.items() if value != ''})


def ring_arcs(values, radius=RING_RADIUS, gap=RING_GAP):
    """Дуги кільця: кожне значення займає свою частку кола → список {dash, offset} для SVG."""
    circumference = 2 * math.pi * radius
    total = sum(values) or 1
    arcs = []
    done = 0.0
    for value in values:
        full = value / total * circumference
        length = max(full - gap, 0.1)
        arcs.append({'dash': f'{length:.1f} {circumference - length:.1f}', 'offset': f'{-(done + gap / 2):.1f}'})
        done += full
    return arcs


def macro_summary(totals, targets):
    """БЖВ для підсумку: грами, фактичний і цільовий відсоток, смужки й дуги кільця."""
    calories = totals['calories'] or 1
    rows = []
    for (key, name, kcal_per_gram), target in zip(MACROS, targets):
        actual = totals[key] * kcal_per_gram / calories * 100
        rows.append({
            'key': key, 'name': name, 'grams': totals[key], 'actual': actual, 'target': target,
            'width': min(actual * 2, 100),     # смужка показує 0–50 %, щоб різницю було видно
            'tick': min(target * 2, 100),
        })
    for row, arc in zip(rows, ring_arcs([row['actual'] for row in rows])):
        row['arc'] = arc
    return rows


def product_color(product):
    """Колір продукту для тарілки й списків."""
    return product.color or DEFAULT_COLOR


def plate_slices(meal):
    """Тарілка страви: кожен продукт займає сектор за своєю вагою → список {d, color} для SVG."""
    total = sum(item['grams'] for item in meal) or 1
    center, radius = PLATE_CENTER, PLATE_FOOD_RADIUS
    slices = []
    angle = -math.pi / 2
    for item in meal:
        share = item['grams'] / total
        color = product_color(item['product'])
        if share > 0.999:   # один продукт: ціле коло з двох дуг
            d = (f'M{center - radius} {center} A{radius} {radius} 0 1 1 {center + radius} {center} '
                 f'A{radius} {radius} 0 1 1 {center - radius} {center} Z')
            return [{'d': d, 'color': color}]
        end = angle + share * 2 * math.pi
        start_x, start_y = center + radius * math.cos(angle), center + radius * math.sin(angle)
        end_x, end_y = center + radius * math.cos(end), center + radius * math.sin(end)
        large = 1 if share > 0.5 else 0
        slices.append({
            'd': f'M{center} {center} L{start_x:.2f} {start_y:.2f} A{radius} {radius} 0 {large} 1 {end_x:.2f} {end_y:.2f} Z',
            'color': color,
        })
        angle = end
    return slices


def dish_name(meal_info):
    """Назва страви; у старих збережених раціонах страви нема — тоді перелік продуктів."""
    if meal_info.get('recipe_name'):
        return meal_info['recipe_name']
    return ', '.join(item['product'].name for item in meal_info['meal']).capitalize()


def short_day_name(name, index, numbered):
    """Коротка назва дня для вкладки: «Пн», а в довгому періоді ще й номер дня."""
    short = SHORT_DAY_NAMES.get(name, name)
    return f'{index + 1} · {short}' if numbered else short


def composition_of(meal):
    """Склад страви для показу: продукти з грамами, штуками, кольором і довжиною смужки."""
    biggest = max((item['grams'] for item in meal), default=0) or 1
    return [
        {
            'name': item['product'].name,
            'grams': item['grams'],
            'pieces': item['product'].pieces_to_use(item['grams']),
            'color': product_color(item['product']),
            'width': round(item['grams'] / biggest * 100),
        }
        for item in meal
    ]


def plan_view(plan_days, targets, day_index, meal_index, link):
    """Дані для сторінки раціону: вкладки днів, прийоми вибраного дня, склад вибраної страви й підсумок дня.
    link(day, meal) повертає адресу, яка відкриває цей день і цю страву."""
    day_index = min(max(day_index, 0), len(plan_days) - 1)
    day = plan_days[day_index]['day']
    meal_index = min(max(meal_index, 0), len(day) - 1)
    numbered = len(plan_days) > len(WEEK_DAYS)

    days = [
        {
            'name': info['name'],
            'short': short_day_name(info['name'], index, numbered),
            'url': link(index, 0),
            'selected': index == day_index,
        }
        for index, info in enumerate(plan_days)
    ]

    meals = []
    for index, meal_info in enumerate(day):
        meals.append({
            'index': index,
            'items': composition_of(meal_info['meal']),
            'name': meal_info['name'],
            'snack': meal_info['name'] in SNACKS,
            'calories': meal_info['calories'],
            'dish': dish_name(meal_info),
            'totals': meal_totals(meal_info['meal']),
            'slices': plate_slices(meal_info['meal']),
            'url': link(day_index, index),
            'selected': index == meal_index,
        })

    totals = day_totals(day)
    period = period_totals(plan_days)
    return {
        'days': days if len(plan_days) > 1 else [],
        'day_name': plan_days[day_index]['name'] if len(plan_days) > 1 else 'Сьогодні',
        'day_index': day_index,
        'meals': meals,
        'chosen': meals[meal_index],
        'summary': {'totals': totals, 'macros': macro_summary(totals, targets)},
        'period': {'days': len(plan_days), 'totals': period},
        'shopping_count': len(shopping_list(plan_days)),
    }


def shopping_rows(days):
    """Рядки списку покупок: продукт, колір, грами, штуки й вартість (None, якщо ціна невідома)."""
    rows = []
    for product, grams in shopping_list(days).items():
        rows.append({
            'product': product,
            'color': product_color(product),
            'grams': grams,
            'pieces': product.pieces_for(grams),
            'cost': product.cost_for(grams),
        })
    return rows


def parse_swaps(raw):
    """Заміни страв з адреси: «0.1.12,2.0.5» (день.прийом.id страви) → {(день, прийом): id}. Погані шматки пропускає."""
    swaps = {}
    for part in (raw or '').split(',')[:MAX_SWAPS]:
        pieces = part.split('.')
        if len(pieces) == 3 and all(piece.isascii() and piece.isdigit() and len(piece) < 10 for piece in pieces):
            day, meal, recipe_id = (int(piece) for piece in pieces)
            swaps[(day, meal)] = recipe_id
    return swaps


def format_swaps(swaps):
    """Заміни страв → рядок для адреси (порожній, якщо замін нема)."""
    return ','.join(f'{day}.{meal}.{recipe_id}' for (day, meal), recipe_id in sorted(swaps.items()))


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
        'variant': min(max(parse_int(data.get('variant'), 0), 0), MAX_VARIANT),
        'swaps': parse_swaps(data.get('swaps')),
    }


def build_plan_days(choices):
    """Будує раціон зі страв за вибором користувача → список днів {name, day}. Кидає ValueError, якщо ціль чи страви не годяться."""
    days_count = 1 if choices['period'] == 'day' else choices['days_count']
    recipes = list(Recipe.objects.prefetch_related('ingredients__product'))
    days = build_recipe_period(
        recipes,
        days_count,
        choices['target_calories'],
        choices['protein_pct'] / 100,
        choices['fat_pct'] / 100,
        choices['carbs_pct'] / 100,
        choices.get('variant', 0),
    )
    by_id = {recipe.pk: recipe for recipe in recipes}
    for (day_index, meal_index), recipe_id in choices.get('swaps', {}).items():
        if recipe_id in by_id:
            swap_meal(days, day_index, meal_index, by_id[recipe_id])
    return days


def search_text(recipe):
    """Рядок, у якому шукає поле «Знайти страву»: назва й продукти зі складу, малими літерами, з одним видом апострофа."""
    words = [recipe.name, *(ingredient.product.name for ingredient in recipe.ingredients.all())]
    return ' '.join(words).lower().replace('’', "'").replace('ʼ', "'")


def swap_options(recipes, choices, request, day_index, meal_index, meal_name, dishes_today):
    """Страви, якими можна замінити прийом, із посиланнями → список {name, search, shares, url}; найближчі до цілі БЖВ спершу.
    Страв, які вже є в цьому дні, не пропонує, щоб за день нічого не повторювалось."""
    targets = {'protein': choices['protein_pct'] / 100, 'fat': choices['fat_pct'] / 100, 'carbs': choices['carbs_pct'] / 100}
    options = []
    for recipe in recipes:
        if recipe.name in dishes_today or not recipe.fits_meal(meal_name) or recipe_totals(recipe)['calories'] <= 0:
            continue
        shares = recipe_shares(recipe)
        swaps = {**choices['swaps'], (day_index, meal_index): recipe.pk}
        options.append({
            'name': recipe.name,
            'search': search_text(recipe),
            'shares': {key: round(value * 100) for key, value in shares.items()},
            'miss': sum(abs(shares[key] - targets[key]) for key in targets),
            'url': f"?{plan_query(request.GET, swaps=format_swaps(swaps), day=day_index, meal=meal_index)}",
        })
    return sorted(options, key=lambda option: option['miss'])


def targets_of(choices):
    """Цільові відсотки БЖВ із вибору."""
    return choices['protein_pct'], choices['fat_pct'], choices['carbs_pct']


def apply_preset(data):
    """Натиснули готову пропорцію (кнопка preset=номер) → копія даних форми з її відсотками БЖВ."""
    data = data.copy()
    index = parse_int(data.pop('preset', [''])[-1], -1)
    if 0 <= index < len(PRESETS):
        _, data['protein_pct'], data['fat_pct'], data['carbs_pct'] = PRESETS[index]
    return data


def goal_context(choices):
    """Дані для панелі «Ціль» на сторінці раціону: готові пропорції, поля БЖВ із грамами, сума відсотків."""
    current = (choices['protein_pct'], choices['fat_pct'], choices['carbs_pct'])
    return {
        'presets': [{'name': name, 'active': current == (protein, fat, carbs)} for name, protein, fat, carbs in PRESETS],
        'macros': [
            {'key': key, 'name': name, 'pct': choices[f'{key}_pct'], 'kcal': kcal_per_gram,
             'grams': choices['target_calories'] * choices[f'{key}_pct'] / 100 / kcal_per_gram}
            for key, name, kcal_per_gram in MACROS
        ],
        'pct_sum': sum(current),
        'max_days': MAX_DAYS,
        'min_calories': MIN_DAY_CALORIES,
        'max_calories': MAX_DAY_CALORIES,
    }


def saved_goal_redirect(request):
    """Людина увійшла, має збережену ціль і відкрила сторінку без параметрів раціону → та сама сторінка з її ціллю.

    Так ціль потрапляє в адресу, і всі посилання (покупки, «Сільпо», збереження) бачать ті самі параметри.
    """
    if not request.user.is_authenticated or any(key in request.GET for key in PLAN_KEYS):
        return None
    profile = Profile.objects.filter(user=request.user).first()
    if profile is None:
        return None
    query = request.GET.copy()
    query.update(profile.goal())
    return redirect(f'{request.path}?{query.urlencode()}')


@login_required
@require_POST
def goal_save_view(request):
    """Зберігає ціль у профіль і відкриває раціон з нею. Ціль, з якої раціон не складається, не зберігаємо."""
    data = apply_preset(request.POST)
    choices = read_choices(data)
    query = plan_query(data)
    try:
        build_plan_days(choices)
    except ValueError:
        return redirect(f"{reverse('planner')}?{query}")     # головна сторінка сама покаже, що не так
    Profile.objects.update_or_create(user=request.user, defaults={
        'period': choices['period'], 'days': choices['days_count'], 'calories': choices['target_calories'],
        'protein_pct': choices['protein_pct'], 'fat_pct': choices['fat_pct'], 'carbs_pct': choices['carbs_pct'],
    })
    messages.success(request, 'Ціль збережено: з нею раціон відкриватиметься й наступного разу.')
    return redirect(f"{reverse('planner')}?{query}")


def planner_view(request):
    """Головна сторінка «Раціон»: будує раціон за параметрами з адреси й показує вибраний день і страву."""
    if goal_redirect := saved_goal_redirect(request):
        return goal_redirect
    if 'preset' in request.GET:          # гість натиснув готову пропорцію → та сама адреса з її відсотками
        return redirect(f"{request.path}?{plan_query(apply_preset(request.GET))}")
    choices = read_choices(request.GET)
    base = plan_query(request.GET)
    context = {
        **choices,
        **goal_context(choices),
        'period_label': PERIOD_LABELS[choices['period']],
        'period_links': [
            {'label': label, 'url': f"?{plan_query(request.GET, period=key)}", 'active': key == choices['period']}
            for key, label in PERIOD_LABELS.items()
        ],
        'query': base,
        # «Скласти заново»: наступний варіант меню; заміни страв скидаються, бо меню вже інше
        'remake_url': f"?{plan_query(request.GET, variant=choices['variant'] % MAX_VARIANT + 1, swaps='')}",
        'first_variant_url': f"?{plan_query(request.GET, variant='', swaps='')}",
        'show_shopping': True,
        'show_save': True,
    }

    try:
        plan_days = build_plan_days(choices)
    except ValueError as error:
        context['error'] = str(error)
        return render(request, 'nutrition/planner.html', context)

    def link(day_index, meal_index):
        """Адреса сторінки з вибраним днем і стравою."""
        return f"?{plan_query(request.GET, day=day_index, meal=meal_index)}"

    view = plan_view(
        plan_days, targets_of(choices),
        parse_int(request.GET.get('day'), 0), parse_int(request.GET.get('meal'), 0), link,
    )
    recipes = list(Recipe.objects.prefetch_related('ingredients__product'))
    dishes_today = {meal['dish'] for meal in view['meals']}
    for meal in view['meals']:
        slot = (view['day_index'], meal['index'])
        meal['swap_options'] = swap_options(recipes, choices, request, *slot, meal['name'], dishes_today)
        if slot in choices['swaps']:
            rest = {key: value for key, value in choices['swaps'].items() if key != slot}
            meal['reset_url'] = f"?{plan_query(request.GET, swaps=format_swaps(rest), day=slot[0], meal=slot[1])}"
    context['view'] = view
    if any(meal['name'] in SNACKS for meal in view['meals']):
        context['snack_from'] = MEAL_SCHEDULES[1][0]     # з яких калорій з'являються перекуси — для примітки
    return render(request, 'nutrition/planner.html', context)


def list_key_for_query(query):
    """Ключ списку покупок раціону з адреси: той самий раціон із тими самими замінами — той самий список."""
    return f'q:{query}'


def list_key_for_plan(plan):
    """Ключ списку покупок збереженого раціону."""
    return f'plan:{plan.pk}'


def valid_list_key(key):
    """Чи схожий ключ списку на наш: «q:…» або «plan:<число>», і не задовгий."""
    if not isinstance(key, str) or len(key) > MAX_LIST_KEY:
        return False
    return key.startswith('q:') or (key.startswith('plan:') and key[5:].isascii() and key[5:].isdigit())


def bought_ids(request, key):
    """Id продуктів, позначених купленими в цьому списку: з акаунта, якщо людина увійшла, інакше із сесії."""
    if request.user.is_authenticated:
        row = BoughtList.objects.filter(user=request.user, key=key).first()
        return set(row.product_ids) if row else set()
    return set(request.session.get(BOUGHT_KEY, {}).get(key, []))


def set_bought(request, key, product_ids, bought):
    """Ставить або знімає галочки «куплено» для кількох продуктів одного списку."""
    if request.user.is_authenticated:
        set_account_bought(request.user, key, product_ids, bought)
    else:
        set_session_bought(request.session, key, product_ids, bought)


@transaction.atomic
def set_account_bought(user, key, product_ids, bought):
    """Галочки в акаунті. Пам'ятає лише останні MAX_ACCOUNT_BOUGHT_LISTS списків."""
    row = BoughtList.objects.select_for_update().filter(user=user, key=key).first() or BoughtList(user=user, key=key)
    ids = set(row.product_ids)
    ids = ids | set(product_ids) if bought else ids - set(product_ids)
    if ids:
        row.product_ids = sorted(ids)
        row.save()                          # updated_at оновлюється: список тепер найсвіжіший
    elif row.pk:
        row.delete()
    stale = BoughtList.objects.filter(user=user).order_by('-updated_at', '-pk').values_list('pk', flat=True)[MAX_ACCOUNT_BOUGHT_LISTS:]
    BoughtList.objects.filter(pk__in=list(stale)).delete()


def clear_bought(request, key):
    """«Зняти всі галочки» в одному списку."""
    if request.user.is_authenticated:
        BoughtList.objects.filter(user=request.user, key=key).delete()
    else:
        lists = dict(request.session.get(BOUGHT_KEY, {}))
        lists.pop(key, None)
        request.session[BOUGHT_KEY] = lists


def move_session_bought(request):
    """Після входу: галочки, поставлені гостем у цьому браузері, переходять в акаунт (додаються до наявних)."""
    lists = request.session.pop(BOUGHT_KEY, {})
    if not isinstance(lists, dict):
        return
    for key, ids in list(lists.items())[-MAX_BOUGHT_LISTS:]:
        clean_ids = [i for i in ids if isinstance(i, int) and 0 < i <= MAX_DB_ID] if isinstance(ids, list) else []
        if valid_list_key(key) and clean_ids:
            set_account_bought(request.user, key, clean_ids, True)


def set_session_bought(session, key, product_ids, bought):
    """Галочки гостя в сесії. Пам'ятає лише останні MAX_BOUGHT_LISTS списків."""
    lists = dict(session.get(BOUGHT_KEY, {}))
    ids = set(lists.pop(key, []))
    ids = ids | set(product_ids) if bought else ids - set(product_ids)
    if ids:
        lists[key] = sorted(ids)            # пересуваємо список у кінець: він тепер найсвіжіший
    while len(lists) > MAX_BOUGHT_LISTS:
        lists.pop(next(iter(lists)))
    session[BOUGHT_KEY] = lists


def mark_bought(rows, bought):
    """Позначає рядки списку покупок купленими → (скільки куплено, скільки всього)."""
    for row in rows:
        row['bought'] = row['product'].pk in bought
    return sum(1 for row in rows if row['bought']), len(rows)


@require_POST
def shopping_check_view(request):
    """Галочка «куплено» в списку покупок: ставить, знімає або скидає всі. Без JavaScript повертає на сторінку списку."""
    key = request.POST.get('list_key', '')
    if valid_list_key(key):
        if request.POST.get('reset'):
            clear_bought(request, key)
        else:
            product_id = parse_int(request.POST.get('product'), 0)
            if 0 < product_id <= MAX_DB_ID:
                set_bought(request, key, [product_id], request.POST.get('bought') == '1')
    if request.headers.get('X-Requested-With') == 'fetch':
        return JsonResponse({'ok': valid_list_key(key)})
    return redirect(safe_next_url(request))


def shopping_view(request):
    """Сторінка «Покупки»: список продуктів на весь раціон."""
    if goal_redirect := saved_goal_redirect(request):
        return goal_redirect
    choices = read_choices(request.GET)
    context = {**choices, 'period_label': PERIOD_LABELS[choices['period']], 'query': plan_query(request.GET)}
    try:
        plan_days = build_plan_days(choices)
    except ValueError as error:
        context['error'] = str(error)
        return render(request, 'nutrition/shopping.html', context)
    totals = period_totals(plan_days)
    rows = shopping_rows(plan_days)
    key = list_key_for_query(context['query'])
    bought_count, total_count = mark_bought(rows, bought_ids(request, key))
    context.update({
        'rows': rows,
        'days_total': len(plan_days),
        'cost': totals['cost'],
        'list_key': key,
        'bought_count': bought_count,
        'total_count': total_count,
    })
    return render(request, 'nutrition/shopping.html', context)


@require_POST
def plan_save_view(request):
    """Зберігає показаний раціон в акаунт і переходить на його сторінку. Гостя спершу просимо увійти."""
    query = QueryDict(request.POST.get('query', ''))
    if not request.user.is_authenticated:
        back = f"{reverse('planner')}?{plan_query(query)}"
        return redirect(f"{reverse('login')}?{urlencode({'next': back})}")
    choices = read_choices(query)
    try:
        plan_days = build_plan_days(choices)
    except ValueError:
        # головна сторінка сама покаже, що не так
        return redirect(f"{reverse('planner')}?{query.urlencode()}")
    plan = save_plan(request.POST.get('name', ''), choices, plan_days, owner=request.user)
    return redirect('plan_detail', pk=plan.pk)


@login_required
def plan_list_view(request):
    """Сторінка зі списком збережених раціонів — лише своїх."""
    plans = MealPlan.objects.filter(owner=request.user).prefetch_related('items__product')
    return render(request, 'nutrition/plan_list.html', {'plans': plans})


@login_required
def plan_detail_view(request, pk):
    """Сторінка одного збереженого раціону: ті самі дні й тарілки, а також список покупок."""
    if pk > MAX_DB_ID:
        raise Http404
    plan = get_object_or_404(MealPlan, pk=pk, owner=request.user)     # чужий раціон — «не знайдено»
    plan_days = load_plan_days(plan)

    def link(day_index, meal_index):
        """Адреса цієї ж сторінки з вибраним днем і стравою."""
        return f'?day={day_index}&meal={meal_index}'

    view = plan_view(
        plan_days, (plan.protein_pct, plan.fat_pct, plan.carbs_pct),
        parse_int(request.GET.get('day'), 0), parse_int(request.GET.get('meal'), 0), link,
    )
    rows = shopping_rows(plan_days)
    key = list_key_for_plan(plan)
    bought_count, total_count = mark_bought(rows, bought_ids(request, key))
    return render(request, 'nutrition/plan_detail.html', {
        'plan': plan,
        'view': view,
        'rows': rows,
        'cost': view['period']['totals']['cost'],
        'list_key': key,
        'bought_count': bought_count,
        'total_count': total_count,
    })


@login_required
@require_POST
def plan_delete_view(request, pk):
    """Видаляє свій збережений раціон разом із його продуктами й галочками «куплено». Чужий — «не знайдено»."""
    if pk > MAX_DB_ID:
        raise Http404
    plan = get_object_or_404(MealPlan, pk=pk, owner=request.user)
    name = plan.name
    with transaction.atomic():
        BoughtList.objects.filter(user=request.user, key=list_key_for_plan(plan)).delete()
        plan.delete()                       # рядки PlanItem видаляються разом із ним (CASCADE)
    messages.success(request, f'Раціон «{name}» видалено.')
    return redirect('plan_list')


def safe_next_url(request):
    """Адреса повернення (поле next у формі або в адресі): лише всередині нашого сайту, інакше — головна сторінка."""
    next_url = request.POST.get('next') or request.GET.get('next', '')
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
