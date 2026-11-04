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

    def test_best_offer_is_cheapest_per_kilogram_not_per_pack(self):
        items = [
            silpo_item('Крупа гречана дрібна', 90, '400г', slug='small'),
            silpo_item('Крупа гречана велика', 144, '1000г', slug='big'),
            silpo_item('Печиво гречане', 10, '500г', slug='other'),
        ]
        offer = silpo.best_offer(items, self.grechka, 'крупа гречана', 100)
        self.assertEqual(offer['url'], silpo.PRODUCT_URL + 'big')

    def test_no_match_gives_no_offer(self):
        self.assertIsNone(silpo.best_offer([silpo_item('Печиво', 10, '500г')], self.grechka, 'крупа гречана', 100))


class FakeConnection:
    """Підміна розмови із «Сільпо»: записує виклики й дає заготовлені відповіді."""

    def __init__(self, exists=True, delivery='DeliveryHome', slots=None, products=None):
        """Заготовлені дані: чи є кошик, спосіб доставки, слоти й товари за пошуками."""
        self.calls = []
        self.exists = exists
        self.delivery = delivery
        self.slots = [{'start': 'start-1', 'end': 'end-1', 'available': True, 'minOrderCost': 699}] if slots is None else slots
        self.products = products or {}

    def call_tool(self, name, **arguments):
        """Відповідає як сервер і запам'ятовує назву інструмента."""
        self.calls.append((name, arguments))
        if name == 'silpo_get_my_shopping_cart':
            return {'success': True, 'exists': self.exists, 'shoppingCartId': 'cart-1' if self.exists else None}
        if name == 'silpo_get_shopping_cart_by_id':
            return {'success': True, 'cart': {'deliveryType': self.delivery, 'shipments': [{'branchId': 'branch-1'}]}}
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
