"""Форми акаунта: реєстрація й вхід за поштою."""
from django import forms
from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils import translation

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
