import asyncio
import functools
import logging
import math
import time
import random
import re

from aiogram import Bot, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import Dispatcher, CancelHandler, ctx
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.executor import start_polling
from aiogram.utils import context
from aiogram.utils.exceptions import Throttled, MessageTextIsEmpty, BadRequest, MessageCantBeDeleted
from aiogram.utils.markdown import italic
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton, ContentType
import asyncpg

from bot import calculate_time, rate_limit
from bot.call_later import call_later
from bot.config import TOKEN, DB, MY_ID, MY_CHANNEL, BOT_ID
from bot.db import create_conn, gen_prepared_query
from bot.text_messages import text_messages, random_mess

log = logging.getLogger('aiogram')
logging.basicConfig(level=logging.INFO)

admins = 'creator', 'administrator'

loop = asyncio.get_event_loop()
storage = MemoryStorage()
bot = Bot(token=TOKEN, loop=loop, parse_mode=ParseMode.MARKDOWN)

dp = Dispatcher(bot, storage=storage)

conn = loop.run_until_complete(create_conn(**DB))  # Подключаемся к БД
prepared_query = loop.run_until_complete(gen_prepared_query(conn))  # Получаем подготовленые выражения


def set_privileges(privilege):
    """
    Декоратор для установки уровня доступа к функции.
    Так же он удаляет сообщения.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(message: types.Message):
            if privilege == 'administrator':  # Привелигия админ
                response = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if response.status in admins:
                    await func(message)
            elif privilege == 'creator':  # Привелигия создатель
                response = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if response.status == 'creator':
                    await func(message)
            elif privilege == MY_ID:  # Доступно только создателю бота
                await func(message)

        return wrapper

    return decorator


async def warn_do(message: types.Message, warn: dict):
    """
    Обработать предупреждения для пользователя.
    """
    parametres = warn['chat_id'], warn['user_id']
    res = await prepared_query['warn_select'].fetch(*parametres)
    # Записи нет в базе данных - создать
    if not res:
        # Выдать 1 предупреждение
        await prepared_query['warn_insert'].fetch(*parametres)
        await bot.send_message(message.chat.id,
                               text_messages['warn_notif'].format(warn['name'], warn['user_id'], 1))
    else:
        # Количество прошлых предупреждений увеличить на 1
        await prepared_query['warn_update'].fetch(*parametres)
        # Настройки предупреждений для чата
        warn_settings = (await prepared_query['get_warn_settings'].fetch(warn['chat_id']))[0]
        max_warn = warn_settings['max_warn']
        time_ban = warn_settings['time_ban']
        # Получить новое количество предупреждений пользователя
        warn_count = (await prepared_query['get_warn_count'].fetch(*parametres))[0]['warn_count']
        await bot.send_message(message.chat.id,
                               text_messages['warn_notif'].format(warn['name'], warn['user_id'], warn_count))
        # Превышение максимального количества предупреждений - забанить
        if warn_count >= max_warn:
            until = math.floor(time.time()) + time_ban * 60
            await bot.restrict_chat_member(message.chat.id, warn['user_id'],
                                           until_date=until,
                                           can_send_messages=False,
                                           can_send_media_messages=False,
                                           can_send_other_messages=False,
                                           can_add_web_page_previews=False)
            await bot.send_message(message.chat.id,
                                   text_messages['max_warning'].format(warn['name'], warn['user_id'], time_ban))
            # Очистить предупреждения для пользователя
            await prepared_query['warn_delete'].fetch(*parametres)


class CallbackAntiFlood(BaseMiddleware):

    def __init__(self):
        super(CallbackAntiFlood, self).__init__()

    @staticmethod
    async def on_pre_process_callback_query(call: types.CallbackQuery):
        """
        Этот обработчик вызывается, когда диспетчер получает обновление о нажатии кнопки
        """
        if call.message:
            if call.message.from_user:
                # Получить диспетчер из контекста
                dispatcher = ctx.get_dispatcher()

                # Использовать Dispatcher.throttle метод
                try:
                    await dispatcher.throttle('settings_callback', rate=0.5)
                except Throttled as throttled:
                    response = await bot.get_chat_member(call.message.chat.id, call.from_user.id)
                    if response.status not in admins:
                        
                        # Заблокировать
                        if throttled.exceeded_count <= 2:
                            name = call.from_user.full_name
                            user_id = call.from_user.id
                            await bot.kick_chat_member(call.message.chat.id, user_id,
                                                       until_date=math.floor(time.time()) + 10 * 60)
                            await bot.send_message(call.message.chat.id,
                                                   f'[{name}](tg://user?id={user_id}) заблокирован '
                                                   'на 10 минут за бездумное нажатие по кнопкам :).')
                        # Отменить текущий обработчик
                        raise CancelHandler()  


class WordsFilter(BaseMiddleware):

    @staticmethod
    async def on_pre_process_message(message):
        """
        Проверяет есть ли в тексте пользователя запрещенные слова.
        И обробатывает их в зависимости от настроек чата.
        """
        if message.text is not None:
            async with asyncpg.create_pool(**DB,
                                           command_timeout=60) as pool:
                async with pool.acquire() as con:
                    try:
                        res = (await con.fetch('SELECT mat_list, auto_warn FROM settings WHERE chat_id=$1', message.chat.id))[0]
                        auto_warn = res['auto_warn']
                        if auto_warn:
                            mat_list = res['mat_list']
                            forbidden_words = frozenset(mat_list.split(','))
                            # Разобрать текст пользователя на слова
                            user_words = frozenset(re.findall(r'\w+', message.text.lower()))
                            # Поиск совпадений
                            mes = forbidden_words & user_words
                            if mes:
                                await bot.delete_message(message.chat.id, message.message_id)
                                warn_list = {'chat_id': message.chat.id,
                                             'user_id': message.from_user.id,
                                             'name': message.from_user.full_name}
                                response = await bot.get_chat_member(message.chat.id, message.from_user.id)
                                if response.status in admins:
                                    await bot.send_message(message.chat.id, text_messages['warn_admin'])
                                else:
                                    await warn_do(message, warn_list)
                    except (AttributeError, IndexError):
                        return


class AntiFlood(BaseMiddleware):

    def __init__(self, limit=0.1, key_prefix='antiflood_'):
        self.rate_limit = limit
        self.prefix = key_prefix
        super(AntiFlood, self).__init__()

    async def on_process_message(self, message: types.Message):
        """
        Этот обработчик вызывается, когда диспетчер получает сообщение
        """
        # Получить текущий обработчик
        handler = context.get_value('handler')

        # Получить диспетчер из контекста
        dispatcher = ctx.get_dispatcher()

        # Если обработчик был настроен, получить ограничение скорости и ключ от обработчика
        if handler:
            limit = getattr(handler, 'throttling_rate_limit', self.rate_limit)
            key = getattr(handler, 'throttling_key', f"{self.prefix}_{handler.__name__}")
        else:
            limit = self.rate_limit
            key = f"{self.prefix}_message"

        # Использовать Dispatcher.throttle метод
        try:
            await dispatcher.throttle(key, rate=limit)
        except Throttled as t:
            response = await bot.get_chat_member(message.chat.id, message.from_user.id)
            if response.status not in admins:
                # Выполнять действия
                await self.message_throttled(message, t)

                # Отменить текущий обработчик
                raise CancelHandler()

    @staticmethod
    async def message_throttled(message: types.Message, throttled: Throttled):
        """
        Заблокировать пользователя за флуд и оповестить его
        """
        # Предотвратить флуд
        if throttled.exceeded_count <= 2:
            name = message.from_user.full_name
            user_id = message.from_user.id
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except MessageCantBeDeleted:
                return
            await bot.restrict_chat_member(message.chat.id, user_id,
                                           until_date=math.floor(time.time()) + 10 * 60,
                                           can_send_messages=False,
                                           can_send_media_messages=False,
                                           can_send_other_messages=False,
                                           can_add_web_page_previews=False)
            await bot.send_message(message.chat.id,
                                   f'[{name}](tg://user?id={user_id}) заблокирован'
                                   ' на 10 минут за попытку зафлудить меня.')


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def welcome(message: types.Message):
    """
    Если бота добавили в чат - показать сообщение и добавить чат в БД.
    Если в чат вступил пользователь показать приветствие(зависит от настроек чата)
    """
    # Слишком большая длинна имени - бан
    if len(message.new_chat_members[0].full_name) > 35:
        await bot.send_message(message.chat.id,
                               text_messages['long_name'].format(message.new_chat_members[0].username))
        await bot.kick_chat_member(message.chat.id, message.new_chat_members[0].id)
        await message.delete()
        return
    res = (await prepared_query['welcome_select'].fetch(message.chat.id))
    # Бота добавили в чат
    if message.new_chat_members[0].id == BOT_ID:
        await bot.send_message(message.chat.id, text_messages['admin_required'])
        # Создаем запись для настроек чата в БД
        try:
            await prepared_query['welcome_insert'].fetch(message.chat.id)
        except asyncpg.exceptions.UniqueViolationError:
            log.info(f'Запись {message.chat.id} уже существует в БД')
    # В чат вступил пользователь, проверяем настройки БД
    elif message.new_chat_members[0].id != BOT_ID and res:
        welcome_mes = res[0]['welcome_mes']
        if welcome_mes is not None:
            user_id = message.new_chat_members[0].id
            name = message.new_chat_members[0].full_name
            # Если в строке указан {name} - выполняем замену и форматирование строки
            if '{name}' in welcome_mes:
                welcome_mes = welcome_mes.replace('{name}', f'[{name}](tg://user?id={user_id})')
                welcome_mes = welcome_mes.replace('_', '\\_')
            await bot.send_message(message.chat.id, welcome_mes, disable_web_page_preview=True)


@dp.message_handler(func=lambda message: message.text.startswith('!pin'))
@rate_limit(2, 'pin')
@set_privileges('administrator')
async def pin(message: types.Message):
    """
    Закрепить сообщение в чате.
    """
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id, disable_notification=True)
    except AttributeError:
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_pin_syntax'])
        # Удалить сообщение об ошибке через время
        call_later(10, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!ban'))
@rate_limit(2, 'ban')
@set_privileges('administrator')
async def ban(message: types.Message):
    """
    Заблокировать пользователя.
    """
    try:
        name = message.reply_to_message.from_user.full_name
        user_id = message.reply_to_message.from_user.id
        if user_id == BOT_ID:  # Попытка забанить бота
            await bot.send_message(message.chat.id, random.choice(random_mess))
            return
        split_message = message.text.split()[1:]
        time_ban = split_message[0]
        time_calc = calculate_time(time_ban)
        # Указана причина и время бана - забанить на указаное время
        if len(split_message) >= 2 and time_ban[:-1].isdigit():
            cause = message.text.split(time_ban)[-1]
            until = math.floor(time.time()) + time_calc[0] * 60
            await bot.kick_chat_member(message.chat.id,
                                       message.reply_to_message.from_user.id,
                                       until_date=until)
            await bot.send_message(message.chat.id,
                                   f'[{name}](tg://user?id={user_id}) забанен на {str(time_calc[0])} {time_calc[1]}\n'
                                   f'Причина: {italic(cause)}.')
        # Указана причина бана - забанить навсегда
        elif not split_message[0][:-1].isdigit():
            cause = message.text[5:]
            await bot.kick_chat_member(message.chat.id, user_id)
            await bot.send_message(message.chat.id,
                                   f'[{name}](tg://user?id={user_id}) забанен навсегда.\n'
                                   f'Причина: {italic(cause)}.')
        # Указано только время бана - показать ошибку
        else:
            raise AttributeError
    except (AttributeError, IndexError, ValueError, TypeError):
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_ban_syntax'])
        call_later(15, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!mute'))
@rate_limit(2, 'mute')
@set_privileges('administrator')
async def mute(message: types.Message):
    """
    Запрещает отправлять сообщения пользователю.
    """
    try:
        user_id = message.reply_to_message.from_user.id
        if user_id == BOT_ID:
            await bot.send_message(message.chat.id, random.choice(random_mess))
            return
        time_mute = message.text.split()[1]
        time_calc = calculate_time(time_mute)
        until = math.floor(time.time()) + time_calc[0] * 60
        name = message.reply_to_message.from_user.full_name
        await bot.restrict_chat_member(message.chat.id, message.reply_to_message.from_user.id,
                                       until_date=until,
                                       can_send_messages=False,
                                       can_send_media_messages=False,
                                       can_send_other_messages=False,
                                       can_add_web_page_previews=False)
        await bot.send_message(message.chat.id,
                               f'[{name}](tg://user?id={user_id}) запрещено отправлять сообщения'
                               f' на {str(time_calc[0])} {time_calc[1]}')
    except (IndexError, ValueError, AttributeError, TypeError):
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_mute_syntax'])
        call_later(15, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!unmute'))
@rate_limit(2, 'unmute')
@set_privileges('administrator')
async def unmute(message: types.Message):
    """
    Снимает все ограничения с пользователя.
    """
    try:
        name = message.reply_to_message.from_user.full_name
        user_id = message.reply_to_message.from_user.id
        if user_id == BOT_ID:
            await bot.send_message(message.chat.id, random.choice(random_mess))
            return
        await bot.restrict_chat_member(message.chat.id, message.reply_to_message.from_user.id,
                                       can_send_messages=True,
                                       can_send_media_messages=True,
                                       can_send_other_messages=True,
                                       can_add_web_page_previews=True)
        await bot.send_message(message.chat.id, f'[{name}](tg://user?id={user_id}) разблокирован.')
    except (AttributeError, BadRequest):
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_unmute_syntax'])
        call_later(10, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!sd_ch'))
@rate_limit(2, 'sd_ch')
@set_privileges(MY_ID)
async def sd_ch(message: types.Message):
    """
    Отправляет сообщение в канал.
    """
    try:
        # Командой /sd_ch ответили на сообщение, то отправляем текст сообщения на которое ответили.
        if message.reply_to_message is not None:
            text = message.reply_to_message.text
        else:
            # Отправляемое в канал сообщение передано аргументом команде(/sd_ch text) - отправляем text.
            text = ' '.join(message.text.split()[1:])
        await bot.send_message(MY_CHANNEL, text)
        await bot.send_message(message.chat.id, text_messages['success_message'], disable_web_page_preview=True)
    except (IndexError, MessageTextIsEmpty):
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_sd_ch_syntax'])
        call_later(10, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!warn'))
@rate_limit(2, 'warn')
@set_privileges('administrator')
async def warn(message: types.Message):
    """
    Выдать предупреждение пользователю.
    """
    if message.reply_to_message.from_user.id == BOT_ID:
        await bot.send_message(message.chat.id, random.choice(random_mess))
        return
    try:
        warn_list = {'chat_id': message.chat.id,
                     'user_id': message.reply_to_message.from_user.id,
                     'name': message.reply_to_message.from_user.full_name}
    except AttributeError:
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_warn_syntax'])
        call_later(10, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)
    else:
        if (await bot.get_chat_member(message.chat.id, message.reply_to_message.from_user.id)).status in admins:
            await bot.send_message(message.chat.id, text_messages['warn_admin'])
        else:
            await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            await warn_do(message, warn_list)


@dp.message_handler(func=lambda message: message.text.startswith('!acquit'))
@rate_limit(2, 'acquit')
@set_privileges('administrator')
async def acquit(message: types.Message):
    """
    Снять все предупреждения с пользователю.
    """
    if message.reply_to_message.from_user.id == BOT_ID:
        await bot.send_message(message.chat.id, random.choice(random_mess))
        return
    try:
        name = message.reply_to_message.from_user.full_name
        user_id = message.reply_to_message.from_user.id
        await bot.send_message(message.chat.id, f'[{name}](tg://user?id={user_id}) больше не имеет предупреждений.')
        await prepared_query['warn_delete'].fetch(message.chat.id, user_id)
    except AttributeError:
        sent_m = await bot.send_message(message.chat.id, text_messages['wrong_acquit_syntax'])
        call_later(10, bot.delete_message, sent_m.chat.id, sent_m.message_id, loop=loop)


@dp.message_handler(func=lambda message: message.text.startswith('!settings'))
@rate_limit(2, 'settings')
@set_privileges('administrator')
async def settings(message: types.Message):
    """
    Отправить настройки чата.
    """
    res = (await prepared_query['get_settings'].fetch(message.chat.id))[0]

    # Настройки предупреждений
    inline = InlineKeyboardMarkup(row_width=4)
    warn_count = InlineKeyboardButton("Макс. warn'ов", callback_data='max_warn')
    plus = InlineKeyboardButton('-', callback_data='-val1')
    value1 = InlineKeyboardButton(res['max_warn'], callback_data='value1')
    minus = InlineKeyboardButton('+', callback_data='+val1')
    inline.add(warn_count, plus, value1, minus)

    # Настройки автоматических предупреждений
    auto = 'Включены' if res['auto_warn'] else 'Выключены'
    auto_warn = InlineKeyboardButton("Авто warn'ы", callback_data='auto_warn')
    value2 = InlineKeyboardButton(auto, callback_data='value2')
    inline.row(auto_warn, value2)

    welcome_bool = 'Включено' if res['welcome_mes'] else 'Выключено'
    welcome_mes = InlineKeyboardButton("Приветствие", callback_data='welcome')
    value3 = InlineKeyboardButton(welcome_bool, callback_data='value3')
    inline.row(welcome_mes, value3)

    inline.add(InlineKeyboardButton("Запрещенные слова", callback_data='mat_list'))

    inline.add(InlineKeyboardButton("Сообщение при вступлении в чат", callback_data='welcome_mes'))

    inline.add(InlineKeyboardButton("На сколько времени ограничивать, после максимума предупреждения",
                                    callback_data='time_ban'))

    await message.reply('Настройки чата:', reply_markup=inline)


@dp.callback_query_handler()
@rate_limit(0.5, 'settings_callback')
async def process_callback_settings(call: types.CallbackQuery):
    """
    Обработка нажатий на кнопки.
    """
    if call.from_user.id == call.message.reply_to_message.from_user.id:
        res = (await prepared_query['get_settings'].fetch(call.message.chat.id))[0]
        if call.data == '-val1':
            if res['max_warn']-1 < 1:
                await bot.answer_callback_query(call.id, text='Допустимые значения от 1 до 10', show_alert=True)
                return

            # Настройки предупреждений
            inline = InlineKeyboardMarkup(row_width=4)
            warn_count = InlineKeyboardButton("Макс. warn'ов", callback_data='max_warn')
            plus = InlineKeyboardButton('-', callback_data='-val1')
            value1 = InlineKeyboardButton(res['max_warn']-1, callback_data='value1')
            minus = InlineKeyboardButton('+', callback_data='+val1')
            inline.add(warn_count, plus, value1, minus)

            # Настройки автоматических предупреждений
            auto = 'Включены' if res['auto_warn'] else 'Выключены'
            auto_warn = InlineKeyboardButton("Авто warn'ы", callback_data='auto_warn')
            value2 = InlineKeyboardButton(auto, callback_data='value2')
            inline.row(auto_warn, value2)

            welcome_bool = 'Включено' if res['welcome_mes'] else 'Выключено'
            welcome_mes = InlineKeyboardButton("Приветствие", callback_data='welcome')
            value3 = InlineKeyboardButton(welcome_bool, callback_data='value3')
            inline.row(welcome_mes, value3)

            inline.add(InlineKeyboardButton("Запрещенные слова", callback_data='mat_list'))

            inline.add(InlineKeyboardButton("Сообщение при вступлении в чат", callback_data='welcome_mes'))

            inline.add(InlineKeyboardButton("На сколько времени ограничивать, после максимума предупреждения",
                                            callback_data='time_ban'))

            await conn.fetch('UPDATE settings SET max_warn=max_warn-1 WHERE chat_id=$1', call.message.chat.id)
            await bot.answer_callback_query(call.id)
            await bot.edit_message_reply_markup(call.message.chat.id,
                                                call.message.message_id,
                                                call.id,
                                                reply_markup=inline)
        elif call.data == '+val1':
            if res['max_warn']+1 > 10:
                await bot.answer_callback_query(call.id, text='Допустимые значения от 1 до 10', show_alert=True)
                return

            # Настройки предупреждений
            inline = InlineKeyboardMarkup(row_width=4)
            warn_count = InlineKeyboardButton("Макс. warn'ов", callback_data='max_warn')
            plus = InlineKeyboardButton('-', callback_data='-val1')
            value1 = InlineKeyboardButton(res['max_warn']+1, callback_data='value1')
            minus = InlineKeyboardButton('+', callback_data='+val1')
            inline.add(warn_count, plus, value1, minus)

            # Настройки автоматических предупреждений
            auto = 'Включены' if res['auto_warn'] else 'Выключены'
            auto_warn = InlineKeyboardButton("Авто warn'ы", callback_data='auto_warn')
            value2 = InlineKeyboardButton(auto, callback_data='value2')
            inline.row(auto_warn, value2)
            
            welcome_bool = 'Включено' if res['welcome_mes'] else 'Выключено'
            welcome_mes = InlineKeyboardButton("Приветствие", callback_data='welcome')
            value3 = InlineKeyboardButton(welcome_bool, callback_data='value3')
            inline.row(welcome_mes, value3)

            inline.add(InlineKeyboardButton("Запрещенные слова", callback_data='mat_list'))

            inline.add(InlineKeyboardButton("Сообщение при вступлении в чат", callback_data='welcome_mes'))

            inline.add(InlineKeyboardButton("На сколько времени ограничивать, после максимума предупреждения",
                                            callback_data='time_ban'))

            await conn.fetch('UPDATE settings SET max_warn=max_warn+1 WHERE chat_id=$1', call.message.chat.id)
            await bot.answer_callback_query(call.id)
            await bot.edit_message_reply_markup(call.message.chat.id,
                                                call.message.message_id,
                                                call.id,
                                                reply_markup=inline)
        elif call.data == 'value2':

            # Настройки предупреждений
            inline = InlineKeyboardMarkup(row_width=4)
            warn_count = InlineKeyboardButton("Макс. warn'ов", callback_data='max_warn')
            plus = InlineKeyboardButton('-', callback_data='-val1')
            value1 = InlineKeyboardButton(res['max_warn'], callback_data='value1')
            minus = InlineKeyboardButton('+', callback_data='+val1')
            inline.add(warn_count, plus, value1, minus)

            # Настройки автоматических предупреждений
            auto = 'Включены' if not res['auto_warn'] else 'Выключены'
            auto_warn = InlineKeyboardButton("Авто warn'ы", callback_data='auto_warn')
            value2 = InlineKeyboardButton(auto, callback_data='value2')
            inline.row(auto_warn, value2)

            welcome_bool = 'Включено' if res['welcome_mes'] else 'Выключено'
            welcome_mes = InlineKeyboardButton("Приветствие", callback_data='welcome')
            value3 = InlineKeyboardButton(welcome_bool, callback_data='value3')
            inline.row(welcome_mes, value3)

            inline.add(InlineKeyboardButton("Запрещенные слова", callback_data='mat_list'))

            inline.add(InlineKeyboardButton("Сообщение при вступлении в чат", callback_data='welcome_mes'))

            inline.add(InlineKeyboardButton("На сколько времени ограничивать, после максимума предупреждения",
                                            callback_data='time_ban'))

            await conn.fetch('UPDATE settings SET auto_warn=NOT auto_warn WHERE chat_id=$1', call.message.chat.id)
            await bot.answer_callback_query(call.id)
            await bot.edit_message_reply_markup(call.message.chat.id,
                                                call.message.message_id,
                                                call.id,
                                                reply_markup=inline)
        elif call.data == 'value3':

            # Настройки предупреждений
            inline = InlineKeyboardMarkup(row_width=4)
            warn_count = InlineKeyboardButton("Макс. warn'ов", callback_data='max_warn')
            plus = InlineKeyboardButton('-', callback_data='-val1')
            value1 = InlineKeyboardButton(res['max_warn'], callback_data='value1')
            minus = InlineKeyboardButton('+', callback_data='+val1')
            inline.add(warn_count, plus, value1, minus)

            # Настройки автоматических предупреждений
            auto = 'Включены' if res['auto_warn'] else 'Выключены'
            auto_warn = InlineKeyboardButton("Авто warn'ы", callback_data='auto_warn')
            value2 = InlineKeyboardButton(auto, callback_data='value2')
            inline.row(auto_warn, value2)

            welcome_bool = 'Включено' if not res['welcome_mes'] else 'Выключено'
            welcome_db = 'Привет, {name}' if not res['welcome_mes'] else None
            welcome_mes = InlineKeyboardButton("Приветствие", callback_data='welcome')
            value3 = InlineKeyboardButton(welcome_bool, callback_data='value3')
            inline.row(welcome_mes, value3)

            inline.add(InlineKeyboardButton("Запрещенные слова", callback_data='mat_list'))

            inline.add(InlineKeyboardButton("Сообщение при вступлении в чат", callback_data='welcome_mes'))

            inline.add(InlineKeyboardButton("На сколько времени ограничивать, после максимума предупреждения",
                                            callback_data='time_ban'))

            await conn.fetch('UPDATE settings SET welcome_mes=$1 WHERE chat_id=$2', welcome_db, call.message.chat.id)

            await bot.answer_callback_query(call.id)
            await bot.edit_message_reply_markup(call.message.chat.id,
                                                call.message.message_id,
                                                call.id,
                                                reply_markup=inline)
        elif call.data == 'mat_list':
            await call.message.reply(text_messages['get_mat_list'])
            state = dp.current_state(chat=call.message.chat.id, user=call.from_user.id)
            await state.set_state('WAITING_MAT_LIST')
        elif call.data == 'welcome_mes':
            await call.message.reply(text_messages['get_welcome_mes'])
            state = dp.current_state(chat=call.message.chat.id, user=call.from_user.id)
            await state.set_state('WAITING_WELCOME_MES')
        elif call.data == 'time_ban':
            await call.message.reply(text_messages['get_time_ban'])
            state = dp.current_state(chat=call.message.chat.id, user=call.from_user.id)
            await state.set_state('WAITING_TIME_BAN')
    else:
        await bot.answer_callback_query(call.id, text='Вы не админ или не вызывали настройки')


@dp.message_handler(state='*', commands=['cancel'])
@dp.message_handler(state='*', func=lambda message: message.text.lower() == 'cancel')
async def cancel_handler(message: types.Message):
    """
    Отменяет состояние получения файла.
    """
    with dp.current_state(chat=message.chat.id, user=message.from_user.id) as state:
        if await state.get_state() is None:
            return

        await state.reset_state(with_data=True)
        await message.reply('Отменено.')


@dp.message_handler(state='WAITING_MAT_LIST', content_types=ContentType.DOCUMENT)
async def process_mat_list(message: types.Message):
    """
    Ожидает файл с запрещенными словами и записывает их в БД.
    """
    with dp.current_state(chat=message.chat.id, user=message.from_user.id) as state:
        if message.document.file_size < 4554432 and message.document.file_name == 'mat-list':
            try:
                file = await bot.download_file_by_id(message.document.file_id)
                text = file.read().decode('utf-8').strip()
                await conn.execute("UPDATE settings SET mat_list=$1 WHERE chat_id=$2", text, message.chat.id)
                await bot.send_message(message.chat.id, f'Файл {message.document.file_name} получен и записан в БД.')
            except:
                await bot.send_message(message.chat.id, f'Ошибка при чтении или записи файла.')
        await state.finish()


@dp.message_handler(state='WAITING_WELCOME_MES', content_types=ContentType.TEXT)
async def process_welcome_mes(message: types.Message):
    """
    Ожидает сообщение с приветствием и записывает их в БД.
    """
    with dp.current_state(chat=message.chat.id, user=message.from_user.id) as state:
        await conn.execute("UPDATE settings SET welcome_mes=$1 WHERE chat_id=$2", message.text, message.chat.id)
        await bot.send_message(message.chat.id, 'Приветствие успешно записано в БД.')
        await state.finish()


@dp.message_handler(state='WAITING_TIME_BAN', content_types=ContentType.TEXT)
async def process_time_ban(message: types.Message):
    """
    Ожидает время бана и записывает их в БД.
    """
    with dp.current_state(chat=message.chat.id, user=message.from_user.id) as state:
        try:
            await conn.execute("UPDATE settings SET time_ban=$1 WHERE chat_id=$2", int(message.text), message.chat.id)
            await bot.send_message(message.chat.id, 'Время блокировки успешно записано в БД.')
        except:
            await bot.send_message(message.chat.id, 'Обнаружена ошибка.')
        await state.finish()


@dp.message_handler(func=lambda message: message.text.startswith('/'))
@rate_limit(1, 'command_filter')
async def command_filter(message: types.Message):
    """
    Фильтр комманд, которые бот не обрабатывает.
    За злоупотребление пользователями - бан.
    """
    await bot.delete_message(message.chat.id, message.message_id)


async def shutdown(dispatcher: Dispatcher):
    """
    Выполняется при выключении бота.
    """
    await conn.close()
    await dispatcher.storage.close()
    await dispatcher.storage.wait_closed()


if __name__ == '__main__':
    dp.middleware.setup(AntiFlood())
    dp.middleware.setup(CallbackAntiFlood())
    dp.middleware.setup(WordsFilter())
    start_polling(dp, loop=loop, on_shutdown=shutdown, skip_updates=True)
