"""Підключення до офіційного MCP-сервера «Сільпо»: вхід через OAuth і запити від імені людини.

Правила «Сільпо» (https://ai-factory.silpo.ua/docs/mcp):
- діємо лише від імені людини, яка сама увійшла у свій акаунт;
- токени зберігаємо на сервері, а не в браузері;
- не збираємо ціни й асортимент масово і не порівнюємо ціни між мережами.
"""
import base64
import hashlib
import json
import math
import re
import secrets
import time
from urllib.parse import quote, urlencode

import requests

from .models import SilpoClient

ISSUER = 'https://mcp.silpo.ua'
AUTHORIZE_URL = f'{ISSUER}/authorize'
TOKEN_URL = f'{ISSUER}/token'
REGISTER_URL = f'{ISSUER}/register'
MCP_URL = f'{ISSUER}/mcp'

CLIENT_NAME = 'Calorie Planner'
PROTOCOL_VERSION = '2025-06-18'
REQUEST_TIMEOUT = 20      # секунд на один запит до «Сільпо»
EXPIRY_MARGIN = 60        # оновлюємо токен за стільки секунд до кінця його дії
SEARCH_BATCH_SIZE = 30    # «Сільпо» приймає до 30 пошуків за один запит
SEARCH_RESULTS = 30       # скільки товарів дивимось на кожен пошук
SLOTS_TO_READ = 20        # скільки слотів доставки читаємо, щоб знайти перший вільний
PRODUCT_URL = 'https://silpo.ua/product/'

# Розмір упаковки в тексті «Сільпо»: 800г, 1кг, 0,5л, 850мл, 10шт, 5*80г
PACK_PATTERN = re.compile(r'(?:(\d+)\s*[*xх×]\s*)?(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт)')
PACK_UNITS = {'кг': (1000, 'g'), 'г': (1, 'g'), 'л': (1000, 'g'), 'мл': (1, 'g'), 'шт': (1, 'pc')}

LOGIN_KEY = 'silpo_login'     # у сесії: дані незавершеного входу
TOKENS_KEY = 'silpo_tokens'   # у сесії: токени доступу

UNAVAILABLE_MESSAGE = '«Сільпо» зараз не відповідає. Спробуй трохи пізніше.'
LOGIN_FAILED_MESSAGE = 'Не вдалося підтвердити вхід у «Сільпо». Спробуй ще раз.'
LOGIN_CANCELLED_MESSAGE = 'Вхід у «Сільпо» скасовано або не завершено.'
LOGIN_REQUIRED_MESSAGE = 'Вхід у «Сільпо» застарів. Увійди ще раз.'
TOO_MANY_REQUESTS_MESSAGE = 'Забагато запитів до «Сільпо». Спробуй за хвилину.'
NO_STORE_MESSAGE = 'У «Сільпо» не вибрано магазин чи адресу доставки. Вибери їх у застосунку чи на сайті «Сільпо» й спробуй ще раз.'
NO_SLOTS_MESSAGE = 'У «Сільпо» зараз немає вільних часових слотів для твого способу доставки. Спробуй пізніше.'


class SilpoError(Exception):
    """Щось пішло не так у розмові із «Сільпо»; текст помилки можна показати людині."""


class SilpoLoginRequired(SilpoError):
    """Потрібно увійти в акаунт «Сільпо» ще раз."""


def send_token_request(data):
    """Запит на сервер токенів → JSON-відповідь. Кидає SilpoLoginRequired, якщо сервер відмовив, і SilpoError, якщо він недоступний."""
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT)
        if 400 <= response.status_code < 500:
            raise SilpoLoginRequired(LOGIN_REQUIRED_MESSAGE)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        raise SilpoError(UNAVAILABLE_MESSAGE) from error


def get_client_id(redirect_uri):
    """client_id застосунку для цієї адреси повернення; якщо його ще нема — реєструє застосунок у «Сільпо»."""
    client = SilpoClient.objects.filter(redirect_uri=redirect_uri).first()
    if client:
        return client.client_id

    registration = {
        'client_name': CLIENT_NAME,
        'redirect_uris': [redirect_uri],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
    }
    try:
        response = requests.post(REGISTER_URL, json=registration, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        client_id = response.json()['client_id']
    except (requests.RequestException, KeyError, TypeError) as error:
        raise SilpoError(UNAVAILABLE_MESSAGE) from error

    SilpoClient.objects.create(redirect_uri=redirect_uri, client_id=client_id)
    return client_id


def code_challenge(verifier):
    """PKCE: відбиток одноразового секрету. Сервер перевірить, що код обмінює той самий застосунок, який почав вхід."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def start_login(session, redirect_uri, next_url):
    """Готує вхід і повертає адресу сторінки входу «Сільпо». state захищає від підміненого повернення, verifier — від перехопленого коду."""
    client_id = get_client_id(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    session[LOGIN_KEY] = {
        'state': state,
        'verifier': verifier,
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'next': next_url,
    }
    return AUTHORIZE_URL + '?' + urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': code_challenge(verifier),
        'code_challenge_method': 'S256',
        'resource': MCP_URL,
    })


def save_tokens(session, tokens, client_id, previous_refresh_token=None):
    """Кладе токени в серверну сесію: браузер бачить лише номер сесії, а не самі токени."""
    if 'access_token' not in tokens:
        raise SilpoError(UNAVAILABLE_MESSAGE)
    session[TOKENS_KEY] = {
        'access_token': tokens['access_token'],
        'refresh_token': tokens.get('refresh_token', previous_refresh_token),
        'expires_at': time.time() + tokens.get('expires_in', 3600),
        'client_id': client_id,
    }


def finish_login(session, params):
    """Завершує вхід після повернення із «Сільпо»: перевіряє відповідь, обмінює код на токени → адреса, куди повернути людину."""
    login = session.pop(LOGIN_KEY, None)
    if login is None or not secrets.compare_digest(params.get('state', '').encode(), login['state'].encode()):
        raise SilpoError(LOGIN_FAILED_MESSAGE)
    if params.get('iss', ISSUER) != ISSUER:
        raise SilpoError(LOGIN_FAILED_MESSAGE)
    if params.get('error') or not params.get('code'):
        raise SilpoError(LOGIN_CANCELLED_MESSAGE)

    try:
        tokens = send_token_request({
            'grant_type': 'authorization_code',
            'code': params['code'],
            'redirect_uri': login['redirect_uri'],
            'client_id': login['client_id'],
            'code_verifier': login['verifier'],
            'resource': MCP_URL,
        })
    except SilpoLoginRequired as error:
        raise SilpoError(LOGIN_FAILED_MESSAGE) from error

    save_tokens(session, tokens, login['client_id'])
    session.cycle_key()   # новий номер сесії після входу: старий більше нічого не відкриває
    return login['next']


def is_connected(session):
    """Чи увійшла людина в акаунт «Сільпо» в цьому браузері."""
    return TOKENS_KEY in session


def access_token(session):
    """Чинний токен доступу; якщо він от-от закінчиться — оновлює. Кидає SilpoLoginRequired, якщо входу нема або він застарів."""
    tokens = session.get(TOKENS_KEY)
    if tokens is None:
        raise SilpoLoginRequired(LOGIN_REQUIRED_MESSAGE)
    if time.time() < tokens['expires_at'] - EXPIRY_MARGIN:
        return tokens['access_token']
    if not tokens.get('refresh_token'):
        session.pop(TOKENS_KEY, None)
        raise SilpoLoginRequired(LOGIN_REQUIRED_MESSAGE)

    try:
        fresh = send_token_request({
            'grant_type': 'refresh_token',
            'refresh_token': tokens['refresh_token'],
            'client_id': tokens['client_id'],
            'resource': MCP_URL,
        })
    except SilpoLoginRequired:
        session.pop(TOKENS_KEY, None)
        raise
    save_tokens(session, fresh, tokens['client_id'], tokens['refresh_token'])
    return session[TOKENS_KEY]['access_token']


def logout(session):
    """Виходить: просить «Сільпо» відкликати токен і забуває його тут, навіть якщо сервер не відповів."""
    session.pop(LOGIN_KEY, None)
    tokens = session.pop(TOKENS_KEY, None)
    if tokens is None:
        return
    try:
        requests.post(TOKEN_URL, data={
            'token': tokens.get('refresh_token') or tokens['access_token'],
            'client_id': tokens['client_id'],
        }, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass


def read_message(response):
    """Дістає відповідь JSON-RPC: сервер може надіслати звичайний JSON або потік подій (SSE)."""
    if response.headers.get('Content-Type', '').startswith('text/event-stream'):
        response.encoding = 'utf-8'   # без цього requests зіпсував би кирилицю
        for line in response.text.splitlines():
            if line.startswith('data:'):
                message = json.loads(line[len('data:'):])
                if 'result' in message or 'error' in message:
                    return message
        raise SilpoError(UNAVAILABLE_MESSAGE)
    return response.json()


class McpConnection:
    """Одна розмова з MCP-сервером «Сільпо» від імені людини, що увійшла."""

    def __init__(self, session):
        """Відкриває розмову: сервер і застосунок домовляються про версію протоколу."""
        self.session = session
        self.mcp_session_id = None
        self.next_id = 1
        self.request('initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': CLIENT_NAME, 'version': '0.1'},
        })
        self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def post(self, payload):
        """Надсилає одне повідомлення на сервер → відповідь requests. Кидає SilpoError, якщо щось не так."""
        headers = {
            'Authorization': f'Bearer {access_token(self.session)}',
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': PROTOCOL_VERSION,
        }
        if self.mcp_session_id:
            headers['Mcp-Session-Id'] = self.mcp_session_id
        try:
            response = requests.post(MCP_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            raise SilpoError(UNAVAILABLE_MESSAGE) from error

        if response.status_code == 401:
            self.session.pop(TOKENS_KEY, None)
            raise SilpoLoginRequired(LOGIN_REQUIRED_MESSAGE)
        if response.status_code == 429:
            raise SilpoError(TOO_MANY_REQUESTS_MESSAGE)
        if not response.ok:
            raise SilpoError(UNAVAILABLE_MESSAGE)
        self.mcp_session_id = response.headers.get('Mcp-Session-Id', self.mcp_session_id)
        return response

    def request(self, method, params=None):
        """Запит JSON-RPC → його результат. Кидає SilpoError, якщо сервер повернув помилку."""
        payload = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method}
        if params is not None:
            payload['params'] = params
        self.next_id += 1
        try:
            message = read_message(self.post(payload))
        except ValueError as error:
            raise SilpoError(UNAVAILABLE_MESSAGE) from error
        if 'error' in message:
            raise SilpoError(UNAVAILABLE_MESSAGE)
        return message['result']

    def call_tool(self, name, **arguments):
        """Викликає інструмент «Сільпо» → його дані (словник). Кидає SilpoError, якщо інструмент повернув помилку."""
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        data = result.get('structuredContent')
        if result.get('isError') or not data or data.get('success') is False:
            raise SilpoError(UNAVAILABLE_MESSAGE)
        return data


def parse_pack(display_ratio):
    """Розмір упаковки → (кількість, одиниця): '0,5л' → (500, 'g'), '10шт' → (10, 'pc'), '5*80г' → (400, 'g'). Рідину рахуємо приблизно: 1 мл ≈ 1 г."""
    match = PACK_PATTERN.fullmatch((display_ratio or '').strip().lower())
    if not match:
        return None
    packs = int(match.group(1) or 1)
    amount = float(match.group(2).replace(',', '.'))
    factor, unit = PACK_UNITS[match.group(3)]
    return packs * amount * factor, unit


def name_matches(name, query):
    """Чи годиться назва товару під пошук: починається з першого слова пошуку й містить решту як окремі слова. Так «рис» не знайде «рисове борошно», а «яловичина» — корм для собак."""
    words = re.findall(r'\w+', name.lower())
    query_words = query.lower().split()
    if not words or not query_words or words[0] != query_words[0]:
        return False
    return all(word in words for word in query_words)


def whole_units(needed, unit_size):
    """Скільки цілих упаковок або кроків ваги треба, щоб вистачило на needed."""
    return math.ceil(round(needed / unit_size, 6))


def make_offer(item, product, grams):
    """Що й за скільки купити, якщо брати цей товар → словник пропозиції або None, якщо товар не годиться."""
    if not item.get('available'):
        return None

    url = PRODUCT_URL + quote(item['slug'])
    if item.get('weighted'):
        if product.unit == 'pc' or not item.get('step'):
            return None
        kilograms = whole_units(grams / 1000, item['step']) * item['step']
        if kilograms > item.get('stock', 0):
            return None
        return {
            'name': item['name'],
            'url': url,
            'unit_price': item['price'] / 1000,
            'used_cost': item['price'] * grams / 1000,
            'cost': item['price'] * kilograms,
            'price_label': f"{item['price']:.2f} грн за кг",
            'quantity': f'{kilograms:g} кг',
        }

    pack = parse_pack(item.get('displayRatio'))
    if pack is None:
        return None
    pack_size, pack_unit = pack
    if pack_unit == 'pc':
        if product.unit != 'pc' or not product.grams_per_piece:
            return None
        needed = grams / product.grams_per_piece
    else:
        if product.unit == 'pc':
            return None
        needed = grams
    packs = whole_units(needed, pack_size)
    if packs > item.get('stock', 0):
        return None
    return {
        'name': item['name'],
        'url': url,
        'unit_price': item['price'] / pack_size,
        'used_cost': item['price'] * needed / pack_size,
        'cost': item['price'] * packs,
        'price_label': f"{item['price']:.2f} грн за {item['displayRatio']}",
        'quantity': f'{packs} шт',
    }


def best_offer(items, product, query, grams):
    """Найдешевша за кілограм чи штуку пропозиція серед результатів пошуку, або None, якщо нічого не годиться."""
    offers = []
    for item in items:
        if name_matches(item.get('name', ''), query):
            offer = make_offer(item, product, grams)
            if offer:
                offers.append(offer)
    if not offers:
        return None
    return min(offers, key=lambda offer: offer['unit_price'])


def store_context(connection):
    """Магазин, спосіб доставки й перший вільний слот із кошика людини. Кошик лише читаємо й нічого в ньому не змінюємо."""
    reference = connection.call_tool('silpo_get_my_shopping_cart')
    if not reference.get('exists'):
        raise SilpoError(NO_STORE_MESSAGE)

    cart = connection.call_tool('silpo_get_shopping_cart_by_id', shoppingCartId=reference['shoppingCartId'])['cart']
    shipments = cart.get('shipments') or []
    branch_id = shipments[0].get('branchId') if shipments else None
    delivery_type = cart.get('deliveryType')
    if not branch_id or not delivery_type:
        raise SilpoError(NO_STORE_MESSAGE)

    slots = connection.call_tool(
        'silpo_get_time_slots', branchId=branch_id, deliveryType=delivery_type, limit=SLOTS_TO_READ,
    ).get('slots', [])
    for slot in slots:
        if slot.get('available'):
            return branch_id, delivery_type, slot
    raise SilpoError(NO_SLOTS_MESSAGE)


def find_prices(connection, shopping):
    """Реальні ціни для списку покупок {продукт: грами} цієї людини. Лише читає: нічого не зберігає й не додає в кошик."""
    branch_id, delivery_type, slot = store_context(connection)

    needed = {product: grams for product, grams in shopping.items() if grams > 0}
    queries = {product: product.silpo_search or product.name for product in needed}
    unique_queries = list(dict.fromkeys(queries.values()))

    found = {}
    for start in range(0, len(unique_queries), SEARCH_BATCH_SIZE):
        results = connection.call_tool(
            'silpo_find_products_batch',
            branchId=branch_id,
            deliveryType=delivery_type,
            timeslotStart=slot['start'],
            timeslotEnd=slot['end'],
            products=unique_queries[start:start + SEARCH_BATCH_SIZE],
            limit=SEARCH_RESULTS,
        )
        for result in results.get('queries', []):
            found[result.get('query')] = result.get('products', [])

    rows = []
    for product, grams in needed.items():
        query = queries[product]
        rows.append({
            'product': product,
            'grams': grams,
            'pieces': product.pieces_for(grams),
            'offer': best_offer(found.get(query, []), product, query, grams),
        })

    offers = [row['offer'] for row in rows if row['offer']]
    return {
        'rows': rows,
        'basket_total': sum(offer['cost'] for offer in offers),
        'used_total': sum(offer['used_cost'] for offer in offers),
        'missing': len(rows) - len(offers),
        'min_order': slot.get('minOrderCost'),
    }
