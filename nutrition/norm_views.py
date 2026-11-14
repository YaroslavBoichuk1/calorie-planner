"""Калькулятор денної норми калорій і збереження результату в профіль."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import energy
from .forms import NormForm
from .models import DailyNorm
from .views import MACROS, PRESETS


def suggested_preset(goal):
    """Пропорція БЖВ, яку підказуємо під мету: (назва, білок %, жир %, вуглеводи %)."""
    name = next(preset for key, _, _, preset in energy.GOALS if key == goal)
    return next(preset for preset in PRESETS if preset[0] == name)


def norm_plan_url(calories, goal):
    """Посилання на «Раціон» з цією нормою і підказаною пропорцією БЖВ."""
    _, protein, fat, carbs = suggested_preset(goal)
    query = {'calories': calories, 'protein_pct': protein, 'fat_pct': fat, 'carbs_pct': carbs}
    return f"{reverse('planner')}?{urlencode(query)}"


def format_change(value):
    """+285 / −267 / 0 — різниця з нормою підтримки."""
    if value > 0:
        return f'+{value}'
    if value < 0:
        return f'−{-value}'
    return '0'


def norm_view(request):
    """Калькулятор. Дані йдуть POST-ом, а не в адресі: вага й вік не мають осідати в історії браузера й журналах."""
    form = NormForm(request.POST if request.method == 'POST' else None)
    context = {
        'form': form,
        'activity_levels': [(key, name, hint) for key, name, hint, _ in energy.ACTIVITY_LEVELS],
        'goals': [(key, name) for key, name, _, _ in energy.GOALS],
        'sexes': energy.SEXES,
        'saved': DailyNorm.objects.filter(user=request.user).first() if request.user.is_authenticated else None,
    }
    if request.method == 'POST' and form.is_valid():
        result = form.result()
        goal = form.cleaned_data['goal']
        preset_name, *percents = suggested_preset(goal)
        context.update({
            'result': result,
            'goal_name': dict(context['goals'])[goal],
            # справжній мінус (−), а не дефіс; нуль — без знака
            'change_text': format_change(result['target'] - result['maintenance']),
            'preset_name': preset_name,
            'macros': [
                {'name': name, 'pct': pct, 'grams': round(result['target'] * pct / 100 / kcal)}
                for (_, name, kcal), pct in zip(MACROS, percents)
            ],
            'planner_url': norm_plan_url(result['target'], goal),
        })
    return render(request, 'nutrition/norm.html', context)


@login_required
@require_POST
def norm_save_view(request):
    """Зберігає в профіль лише результат. Дані форми перераховуємо на сервері, а не віримо готовим числам."""
    form = NormForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Не вдалося зберегти: перевір дані й порахуй ще раз.')
        return redirect('calorie_norm')
    result = form.result()
    DailyNorm.objects.update_or_create(user=request.user, defaults={
        'calories': result['target'], 'maintenance': result['maintenance'], 'goal': form.cleaned_data['goal'],
    })
    messages.success(request, f"Денну норму {result['target']} ккал збережено в профілі.")
    return redirect('account')
