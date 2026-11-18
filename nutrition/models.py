import math

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from .energy import GOALS

MAX_LIST_KEY = 600       # найдовший ключ списку покупок (параметри раціону з адреси)
class Product(models.Model):
    UNIT_CHOICES = [
        ('g', 'на 100 г'),
        ('pc', 'штучно'),
    ]
    unit = models.CharField(max_length=2, choices=UNIT_CHOICES, default='g')
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)   # необов'язкова: реальні ціни — із «Сільпо»
    calories = models.IntegerField()
    proteins = models.DecimalField(max_digits=6, decimal_places=1)
    fats = models.DecimalField(max_digits=6, decimal_places=1)
    carbs = models.DecimalField(max_digits=6, decimal_places=1)
    grams_per_piece = models.FloatField(null=True, blank=True)
    silpo_search = models.CharField('Пошук у «Сільпо»', max_length=100, blank=True)   # якщо порожньо — шукаємо за назвою
    fdc_id = models.PositiveIntegerField('USDA FDC ID', null=True, blank=True)        # звідки взято БЖВ (USDA FoodData Central)
    color = models.CharField(                                                          # колір на тарілці й у списку покупок
        'Колір', max_length=7, blank=True, validators=[RegexValidator(r'^#[0-9A-Fa-f]{6}$', 'Колір у форматі #A1B2C3.')],
    )

    def __str__(self):
        """Назва продукту — так він показується в адмінці й списках."""
        return self.name


    def calories_for(self, grams):
        """Скільки ккал у вказаних грамах продукту."""
        return self.calories / 100 * grams

    def grams_for_calories(self, target_calories):
        """Скільки грамів продукту потрібно, щоб отримати вказані ккал."""
        return target_calories / (self.calories / 100)

    def cost_for(self, grams):
        """Скільки коштують вказані грами продукту (ціна штучного продукту — за одну штуку); None, якщо ціна невідома."""
        if self.price is None:
            return None
        if self.unit == 'pc' and self.grams_per_piece:
            return float(self.price) * grams / self.grams_per_piece
        return float(self.price) / 100 * grams

    def pieces_for(self, grams):
        """Скільки штук купити на вказані грами (округлює вгору); для вагового продукту — None."""
        if self.unit == 'pc' and self.grams_per_piece:
            return math.ceil(grams / self.grams_per_piece)
        return None

    def pieces_to_use(self, grams):
        """Скільки приблизно штук покласти на вказані грами (до найближчого цілого); для вагового продукту — None."""
        if self.unit == 'pc' and self.grams_per_piece:
            return round(grams / self.grams_per_piece)
        return None

    def protein_for(self, grams):
        """Скільки грамів білка у вказаних грамах продукту."""
        return float(self.proteins) / 100 * grams

    def grams_for_protein(self, target_proteins):
        """Скільки грамів продукту потрібно, щоб отримати вказаний білок."""
        return target_proteins / (float(self.proteins) / 100)

    def fat_for(self, grams):
        """Скільки грамів жиру у вказаних грамах продукту."""
        return float(self.fats) / 100 * grams

    def grams_for_fat(self, target_fats):
        """Скільки грамів продукту потрібно, щоб отримати вказаний жир."""
        return target_fats / (float(self.fats) / 100)

    def carbs_for(self, grams):
        """Скільки грамів вуглеводів у вказаних грамах продукту."""
        return float(self.carbs) / 100 * grams


class MealPlan(models.Model):
    """Збережений раціон: назва й параметри, з якими його склали."""
    # Чий раціон. Порожньо — раціон зі старих часів без акаунтів: його не бачить ніхто, доки не віддати командою claim_plans.
    # Видалили акаунт — видаляються і його раціони.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name='meal_plans',
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    period = models.CharField(max_length=10)
    days_count = models.PositiveSmallIntegerField()
    calories = models.IntegerField()
    protein_pct = models.IntegerField()
    fat_pct = models.IntegerField()
    carbs_pct = models.IntegerField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Назва раціону — так він показується в адмінці."""
        return self.name

    def total_cost(self):
        """Вартість усього раціону за поточними цінами продуктів; None, якщо хоча б одна ціна невідома."""
        costs = [item.product.cost_for(item.grams) for item in self.items.all()]
        return None if None in costs else sum(costs)


class PlanItem(models.Model):
    """Один продукт в одному прийомі одного дня збереженого раціону."""
    plan = models.ForeignKey(MealPlan, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    day_number = models.PositiveSmallIntegerField()
    day_name = models.CharField(max_length=30)
    meal_number = models.PositiveSmallIntegerField()
    meal_name = models.CharField(max_length=30)
    role = models.CharField(max_length=30, blank=True)                 # лише в раціонах з окремих продуктів
    recipe_name = models.CharField(max_length=200, blank=True, default='')   # лише в раціонах зі страв
    grams = models.FloatField()

    class Meta:
        ordering = ['day_number', 'meal_number', 'id']

    def __str__(self):
        """Короткий опис рядка — для адмінки."""
        return f'{self.day_name}, {self.meal_name}: {self.product}'


class SilpoClient(models.Model):
    """Реєстрація застосунку на сервері входу «Сільпо»: один запис на адресу повернення."""
    redirect_uri = models.URLField(unique=True)
    client_id = models.CharField(max_length=255)

    def __str__(self):
        """Адреса повернення — так запис показується в адмінці."""
        return self.redirect_uri


# Назва прийому → позначка страви, яка каже, чи годиться страва на цей прийом.
# Перекусів у дні буває до двох, і на обидва годяться ті самі страви.
MEAL_FLAGS = {
    'Сніданок': 'for_breakfast',
    'Обід': 'for_lunch',
    'Вечеря': 'for_dinner',
    'Перекус': 'for_snack',
    'Ранковий перекус': 'for_snack',
    'Денний перекус': 'for_snack',
}


class Recipe(models.Model):
    """Страва: назва й прийоми, на які вона годиться; продукти з грамами лежать у RecipeIngredient."""
    name = models.CharField(max_length=200)
    for_breakfast = models.BooleanField('Сніданок', default=True)
    for_lunch = models.BooleanField('Обід', default=True)
    for_dinner = models.BooleanField('Вечеря', default=True)
    # Перекус — невелика ситна страва між основними прийомами; з'являється в раціонах від 2200 ккал
    for_snack = models.BooleanField('Перекус', default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        """Назва страви — так вона показується в адмінці."""
        return self.name

    def fits_meal(self, meal_name):
        """Чи годиться страва на прийом із такою назвою."""
        return getattr(self, MEAL_FLAGS[meal_name])


class RecipeIngredient(models.Model):
    """Один продукт однієї страви: скільки грамів потрібно на порцію."""
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    grams = models.FloatField(validators=[MinValueValidator(0.1)])

    class Meta:
        ordering = ['id']

    def __str__(self):
        """Короткий опис рядка — для адмінки."""
        return f'{self.product}: {self.grams} г'


class BoughtList(models.Model):
    """Галочки «куплено» в одному списку покупок людини, яка увійшла. (У гостя вони лежать у сесії браузера.)"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bought_lists')
    key = models.CharField(max_length=MAX_LIST_KEY)      # «q:<параметри раціону>» або «plan:<id збереженого>»
    product_ids = models.JSONField(default=list)        # id куплених продуктів
    updated_at = models.DateTimeField(auto_now=True)    # за цим забуваємо найстаріші списки

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='unique_bought_list')]

    def __str__(self):
        return f'{self.user}: {self.key}'


class Profile(models.Model):
    """Збережена ціль людини: з нею відкривається раціон, коли в адресі немає своїх параметрів."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    period = models.CharField(max_length=10)
    days = models.PositiveSmallIntegerField()
    calories = models.IntegerField()
    protein_pct = models.IntegerField()
    fat_pct = models.IntegerField()
    carbs_pct = models.IntegerField()

    def __str__(self):
        return f'{self.user}: {self.calories} ккал'

    def goal(self):
        """Ціль у вигляді параметрів адреси (ті самі назви, що в PLAN_KEYS)."""
        return {
            'period': self.period, 'days': self.days, 'calories': self.calories,
            'protein_pct': self.protein_pct, 'fat_pct': self.fat_pct, 'carbs_pct': self.carbs_pct,
        }


class DailyNorm(models.Model):
    """Денна норма калорій з калькулятора — лише результат. Вагу, зріст, вік і стать не зберігаємо."""
    GOAL_CHOICES = [(key, name) for key, name, _, _ in GOALS]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='daily_norm')
    calories = models.PositiveIntegerField()           # ціль під мету
    maintenance = models.PositiveIntegerField()        # скільки треба, щоб вага не мінялась
    goal = models.CharField(max_length=10, choices=GOAL_CHOICES)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user}: {self.calories} ккал ({self.get_goal_display()})'
