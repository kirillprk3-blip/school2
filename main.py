import asyncio
import logging
import signal

from aiogram import Bot as TgBot, Dispatcher
from vkbottle.bot import Bot as VkBot

from config import config
from database import init_db
from router import MessageRouter
from tg_handlers import create_bot, create_dispatcher, set_message_router, set_vk_community_url
from vk_listener import VKListener

logger = logging.getLogger(__name__)


async def run_telegram_polling(bot: TgBot, dp: Dispatcher) -> None:
    """Запускает aiogram polling с защитным переподключением.

    aiogram сам ловит TelegramNetworkError внутри start_polling и
    переподключается с backoff. Здесь добавлен внешний retry-контур на
    случай исключения, которое всё же вылетит наружу (например, обрыв
    соединения на уровне транспорта), чтобы весь бот не падал целиком.
    """
    delay = 3
    max_delay = 60

    while True:
        try:
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Telegram polling упал с ошибкой: %s. Переподключение через %sс...",
                e, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def run_vk_polling(vk_bot: VkBot) -> None:
    """Собственный цикл поллинга vkbottle с защитным переподключением.

    `vk_bot.run_polling()` не используется напрямую: внутри он либо
    блокирующе крутит свой loop_wrapper (`run_until_complete` внутри уже
    запущенного event loop - RuntimeError), либо требует ручной пометки
    loop_wrapper как "running". Чтобы безопасно жить внутри нашего
    `asyncio.gather()`, цикл собран вручную поверх низкоуровневых
    `vk_bot.polling.listen()` / `vk_bot.router.route()` - тех же примитивов,
    которые `run_polling()` использует под капотом.
    """
    delay = 3
    max_delay = 60

    while True:
        try:
            async for event in vk_bot.polling.listen():
                for update in event.get("updates", []):
                    await vk_bot.router.route(update, vk_bot.polling.api)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "VK polling упал с ошибкой: %s. Переподключение через %sс...",
                e, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    await init_db()
    logger.info("База данных инициализирована.")

    tg_bot = create_bot()
    vk_bot: VkBot | None = None

    try:
        dp = create_dispatcher()

        tg_bot_info = await tg_bot.get_me()
        tg_bot_username = tg_bot_info.username
        logger.info("Telegram-бот: @%s", tg_bot_username)

        vk_bot = VkBot(token=config.vk_group_token)
        vk_bot.polling.group_id = config.vk_group_id

        group_info = None
        try:
            group_info = await vk_bot.api.groups.get_by_id(group_id=config.vk_group_id)
        except Exception as e:
            logger.warning("Не удалось получить screen_name сообщества VK: %s", e)

        if group_info and group_info.groups:
            vk_community_url = f"https://vk.com/{group_info.groups[0].screen_name}"
        else:
            vk_community_url = f"https://vk.com/public{config.vk_group_id}"
        set_vk_community_url(vk_community_url)

        message_router = MessageRouter(tg_bot=tg_bot, vk_bot=vk_bot)
        set_message_router(message_router)

        async def _notify_verified(tg_user_id: int, vk_full_name: str, vk_profile_url: str) -> None:
            try:
                await tg_bot.send_message(
                    tg_user_id,
                    f"Готово! Твоя страница VK подтверждена: {vk_full_name} "
                    f"(https://{vk_profile_url}).\n\nТеперь можно пользоваться ботом - напиши /start.",
                )
            except Exception as e:
                logger.warning("Не удалось уведомить TG-пользователя %s о верификации: %s", tg_user_id, e)

        vk_listener = VKListener(
            bot=vk_bot,
            tg_bot_username=tg_bot_username,
            on_bridged_message=message_router.handle_vk_message,
            on_edit_message=message_router.handle_vk_edit,
            on_vk_verified=_notify_verified,
        )

        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()

        def _request_shutdown() -> None:
            logger.info("Получен сигнал остановки, начинаю graceful shutdown...")
            vk_bot.polling.stop = True
            if main_task is not None:
                main_task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except NotImplementedError:
                # Например, Windows - падаем обратно на KeyboardInterrupt в __main__
                pass

        logger.info("Запуск Telegram polling и VK LongPoll listener...")
        await asyncio.gather(
            run_telegram_polling(tg_bot, dp),
            run_vk_polling(vk_bot),
        )
    except asyncio.CancelledError:
        logger.info("Задачи отменены, выполняю остановку.")
    finally:
        # tg_bot всегда создан к этому месту; vk_bot мог не успеть
        # создаться, если упало раньше (например, tg_bot.get_me() с
        # неверным токеном) - закрываем то, что реально было открыто.
        if vk_bot is not None:
            vk_bot.polling.stop = True
        await tg_bot.session.close()
        if vk_bot is not None:
            await vk_bot.api.http_client.close()
        logger.info("Соединения TG-бота и VK-бота закрыты.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Остановлено пользователем (KeyboardInterrupt).")
