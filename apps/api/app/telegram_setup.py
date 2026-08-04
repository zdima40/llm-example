import asyncio

from .config import get_settings
from .telegram_bot import configure_telegram


async def _main() -> None:
    await configure_telegram(get_settings())
    print("Telegram webhook and menu button configured")


if __name__ == "__main__":
    asyncio.run(_main())

