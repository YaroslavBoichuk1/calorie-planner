import re
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import silpo
from .models import MealPlan, Product, Recipe, RecipeIngredient, SilpoClient
from .planner import (
    DAY_PERCENTAGE, NO_RECIPES_MESSAGE, build_recipe_period, day_totals, meal_totals, period_totals,
)


def fake_response(data, status=200, content_type='application/json'):
    """Справжня відповідь requests, але без мережі: тести ніколи не звертаються до «Сільпо»."""
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Type'] = content_type
    response._content = data if isinstance(data, bytes) else json.dumps(data).encode()
    return response


TOKENS = {'access_token': 'access-secret', 'refresh_token': 'refresh-secret', 'expires_in': 3600}


class SilpoLoginTests(TestCase):
    """Вхід: кнопка, повернення із «Сільпо» і вихід."""

    def setUp(self):
        """Підміняє мережу й додає страву, щоб головна сторінка показувала результат."""
        patcher = patch('nutrition.silpo.requests.post')
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.post.side_effect = self.answer

        product = Product.objects.create(name='гречка', price=7, calories=343, proteins=13, fats=3.4, carbs=72)
        recipe = Recipe.objects.create(name='Гречка')
        RecipeIngredient.objects.create(recipe=recipe, product=product, grams=100)

    def answer(self, url, **kwargs):
        """Відповідає замість сервера «Сільпо»."""
        if url == silpo.REGISTER_URL:
            return fake_response({'client_id': 'client-1'})
        return fake_response(TOKENS)

    def requests_to(self, url):
        """Усі запити, які застосунок надіслав на цю адресу."""
        return [call for call in self.post.call_args_list if call.args[0] == url]

    def start_login(self, next_url='/?period=week'):
        """Натискає кнопку входу → параметри адреси, на яку застосунок перекидає людину."""
        response = self.client.post(reverse('silpo_login'), {'next': next_url})
        self.assertEqual(response.status_code, 302)
        location = urlparse(response['Location'])
        self.assertEqual(f'{location.scheme}://{location.netloc}{location.path}', silpo.AUTHORIZE_URL)
        return {key: values[0] for key, values in parse_qs(location.query).items()}

    def test_login_sends_person_to_silpo_with_pkce(self):
        params = self.start_login()
        verifier = self.client.session[silpo.LOGIN_KEY]['verifier']
        self.assertEqual(params['response_type'], 'code')
        self.assertEqual(params['client_id'], 'client-1')
        self.assertEqual(params['redirect_uri'], 'http://testserver/silpo/callback/')
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertEqual(params['code_challenge'], silpo.code_challenge(verifier))
        self.assertEqual(params['resource'], silpo.MCP_URL)
        self.assertNotIn(verifier, params.values())

    def test_app_registers_once_per_redirect_address(self):
        self.start_login()
        self.start_login()
        self.assertEqual(len(self.requests_to(silpo.REGISTER_URL)), 1)
        self.assertEqual(SilpoClient.objects.count(), 1)

    def test_login_button_needs_post(self):
        self.assertEqual(self.client.get(reverse('silpo_login')).status_code, 405)
        self.assertEqual(self.client.get(reverse('silpo_logout')).status_code, 405)

    def test_callback_keeps_tokens_on_server_and_returns_to_page(self):
        params = self.start_login()
        verifier = self.client.session[silpo.LOGIN_KEY]['verifier']
        response = self.client.get(reverse('silpo_callback'), {
            'code': 'code-1', 'state': params['state'], 'iss': silpo.ISSUER,
        })
        self.assertRedirects(response, '/?period=week', fetch_redirect_response=False)

        sent = self.requests_to(silpo.TOKEN_URL)[0].kwargs['data']
        self.assertEqual(sent['code'], 'code-1')
        self.assertEqual(sent['code_verifier'], verifier)

        self.assertEqual(self.client.session[silpo.TOKENS_KEY]['access_token'], 'access-secret')
        self.assertNotIn(silpo.LOGIN_KEY, self.client.session)
        for cookie in response.cookies.values():
            self.assertNotIn('secret', cookie.value)

        page = self.client.get('/').content.decode()
        self.assertIn('«Сільпо» підключено', page)
        self.assertNotIn('access-secret', page)

    def test_wrong_state_is_rejected_without_asking_for_tokens(self):
        for state in ['wrong', 'неправильний', '']:
            with self.subTest(state=state):
                self.start_login()
                response = self.client.get(reverse('silpo_callback'), {'code': 'code-1', 'state': state})
                self.assertRedirects(response, '/', fetch_redirect_response=False)
                self.assertNotIn(silpo.TOKENS_KEY, self.client.session)
        self.assertEqual(self.requests_to(silpo.TOKEN_URL), [])

    def test_wrong_issuer_is_rejected(self):
        params = self.start_login()
        self.client.get(reverse('silpo_callback'), {
            'code': 'code-1', 'state': params['state'], 'iss': 'https://evil.example',
        })
        self.assertNotIn(silpo.TOKENS_KEY, self.client.session)
        self.assertEqual(self.requests_to(silpo.TOKEN_URL), [])

    def test_state_works_only_once(self):
        params = self.start_login()
        query = {'code': 'code-1', 'state': params['state']}
        self.client.get(reverse('silpo_callback'), query)
        self.client.get(reverse('silpo_callback'), query)
        self.assertEqual(len(self.requests_to(silpo.TOKEN_URL)), 1)

    def test_callback_without_started_login_is_rejected(self):
        response = self.client.get(reverse('silpo_callback'), {'code': 'code-1', 'state': 'x'})
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(self.requests_to(silpo.TOKEN_URL), [])

    def test_cancelled_login_shows_message(self):
        params = self.start_login()
        response = self.client.get(reverse('silpo_callback'), {'error': 'access_denied', 'state': params['state']}, follow=True)
        self.assertContains(response, silpo.LOGIN_CANCELLED_MESSAGE)
        self.assertNotIn(silpo.TOKENS_KEY, self.client.session)

    def test_rejected_code_shows_message(self):
        params = self.start_login()
        self.post.side_effect = lambda url, **kwargs: fake_response({'error': 'invalid_grant'}, status=400)
        response = self.client.get(reverse('silpo_callback'), {'code': 'bad', 'state': params['state']}, follow=True)
        self.assertContains(response, silpo.LOGIN_FAILED_MESSAGE)

    def test_foreign_next_address_is_ignored(self):
        for next_url in ['https://evil.example/', '//evil.example/', 'javascript:alert(1)']:
            with self.subTest(next_url=next_url):
                params = self.start_login(next_url)
                response = self.client.get(reverse('silpo_callback'), {'code': 'code-1', 'state': params['state']})
                self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_silpo_down_shows_message_instead_of_error_page(self):
        self.post.side_effect = requests.ConnectionError()
        response = self.client.post(reverse('silpo_login'), {'next': '/'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, silpo.UNAVAILABLE_MESSAGE)

    def test_logout_forgets_tokens_even_if_silpo_is_down(self):
        params = self.start_login()
        self.client.get(reverse('silpo_callback'), {'code': 'code-1', 'state': params['state']})
        self.post.side_effect = requests.ConnectionError()
        response = self.client.post(reverse('silpo_logout'), {'next': '/?period=week'})
        self.assertRedirects(response, '/?period=week', fetch_redirect_response=False)
        self.assertNotIn(silpo.TOKENS_KEY, self.client.session)
        self.assertContains(self.client.get('/'), 'Увійти через «Сільпо»')


class SilpoTokenTests(SimpleTestCase):
    """Оновлення токена."""

    def session(self, expires_in):
        """Сесія з токенами, що закінчуються через expires_in секунд."""
        session = {}
        silpo.save_tokens(session, {**TOKENS, 'expires_in': expires_in}, 'client-1')
        return session

    @patch('nutrition.silpo.requests.post')
    def test_fresh_token_is_used_without_network(self, post):
        self.assertEqual(silpo.access_token(self.session(3600)), 'access-secret')
        post.assert_not_called()

    @patch('nutrition.silpo.requests.post')
    def test_expiring_token_is_refreshed(self, post):
        post.return_value = fake_response({'access_token': 'new-access', 'expires_in': 3600})
        session = self.session(10)
        self.assertEqual(silpo.access_token(session), 'new-access')
        self.assertEqual(post.call_args.kwargs['data']['grant_type'], 'refresh_token')
        self.assertEqual(session[silpo.TOKENS_KEY]['refresh_token'], 'refresh-secret')

    @patch('nutrition.silpo.requests.post')
    def test_rejected_refresh_asks_to_log_in_again(self, post):
        post.return_value = fake_response({'error': 'invalid_grant'}, status=400)
        session = self.session(10)
        with self.assertRaises(silpo.SilpoLoginRequired):
            silpo.access_token(session)
        self.assertNotIn(silpo.TOKENS_KEY, session)

    @patch('nutrition.silpo.requests.post')
    def test_network_failure_keeps_login(self, post):
        post.side_effect = requests.ConnectionError()
        session = self.session(10)
        with self.assertRaises(silpo.SilpoError) as caught:
            silpo.access_token(session)
        self.assertNotIsInstance(caught.exception, silpo.SilpoLoginRequired)
        self.assertIn(silpo.TOKENS_KEY, session)

    def test_no_login_asks_to_log_in(self):
        with self.assertRaises(silpo.SilpoLoginRequired):
            silpo.access_token({})


class RecipeDataMixin:
    """Страви для тестів двигуна: білкові, жирні й вуглеводні, щоб було з чого вибирати."""

    def make_recipes(self):
        """Шість страв по 400 ккал із різним БЖВ; одна лише на сніданок."""
        protein = Product.objects.create(name='білок', calories=400, proteins=100, fats=0, carbs=0)
        fat = Product.objects.create(name='жир', calories=900, proteins=0, fats=100, carbs=0)
        carb = Product.objects.create(name='вуглеводи', calories=400, proteins=0, fats=0, carbs=100)
        self.products = {'protein': protein, 'fat': fat, 'carb': carb}
        mixes = {
            'Страва А': (40, 15, 50), 'Страва Б': (30, 20, 40), 'Страва В': (20, 25, 30),
            'Страва Г': (50, 10, 40), 'Страва Д': (25, 15, 60),
        }
        for name, (protein_grams, fat_grams, carb_grams) in mixes.items():
            self.make_recipe(name, protein_grams, fat_grams, carb_grams)
        self.make_recipe('Сніданкова', 30, 15, 40, for_lunch=False, for_dinner=False)

    def make_recipe(self, name, protein_grams, fat_grams, carb_grams, **flags):
        """Страва з трьох продуктів із вказаними грамами."""
        recipe = Recipe.objects.create(name=name, **flags)
        RecipeIngredient.objects.create(recipe=recipe, product=self.products['protein'], grams=protein_grams)
        RecipeIngredient.objects.create(recipe=recipe, product=self.products['fat'], grams=fat_grams)
        RecipeIngredient.objects.create(recipe=recipe, product=self.products['carb'], grams=carb_grams)
        return recipe

    def build(self, days_count=7, calories=2000):
        """Раціон на days_count днів із ціллю 30/30/40."""
        recipes = Recipe.objects.prefetch_related('ingredients__product')
        return build_recipe_period(recipes, days_count, calories, 0.3, 0.3, 0.4)


def menu(day_info):
    """Назви страв одного дня."""
    return [meal['recipe_name'] for meal in day_info['day']]


class RecipeEngineTests(RecipeDataMixin, TestCase):
    """Правила підбору страв."""

    def setUp(self):
        """Шість страв у базі."""
        self.make_recipes()

    def test_every_meal_matches_its_calories(self):
        for day_info in self.build(days_count=3):
            self.assertEqual([meal['name'] for meal in day_info['day']], list(DAY_PERCENTAGE))
            for meal in day_info['day']:
                self.assertAlmostEqual(meal_totals(meal['meal'])['calories'], meal['calories'])
            self.assertAlmostEqual(day_totals(day_info['day'])['calories'], 2000)

    def test_portions_scale_to_calories(self):
        small = self.build(days_count=1, calories=1000)[0]['day']
        large = self.build(days_count=1, calories=2000)[0]['day']
        self.assertEqual(menu({'day': small}), menu({'day': large}))
        self.assertAlmostEqual(large[0]['meal'][0]['grams'], small[0]['meal'][0]['grams'] * 2)

    def test_no_dish_repeats_in_a_day_or_from_yesterday(self):
        days = self.build(days_count=7)
        for index, day_info in enumerate(days):
            names = menu(day_info)
            self.assertEqual(len(set(names)), 3)
            if index:
                self.assertFalse(set(names) & set(menu(days[index - 1])))

    def test_breakfast_only_dish_stays_at_breakfast(self):
        for day_info in self.build(days_count=14):
            self.assertNotIn('Сніданкова', menu(day_info)[1:])

    def test_same_request_gives_same_plan(self):
        self.assertEqual([menu(day) for day in self.build()], [menu(day) for day in self.build()])

    def test_week_uses_many_dishes(self):
        names = [name for day in self.build(days_count=7) for name in menu(day)]
        self.assertEqual(len(set(names)), 6)

    def test_one_dish_still_builds_a_plan(self):
        Recipe.objects.exclude(name='Страва А').delete()
        self.assertEqual(menu(self.build(days_count=1)[0]), ['Страва А'] * 3)

    def test_dish_without_calories_is_skipped(self):
        Recipe.objects.create(name='Порожня')
        for day_info in self.build(days_count=7):
            self.assertNotIn('Порожня', menu(day_info))

    def test_no_dishes_gives_clear_message(self):
        Recipe.objects.all().delete()
        with self.assertRaisesMessage(ValueError, NO_RECIPES_MESSAGE):
            self.build()

    def test_meal_without_dish_gives_clear_message(self):
        Recipe.objects.update(for_dinner=False)
        with self.assertRaisesMessage(ValueError, 'Немає страви на прийом «Вечеря»'):
            self.build()


class SwapTests(RecipeDataMixin, TestCase):
    """Заміна страви в одному прийомі."""

    def setUp(self):
        """Шість страв у базі."""
        self.make_recipes()

    def recipe(self, name):
        """Страва за назвою."""
        return Recipe.objects.get(name=name)

    def page(self, **params):
        """Головна сторінка з параметрами → HTML."""
        return self.client.get('/', params).content.decode()

    def first_dinner(self):
        """Яка страва стоїть на вечерю першого дня тижня без замін."""
        from nutrition.views import build_plan_days, read_choices
        return build_plan_days(read_choices({'period': 'week'}))[0]['day'][2]['recipe_name']

    def test_parse_and_format_swaps(self):
        from nutrition.views import format_swaps, parse_swaps
        self.assertEqual(parse_swaps('0.1.12,2.0.5'), {(0, 1): 12, (2, 0): 5})
        self.assertEqual(parse_swaps('bad,1.2,1.2.x,-1.0.3,0.0.²,' + '9' * 20 + '.0.1'), {})
        self.assertEqual(parse_swaps('0.1.3,0.1.4'), {(0, 1): 4})
        self.assertEqual(parse_swaps(None), {})
        self.assertEqual(format_swaps({(2, 0): 5, (0, 1): 12}), '0.1.12,2.0.5')

    def test_swap_replaces_dish_and_keeps_meal_calories(self):
        other = 'Страва А' if self.first_dinner() != 'Страва А' else 'Страва Б'
        from nutrition.views import build_plan_days, read_choices
        days = build_plan_days(read_choices({'period': 'week', 'swaps': f'0.2.{self.recipe(other).pk}'}))
        dinner = days[0]['day'][2]
        self.assertEqual(dinner['recipe_name'], other)
        self.assertAlmostEqual(meal_totals(dinner['meal'])['calories'], dinner['calories'])

    def test_swap_that_does_not_fit_the_meal_is_ignored(self):
        from nutrition.views import build_plan_days, read_choices
        breakfast_only = self.recipe('Сніданкова').pk
        days = build_plan_days(read_choices({'period': 'week', 'swaps': f'0.2.{breakfast_only},0.9.{breakfast_only},99.0.1,0.0.999999'}))
        self.assertEqual(days[0]['day'][2]['recipe_name'], self.first_dinner())

    def test_page_offers_only_fitting_dishes_and_links_keep_the_plan(self):
        html = self.page(period='week', meal=2)
        self.assertIn('Замінити страву', html)
        swap_links = re.findall(r'class="swap-option" href="([^"]+)"', html)
        self.assertTrue(swap_links)
        self.assertFalse(any(f'.{self.recipe("Сніданкова").pk}' in link.split('swaps=')[1].split('&amp;')[0][-6:] for link in swap_links if 'swaps=0.2.' in link))
        self.assertTrue(all('period=week' in link for link in swap_links))

    def test_swap_shows_on_page_with_reset_link_and_reaches_shopping_and_save(self):
        other = 'Страва А' if self.first_dinner() != 'Страва А' else 'Страва Б'
        swaps = f'0.2.{self.recipe(other).pk}'
        html = self.page(period='week', swaps=swaps, meal=2)
        self.assertIn(f'<h2 class="panel-title">{other}</h2>', html)
        self.assertIn('Повернути страву, яку підібрав застосунок', html)
        self.assertIn(f'swaps={swaps.replace(".", ".")}', html)

        self.client.post(reverse('plan_save'), {'query': f'period=week&swaps={swaps}', 'name': 'Із заміною'})
        plan = MealPlan.objects.get(name='Із заміною')
        self.assertIn(other, set(plan.items.filter(day_number=0, meal_number=2).values_list('recipe_name', flat=True)))

    def test_dishes_already_in_the_day_are_not_offered(self):
        html = self.page(period='week', meal=0)
        dishes_today = re.findall(r'<span class="meal-dish">([^<]+)</span>', html)[:3]
        offered = set(re.findall(r'<span class="swap-name">([^<]+)</span>', html))
        self.assertTrue(offered)
        self.assertFalse(offered & set(dishes_today))

    def test_phone_cards_fold_out_with_composition(self):
        html = self.page(period='week', meal=1)
        self.assertEqual(html.count('<details class="meal-fold"'), 3)
        self.assertEqual(html.count('<details class="meal-fold" open>'), 1)


class NoPricesTests(RecipeDataMixin, TestCase):
    """Стандартний варіант: продукти без цін."""

    def setUp(self):
        """Страви, у продуктів яких цін немає."""
        self.make_recipes()

    def test_totals_have_no_cost(self):
        days = self.build(days_count=2)
        self.assertIsNone(meal_totals(days[0]['day'][0]['meal'])['cost'])
        self.assertIsNone(day_totals(days[0]['day'])['cost'])
        self.assertIsNone(period_totals(days)['cost'])
        self.assertAlmostEqual(period_totals(days)['calories'], 4000)

    def test_one_missing_price_hides_the_whole_cost(self):
        self.products['protein'].price = 10
        self.products['protein'].save()
        days = self.build(days_count=1)
        self.assertIsNone(day_totals(days[0]['day'])['cost'])

    def test_all_prices_known_shows_cost(self):
        for product in self.products.values():
            product.price = 10
            product.save()
        cost = day_totals(self.build(days_count=1)[0]['day'])['cost']
        self.assertGreater(cost, 0)
        self.assertContains(self.client.get('/'), 'Вартість дня')

    def test_pages_work_without_prices_and_show_no_cost(self):
        for url in ['/', '/?period=week', '/?period=custom&days=31']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'Вартість')
                self.assertNotContains(response, 'грн')
                self.assertContains(response, 'Список покупок')

    def test_saved_plan_without_prices(self):
        response = self.client.post(reverse('plan_save'), {'query': 'period=week', 'name': 'Без цін'})
        plan = MealPlan.objects.get(name='Без цін')
        self.assertIsNone(plan.total_cost())
        self.assertRedirects(response, reverse('plan_detail', args=[plan.pk]))
        for url in [reverse('plan_detail', args=[plan.pk]), reverse('plan_list')]:
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'грн')

    def test_site_credits_the_data_source(self):
        self.assertContains(self.client.get('/'), 'USDA FoodData Central')

    def test_admin_list_works_without_prices(self):
        user = get_user_model().objects.create_superuser('admin-test', password=None)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/admin/nutrition/recipe/').status_code, 200)
        self.assertEqual(self.client.get('/admin/nutrition/product/').status_code, 200)


def silpo_item(name, price, ratio=None, weighted=False, step=1, stock=10, available=True, slug='slug'):
    """Товар у форматі, який повертає пошук «Сільпо»."""
    return {
        'id': f'id-{slug}', 'companyId': 'company-1', 'branchId': 'branch-1',
        'name': name, 'slug': slug, 'price': price, 'displayRatio': ratio,
        'weighted': weighted, 'step': step, 'stock': stock, 'available': available,
    }


class SilpoOfferTests(SimpleTestCase):
    """Вибір товару й рахунок упаковок."""

    def setUp(self):
        """Два продукти: ваговий і штучний."""
        self.grechka = Product(name='гречка', unit='g')
        self.egg = Product(name='яйце', unit='pc', grams_per_piece=60)

    def test_parse_pack(self):
        cases = {
            '800г': (800, 'g'), '1кг': (1000, 'g'), '0,5л': (500, 'g'), '0.85л': (850, 'g'), '850мл': (850, 'g'),
            '10шт': (10, 'pc'), '5*80г': (400, 'g'), '500 г': (500, 'g'), '100Г': (100, 'g'),
            None: None, '': None, 'уп.': None, '2 уп 500г': None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(silpo.parse_pack(text), expected)

    def test_name_must_start_with_first_word_and_contain_the_rest(self):
        self.assertTrue(silpo.name_matches('Рис Trapeza довгозернистий', 'рис'))
        self.assertTrue(silpo.name_matches('Крупа Хатинка гречана ядриця', 'крупа гречана'))
        self.assertFalse(silpo.name_matches('Рисове борошно', 'рис'))
        self.assertFalse(silpo.name_matches('Салат з курячим філе', 'куряче філе'))
        self.assertFalse(silpo.name_matches('Корм для собак яловичина', 'яловичина'))
        self.assertFalse(silpo.name_matches('Нектар Вогняний томат', 'томат'))
        self.assertFalse(silpo.name_matches('', 'рис'))
        self.assertFalse(silpo.name_matches('Рис', ''))

    def test_pack_is_rounded_up_to_whole_packs(self):
        offer = silpo.make_offer(silpo_item('Крупа', 99, '800г'), self.grechka, 900)
        self.assertEqual(offer['quantity'], '2 шт')
        self.assertEqual(offer['cost'], 198)
        self.assertAlmostEqual(offer['used_cost'], 111.375)

    def test_weighted_item_uses_step_and_price_per_kilogram(self):
        offer = silpo.make_offer(silpo_item('Філе', 189, '100г', weighted=True, step=0.5), self.grechka, 212)
        self.assertEqual(offer['quantity'], '0.5 кг')
        self.assertAlmostEqual(offer['cost'], 94.5)
        self.assertAlmostEqual(offer['used_cost'], 40.068)

    def test_eggs_are_counted_in_pieces(self):
        offer = silpo.make_offer(silpo_item('Яйця', 100, '10шт'), self.egg, 660)
        self.assertEqual(offer['quantity'], '2 шт')
        self.assertEqual(offer['cost'], 200)
        self.assertAlmostEqual(offer['used_cost'], 110)

    def test_exact_amount_does_not_buy_extra_pack(self):
        self.assertEqual(silpo.make_offer(silpo_item('Крупа', 99, '800г'), self.grechka, 800)['quantity'], '1 шт')

    def test_unsuitable_items_are_skipped(self):
        cases = {
            'unavailable': silpo_item('Крупа', 99, '800г', available=False),
            'no stock for the amount': silpo_item('Крупа', 99, '800г', stock=1),
            'unknown pack': silpo_item('Крупа', 99, 'уп.'),
            'pieces for a weighed product': silpo_item('Крупа', 99, '10шт'),
        }
        for label, item in cases.items():
            with self.subTest(label):
                self.assertIsNone(silpo.make_offer(item, self.grechka, 900))
        self.assertIsNone(silpo.make_offer(silpo_item('Яйця', 99, '800г'), self.egg, 60))
        self.assertIsNone(silpo.make_offer(silpo_item('Яйця', 99, '100г', weighted=True, step=0.5), self.egg, 60))

    def test_offers_go_from_cheapest_per_kilogram_not_per_pack(self):
        items = [
            silpo_item('Крупа гречана дрібна', 90, '400г', slug='small'),
            silpo_item('Крупа гречана велика', 144, '1000г', slug='big'),
            silpo_item('Печиво гречане', 10, '500г', slug='other'),
        ]
        offers = silpo.offers_for(items, self.grechka, 'крупа гречана', 100)
        self.assertEqual([offer['id'] for offer in offers], ['id-big', 'id-small'])

    def test_no_match_gives_no_offers(self):
        self.assertEqual(silpo.offers_for([silpo_item('Печиво', 10, '500г')], self.grechka, 'крупа гречана', 100), [])

    def test_choose_offer(self):
        offers = [{'id': 'cheap'}, {'id': 'dear'}]
        self.assertEqual(silpo.choose_offer(offers, None)['id'], 'cheap')
        self.assertEqual(silpo.choose_offer(offers, 'dear')['id'], 'dear')
        self.assertEqual(silpo.choose_offer(offers, 'gone')['id'], 'cheap')
        self.assertIsNone(silpo.choose_offer(offers, silpo.SKIP))
        self.assertIsNone(silpo.choose_offer([], 'dear'))

    def test_same_moment_understands_both_time_formats(self):
        self.assertTrue(silpo.same_moment('2026-11-04T18:30:00Z', '2026-11-04T18:30:00+00:00'))
        self.assertTrue(silpo.same_moment('2026-11-04T20:30:00+02:00', '2026-11-04T18:30:00+00:00'))
        self.assertFalse(silpo.same_moment('2026-11-04T18:30:00Z', '2026-11-04T19:00:00Z'))
        self.assertFalse(silpo.same_moment(None, '2026-11-04T19:00:00Z'))
        self.assertFalse(silpo.same_moment('щось', 'щось'))

    def test_slot_label_shows_kyiv_time(self):
        label = silpo.slot_label({'start': '2026-11-04T18:30:00+00:00', 'end': '2026-11-04T20:00:00+00:00'})
        self.assertEqual(label, '04.11, 20:30–22:00')


class FakeConnection:
    """Підміна розмови із «Сільпо»: записує виклики й дає заготовлені відповіді."""

    def __init__(self, exists=True, delivery='DeliveryHome', slots=None, products=None, lines=None, timeslot=None,
                 drop_added=False, extra=None, calculation=None, ignore_remove=False):
        """Заготовлені дані: кошик і його рядки, спосіб доставки, слоти й товари за пошуками."""
        self.calls = []
        self.exists = exists
        self.delivery = delivery
        self.slots = [{'start': 'start-1', 'end': 'end-1', 'available': True, 'minOrderCost': 699}] if slots is None else slots
        self.products = products or {}
        self.lines = lines or []
        self.timeslot = timeslot
        self.drop_added = drop_added   # «Сільпо» може мовчки не додати товар
        self.ignore_remove = ignore_remove   # …або не прибрати
        self.extra = extra or {}
        self.calculation = calculation or {}

    def tool_names(self):
        """Назви інструментів у порядку викликів."""
        return [call[0] for call in self.calls]

    def call_tool(self, name, **arguments):
        """Відповідає як сервер і запам'ятовує назву інструмента. Незнайомий інструмент — помилка тесту."""
        self.calls.append((name, arguments))
        if name == 'silpo_get_my_shopping_cart':
            return {'success': True, 'exists': self.exists, 'shoppingCartId': 'cart-1' if self.exists else None}
        if name == 'silpo_get_shopping_cart_by_id':
            return {
                'success': True,
                **self.extra,
                'cart': {
                    'deliveryType': self.delivery,
                    'timeslot': self.timeslot,
                    'address': {'addressType': 'flat', 'phone': 'secret-phone', 'latitude': '50.4'},
                    'calculation': self.calculation,
                    'shipments': [{'id': 's1', 'branchId': 'branch-1', 'companyId': 'company-1', 'products': self.lines}],
                },
            }
        if name == 'silpo_add_cart_products':
            if not self.drop_added:
                for product in arguments['products']:
                    self.lines.append({'id': f"line-{product['productId']}", 'productId': product['productId'], 'quantity': product['quantity']})
            return {'success': True}
        if name == 'silpo_update_cart_products':
            for change in arguments['products']:
                for line in self.lines:
                    if line['id'] == change['id']:
                        line['quantity'] = change['quantity']
            return {'success': True}
        if name == 'silpo_remove_cart_products':
            if not self.ignore_remove:
                removed = {product['id'] for product in arguments['products']}
                self.lines[:] = [line for line in self.lines if line['id'] not in removed]
            return {'success': True}
        if name == 'silpo_update_shopping_cart':
            self.timeslot = arguments['timeslot']
            return {'success': True}
        if name == 'silpo_get_time_slots':
            return {'success': True, 'slots': self.slots}
        if name == 'silpo_find_products_batch':
            return {'success': True, 'queries': [
                {'query': query, 'products': self.products.get(query, [])} for query in arguments['products']
            ]}
        raise AssertionError(f'Несподіваний інструмент: {name}')


class SilpoPricesTests(TestCase):
    """Ціни для списку покупок."""

    def setUp(self):
        """Два продукти з пошуковими запитами й одна страва."""
        self.grechka = Product.objects.create(
            name='гречка', price=7, calories=343, proteins=13, fats=3.4, carbs=72, silpo_search='крупа гречана',
        )
        self.egg = Product.objects.create(
            name='яйце', unit='pc', grams_per_piece=60, price=3, calories=155, proteins=13, fats=11, carbs=1.1,
            silpo_search='яйця курячі',
        )
        recipe = Recipe.objects.create(name='Гречка з яйцем')
        RecipeIngredient.objects.create(recipe=recipe, product=self.grechka, grams=80)
        RecipeIngredient.objects.create(recipe=recipe, product=self.egg, grams=120)

    def connection(self):
        """Розмова, де знайдеться лише гречка."""
        return FakeConnection(products={'крупа гречана': [silpo_item('Крупа гречана', 99, '800г')]})

    def test_totals_and_missing_products(self):
        prices = silpo.find_prices(self.connection(), {self.grechka: 900, self.egg: 120})
        self.assertEqual(prices['basket_total'], 198)
        self.assertAlmostEqual(prices['used_total'], 111.375)
        self.assertEqual(prices['missing'], 1)
        self.assertEqual(prices['min_order'], 699)
        self.assertIsNone(prices['rows'][1]['offer'])

    def test_prices_only_use_read_tools(self):
        connection = self.connection()
        silpo.find_prices(connection, {self.grechka: 900})
        self.assertEqual([call[0] for call in connection.calls], [
            'silpo_get_my_shopping_cart', 'silpo_get_shopping_cart_by_id',
            'silpo_get_time_slots', 'silpo_find_products_batch',
        ])

    def test_search_uses_store_and_first_free_slot(self):
        slots = [{'start': 'busy', 'end': 'busy', 'available': False}, {'start': 'free', 'end': 'free-end', 'available': True}]
        connection = FakeConnection(slots=slots)
        silpo.find_prices(connection, {self.grechka: 100})
        search = connection.calls[-1][1]
        self.assertEqual(search['branchId'], 'branch-1')
        self.assertEqual(search['deliveryType'], 'DeliveryHome')
        self.assertEqual((search['timeslotStart'], search['timeslotEnd']), ('free', 'free-end'))
        self.assertEqual(search['products'], ['крупа гречана'])

    def test_empty_grams_are_not_searched(self):
        connection = self.connection()
        prices = silpo.find_prices(connection, {self.grechka: 0})
        self.assertEqual(prices['rows'], [])
        self.assertNotIn('silpo_find_products_batch', [call[0] for call in connection.calls])

    def test_no_cart_does_not_create_one(self):
        connection = FakeConnection(exists=False)
        with self.assertRaisesMessage(silpo.SilpoError, silpo.NO_STORE_MESSAGE):
            silpo.find_prices(connection, {self.grechka: 100})
        self.assertEqual([call[0] for call in connection.calls], ['silpo_get_my_shopping_cart'])

    def test_no_free_slots_shows_message(self):
        with self.assertRaisesMessage(silpo.SilpoError, silpo.NO_SLOTS_MESSAGE):
            silpo.find_prices(FakeConnection(slots=[{'start': 'a', 'end': 'b', 'available': False}]), {self.grechka: 100})

    def connect_browser(self):
        """Позначає цей браузер як підключений до «Сільпо»."""
        session = self.client.session
        silpo.save_tokens(session, TOKENS, 'client-1')
        session.save()

    def test_page_needs_login(self):
        response = self.client.get(reverse('silpo_prices'), {'period': 'week'})
        self.assertRedirects(response, '/?period=week', fetch_redirect_response=False)

    def test_page_shows_prices_and_never_the_token(self):
        self.connect_browser()
        with patch('nutrition.views.silpo.McpConnection', return_value=self.connection()):
            response = self.client.get(reverse('silpo_prices'), {'calories': '1000'})
        self.assertContains(response, 'Крупа гречана')
        self.assertContains(response, 'Не знайдено в цьому магазині')
        self.assertContains(response, 'href="/?calories=1000"')
        self.assertNotContains(response, 'access-secret')

    def test_page_handles_bad_targets_and_silpo_errors(self):
        self.connect_browser()
        response = self.client.get(reverse('silpo_prices'), {'calories': '-5'}, follow=True)
        self.assertContains(response, 'Калорії на день')
        with patch('nutrition.views.silpo.McpConnection', side_effect=silpo.SilpoError(silpo.UNAVAILABLE_MESSAGE)):
            response = self.client.get(reverse('silpo_prices'), follow=True)
        self.assertContains(response, silpo.UNAVAILABLE_MESSAGE)

    def test_planner_shows_price_button_only_when_connected(self):
        self.assertNotContains(self.client.get('/'), 'Показати ціни')
        self.connect_browser()
        self.assertContains(self.client.get('/?period=week'), f"{reverse('silpo_prices')}?period=week")

    def test_planner_page_never_calls_silpo(self):
        self.connect_browser()
        with patch('nutrition.silpo.requests.post') as post:
            self.client.get('/')
        post.assert_not_called()


SLOT_ONE = {'start': '2026-11-05T08:00:00+00:00', 'end': '2026-11-05T10:00:00+00:00', 'available': True, 'minOrderCost': 699}
SLOT_TWO = {'start': '2026-11-05T12:00:00+00:00', 'end': '2026-11-05T14:00:00+00:00', 'available': True, 'minOrderCost': 699}
SLOT_BUSY = {'start': '2026-11-05T06:00:00+00:00', 'end': '2026-11-05T08:00:00+00:00', 'available': False}


class SilpoCartTests(TestCase):
    """Вибір товарів, час доставки й додавання в кошик."""

    def setUp(self):
        """Гречка й куряче філе та страва, яка їх використовує."""
        self.grechka = Product.objects.create(
            name='гречка', calories=343, proteins=13, fats=3.4, carbs=71.5, silpo_search='крупа гречана',
        )
        self.chicken = Product.objects.create(
            name='філе', calories=120, proteins=22.5, fats=2.6, carbs=0, silpo_search='куряче філе',
        )
        recipe = Recipe.objects.create(name='Гречка з куркою')
        RecipeIngredient.objects.create(recipe=recipe, product=self.grechka, grams=80)
        RecipeIngredient.objects.create(recipe=recipe, product=self.chicken, grams=150)
        self.search = {
            'крупа гречана': [
                silpo_item('Крупа гречана дорога', 200, '1кг', slug='dear'),
                silpo_item('Крупа гречана дешева', 100, '1кг', slug='cheap'),
            ],
            'куряче філе': [silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, slug='chicken')],
        }

    def connection(self, **kwargs):
        """Розмова із заготовленими товарами й двома вільними слотами."""
        kwargs.setdefault('slots', [SLOT_BUSY, SLOT_ONE, SLOT_TWO])
        kwargs.setdefault('timeslot', {'start': SLOT_ONE['start'], 'end': SLOT_ONE['end']})
        return FakeConnection(products=self.search, **kwargs)

    def shopping(self):
        """Список покупок: 500 г гречки й 300 г філе."""
        return {self.grechka: 500, self.chicken: 300}

    def test_cheapest_item_is_chosen_by_default(self):
        prices = silpo.find_prices(self.connection(), self.shopping())
        self.assertEqual(prices['rows'][0]['offer']['id'], 'id-cheap')
        self.assertEqual([offer['id'] for offer in prices['rows'][0]['offers']], ['id-cheap', 'id-dear'])
        self.assertEqual(prices['basket_total'], 100 + 189 * 0.5)

    def test_person_can_replace_an_item(self):
        prices = silpo.find_prices(self.connection(), self.shopping(), {self.grechka.pk: 'id-dear'})
        self.assertEqual(prices['rows'][0]['offer']['id'], 'id-dear')
        self.assertEqual(prices['basket_total'], 200 + 189 * 0.5)

    def test_skipped_product_is_left_out_of_totals(self):
        prices = silpo.find_prices(self.connection(), self.shopping(), {self.grechka.pk: silpo.SKIP})
        self.assertTrue(prices['rows'][0]['skipped'])
        self.assertIsNone(prices['rows'][0]['offer'])
        self.assertEqual(prices['basket_total'], 189 * 0.5)
        self.assertEqual(prices['missing'], 0)

    def test_chosen_item_stays_in_the_list_even_when_it_is_not_among_the_cheapest(self):
        self.search['крупа гречана'] = [
            silpo_item(f'Крупа гречана {index}', 100 + index, '1кг', slug=f's{index}') for index in range(silpo.MAX_OFFERS + 3)
        ]
        expensive = f's{silpo.MAX_OFFERS + 2}'
        prices = silpo.find_prices(self.connection(), self.shopping(), {self.grechka.pk: f'id-{expensive}'})
        self.assertEqual(len(prices['rows'][0]['offers']), silpo.MAX_OFFERS + 1)
        self.assertEqual(prices['rows'][0]['offer']['id'], f'id-{expensive}')

    def test_slot_comes_from_cart_when_it_is_still_free(self):
        prices = silpo.find_prices(self.connection(timeslot={'start': SLOT_TWO['start'], 'end': SLOT_TWO['end']}), self.shopping())
        self.assertEqual(prices['slot_start'], SLOT_TWO['start'])

    def test_stale_cart_slot_falls_back_to_first_free(self):
        prices = silpo.find_prices(self.connection(timeslot={'start': '2020-01-01T00:00:00Z', 'end': '2020-01-01T02:00:00Z'}), self.shopping())
        self.assertEqual(prices['slot_start'], SLOT_ONE['start'])
        self.assertEqual([slot['start'] for slot in prices['slots']], [SLOT_ONE['start'], SLOT_TWO['start']])

    def test_default_slot_avoids_limited_hours_but_person_can_still_choose_them(self):
        limited = {**SLOT_ONE, 'constraints': {'isLimitedCookedFood': True, 'isLimitedOwnCooking': True}}
        free = {**SLOT_TWO, 'constraints': {'isLimitedCookedFood': False, 'isLimitedOwnCooking': False}}
        stale = {'start': '2020-01-01T00:00:00Z', 'end': '2020-01-01T02:00:00Z'}
        connection = self.connection(slots=[limited, free], timeslot=stale)
        self.assertEqual(silpo.find_prices(connection, self.shopping())['slot_start'], free['start'])
        self.assertEqual(silpo.find_prices(connection, self.shopping(), requested_slot=limited['start'])['slot_start'], limited['start'])
        only_limited = self.connection(slots=[limited], timeslot=stale)
        self.assertEqual(silpo.find_prices(only_limited, self.shopping())['slot_start'], limited['start'])

    def test_person_can_choose_a_slot_and_a_busy_one_is_ignored(self):
        self.assertEqual(silpo.find_prices(self.connection(), self.shopping(), requested_slot=SLOT_TWO['start'])['slot_start'], SLOT_TWO['start'])
        self.assertEqual(silpo.find_prices(self.connection(), self.shopping(), requested_slot=SLOT_BUSY['start'])['slot_start'], SLOT_ONE['start'])

    def calls_of(self, connection, name):
        """Аргументи всіх викликів інструмента з цією назвою."""
        return [arguments for tool, arguments in connection.calls if tool == name]

    def test_cart_gets_new_products_and_result_is_checked(self):
        connection = self.connection()
        result = silpo.build_cart(connection, self.shopping())
        self.assertEqual(result['problems'], [])
        self.assertEqual(result['added'], {'cart_id': 'cart-1', 'products': {'id-cheap': 1, 'id-chicken': 0.5}})
        add = self.calls_of(connection, 'silpo_add_cart_products')[0]
        self.assertEqual(add['shoppingCartId'], 'cart-1')
        self.assertEqual(add['products'], [
            {'productId': 'id-cheap', 'companyId': 'company-1', 'branchId': 'branch-1', 'quantity': 1},
            {'productId': 'id-chicken', 'companyId': 'company-1', 'branchId': 'branch-1', 'quantity': 0.5},
        ])
        self.assertEqual(connection.tool_names()[-1], 'silpo_get_shopping_cart_by_id')

    def test_cart_uses_person_choices(self):
        connection = self.connection()
        silpo.build_cart(connection, self.shopping(), {self.grechka.pk: 'id-dear', self.chicken.pk: silpo.SKIP})
        add = self.calls_of(connection, 'silpo_add_cart_products')[0]
        self.assertEqual([product['productId'] for product in add['products']], ['id-dear'])

    def test_silently_dropped_product_is_reported_and_not_remembered(self):
        result = silpo.build_cart(self.connection(drop_added=True), self.shopping())
        self.assertEqual(result['problems'], ['Не вдалося додати: Крупа гречана дешева, Куряче філе.'])
        self.assertEqual(result['added']['products'], {})

    def test_person_item_is_raised_to_needed_amount_but_stays_theirs(self):
        connection = self.connection(lines=[{'id': 'line-1', 'productId': 'id-chicken', 'quantity': 0.25}])
        result = silpo.build_cart(connection, self.shopping())
        self.assertEqual(self.calls_of(connection, 'silpo_update_cart_products')[0]['products'], [{'id': 'line-1', 'quantity': 0.5}])
        self.assertEqual([product['productId'] for product in self.calls_of(connection, 'silpo_add_cart_products')[0]['products']], ['id-cheap'])
        self.assertEqual(result['added']['products'], {'id-cheap': 1})

    def test_larger_amount_of_person_item_is_not_reduced(self):
        connection = self.connection(lines=[
            {'id': 'line-1', 'productId': 'id-chicken', 'quantity': 3},
            {'id': 'line-2', 'productId': 'id-cheap', 'quantity': 2},
        ])
        silpo.build_cart(connection, self.shopping())
        self.assertNotIn('silpo_update_cart_products', connection.tool_names())
        self.assertNotIn('silpo_add_cart_products', connection.tool_names())

    def test_second_click_with_same_choice_changes_nothing(self):
        connection = self.connection()
        first = silpo.build_cart(connection, self.shopping())
        lines_after_first = [dict(line) for line in connection.lines]
        second = silpo.build_cart(connection, self.shopping(), added=first['added'])
        self.assertEqual(connection.lines, lines_after_first)
        self.assertEqual(second['added'], first['added'])
        self.assertNotIn('silpo_remove_cart_products', connection.tool_names())

    def test_new_choice_replaces_items_the_app_added_before(self):
        connection = self.connection()
        first = silpo.build_cart(connection, self.shopping())
        result = silpo.build_cart(connection, self.shopping(), {self.grechka.pk: 'id-dear'}, added=first['added'])
        self.assertEqual(self.calls_of(connection, 'silpo_remove_cart_products'), [{'shoppingCartId': 'cart-1', 'products': [{'id': 'line-id-cheap'}]}])
        self.assertEqual(sorted(line['productId'] for line in connection.lines), ['id-chicken', 'id-dear'])
        self.assertEqual(result['added']['products'], {'id-dear': 1, 'id-chicken': 0.5})

    def test_app_items_get_exact_amount_even_when_smaller(self):
        connection = self.connection()
        first = silpo.build_cart(connection, {self.chicken: 1200})
        self.assertEqual(connection.lines[0]['quantity'], 1.5)
        silpo.build_cart(connection, {self.chicken: 300}, added=first['added'])
        self.assertEqual(connection.lines[0]['quantity'], 0.5)

    def test_skipping_a_product_removes_what_the_app_added_for_it(self):
        connection = self.connection()
        first = silpo.build_cart(connection, self.shopping())
        silpo.build_cart(connection, self.shopping(), {self.grechka.pk: silpo.SKIP}, added=first['added'])
        self.assertEqual([line['productId'] for line in connection.lines], ['id-chicken'])

    def test_person_items_are_never_removed(self):
        connection = self.connection(lines=[{'id': 'own', 'productId': 'id-milk', 'name': 'Молоко', 'quantity': 1}])
        first = silpo.build_cart(connection, self.shopping())
        silpo.build_cart(connection, self.shopping(), {self.grechka.pk: 'id-dear'}, added=first['added'])
        self.assertIn('id-milk', [line['productId'] for line in connection.lines])
        removed = [product['id'] for call in self.calls_of(connection, 'silpo_remove_cart_products') for product in call['products']]
        self.assertNotIn('own', removed)

    def test_record_about_another_cart_is_ignored(self):
        connection = self.connection(lines=[{'id': 'line-1', 'productId': 'id-cheap', 'name': 'Крупа', 'quantity': 3}])
        silpo.build_cart(connection, {self.chicken: 300}, added={'cart_id': 'old-cart', 'products': {'id-cheap': 3}})
        self.assertNotIn('silpo_remove_cart_products', connection.tool_names())

    def test_failed_removal_is_reported(self):
        connection = self.connection(ignore_remove=True)
        first = silpo.build_cart(connection, self.shopping())
        connection.lines[0]['name'] = 'Крупа гречана дешева'
        result = silpo.build_cart(connection, self.shopping(), {self.grechka.pk: 'id-dear'}, added=first['added'])
        self.assertEqual(result['problems'], ['Не вдалося прибрати старі товари: Крупа гречана дешева.'])

    def test_stock_problem_names_the_item(self):
        connection = self.connection(
            lines=[
                {'id': 'a', 'productId': 'gone', 'name': 'Рис Trapeza', 'quantity': 1, 'weighted': False},
                {'id': 'b', 'productId': 'few', 'name': 'Куряче філе', 'quantity': 2, 'weighted': True},
            ],
            calculation={'validations': [
                {'level': 'error', 'type': 'product', 'message': 'product.offer.stock.max', 'context': {'productId': 'gone', 'stock': 0}},
                {'level': 'error', 'type': 'product', 'message': 'product.offer.stock.max', 'context': {'productId': 'few', 'stock': 1.5}},
            ]},
        )
        problems = silpo.read_cart(connection)['problems']
        self.assertIn('«Рис Trapeza» закінчився на вибраний час', problems[0])
        self.assertIn('«Куряче філе» є лише 1.5 кг', problems[1])

    def test_same_item_for_two_products_is_summed(self):
        self.search['куряче філе'] = [silpo_item('Куряче філе', 189, '1кг', slug='same')]
        self.search['крупа гречана'] = [silpo_item('Крупа гречана', 100, '1кг', slug='same')]
        connection = self.connection()
        silpo.build_cart(connection, self.shopping())
        add = [call for call in connection.calls if call[0] == 'silpo_add_cart_products'][0][1]
        self.assertEqual(add['products'], [{'productId': 'id-same', 'companyId': 'company-1', 'branchId': 'branch-1', 'quantity': 2}])

    def test_stale_slot_is_updated_first_with_address_from_cart(self):
        connection = self.connection(timeslot={'start': '2020-01-01T00:00:00Z', 'end': '2020-01-01T02:00:00Z'})
        silpo.build_cart(connection, self.shopping(), requested_slot=SLOT_TWO['start'])
        names = connection.tool_names()
        self.assertLess(names.index('silpo_update_shopping_cart'), names.index('silpo_add_cart_products'))
        update = connection.calls[names.index('silpo_update_shopping_cart')][1]
        self.assertEqual(update['timeslot'], {'start': SLOT_TWO['start'], 'end': SLOT_TWO['end']})
        self.assertEqual(update['deliveryType'], 'DeliveryHome')
        self.assertEqual(update['address']['phone'], 'secret-phone')
        self.assertEqual(update['shipments'], [{'companyId': 'company-1', 'branchId': 'branch-1'}])

    def test_valid_slot_is_not_touched(self):
        connection = self.connection()
        silpo.build_cart(connection, self.shopping())
        self.assertNotIn('silpo_update_shopping_cart', connection.tool_names())

    def test_nothing_to_add_changes_nothing(self):
        connection = self.connection(timeslot={'start': '2020-01-01T00:00:00Z', 'end': '2020-01-01T02:00:00Z'})
        with self.assertRaisesMessage(silpo.SilpoError, silpo.NOTHING_TO_ADD_MESSAGE):
            silpo.build_cart(connection, self.shopping(), {self.grechka.pk: silpo.SKIP, self.chicken.pk: silpo.SKIP})
        self.assertFalse(set(connection.tool_names()) & {
            'silpo_add_cart_products', 'silpo_update_cart_products', 'silpo_update_shopping_cart',
        })

    def test_read_cart_shows_only_what_page_needs(self):
        connection = self.connection(
            lines=[
                {'id': 'l', 'productId': 'p', 'name': 'Крупа', 'quantity': 2, 'weighted': False, 'total': 198, 'slug': 'krupa'},
                {'id': 'm', 'productId': 'q', 'name': 'Томат', 'quantity': 0.75, 'weighted': True, 'total': 72.74},
            ],
            calculation={'total': 500, 'totalAfterDiscounts': 450, 'productsTotal': 449, 'delivery': {'total': 1}, 'validations': [
                {'level': 'error', 'type': 'timeslot', 'message': 'timeslot.not_found', 'context': []},
                {'level': 'warning', 'type': 'x', 'message': 'new.code', 'context': []},
                {'level': 'info', 'type': 'order', 'message': 'order.payment_types.disabled', 'context': {}},
            ]},
            extra={'checkoutWebLink': 'https://silpo.ua/checkout/1', 'checkoutMobileLink': 'javascript:alert(1)'},
        )
        cart = silpo.read_cart(connection)
        self.assertEqual(cart['total'], 450)
        self.assertEqual(cart['products_total'], 449)
        self.assertEqual(cart['delivery_total'], 1)
        self.assertEqual(cart['slot'], silpo.slot_label(SLOT_ONE))
        self.assertEqual(cart['lines'], [
            {'name': 'Крупа', 'quantity': '2 шт', 'total': 198, 'url': silpo.PRODUCT_URL + 'krupa'},
            {'name': 'Томат', 'quantity': '0.75 кг', 'total': 72.74, 'url': None},
        ])
        self.assertEqual(cart['problems'], [
            silpo.VALIDATION_MESSAGES['timeslot.not_found'],
            f'{silpo.UNKNOWN_VALIDATION_MESSAGE} (new.code).',
        ])
        self.assertEqual(cart['web_link'], 'https://silpo.ua/checkout/1')
        self.assertIsNone(cart['mobile_link'])
        self.assertNotIn('secret-phone', str(cart))

    def test_time_limit_problem_names_the_item_and_hours_once(self):
        context = {'productIds': ['salted'], 'availablePeriod': {'start': '10:30', 'end': '22:01'}}
        connection = self.connection(
            lines=[
                {'id': 'a', 'productId': 'salted', 'name': 'Огірок солоний', 'quantity': 0.2, 'weighted': True},
                {'id': 'b', 'productId': 'other', 'name': 'Морква', 'quantity': 0.5, 'weighted': True},
            ],
            calculation={'validations': [
                {'level': 'error', 'type': 'timeslot', 'message': 'timeslot.cooked_food.limited', 'context': context},
                {'level': 'error', 'type': 'timeslot', 'message': 'timeslot.own_cooking.limited', 'context': context},
            ]},
        )
        problems = silpo.read_cart(connection)['problems']
        self.assertEqual(len(problems), 1)
        self.assertIn('Огірок солоний', problems[0])
        self.assertIn('з 10:30 до 22:01', problems[0])
        self.assertNotIn('Морква', problems[0])

    def test_time_limit_problem_with_empty_context_still_gives_message(self):
        connection = self.connection(calculation={'validations': [
            {'level': 'error', 'type': 'timeslot', 'message': 'timeslot.cooked_food.limited', 'context': []},
        ]})
        self.assertEqual(
            silpo.read_cart(connection)['problems'],
            [f'{silpo.UNKNOWN_VALIDATION_MESSAGE} (timeslot.cooked_food.limited).'],
        )

    def connect_browser(self):
        """Позначає цей браузер як підключений до «Сільпо»."""
        session = self.client.session
        silpo.save_tokens(session, TOKENS, 'client-1')
        session.save()

    def test_prices_page_offers_choices_and_only_reads(self):
        self.connect_browser()
        connection = self.connection()
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            response = self.client.get(reverse('silpo_prices'), {
                'period': 'week', f'item_{self.grechka.pk}': 'id-dear', 'slot': SLOT_TWO['start'],
            })
        self.assertContains(response, f'name="item_{self.grechka.pk}"')
        self.assertContains(response, 'value="id-dear" selected')
        self.assertContains(response, 'Не купувати: вже є вдома')
        self.assertContains(response, 'name="period" value="week"')
        self.assertContains(response, 'Зібрати кошик у «Сільпо»')
        self.assertContains(response, 'href="/?period=week"')
        self.assertFalse(set(connection.tool_names()) & {
            'silpo_add_cart_products', 'silpo_update_cart_products', 'silpo_update_shopping_cart',
        })

    def test_cart_button_needs_login(self):
        with patch('nutrition.views.silpo.McpConnection') as connection:
            response = self.client.post(reverse('silpo_cart'), {'query': ''})
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        connection.assert_not_called()

    def test_cart_button_needs_csrf_token(self):
        guarded = self.client_class(enforce_csrf_checks=True)
        self.assertEqual(guarded.post(reverse('silpo_cart'), {'query': ''}).status_code, 403)

    def test_cart_button_adds_chosen_items_and_shows_cart(self):
        self.connect_browser()
        connection = self.connection(
            extra={'checkoutWebLink': 'https://silpo.ua/checkout/1'},
            calculation={'totalAfterDiscounts': 321},
        )
        query = f'period=week&item_{self.grechka.pk}=id-dear&slot={SLOT_ONE["start"].replace("+", "%2B")}'
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            response = self.client.post(reverse('silpo_cart'), {'query': query}, follow=True)
        add = [call for call in connection.calls if call[0] == 'silpo_add_cart_products'][0][1]
        self.assertIn('id-dear', [product['productId'] for product in add['products']])
        self.assertContains(response, 'Кошик «Сільпо» зібрано')
        self.assertContains(response, 'Оформити на сайті')
        self.assertContains(response, '321.00 грн')
        self.assertNotContains(response, 'secret-phone')
        self.assertNotContains(response, 'access-secret')
        self.assertEqual(sorted(self.client.session[silpo.ADDED_KEY]['products']), ['id-chicken', 'id-dear'])

        # другий раз з іншим вибором: старий товар застосунку прибирається, а не лишається поруч
        query = f'period=week&slot={SLOT_ONE["start"].replace("+", "%2B")}'
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            self.client.post(reverse('silpo_cart'), {'query': query})
        self.assertEqual(sorted(line['productId'] for line in connection.lines), ['id-cheap', 'id-chicken'])

    def test_prices_page_says_how_many_items_are_already_in_cart(self):
        self.connect_browser()
        connection = self.connection(lines=[{'id': 'own', 'productId': 'id-milk', 'name': 'Молоко', 'quantity': 1}])
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            response = self.client.get(reverse('silpo_prices'))
        self.assertContains(response, 'Зараз у кошику товарів: 1')

    def test_logout_forgets_what_the_app_added(self):
        self.connect_browser()
        session = self.client.session
        session[silpo.ADDED_KEY] = {'cart_id': 'cart-1', 'products': {'id-cheap': 1}}
        session.save()
        with patch('nutrition.silpo.requests.post'):
            self.client.post(reverse('silpo_logout'), {'next': '/'})
        self.assertNotIn(silpo.ADDED_KEY, self.client.session)

    def test_cart_button_reports_silpo_errors_on_prices_page(self):
        self.connect_browser()
        with patch('nutrition.views.silpo.McpConnection', side_effect=silpo.SilpoError(silpo.UNAVAILABLE_MESSAGE)):
            response = self.client.post(reverse('silpo_cart'), {'query': 'period=week'})
        self.assertRedirects(response, f"{reverse('silpo_prices')}?period=week", fetch_redirect_response=False)

    def test_cart_page_needs_login_and_handles_empty_cart(self):
        self.assertRedirects(self.client.get(reverse('silpo_cart')), '/', fetch_redirect_response=False)
        self.connect_browser()
        connection = self.connection(calculation={'totalAfterDiscounts': 99, 'validations': [
            {'level': 'error', 'type': 'order', 'message': 'order.cost.min', 'context': {}},
        ]})
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            response = self.client.get(reverse('silpo_cart'))
        self.assertContains(response, 'Кошик порожній')
        self.assertContains(response, 'Відкрити сайт «Сільпо»')
        self.assertContains(response, 'менша за мінімальне замовлення')

    def test_strange_item_keys_are_ignored(self):
        from nutrition.views import silpo_item_choices
        self.assertEqual(silpo_item_choices({'item_5': 'a', 'item_abc': 'b', 'item_²': 'c', 'other': 'd'}), {5: 'a'})


class SilpoSubstitutionTests(TestCase):
    """Оброблені товари й попередження про заміну через нестачу."""

    def setUp(self):
        """Куряче філе, яке купують на вагу."""
        self.chicken = Product.objects.create(
            name='філе', calories=120, proteins=22.5, fats=2.6, carbs=0, silpo_search='куряче філе',
        )

    def prices(self, items, choices=None, grams=300):
        """Ціни для 300 г філе із заготовленими результатами пошуку."""
        connection = FakeConnection(slots=[SLOT_ONE], products={'куряче філе': items})
        return silpo.find_prices(connection, {self.chicken: grams}, choices)

    def test_processed_goods_need_the_word_in_the_search(self):
        self.assertFalse(silpo.name_matches('Огірок солоний', 'огірок'))
        self.assertFalse(silpo.name_matches('Огірок ПП Діана-М Бочкові соління', 'огірок'))
        self.assertFalse(silpo.name_matches('Банан «Премія» сушений', 'банан'))
        self.assertFalse(silpo.name_matches('Капуста квашена', 'капуста'))
        self.assertFalse(silpo.name_matches('Куряче філе копчене', 'куряче філе'))
        self.assertFalse(silpo.name_matches('Квасоля консервована', 'квасоля'))
        self.assertTrue(silpo.name_matches('Огірок солоний', 'огірок солоний'))
        self.assertTrue(silpo.name_matches('Картопля біла для смаження та запікання', 'картопля'))
        self.assertTrue(silpo.name_matches('Огірок', 'огірок'))

    def test_no_warning_when_cheapest_is_available(self):
        row = self.prices([silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, slug='a')])['rows'][0]
        self.assertIsNone(row['warning'])

    def test_warning_when_cheapest_has_too_little(self):
        prices = self.prices([
            silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, stock=0.3, slug='cheap'),
            silpo_item('Куряче філе зі стегна', 282, '100г', weighted=True, step=0.5, slug='thigh'),
        ])
        row = prices['rows'][0]
        self.assertEqual(row['offer']['id'], 'id-thigh')
        self.assertEqual(row['warning'], 'Найдешевшого товару «Куряче філе» є лише 0.3 кг, а потрібно більше, тому вибрано інший.')
        self.assertEqual(prices['warnings'], 1)

    def test_warning_when_cheapest_is_out_of_stock(self):
        row = self.prices([
            silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, available=False, slug='cheap'),
            silpo_item('Куряче філе зі стегна', 282, '100г', weighted=True, step=0.5, slug='thigh'),
        ])['rows'][0]
        self.assertEqual(row['warning'], 'Найдешевшого товару «Куряче філе» зараз немає, тому вибрано інший.')

    def test_warning_when_nothing_is_enough(self):
        row = self.prices([silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, stock=0.3, slug='cheap')])['rows'][0]
        self.assertIsNone(row['offer'])
        self.assertEqual(row['warning'], 'Найдешевшого товару «Куряче філе» є лише 0.3 кг, а потрібно більше.')

    def test_person_choice_of_a_dearer_item_gives_no_warning(self):
        items = [
            silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, slug='cheap'),
            silpo_item('Куряче філе зі стегна', 282, '100г', weighted=True, step=0.5, slug='thigh'),
        ]
        self.assertIsNone(self.prices(items, {self.chicken.pk: 'id-thigh'})['rows'][0]['warning'])
        self.assertIsNone(self.prices(items, {self.chicken.pk: silpo.SKIP})['rows'][0]['warning'])

    def test_warning_when_chosen_item_ran_out(self):
        items = [
            silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, slug='cheap'),
            silpo_item('Куряче філе зі стегна', 282, '100г', weighted=True, step=0.5, stock=0.2, slug='thigh'),
        ]
        row = self.prices(items, {self.chicken.pk: 'id-thigh'})['rows'][0]
        self.assertEqual(row['offer']['id'], 'id-cheap')
        self.assertEqual(row['warning'], 'Вибраного раніше товару вже не вистачає, тому вибрано інший.')

    def test_page_marks_rows_and_shows_summary(self):
        session = self.client.session
        silpo.save_tokens(session, TOKENS, 'client-1')
        session.save()
        recipe = Recipe.objects.create(name='Філе')
        RecipeIngredient.objects.create(recipe=recipe, product=self.chicken, grams=150)
        connection = FakeConnection(slots=[SLOT_ONE], products={'куряче філе': [
            silpo_item('Куряче філе', 189, '100г', weighted=True, step=0.5, stock=0.3, slug='cheap'),
            silpo_item('Куряче філе зі стегна', 282, '100г', weighted=True, step=0.5, slug='thigh'),
        ]})
        with patch('nutrition.views.silpo.McpConnection', return_value=connection):
            response = self.client.get(reverse('silpo_prices'))
        self.assertContains(response, 'class="silpo-warning"')
        self.assertContains(response, 'позначених рядків: 1')


class McpConnectionTests(SimpleTestCase):
    """Розмова з MCP-сервером."""

    def setUp(self):
        """Сесія людини, яка вже увійшла, і підмінена мережа."""
        self.session = {}
        silpo.save_tokens(self.session, TOKENS, 'client-1')
        patcher = patch('nutrition.silpo.requests.post')
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, *answers):
        """Відкриває розмову: перші дві відповіді — на initialize і notifications/initialized."""
        opening = fake_response({'jsonrpc': '2.0', 'id': 1, 'result': {}})
        opening.headers['Mcp-Session-Id'] = 'session-1'
        self.post.side_effect = [opening, fake_response(b'', status=202), *answers]
        return silpo.McpConnection(self.session)

    def test_sends_token_and_session_id(self):
        answer = fake_response({'jsonrpc': '2.0', 'id': 2, 'result': {'tools': []}})
        connection = self.connect(answer)
        self.assertEqual(connection.request('tools/list'), {'tools': []})
        headers = self.post.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer access-secret')
        self.assertEqual(headers['Mcp-Session-Id'], 'session-1')

    def test_reads_event_stream_with_cyrillic(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"name":"гречка"}}\n\n'.encode()
        connection = self.connect(fake_response(body, content_type='text/event-stream'))
        self.assertEqual(connection.request('tools/list'), {'name': 'гречка'})

    def test_expired_login_is_forgotten(self):
        connection = self.connect(fake_response({}, status=401))
        with self.assertRaises(silpo.SilpoLoginRequired):
            connection.request('tools/list')
        self.assertNotIn(silpo.TOKENS_KEY, self.session)

    def test_rate_limit_and_server_errors_have_clear_messages(self):
        connection = self.connect(fake_response({}, status=429))
        with self.assertRaisesMessage(silpo.SilpoError, silpo.TOO_MANY_REQUESTS_MESSAGE):
            connection.request('tools/list')

        connection = self.connect(fake_response({'jsonrpc': '2.0', 'id': 2, 'error': {'code': -32601}}))
        with self.assertRaisesMessage(silpo.SilpoError, silpo.UNAVAILABLE_MESSAGE):
            connection.request('nothing')

    def test_tool_returns_structured_data(self):
        answer = fake_response({'jsonrpc': '2.0', 'id': 2, 'result': {'structuredContent': {'success': True, 'exists': True}}})
        connection = self.connect(answer)
        self.assertEqual(connection.call_tool('silpo_get_my_shopping_cart'), {'success': True, 'exists': True})
        self.assertEqual(self.post.call_args.kwargs['json']['params']['name'], 'silpo_get_my_shopping_cart')

    def test_tool_errors_have_clear_message(self):
        for result in [{'isError': True, 'content': []}, {'structuredContent': {'success': False}}, {'content': []}]:
            with self.subTest(result=result):
                connection = self.connect(fake_response({'jsonrpc': '2.0', 'id': 2, 'result': result}))
                with self.assertRaisesMessage(silpo.SilpoError, silpo.UNAVAILABLE_MESSAGE):
                    connection.call_tool('silpo_get_my_shopping_cart')
