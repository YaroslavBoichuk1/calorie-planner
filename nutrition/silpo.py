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
from datetime import datetime
from urllib.parse import quote, urlencode

import requests
from django.utils import timezone

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
SLOTS_TO_READ = 20        # скільки слотів доставки читаємо, щоб знайти вільні
MAX_OFFERS = 6            # скільки варіантів товару показуємо на вибір
SKIP = 'skip'             # вибір людини: продукт уже є вдома, не купувати
PRODUCT_URL = 'https://silpo.ua/product/'
SITE_URL = 'https://silpo.ua/'

# Розмір упаковки в тексті «Сільпо»: 800г, 1кг, 0,5л, 850мл, 10шт, 5*80г
PACK_PATTERN = re.compile(r'(?:(\d+)\s*[*xх×]\s*)?(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт)')
PACK_UNITS = {'кг': (1000, 'g'), 'г': (1, 'g'), 'л': (1000, 'g'), 'мл': (1, 'g'), 'шт': (1, 'pc')}

LOGIN_KEY = 'silpo_login'     # у сесії: дані незавершеного входу
TOKENS_KEY = 'silpo_tokens'   # у сесії: токени доступу
ADDED_KEY = 'silpo_added'     # у сесії: які товари застосунок сам додав у кошик {'cart_id', 'products': {id: кількість}}

UNAVAILABLE_MESSAGE = '«Сільпо» зараз не відповідає. Спробуй трохи пізніше.'
LOGIN_FAILED_MESSAGE = 'Не вдалося підтвердити вхід у «Сільпо». Спробуй ще раз.'
LOGIN_CANCELLED_MESSAGE = 'Вхід у «Сільпо» скасовано або не завершено.'
LOGIN_REQUIRED_MESSAGE = 'Вхід у «Сільпо» застарів. Увійди ще раз.'
TOO_MANY_REQUESTS_MESSAGE = 'Забагато запитів до «Сільпо». Спробуй за хвилину.'
NO_STORE_MESSAGE = 'У «Сільпо» не вибрано магазин чи адресу доставки. Вибери їх у застосунку чи на сайті «Сільпо» й спробуй ще раз.'
NO_SLOTS_MESSAGE = 'У «Сільпо» зараз немає вільних часових слотів для твого способу доставки. Спробуй пізніше.'
NOTHING_TO_ADD_MESSAGE = 'Немає товарів, які можна додати в кошик.'

# Коди попереджень кошика, які «Сільпо» повертає замість тексту
VALIDATION_MESSAGES = {
    'timeslot.not_found': 'Вибраний час доставки вже недоступний. Вибери інший.',
    'product.offer.stock.max': 'Одного з товарів немає в потрібній кількості.',
    'order.cost.min': 'Сума товарів менша за мінімальне замовлення для доставки.',
}
UNKNOWN_VALIDATION_MESSAGE = '«Сільпо» просить перевірити кошик на своєму сайті'
# Початки слів обробленої їжі: такий товар не годиться, якщо цього слова нема в самому пошуку
PROCESSED_STEMS = ('солон', 'солін', 'маринов', 'квашен', 'консерв', 'копчен', 'сушен', 'стерилізов')
# Коди, коли товар власного приготування можна доставити лише в певні години
TIME_LIMITED_CODES = {'timeslot.cooked_food.limited', 'timeslot.own_cooking.limited'}


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
    session.pop(ADDED_KEY, None)
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
    """Чи годиться назва товару під пошук: починається з першого слова пошуку, містить решту як окремі слова
    й не є обробленим (солоним, маринованим тощо), якщо пошук цього не просить.
    Так «рис» не знайде «рисове борошно», «яловичина» — корм для собак, а «огірок» — солоний огірок."""
    words = re.findall(r'\w+', name.lower())
    query_words = query.lower().split()
    if not words or not query_words or words[0] != query_words[0]:
        return False
    if not all(word in words for word in query_words):
        return False
    for stem in PROCESSED_STEMS:
        in_name = any(word.startswith(stem) for word in words)
        in_query = any(word.startswith(stem) for word in query_words)
        if in_name and not in_query:
            return False
    return True


def whole_units(needed, unit_size):
    """Скільки цілих упаковок або кроків ваги треба, щоб вистачило на needed."""
    return math.ceil(round(needed / unit_size, 6))


def make_offer(item, product, grams, check_stock=True):
    """Що й за скільки купити, якщо брати цей товар → словник пропозиції або None, якщо товар не годиться.
    check_stock=False — не зважати на наявність: так знаходимо товар, який був би найкращим, якби його вистачало."""
    if check_stock and not item.get('available'):
        return None

    common = {
        'id': item.get('id'),
        'company_id': item.get('companyId'),
        'branch_id': item.get('branchId'),
        'name': item['name'],
        'url': PRODUCT_URL + quote(item['slug']),
        'available': bool(item.get('available')),
        'stock': item.get('stock', 0),
        'unit': 'кг' if item.get('weighted') else 'шт',
    }
    if item.get('weighted'):
        if product.unit == 'pc' or not item.get('step'):
            return None
        kilograms = whole_units(grams / 1000, item['step']) * item['step']
        if check_stock and kilograms > item.get('stock', 0):
            return None
        return {
            **common,
            'unit_price': item['price'] / 1000,
            'used_cost': item['price'] * grams / 1000,
            'cost': item['price'] * kilograms,
            'price_label': f"{item['price']:.2f} грн за кг",
            'quantity': f'{kilograms:g} кг',
            'quantity_value': kilograms,
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
    if check_stock and packs > item.get('stock', 0):
        return None
    return {
        **common,
        'unit_price': item['price'] / pack_size,
        'used_cost': item['price'] * needed / pack_size,
        'cost': item['price'] * packs,
        'price_label': f"{item['price']:.2f} грн за {item['displayRatio']}",
        'quantity': f'{packs} шт',
        'quantity_value': packs,
    }


def offers_for(items, product, query, grams, check_stock=True):
    """Усі товари з результатів пошуку, що годяться для продукту, від найдешевшого за кілограм чи штуку."""
    offers = []
    for item in items:
        if name_matches(item.get('name', ''), query):
            offer = make_offer(item, product, grams, check_stock)
            if offer:
                offers.append(offer)
    return sorted(offers, key=lambda offer: offer['unit_price'])


def shortage_warning(best, offer, choice):
    """Попередження, якщо вибрано не той товар, який мав би бути: найдешевшого чи вибраного людиною не вистачає. Інакше None."""
    if choice == SKIP:
        return None
    if choice and (offer is None or offer['id'] != choice):
        return 'Вибраного раніше товару вже не вистачає, тому вибрано інший.' if offer else 'Вибраного раніше товару вже не вистачає.'
    if choice or best is None or (offer is not None and offer['id'] == best['id']):
        return None
    if not best['available'] or best['stock'] <= 0:
        shortage = f"Найдешевшого товару «{best['name']}» зараз немає"
    else:
        shortage = f"Найдешевшого товару «{best['name']}» є лише {best['stock']:g} {best['unit']}, а потрібно більше"
    return f'{shortage}, тому вибрано інший.' if offer else f'{shortage}.'


def choose_offer(offers, choice):
    """Товар, який вибрала людина; якщо вибору нема або товар зник — найдешевший, а якщо «вже є вдома» — None."""
    if choice == SKIP or not offers:
        return None
    for offer in offers:
        if offer['id'] == choice:
            return offer
    return offers[0]


def parse_moment(text):
    """Час зі слота → datetime; None, якщо час у незнайомому форматі."""
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def same_moment(first, second):
    """Чи це той самий момент часу: «Сільпо» може записувати один час по-різному (Z і +00:00)."""
    first_moment, second_moment = parse_moment(first), parse_moment(second)
    return first_moment is not None and first_moment == second_moment


def slot_label(slot):
    """Слот доставки для людини: у «Сільпо» час в UTC, а показуємо київський."""
    start, end = parse_moment(slot['start']), parse_moment(slot['end'])
    if start is None or end is None:
        return f"{slot['start']} – {slot['end']}"
    return f'{timezone.localtime(start):%d.%m, %H:%M}–{timezone.localtime(end):%H:%M}'


def slot_is_limited(slot):
    """Чи діють у цей час обмеження на товари власного приготування й готову їжу."""
    constraints = slot.get('constraints') or {}
    return bool(constraints.get('isLimitedCookedFood') or constraints.get('isLimitedOwnCooking'))


def store_context(connection, requested_slot=''):
    """Кошик людини, її магазин і вільні слоти доставки → словник. Лише читає. Вибирає слот: той, що просять, той, що в кошику, або перший вільний."""
    reference = connection.call_tool('silpo_get_my_shopping_cart')
    if not reference.get('exists'):
        raise SilpoError(NO_STORE_MESSAGE)

    cart_id = reference['shoppingCartId']
    cart = connection.call_tool('silpo_get_shopping_cart_by_id', shoppingCartId=cart_id)['cart']
    shipments = cart.get('shipments') or []
    branch_id = shipments[0].get('branchId') if shipments else None
    delivery_type = cart.get('deliveryType')
    if not branch_id or not delivery_type:
        raise SilpoError(NO_STORE_MESSAGE)
    # для експрес-доставки товари й слоти шукаємо як для звичайної
    search_delivery = 'DeliveryHome' if delivery_type == 'DeliveryExpressByPromise' else delivery_type

    slots = [
        slot for slot in connection.call_tool(
            'silpo_get_time_slots', branchId=branch_id, deliveryType=search_delivery, limit=SLOTS_TO_READ,
        ).get('slots', [])
        if slot.get('available')
    ]
    if not slots:
        raise SilpoError(NO_SLOTS_MESSAGE)

    cart_slot_start = (cart.get('timeslot') or {}).get('start')
    # типово беремо перший слот без обмежень: у ранні години частину товарів не доставляють
    chosen = next((slot for slot in slots if not slot_is_limited(slot)), slots[0])
    for wanted in (cart_slot_start, requested_slot):   # пізніший у циклі важливіший: вибір людини перемагає
        for slot in slots:
            if same_moment(slot['start'], wanted):
                chosen = slot
    return {
        'cart_id': cart_id,
        'cart': cart,
        'branch_id': branch_id,
        'company_id': shipments[0].get('companyId'),
        'delivery_type': search_delivery,
        'slots': slots,
        'slot': chosen,
    }


def find_prices(connection, shopping, choices=None, requested_slot=''):
    """Реальні ціни для списку покупок {продукт: грами} цієї людини. Лише читає: нічого не зберігає й не додає в кошик."""
    choices = choices or {}
    context = store_context(connection, requested_slot)
    branch_id, delivery_type, slot = context['branch_id'], context['delivery_type'], context['slot']

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
        offers = offers_for(found.get(query, []), product, query, grams)
        everything = offers_for(found.get(query, []), product, query, grams, check_stock=False)
        choice = choices.get(product.pk)
        offer = choose_offer(offers, choice)
        shown = offers[:MAX_OFFERS]
        if offer and offer not in shown:
            shown.append(offer)
        rows.append({
            'product': product,
            'grams': grams,
            'pieces': product.pieces_for(grams),
            'offers': shown,
            'offer': offer,
            'skipped': choice == SKIP,
            'warning': shortage_warning(everything[0] if everything else None, offer, choice),
        })

    chosen = [row['offer'] for row in rows if row['offer']]
    return {
        'rows': rows,
        'basket_total': sum(offer['cost'] for offer in chosen),
        'used_total': sum(offer['used_cost'] for offer in chosen),
        'missing': sum(1 for row in rows if not row['offer'] and not row['skipped']),
        'min_order': slot.get('minOrderCost'),
        'slots': [{'start': item['start'], 'label': slot_label(item)} for item in context['slots']],
        'slot_start': slot['start'],
        'cart_count': len(cart_lines(context['cart'])),
        'warnings': sum(1 for row in rows if row['warning']),
        'context': context,
    }


def update_slot(connection, context):
    """Записує в кошик вибраний час доставки. Адресу й магазин передає такими, як вони вже є в кошику."""
    cart = context['cart']
    connection.call_tool(
        'silpo_update_shopping_cart',
        shoppingCartId=context['cart_id'],
        deliveryType=cart['deliveryType'],
        timeslot={'start': context['slot']['start'], 'end': context['slot']['end']},
        address=cart['address'],
        shipments=[
            {'companyId': shipment['companyId'], 'branchId': shipment['branchId']}
            for shipment in cart['shipments']
        ],
    )


def cart_lines(cart):
    """Усі рядки товарів кошика."""
    lines = []
    for shipment in cart.get('shipments') or []:
        lines.extend(shipment.get('products') or [])
    return lines


def same_quantity(first, second):
    """Чи однакова кількість: дробові кілограми порівнюємо з невеликим запасом."""
    return first is not None and second is not None and abs(first - second) < 1e-6


def build_cart(connection, shopping, choices=None, requested_slot='', added=None):
    """Збирає кошик людини під вибраний раціон і перевіряє результат → {'problems': [...], 'added': {...}}. Замовлення не оформлює.

    added — товари, які застосунок додав раніше: {'cart_id': ..., 'products': {id товару: кількість}}.
    Своїм товарам застосунок ставить точну кількість, а ті, яких більше нема у виборі, прибирає.
    Товари, які людина поклала сама, він лише збільшує до потрібної кількості й ніколи не прибирає.
    """
    prices = find_prices(connection, shopping, choices, requested_slot)
    context = prices['context']
    cart_id = context['cart_id']

    wanted = {}
    for row in prices['rows']:
        offer = row['offer']
        if offer:
            line = wanted.setdefault(offer['id'], {'offer': offer, 'quantity': 0})
            line['quantity'] += offer['quantity_value']
    if not wanted:
        raise SilpoError(NOTHING_TO_ADD_MESSAGE)

    # записи про інший кошик не рахуються: тоді всі товари в кошику вважаємо товарами людини
    previous = (added or {}).get('products', {}) if (added or {}).get('cart_id') == cart_id else {}

    if not same_moment((context['cart'].get('timeslot') or {}).get('start'), context['slot']['start']):
        update_slot(connection, context)

    in_cart = {line.get('productId'): line for line in cart_lines(context['cart'])}
    to_add, to_update, to_remove = [], [], []
    for product_id, line in wanted.items():
        existing = in_cart.get(product_id)
        if existing is None:
            to_add.append({
                'productId': product_id,
                'companyId': line['offer']['company_id'] or context['company_id'],
                'branchId': line['offer']['branch_id'] or context['branch_id'],
                'quantity': line['quantity'],
            })
        elif product_id in previous:
            if not same_quantity(existing.get('quantity'), line['quantity']):
                to_update.append({'id': existing['id'], 'quantity': line['quantity']})
        elif existing.get('quantity', 0) < line['quantity']:
            to_update.append({'id': existing['id'], 'quantity': line['quantity']})
    for product_id in previous:
        if product_id not in wanted and product_id in in_cart:
            to_remove.append(in_cart[product_id])

    if to_remove:
        connection.call_tool('silpo_remove_cart_products', shoppingCartId=cart_id,
                             products=[{'id': line['id']} for line in to_remove])
    if to_update:
        connection.call_tool('silpo_update_cart_products', shoppingCartId=cart_id, products=to_update)
    if to_add:
        connection.call_tool('silpo_add_cart_products', shoppingCartId=cart_id, products=to_add)

    # «Сільпо» просить завжди перевіряти результат: запит можуть прийняти, але не виконати
    after = {line.get('productId'): line for line in cart_lines(
        connection.call_tool('silpo_get_shopping_cart_by_id', shoppingCartId=cart_id)['cart']
    )}
    not_added = [wanted[item['productId']]['offer']['name'] for item in to_add if item['productId'] not in after]
    updated_ids = {item['id']: item['quantity'] for item in to_update}
    not_updated = [
        wanted[product_id]['offer']['name'] for product_id, line in in_cart.items()
        if line['id'] in updated_ids and not same_quantity((after.get(product_id) or {}).get('quantity'), updated_ids[line['id']])
    ]
    not_removed = [line.get('name', 'Товар') for line in to_remove if line.get('productId') in after]

    problems = []
    if not_added:
        problems.append(f"Не вдалося додати: {', '.join(not_added)}.")
    if not_updated:
        problems.append(f"Не вдалося змінити кількість: {', '.join(not_updated)}.")
    if not_removed:
        problems.append(f"Не вдалося прибрати старі товари: {', '.join(not_removed)}.")

    managed = {
        product_id: line['quantity'] for product_id, line in wanted.items()
        if product_id in after and (product_id in previous or product_id not in in_cart)
    }
    return {'problems': problems, 'added': {'cart_id': cart_id, 'products': managed}}


def https_only(url):
    """Посилання для сторінки: показуємо лише https, щоб у href не потрапило нічого іншого."""
    return url if isinstance(url, str) and url.startswith('https://') else None


def problem_text(validation, lines):
    """Попередження «Сільпо» людською мовою: для обмеження за часом називає товар і години."""
    code = validation.get('message', '')
    context = validation.get('context')
    if code == 'product.offer.stock.max' and isinstance(context, dict):
        line = next((line for line in lines if line.get('productId') == context.get('productId')), {})
        name = line.get('name', 'Товар')
        stock = context.get('stock') or 0
        if stock <= 0:
            return f"«{name}» закінчився на вибраний час. Вибери інший товар чи час і збери кошик ще раз."
        unit = 'кг' if line.get('weighted') else 'шт'
        return f"«{name}» є лише {stock:g} {unit}, а в кошику більше. Зменш кількість або вибери інший товар."
    if code in TIME_LIMITED_CODES and isinstance(context, dict):
        product_ids = context.get('productIds') or []
        names = ', '.join(line.get('name', 'Товар') for line in lines if line.get('productId') in product_ids)
        period = context.get('availablePeriod') or {}
        return (
            f"Товар «{names}» доставляють лише з {period.get('start')} до {period.get('end')}, а вибраний час "
            f"починається раніше. Вибери пізніший час або заміни товар на сторінці цін і збери кошик ще раз."
        )
    return VALIDATION_MESSAGES.get(code, f'{UNKNOWN_VALIDATION_MESSAGE} ({code}).')


def cart_line_view(line):
    """Рядок кошика для показу: назва, кількість з одиницею, сума й посилання на товар."""
    quantity = line.get('quantity')
    unit = 'кг' if line.get('weighted') else 'шт'
    return {
        'name': line.get('name') or 'Товар',
        'quantity': f'{quantity:g} {unit}' if isinstance(quantity, (int, float)) else '',
        'total': line.get('total'),
        'url': PRODUCT_URL + quote(line['slug']) if line.get('slug') else None,
    }


def read_cart(connection):
    """Поточний кошик для показу: товари, сума до сплати, попередження й посилання на оформлення. Лише читає; адресу не повертає."""
    reference = connection.call_tool('silpo_get_my_shopping_cart')
    if not reference.get('exists'):
        raise SilpoError(NO_STORE_MESSAGE)
    full = connection.call_tool('silpo_get_shopping_cart_by_id', shoppingCartId=reference['shoppingCartId'])
    cart = full['cart']
    calculation = cart.get('calculation') or {}
    lines = cart_lines(cart)

    problems = []
    for validation in calculation.get('validations') or []:
        if validation.get('level') in ('error', 'warning'):
            problems.append(problem_text(validation, lines))

    return {
        'lines': [cart_line_view(line) for line in lines],
        'total': calculation.get('totalAfterDiscounts'),
        'products_total': calculation.get('productsTotal'),
        'delivery_total': (calculation.get('delivery') or {}).get('total'),
        'slot': slot_label(cart['timeslot']) if (cart.get('timeslot') or {}).get('start') else None,
        'problems': list(dict.fromkeys(problems)),   # однакові попередження показуємо один раз
        'web_link': https_only(full.get('checkoutWebLink')),
        'mobile_link': https_only(full.get('checkoutMobileLink')),
        'site_url': SITE_URL,
    }
