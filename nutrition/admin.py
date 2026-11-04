from django.contrib import admin
from .models import MealPlan, Product, Recipe, RecipeIngredient
from .planner import recipe_totals

class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'calories', 'proteins', 'fats', 'carbs', 'fdc_id', 'silpo_search']
    list_filter = ['unit']          # бічний фільтр: показати лише штучні / лише вагові
    search_fields = ['name']        # поле пошуку за назвою вгорі

admin.site.register(Product, ProductAdmin)


class MealPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'days_count', 'calories']
    search_fields = ['name']

admin.site.register(MealPlan, MealPlanAdmin)


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 3                       # скільки порожніх рядків показати для нових продуктів


class RecipeAdmin(admin.ModelAdmin):
    list_display = ['name', 'for_breakfast', 'for_lunch', 'for_dinner', 'portion_summary']
    search_fields = ['name']
    inlines = [RecipeIngredientInline]   # продукти редагуємо прямо на сторінці страви

    @admin.display(description='На порцію')
    def portion_summary(self, recipe):
        """Калорії, БЖВ і вартість однієї порції — колонка в списку страв."""
        totals = recipe_totals(recipe)
        summary = (
            f"{totals['calories']:.0f} ккал · Б {totals['protein']:.1f} г · "
            f"Ж {totals['fat']:.1f} г · В {totals['carbs']:.1f} г"
        )
        if totals['cost'] is not None:
            summary += f" · {totals['cost']:.2f} грн"
        return summary

admin.site.register(Recipe, RecipeAdmin)
