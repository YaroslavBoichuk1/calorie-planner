import itertools
import random

MIN_DAY_CALORIES = 1200  # ккал на день: менше без нагляду лікаря зазвичай не радять
MAX_DAY_CALORIES = 10000   # ккал на день: вище — майже напевно помилка вводу
NO_RECIPES_MESSAGE = "Бракує страв для раціону. Додай їх в адмінці."
VARIANT_JITTER = 0.04      # «Скласти заново»: найбільший випадковий зсув оцінки страви (4 в. п. промаху БЖВ)
VARIETY_PENALTY = 0.025    # скільки промаху БЖВ «коштує» кожне вживання страви за період: більше — різноманітніше, менше — точніше
MAX_MEAL_GRAMS = 800       # г на прийом: важча порція виглядає нереально (2 кг рагу), тож такі страви штрафуємо
MAX_SNACK_GRAMS = 400      # г на перекус: перекус має бути невеликим
HARD_MEAL_GRAMS = 1000     # г: страву, якої на прийом вийшло б більше, на цей прийом не пропонуємо взагалі
HARD_SNACK_GRAMS = 500     # те саме для перекусу
MIN_CHOICE = 6             # якщо легших страв на прийом менше, беремо стільки найлегших
HEAVY_PENALTY = 0.01       # скільки промаху БЖВ «коштують» кожні 100 г понад MAX_MEAL_GRAMS (сильніший ламає БЖВ)
SHORTLIST = 15             # скільки страв на прийом перебирати всіма поєднаннями (усі — надто повільно: 34×77×75)
RICHEST = 3                # з них по стільки найбагатших на білок, жир і вуглеводи — щоб було чим урівноважити день
SNACK_SHORTLIST = 24       # перекусів небагато, тож на перекус перебираємо майже всі
DAY_PERCENTAGE = {'Сніданок': 0.25, 'Обід': 0.40, 'Вечеря': 0.35}
# Скільки прийомів і як ділити калорії: (від скількох ккал на день, {прийом: частка калорій}).
# На великі калорії трьох прийомів замало: обід на 1200 ккал важив би понад кілограм.
# Тому додаємо перекуси — ситні й невеликі (горіхи, сир, хлібці): 400 ккал займають 100–200 г.
MEAL_SCHEDULES = [
    (0, DAY_PERCENTAGE),
    (2200, {'Сніданок': 0.25, 'Обід': 0.35, 'Перекус': 0.15, 'Вечеря': 0.25}),
    (2800, {'Сніданок': 0.20, 'Ранковий перекус': 0.15, 'Обід': 0.25, 'Денний перекус': 0.15, 'Вечеря': 0.25}),
]
SNACKS = {'Перекус', 'Ранковий перекус', 'Денний перекус'}
WEEK_DAYS = ['Понеділок', 'Вівторок', 'Середа', 'Четвер', "П'ятниця", 'Субота', 'Неділя']


def day_schedule(calories):
    """Прийоми дня під цю норму калорій → {назва прийому: частка калорій}."""
    return next(schedule for start, schedule in reversed(MEAL_SCHEDULES) if calories >= start)


def split_calories(calories, schedule=DAY_PERCENTAGE):
    """Ділить денну норму калорій між прийомами за частками schedule → {назва: ккал}."""
    day_calories = {}
    for name, share in schedule.items():
        day_calories[name] = (calories * share)
    return day_calories


def check_day_targets(day_calories, protein_pct, fat_pct, carbs_pct):
    """Перевіряє ціль дня: калорії в межах, частки БЖВ невід'ємні й разом дають 100 %. Кидає ValueError, якщо ні."""
    if not MIN_DAY_CALORIES <= day_calories <= MAX_DAY_CALORIES:
        raise ValueError(f"Калорії на день мають бути від {MIN_DAY_CALORIES} до {MAX_DAY_CALORIES}.")

    if min(protein_pct, fat_pct, carbs_pct) < 0:
        raise ValueError("Частка білка, жиру чи вуглеводів не може бути від'ємною.")

    total_pct = round((protein_pct + fat_pct + carbs_pct) * 100)
    if total_pct != 100:
        raise ValueError(f"Білок, жир і вуглеводи разом мають давати 100 %, а зараз — {total_pct} %.")


def day_name(index, days_count):
    """Назва дня: від 7 днів — дні тижня по колу, менше — «День N»."""
    if days_count >= len(WEEK_DAYS):
        return WEEK_DAYS[index % len(WEEK_DAYS)]
    return f'День {index + 1}'


def period_totals(days):
    """Підсумки за період: складає day_totals усіх днів → {calories, protein, fat, carbs, cost}."""
    totals = {'calories' : 0, 'protein' : 0, 'fat' : 0, 'carbs': 0, 'cost' : 0}
    for day_info in days:
        add_totals(totals, day_totals(day_info['day']))
    return totals


def shopping_list(days):
    """Список покупок за період: сумує грами кожного продукту за всі дні → {продукт: грами}."""
    grams_by_product = {}
    for day_info in days:
        for meal_info in day_info['day']:
            for item in meal_info['meal']:
                product = item['product']
                grams_by_product[product] = grams_by_product.get(product, 0) + item['grams']
    return grams_by_product


def day_totals(meals):
    """Підсумки за день: складає meal_totals усіх прийомів → {calories, protein, fat, carbs, cost}."""
    totals = {'calories' : 0, 'protein' : 0, 'fat' : 0, 'carbs': 0, 'cost' : 0}
    for meal_info in meals:
        add_totals(totals, meal_totals(meal_info['meal']))
    return totals


def add_cost(total, cost):
    """Додає вартість до суми. Якщо хоча б одна вартість невідома (None), невідома й сума."""
    if total is None or cost is None:
        return None
    return total + cost


def add_totals(totals, part):
    """Додає підсумки частини (прийому чи дня) до загальних підсумків."""
    for key in ('calories', 'protein', 'fat', 'carbs'):
        totals[key] += part[key]
    totals['cost'] = add_cost(totals['cost'], part['cost'])


def meal_totals(meal):
    """Підсумки одного прийому: калорії, БЖВ і вартість усіх його продуктів (cost = None, якщо якоїсь ціни нема)."""
    totals = {'calories' : 0, 'protein' : 0, 'fat' : 0, 'carbs': 0, 'cost' : 0}
    for item in meal:
        product = item['product']
        grams = item['grams']
        totals['calories'] += product.calories_for(grams)
        totals['protein'] += product.protein_for(grams)
        totals['fat'] += product.fat_for(grams)
        totals['carbs'] += product.carbs_for(grams)
        totals['cost'] = add_cost(totals['cost'], product.cost_for(grams))
    return totals


# ── Раціон зі страв

def scale_recipe(recipe, portion):
    """Продукти страви з грамами, помноженими на portion → список {product, grams}."""
    meal = []
    for ingredient in recipe.ingredients.all():
        meal.append({'product': ingredient.product, 'grams': ingredient.grams * portion})
    return meal


def recipe_totals(recipe):
    """Підсумки однієї порції страви: калорії, БЖВ і вартість усіх її продуктів → {calories, protein, fat, carbs, cost}."""
    return meal_totals(scale_recipe(recipe, 1))


def recipe_shares(recipe):
    """Які частки калорій страви дають білок, жир і вуглеводи → {protein, fat, carbs}. Масштабування їх не змінює."""
    totals = recipe_totals(recipe)
    return {
        'protein': totals['protein'] * 4 / totals['calories'],
        'fat': totals['fat'] * 9 / totals['calories'],
        'carbs': totals['carbs'] * 4 / totals['calories'],
    }


def recipe_grams(recipe):
    """Скільки важить одна порція страви (сума грамів продуктів)."""
    return sum(ingredient.grams for ingredient in recipe.ingredients.all())


def heaviness_penalty(recipe_calories, grams, meal_calories, limit=MAX_MEAL_GRAMS):
    """Штраф для страви, якої на калорії прийому вийшло б понад limit грамів: 0, якщо порція нормальна."""
    portion_grams = grams * meal_calories / recipe_calories
    return HEAVY_PENALTY * max(0.0, portion_grams - limit) / 100


def shortlist(options, shares, dish_uses, jitter, size=SHORTLIST):
    """Страви прийому, серед яких перебираємо всі поєднання дня (size штук).

    Більшість — найперспективніші: самі по собі близькі до цілі, не надто важкі й не заїжджені.
    Решта — по RICHEST найбагатших на білок, на жир і на вуглеводи: страва, далека від цілі, буває якраз тим,
    що врівноважує решту дня. options — список (страва, постійна оцінка).
    """
    def promise(option):
        recipe, static_cost = option
        return static_cost + VARIETY_PENALTY * dish_uses.get(recipe, 0) + jitter.get(recipe, 0)

    ranked = [recipe for recipe, _ in sorted(options, key=promise)]
    chosen = ranked[:size - 3 * RICHEST]
    for macro in ('protein', 'fat', 'carbs'):
        richest = [recipe for recipe in sorted(ranked, key=lambda recipe: -shares[recipe][macro]) if recipe not in chosen]
        chosen.extend(richest[:RICHEST])
    return chosen


def choose_day_recipes(shortlists, snack_slots, meal_shares, weighted, heavy, targets, previous_day, times_used, cost):
    """Вибирає по страві на кожен прийом дня → кортеж страв у порядку прийомів.

    Без перекусів перебирає всі поєднання коротких списків і бере найкраще за rank.
    З перекусами всіх поєднань забагато (15⁵ = 760 тис. на день), тож у три кроки:
    1) основні прийоми, ніби перекуси дадуть рівно ціль на свою частку калорій;
    2) перекуси — під те, чого основним бракує;
    3) ще раз основні — під уже вибрані перекуси.

    snack_slots — номери прийомів-перекусів; meal_shares[i] — частка калорій прийому i;
    weighted[i][страва] — частки БЖВ страви, помножені на частку калорій прийому i (щоб день просто складати);
    heavy[i][страва] — штраф за надто важку порцію на прийомі i; cost[страва] — штраф за різноманітність.
    """
    yesterday = set(previous_day)
    main_slots = [slot for slot in range(len(shortlists)) if slot not in snack_slots]

    def rank(day):
        """Оцінка дня {прийом: страва}: кортежі порівнюються зліва направо, тож перші правила важливіші, а менше — краще.
        Прийоми без страви ще не вибрані: ціль для решти береться пропорційно їхній частці калорій."""
        protein = fat = carbs = extra = share = 0.0
        for slot, recipe in day.items():
            meal_protein, meal_fat, meal_carbs = weighted[slot][recipe]
            protein += meal_protein
            fat += meal_fat
            carbs += meal_carbs
            extra += cost[recipe] + heavy[slot][recipe]
            share += meal_shares[slot]
        miss = (abs(protein - targets['protein'] * share) + abs(fat - targets['fat'] * share)
                + abs(carbs - targets['carbs'] * share))
        recipes = list(day.values())
        return (
            len(recipes) - len(set(recipes)),                                 # одна страва двічі за день
            len(yesterday.intersection(recipes)),                             # страви, які були вчора
            times_used.get(tuple(day[slot] for slot in main_slots), 0),       # такі самі основні страви вже були
            miss + extra,                                                     # промах БЖВ + різноманітність + вага порцій
        )

    def best(slots, fixed):
        """Найкращі страви на прийоми slots, коли на решту вже вибрано fixed {прийом: страва}."""
        combos = itertools.product(*(shortlists[slot] for slot in slots))
        return min(({**fixed, **dict(zip(slots, combo))} for combo in combos), key=rank)

    day = best(main_slots, {})
    if snack_slots:
        day = best(snack_slots, day)
        day = best(main_slots, {slot: day[slot] for slot in snack_slots})
    return tuple(day[slot] for slot in range(len(shortlists)))


def build_recipe_day(day_recipes, day_calories, schedule=DAY_PERCENTAGE):
    """Будує день зі страв: порцію кожної страви підганяє під калорії її прийому → список прийомів."""
    meals = []
    for (name, meal_calories), recipe in zip(split_calories(day_calories, schedule).items(), day_recipes):
        portion = meal_calories / recipe_totals(recipe)['calories']
        meals.append({
            'name': name,
            'calories': meal_calories,
            'recipe_name': recipe.name,
            'meal': scale_recipe(recipe, portion),
        })
    return meals


def swap_meal(days, day_index, meal_index, recipe):
    """Ставить іншу страву в один прийом: порція підганяється під калорії цього прийому.
    Повертає False і нічого не змінює, якщо такого прийому нема або страва на нього не годиться."""
    if not 0 <= day_index < len(days):
        return False
    day = days[day_index]['day']
    if not 0 <= meal_index < len(day):
        return False
    meal_info = day[meal_index]
    calories = recipe_totals(recipe)['calories']
    if calories <= 0 or not recipe.fits_meal(meal_info['name']):
        return False
    meal_info['recipe_name'] = recipe.name
    meal_info['meal'] = scale_recipe(recipe, meal_info['calories'] / calories)
    return True


def build_recipe_period(recipes, days_count, day_calories, protein_pct, fat_pct, carbs_pct, variant=0):
    """Будує раціон зі страв на days_count днів → список днів {name, day}. Кидає ValueError, якщо ціль чи страви не годяться."""
    check_day_targets(day_calories, protein_pct, fat_pct, carbs_pct)

    # страву без калорій не масштабувати, тому беремо лише ті, де вони є
    shares = {}
    recipe_calories = {}
    for recipe in recipes:
        calories = recipe_totals(recipe)['calories']
        if calories > 0:
            recipe_calories[recipe] = calories
            shares[recipe] = recipe_shares(recipe)
    if not shares:
        raise ValueError(NO_RECIPES_MESSAGE)

    schedule = day_schedule(day_calories)
    if not any(recipe.fits_meal('Перекус') for recipe in shares):
        schedule = DAY_PERCENTAGE     # перекусів у базі ще нема (скажімо, каталог не завантажили) — лишаємо три прийоми
    snack_slots = {slot for slot, name in enumerate(schedule) if name in SNACKS}

    # candidates: для кожного прийому — страви, які на нього годяться і порція яких не вийде завеликою
    candidates = []
    for meal_name, meal_share in schedule.items():
        fitting = [recipe for recipe in shares if recipe.fits_meal(meal_name)]
        if not fitting:
            raise ValueError(f"Немає страви на прийом «{meal_name}». Додай її в адмінці.")
        limit = HARD_SNACK_GRAMS if meal_name in SNACKS else HARD_MEAL_GRAMS
        def portion(recipe):
            return recipe_grams(recipe) * day_calories * meal_share / recipe_calories[recipe]
        light = [recipe for recipe in fitting if portion(recipe) <= limit]
        # якщо легких страв замало, беремо найлегші: краще важка порція, ніж раціон без вибору
        candidates.append(light if len(light) >= MIN_CHOICE else sorted(fitting, key=portion)[:MIN_CHOICE])

    targets = {'protein': protein_pct, 'fat': fat_pct, 'carbs': carbs_pct}
    # Усе, що не міняється від дня до дня, рахуємо один раз: частки БЖВ на прийом, штраф за вагу, близькість до цілі
    meal_shares = list(schedule.values())
    meal_calories = list(split_calories(day_calories, schedule).values())
    weighted, heavy, options = [], [], []
    for slot, (meal_share, calories, fitting) in enumerate(zip(meal_shares, meal_calories, candidates)):
        limit = MAX_SNACK_GRAMS if slot in snack_slots else MAX_MEAL_GRAMS
        weighted.append({
            recipe: tuple(meal_share * shares[recipe][macro] for macro in ('protein', 'fat', 'carbs')) for recipe in fitting
        })
        heavy.append({
            recipe: heaviness_penalty(recipe_calories[recipe], recipe_grams(recipe), calories, limit) for recipe in fitting
        })
        options.append([
            (recipe, sum(abs(shares[recipe][macro] - target) for macro, target in targets.items()) + heavy[-1][recipe])
            for recipe in fitting
        ])

    days = []
    # «Скласти заново»: кожен варіант трохи по-своєму зсуває оцінки страв. Той самий номер дає те саме меню.
    jitter = {}
    if variant:
        generator = random.Random(variant)
        jitter = {recipe: generator.uniform(0, VARIANT_JITTER) for recipe in sorted(shares, key=lambda recipe: recipe.pk)}

    previous_day = ()
    times_used = {}    # скільки разів було кожне поєднання основних страв (без перекусів)
    dish_uses = {}     # скільки разів була кожна страва
    for i in range(days_count):
        shortlists = [
            shortlist(meal_options, shares, dish_uses, jitter, SNACK_SHORTLIST if slot in snack_slots else SHORTLIST)
            for slot, meal_options in enumerate(options)
        ]
        cost = {recipe: VARIETY_PENALTY * dish_uses.get(recipe, 0) + jitter.get(recipe, 0)
                for recipes in shortlists for recipe in recipes}
        day_recipes = choose_day_recipes(
            shortlists, snack_slots, meal_shares, weighted, heavy, targets, previous_day, times_used, cost,
        )
        main = tuple(recipe for slot, recipe in enumerate(day_recipes) if slot not in snack_slots)
        times_used[main] = times_used.get(main, 0) + 1
        for recipe in day_recipes:
            dish_uses[recipe] = dish_uses.get(recipe, 0) + 1
        previous_day = day_recipes
        days.append({'name': day_name(i, days_count), 'day': build_recipe_day(day_recipes, day_calories, schedule)})
    return days
