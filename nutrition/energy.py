"""Денна норма калорій за формулою Міффліна — Сан-Жеора (1990).

1. Базовий обмін (BMR) — скільки організм витрачає в повному спокої:
       10 × вага (кг) + 6,25 × зріст (см) − 5 × вік (років) + 5     — чоловіки
       10 × вага (кг) + 6,25 × зріст (см) − 5 × вік (років) − 161   — жінки
2. Норма для підтримки ваги = базовий обмін × коефіцієнт активності.
3. Ціль = норма підтримки ± відсоток під мету (схуднути / набрати), округлена до 50 ккал.

Формула — для дорослих. Для дітей, вагітних і під час годування груддю вона не підходить.
"""
import math

# ключ, назва, опис, коефіцієнт
ACTIVITY_LEVELS = (
    ('sedentary', 'Мінімальна', 'Сидяча робота, майже без тренувань', 1.2),
    ('light', 'Низька', 'Легкі тренування 1–3 рази на тиждень або багато ходьби', 1.375),
    ('moderate', 'Середня', 'Тренування 3–5 разів на тиждень', 1.55),
    ('high', 'Висока', 'Інтенсивні тренування 6–7 разів на тиждень', 1.725),
    ('extreme', 'Дуже висока', 'Важка фізична робота або тренування двічі на день', 1.9),
)
# ключ, назва, зміна від норми підтримки, яку пропорцію БЖВ підказати (назва з PRESETS у views.py)
GOALS = (
    ('lose', 'Схуднути', -0.15, 'Більше білка'),
    ('maintain', 'Тримати вагу', 0.0, 'Збалансовано'),
    ('gain', 'Набрати вагу', 0.10, 'Збалансовано'),
)
SEXES = (('female', 'Жіноча'), ('male', 'Чоловіча'))
SEX_CONSTANT = {'male': 5, 'female': -161}

# Межі введення: поза ними формула або не працює, або це помилка набору
MIN_AGE, MAX_AGE = 18, 100
MIN_HEIGHT, MAX_HEIGHT = 120, 230      # см
MIN_WEIGHT, MAX_WEIGHT = 30, 300       # кг
# Нижче цього без нагляду лікаря зазвичай не радять їсти (дорослим жінкам / чоловікам)
SAFE_MINIMUM = {'female': 1200, 'male': 1500}
ROUND_TO = 50                          # крок поля «Калорії на день»


def basal_metabolic_rate(sex, age, height_cm, weight_kg):
    """Базовий обмін, ккал на день."""
    return 10 * weight_kg + 6.25 * height_cm - 5 * age + SEX_CONSTANT[sex]


def round_to_step(value, step=ROUND_TO):
    return int(round(value / step) * step)


def daily_norm(sex, age, height_cm, weight_kg, activity, goal):
    """Усе, що показує калькулятор: базовий обмін, підтримка, ціль під мету і чи не замало це."""
    factor = dict((key, k) for key, _, _, k in ACTIVITY_LEVELS)[activity]
    change = dict((key, c) for key, _, c, _ in GOALS)[goal]

    bmr = basal_metabolic_rate(sex, age, height_cm, weight_kg)
    maintenance = bmr * factor
    target = round_to_step(maintenance * (1 + change))
    # Худнути нижче базового обміну не радимо: беремо найближче «кругле» число не менше за нього
    if target < bmr:
        target = math.ceil(bmr / ROUND_TO) * ROUND_TO
    return {
        'bmr': round(bmr),
        'maintenance': round(maintenance),
        'target': target,
        'below_safe_minimum': target < SAFE_MINIMUM[sex],
        'safe_minimum': SAFE_MINIMUM[sex],
    }
