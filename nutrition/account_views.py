"""Сторінки акаунта: реєстрація, вхід, вихід, відновлення пароля, профіль, видалення акаунта й приватність."""
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetConfirmView, PasswordResetView
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST

from . import login_limits
from .forms import (
    PASSWORD_HINT, DeleteAccountForm, LoginForm, RegisterForm, ResetRequestForm, SetNewPasswordForm, normalize_email,
)
from .models import DailyNorm
from .norm_views import norm_plan_url
from .views import move_session_bought, safe_next_url

# Коли востаннє мінявся текст сторінки «Приватність». Міняєш текст — онови дату.
PRIVACY_UPDATED = date(2026, 11, 14)


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
                move_session_bought(request)
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
            move_session_bought(request)
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


class ResetRequestView(PasswordResetView):
    """Відновлення, крок 1: людина вводить пошту, і якщо такий акаунт є, Django надсилає лист з одноразовим посиланням.

    Відповідь однакова, є акаунт чи нема, — щоб через цю форму не можна було перевіряти, хто зареєстрований.
    """
    template_name = 'nutrition/password_reset.html'
    form_class = ResetRequestForm
    subject_template_name = 'nutrition/emails/password_reset_subject.txt'
    email_template_name = 'nutrition/emails/password_reset.txt'
    success_url = reverse_lazy('password_reset_sent')
    extra_email_context = {'valid_minutes': settings.PASSWORD_RESET_TIMEOUT // 60}

    def form_valid(self, form):
        email, ip = form.cleaned_data['email'], login_limits.client_ip(self.request)
        if login_limits.reset_blocked(email, ip):
            form.add_error(None, login_limits.RESET_LIMIT_MESSAGE)
            return self.form_invalid(form)
        login_limits.record_reset(email, ip)
        return super().form_valid(form)


def reset_sent_view(request):
    """Відновлення, крок 1½: «перевір пошту»."""
    return render(request, 'nutrition/password_reset_sent.html', {'valid_minutes': settings.PASSWORD_RESET_TIMEOUT // 60})


class ResetConfirmView(PasswordResetConfirmView):
    """Відновлення, крок 2: посилання з листа → новий пароль → одразу вхід.

    Django спершу перекладає ключ з адреси в сесію й перенаправляє на адресу без нього — щоб ключ не витік далі.
    Інші браузери, де був вхід зі старим паролем, після зміни пароля автоматично виходять з акаунта.
    """
    template_name = 'nutrition/password_reset_confirm.html'
    form_class = SetNewPasswordForm
    post_reset_login = True
    success_url = reverse_lazy('planner')

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), 'password_hint': PASSWORD_HINT}

    def form_valid(self, form):
        response = super().form_valid(form)
        login_limits.clear_login_failures(self.user.username)    # старі невдалі спроби входу вже не важливі
        move_session_bought(self.request)
        messages.success(self.request, 'Пароль змінено, вхід виконано.')
        return response


@login_required
def account_view(request):
    """Профіль: денна норма калорій, пошта, кнопки «Вийти» й «Видалити акаунт»."""
    norm = DailyNorm.objects.filter(user=request.user).first()
    return render(request, 'nutrition/account.html', {
        'norm': norm,
        'norm_plan_url': norm_plan_url(norm.calories, norm.goal) if norm else '',
    })


@login_required
def delete_account_view(request):
    """Видалення акаунта разом з усім, що до нього прив'язано: раціонами, ціллю, позначками «куплено».

    Потрібен пароль. Невдалі спроби рахуються так само, як при вході: інакше тут можна було б підбирати пароль.
    """
    user = request.user
    form = DeleteAccountForm(user, request.POST if request.method == 'POST' else None)
    blocked = ''
    if request.method == 'POST':
        email, ip = user.username, login_limits.client_ip(request)
        if login_limits.login_blocked(email, ip):
            form = DeleteAccountForm(user)
            blocked = login_limits.LOCKOUT_MESSAGE
        elif form.is_valid():
            with transaction.atomic():
                user.delete()        # раціони, ціль і позначки видаляються разом із ним (on_delete=CASCADE)
            logout(request)          # сесія очищається повністю, разом із входом у «Сільпо»
            login_limits.clear_login_failures(email)
            messages.success(request, 'Акаунт і всі його дані видалено.')
            return redirect('planner')
        elif request.POST.get('password'):
            login_limits.record_login_failure(email, ip)
    return render(request, 'nutrition/delete_account.html', {
        'form': form,
        'blocked': blocked,
        'plan_count': user.meal_plans.count(),
    })


def privacy_view(request):
    """Сторінка «Приватність»: які дані зберігаємо, навіщо, скільки часу і як їх видалити."""
    return render(request, 'nutrition/privacy.html', {
        'contact_email': settings.CONTACT_EMAIL,
        'updated': PRIVACY_UPDATED,
        'session_days': settings.SESSION_COOKIE_AGE // (24 * 60 * 60),
        'lockout_minutes': login_limits.LOCKOUT_SECONDS // 60,
        'register_window_hours': login_limits.REGISTER_WINDOW_SECONDS // (60 * 60),
    })
