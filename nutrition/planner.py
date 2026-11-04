import itertools

MIN_PROTEIN = 5   # г білка на 100 г, щоб продукт вважався джерелом білка
MIN_FAT = 2       # г жиру на 100 г, щоб продукт вважався джерелом жиру
MIN_DAY_CALORIES = 0     # ккал на день
MAX_DAY_CALORIES = 10000   # ккал на день: вище — майже напевно помилка вводу
NO_PRODUCTS_MESSAGE = "Бракує продуктів для раціону. Додай їх в адмінці."
NO_RECIPES_MESSAGE = "Бракує страв для раціону. Додай їх в адмінці."
VARIETY_PENALTY = 0.015    # скільки промаху БЖВ «коштує» кожне вживання страви за період: більше — різноманітніше, менше — точніше
DAY_PERCENTAGE = {'Сніданок': 0.25, 'Обід': 0.40, 'Вечеря': 0.35}
WEEK_DAYS = ['Понеділок', 'Вівторок', 'Середа', 'Четвер', "П'ятниця", 'Субота', 'Неділя']

def split_calories(calories):
    """Ділить денну норму калорій між прийомами за частками DAY_PERCENTAGE → {назва: ккал}."""
    day_calories = {}
    for name, share in DAY_PERCENTAGE.items():
        day_calories[name] = (calories * share)
    return day_calories


def build_meal(products, target_calories):
    """Найперша, наївна версія: ділить калорії між продуктами порівну (зараз не використовується)."""
    portion = target_calories / len(products)
    meals = []
    for product in products:
        grams = product.grams_for_calories(portion)
        meals.append({
            'product': product,
            'grams': grams,
        })
    return meals


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


# Двигун з окремих продуктів: build_day, build_period, macro_targets, build_balanced_meal.
# Сторінка тепер складає раціон зі страв (кінець файлу), а цей код лишається як перша версія.
def build_day(protein_source, fat_source, carb_source, day_calories, protein_pct, fat_pct, carbs_pct):
    """Будує раціон на день: для кожного прийому з split_calories викликає build_balanced_meal → список прийомів."""
    check_day_targets(day_calories, protein_pct, fat_pct, carbs_pct)

    meals_in_one_day = []
    for name, meal_calories in split_calories(day_calories).items():
        meal = build_balanced_meal(
            protein_source, fat_source, carb_source, meal_calories,
            protein_pct, fat_pct, carbs_pct,
        )
        meals_in_one_day.append({
            'name': name,
            'calories': meal_calories,
            'meal': meal,
        })
    return meals_in_one_day


def build_period(protein_sources, fat_sources, carb_sources, days_count, day_calories, protein_pct, fat_pct, carbs_pct):
    """Будує раціон на days_count днів: щодня бере продукти зі списків по колу → список днів {name, day}."""
    if not (protein_sources and fat_sources and carb_sources):
        raise ValueError(NO_PRODUCTS_MESSAGE)

    days = []
    for i in range(days_count):
        protein_source = protein_sources[i % len(protein_sources)]
        fat_source = fat_sources[i % len(fat_sources)]
        carb_source = carb_sources[i % len(carb_sources)]
        day = build_day(protein_source, fat_source, carb_source, day_calories, protein_pct, fat_pct, carbs_pct)
        days.append({'name': day_name(i, days_count), 'day': day})
    return days


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


def macro_targets(target_calories, protein_pct, fat_pct, carbs_pct):
    """Ціль БЖВ у грамах: частка калорій ÷ 4 (білок, вуглеводи) або ÷ 9 (жир)."""
    protein_grams = target_calories * protein_pct / 4
    fat_grams     = target_calories * fat_pct / 9
    carbs_grams   = target_calories * carbs_pct / 4
    return {'protein': protein_grams, 'fat': fat_grams, 'carbs': carbs_grams}


def build_balanced_meal(protein_source, fat_source, carb_source, target_calories, protein_pct, fat_pct, carbs_pct):
    """Будує один прийом із 3 продуктів: білок і жир — під ціль, вуглеводи — на решту калорій. Кидає ValueError, якщо продукт не годиться."""
    # ── Перевірка 0: чи всі продукти знайшлись у базі
    if None in (protein_source, fat_source, carb_source):
        raise ValueError(NO_PRODUCTS_MESSAGE)

    # ── Перевірка 1: чи годиться продукт на роль «білок»
    if protein_source.proteins <= MIN_PROTEIN:
        raise ValueError(
            f"«{protein_source.name}» містить замало білка "
            f"({protein_source.proteins} г на 100 г), щоб бути джерелом білка."
        )

    # ── Перевірка 2: чи годиться продукт на роль «жир»
    if fat_source.fats <= MIN_FAT:
        raise ValueError(
            f"«{fat_source.name}» містить замало жиру "
            f"({fat_source.fats} г на 100 г), щоб бути джерелом жиру."
        )

    # ── Перевірка 2б: вуглеводи набираємо калоріями, тож без калорій було б ділення на 0
    if carb_source.calories <= 0:
        raise ValueError(
            f"У продукту «{carb_source.name}» вказано {carb_source.calories} ккал на 100 г, "
            f"тому порцію не порахувати. Виправ це в адмінці."
        )

    targets = macro_targets(target_calories, protein_pct, fat_pct, carbs_pct)

    protein_grams = protein_source.grams_for_protein(targets['protein'])
    protein_calories = protein_source.calories_for(protein_grams)

    fat_grams = fat_source.grams_for_fat(targets['fat'])
    fat_calories = fat_source.calories_for(fat_grams)

    remaining_calories = target_calories - protein_calories - fat_calories

    # ── Перевірка 3: чи залишилось місце для вуглеводів
    if remaining_calories <= 0:
        raise ValueError(
            f"Білок і жир уже дають {protein_calories + fat_calories:.0f} ккал, "
            f"а ціль — {target_calories} ккал. На вуглеводи нічого не лишається. "
            f"Збільш калорії або обери продукти з меншою калорійністю."
        )

    carb_grams = carb_source.grams_for_calories(remaining_calories)


    meal = [
        {'role': 'Білок', 'product': protein_source, 'grams': protein_grams},
        {'role': 'Жир', 'product': fat_source, 'grams': fat_grams},
        {'role': 'Вуглеводи', 'product': carb_source, 'grams': carb_grams},
    ]
    return meal


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


def day_miss(day_recipes, shares, targets):
    """На скільки страви дня разом промахуються повз ціль БЖВ: сума відхилень трьох часток (0 — точно в ціль)."""
    miss = 0
    for macro, target in targets.items():
        day_share = 0
        for meal_share, recipe in zip(DAY_PERCENTAGE.values(), day_recipes):
            day_share += meal_share * shares[recipe][macro]
        miss += abs(day_share - target)
    return miss


def choose_day_recipes(candidates, shares, targets, previous_day, times_used, dish_uses):
    """Вибирає по страві на кожен прийом дня: перебирає всі поєднання й бере найкраще за правилами в rank."""
    def rank(day_recipes):
        """Оцінка поєднання: кортежі порівнюються зліва направо, тож перші правила важливіші, а менше — краще."""
        repeats_today = len(day_recipes) - len(set(day_recipes))
        repeats_from_yesterday = len(set(day_recipes) & set(previous_day))
        uses_in_period = sum(dish_uses.get(recipe, 0) for recipe in day_recipes)
        return (
            repeats_today,
            repeats_from_yesterday,
            times_used.get(day_recipes, 0),
            day_miss(day_recipes, shares, targets) + VARIETY_PENALTY * uses_in_period,
        )

    return min(itertools.product(*candidates), key=rank)


def build_recipe_day(day_recipes, day_calories):
    """Будує день зі страв: порцію кожної страви підганяє під калорії її прийому → список прийомів."""
    meals = []
    for (name, meal_calories), recipe in zip(split_calories(day_calories).items(), day_recipes):
        portion = meal_calories / recipe_totals(recipe)['calories']
        meals.append({
            'name': name,
            'calories': meal_calories,
            'recipe_name': recipe.name,
            'meal': scale_recipe(recipe, portion),
        })
    return meals


def build_recipe_period(recipes, days_count, day_calories, protein_pct, fat_pct, carbs_pct):
    """Будує раціон зі страв на days_count днів → список днів {name, day}. Кидає ValueError, якщо ціль чи страви не годяться."""
    check_day_targets(day_calories, protein_pct, fat_pct, carbs_pct)

    # страву без калорій не масштабувати, тому беремо лише ті, де вони є
    shares = {}
    for recipe in recipes:
        if recipe_totals(recipe)['calories'] > 0:
            shares[recipe] = recipe_shares(recipe)
    if not shares:
        raise ValueError(NO_RECIPES_MESSAGE)

    # candidates: для кожного прийому — страви, які на нього годяться
    candidates = []
    for meal_name in DAY_PERCENTAGE:
        fitting = [recipe for recipe in shares if recipe.fits_meal(meal_name)]
        if not fitting:
            raise ValueError(f"Немає страви на прийом «{meal_name}». Додай її в адмінці.")
        candidates.append(fitting)

    targets = {'protein': protein_pct, 'fat': fat_pct, 'carbs': carbs_pct}
    days = []
    previous_day = ()
    times_used = {}    # скільки разів було кожне поєднання страв
    dish_uses = {}     # скільки разів була кожна страва
    for i in range(days_count):
        day_recipes = choose_day_recipes(candidates, shares, targets, previous_day, times_used, dish_uses)
        times_used[day_recipes] = times_used.get(day_recipes, 0) + 1
        for recipe in day_recipes:
            dish_uses[recipe] = dish_uses.get(recipe, 0) + 1
        previous_day = day_recipes
        days.append({'name': day_name(i, days_count), 'day': build_recipe_day(day_recipes, day_calories)})
    return days