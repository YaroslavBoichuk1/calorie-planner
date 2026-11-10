from . import silpo
from .views import plan_query


def navigation(request):
    """Дані для меню на кожній сторінці: параметри раціону з адреси й чи підключено «Сільпо»."""
    return {
        'plan_query': plan_query(request.GET),
        'silpo_connected': silpo.is_connected(request.session),
    }
