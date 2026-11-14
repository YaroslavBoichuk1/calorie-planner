"""Форми: акаунт (реєстрація, вхід, відновлення пароля, видалення) і калькулятор денної норми."""
from django import forms
from django.contrib.auth import authenticate, get_user_model, password_validation
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.utils import translation

from . import energy

MAX_EMAIL_LENGTH = 150     # пошта лягає в поле username, а воно вміщує 150 символів
PASSWORD_HINT = 'Щонайменше 8 символів; не лише цифри й не надто простий.'
EMAIL_ERRORS = {
    'required': 'Вкажи пошту.',
    'invalid': 'Це не схоже на адресу пошти.',
    'max_length': f'Пошта задовга: максимум {MAX_EMAIL_LENGTH} символів.',
}
PASSWORD_ERRORS = {'required': 'Вкажи пароль.'}


def normalize_email(raw):
    """Одна пошта — один акаунт, хоч великими літерами, хоч малими, хоч із пробілами по краях."""
    return raw.strip().lower()


def email_field():
    return forms.EmailField(
        label='Пошта', max_length=MAX_EMAIL_LENGTH, error_messages=EMAIL_ERRORS,
        widget=forms.EmailInput(attrs={'class': 'field-input', 'autocomplete': 'email', 'autofocus': True}),
    )


def password_field(label, autocomplete):
    return forms.CharField(
        label=label, strip=False, error_messages=PASSWORD_ERRORS,
        widget=forms.PasswordInput(attrs={'class': 'field-input', 'autocomplete': autocomplete}),
    )


class RegisterForm(forms.Form):
    email = email_field()
    password1 = password_field('Пароль', 'new-password')
    password2 = password_field('Пароль ще раз', 'new-password')

    def clean_email(self):
        email = normalize_email(self.cleaned_data['email'])
        if get_user_model().objects.filter(username__iexact=email).exists():
            raise ValidationError('Акаунт із цією поштою вже є. Увійди в нього.')
        return email

    def clean(self):
        cleaned = super().clean()
        password1, password2 = cleaned.get('password1'), cleaned.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Паролі не збігаються.')
        elif password1 and cleaned.get('email'):
            email = cleaned['email']
            # Перевірки пароля — ті, що в AUTH_PASSWORD_VALIDATORS; їхні тексти беремо українською
            with translation.override('uk'):
                try:
                    password_validation.validate_password(password1, get_user_model()(username=email, email=email))
                except ValidationError as error:
                    self.add_error('password1', error)
        return cleaned

    def save(self):
        """Створює користувача. Пароль Django зберігає лише як хеш."""
        email = self.cleaned_data['email']
        return get_user_model().objects.create_user(username=email, email=email, password=self.cleaned_data['password1'])


class DeleteAccountForm(forms.Form):
    """Підтвердження видалення акаунта паролем: щоб ніхто не стер чужий акаунт із забутого відкритого браузера."""
    password = password_field('Пароль', 'current-password')

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        password = self.cleaned_data['password']
        if not self.user.check_password(password):
            raise ValidationError('Пароль не підходить.')
        return password


class LoginForm(forms.Form):
    email = email_field()
    password = password_field('Пароль', 'current-password')

    def __init__(self, request, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user = None

    def clean(self):
        cleaned = super().clean()
        email, password = cleaned.get('email'), cleaned.get('password')
        if email and password:
            self.user = authenticate(self.request, username=normalize_email(email), password=password)
            if self.user is None:
                # Не уточнюємо, що саме не так: інакше можна було б дізнатися, які пошти зареєстровані
                raise ValidationError('Неправильна пошта або пароль.')
        return cleaned


class ResetRequestForm(PasswordResetForm):
    """Крок 1 відновлення: пошта, на яку надіслати посилання. Сам лист складає й надсилає Django."""
    email = email_field()

    def clean_email(self):
        return normalize_email(self.cleaned_data['email'])


class SetNewPasswordForm(SetPasswordForm):
    """Крок 2 відновлення: новий пароль двічі (з тими самими вимогами, що й при реєстрації)."""
    new_password1 = password_field('Новий пароль', 'new-password')
    new_password2 = password_field('Новий пароль ще раз', 'new-password')
    error_messages = {'password_mismatch': 'Паролі не збігаються.'}

    def clean(self):
        # Помилки вимог показуємо під першим полем, як на реєстрації (у Django вони під другим)
        self.validate_passwords('new_password1', 'new_password2')
        if not self.has_error('new_password2'):
            self.validate_password_for_user(self.user, 'new_password1')
        return self.cleaned_data


def number_field(name, low, high, unit, step=1, decimal=False):
    """Числове поле калькулятора з українськими повідомленнями про межі."""
    errors = {
        'required': f'Вкажи {name.lower()}.',
        'invalid': 'Потрібне число.',
        'min_value': f'Від {low} до {high} {unit}.',
        'max_value': f'Від {low} до {high} {unit}.',
        'max_digits': 'Задовге число.',
        'max_decimal_places': 'Досить одного знака після коми.',
        'max_whole_digits': 'Задовге число.',
    }
    label = f'{name}, {unit}'
    widget = forms.NumberInput(attrs={
        'class': 'field-input num', 'min': low, 'max': high, 'step': step,
        'inputmode': 'decimal' if decimal else 'numeric',
    })
    if decimal:
        return forms.DecimalField(label=label, min_value=low, max_value=high, max_digits=4, decimal_places=1,
                                  error_messages=errors, widget=widget)
    return forms.IntegerField(label=label, min_value=low, max_value=high, error_messages=errors, widget=widget)


class NormForm(forms.Form):
    """Калькулятор денної норми. Дані потрібні лише для розрахунку й ніде не зберігаються."""
    sex = forms.ChoiceField(label='Стать', choices=energy.SEXES, widget=forms.RadioSelect,
                            error_messages={'required': 'Обери стать: від неї залежить формула.'})
    age = number_field('Вік', energy.MIN_AGE, energy.MAX_AGE, 'років')
    height = number_field('Зріст', energy.MIN_HEIGHT, energy.MAX_HEIGHT, 'см')
    weight = number_field('Вага', energy.MIN_WEIGHT, energy.MAX_WEIGHT, 'кг', step='0.1', decimal=True)
    activity = forms.ChoiceField(label='Активність', choices=[(key, name) for key, name, _, _ in energy.ACTIVITY_LEVELS],
                                 widget=forms.RadioSelect, error_messages={'required': 'Обери рівень активності.'})
    goal = forms.ChoiceField(label='Мета', choices=[(key, name) for key, name, _, _ in energy.GOALS], initial='maintain',
                             widget=forms.RadioSelect, error_messages={'required': 'Обери мету.'})

    def result(self):
        """Розрахунок для перевіреної форми (див. energy.daily_norm)."""
        data = self.cleaned_data
        return energy.daily_norm(data['sex'], data['age'], data['height'], float(data['weight']),
                                 data['activity'], data['goal'])
