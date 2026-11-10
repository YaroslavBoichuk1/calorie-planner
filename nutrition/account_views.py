"""Сторінки акаунта: реєстрація, вхід, вихід і профіль."""
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import login_limits
from .forms import PASSWORD_HINT, LoginForm, RegisterForm, normalize_email
from .views import safe_next_url


def auth_context(request, form, blocked=''):
    """Спільне для сторінок входу й реєстрації: форма, повідомлення про блокування й куди повернутись після неї."""
    next_url = safe_next_url(request)
    return {
        'form': form,
        'blocked': blocked,
        'next': next_url if next_url != reverse('planner') else '',
        'password_hint': PASSWORD_HINT,
    }


def register_view(request):
    """Реєстрація: пошта й пароль двічі. Після неї людина одразу увійшла."""
    if request.user.is_authenticated:
        return redirect(safe_next_url(request))
    # Не «request.POST or None»: порожній POST — теж надіслана форма, просто без полів
    form = RegisterForm(request.POST if request.method == 'POST' else None)
    blocked = ''
    if request.method == 'POST':
        ip = login_limits.client_ip(request)
        if login_limits.register_blocked(ip):
            form = RegisterForm(initial={'email': request.POST.get('email', '')})
            blocked = login_limits.REGISTER_LIMIT_MESSAGE
        elif form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save()
            except IntegrityError:       # хтось щойно зареєстрував цю ж пошту
                form.add_error('email', 'Акаунт із цією поштою вже є. Увійди в нього.')
            else:
                login_limits.record_registration(ip)
                login(request, user)     # дані сесії (галочки, «Сільпо») лишаються, міняється лише її ключ
                messages.success(request, 'Акаунт створено, вхід виконано.')
                return redirect(safe_next_url(request))
    return render(request, 'nutrition/register.html', auth_context(request, form, blocked))


def login_view(request):
    """Вхід за поштою й паролем, з обмеженням кількості невдалих спроб."""
    if request.user.is_authenticated:
        return redirect(safe_next_url(request))
    form = LoginForm(request, request.POST if request.method == 'POST' else None)
    blocked = ''
    if request.method == 'POST':
        email = normalize_email(request.POST.get('email', ''))
        ip = login_limits.client_ip(request)
        if login_limits.login_blocked(email, ip):
            # Пароль навіть не перевіряємо, інакше блокування нічого б не давало
            form = LoginForm(request, initial={'email': request.POST.get('email', '')})
            blocked = login_limits.LOCKOUT_MESSAGE
        elif form.is_valid():
            login_limits.clear_login_failures(email)
            login(request, form.user)
            return redirect(safe_next_url(request))
        elif form.cleaned_data.get('email') and form.cleaned_data.get('password'):
            login_limits.record_login_failure(email, ip)    # пароль перевірили, і він не підійшов
    return render(request, 'nutrition/login.html', auth_context(request, form, blocked))


@require_POST
def logout_view(request):
    """Вихід. Сесія очищається повністю, тож «Сільпо» в цьому браузері теж відключається."""
    logout(request)
    messages.success(request, 'Вихід з акаунта виконано.')
    return redirect('planner')


@login_required
def account_view(request):
    """Профіль: хто увійшов і кнопка «Вийти»."""
    return render(request, 'nutrition/account.html')
