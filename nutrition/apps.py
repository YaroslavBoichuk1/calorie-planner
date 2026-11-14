from django.apps import AppConfig


class NutritionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'nutrition'

    def ready(self):
        from . import checks  # noqa: F401 — реєструє власні перевірки
