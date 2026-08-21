"""Entry point for the telegram-worker service.

Runs the MTProto WorkerApp: persistent Telethon clients for every active
TelegramAccount across all tenants + the internal send/control/health
HTTP API on port 8010. See social_integrations/services/telegram_worker.py.
"""
import asyncio
import logging

from django.core.management.base import BaseCommand

from social_integrations.services.telegram_worker import WorkerApp

logger = logging.getLogger('telegram_worker')


class Command(BaseCommand):
    help = 'Run the Telegram MTProto listener (long-running worker service)'

    def handle(self, *args, **options):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s %(name)s %(message)s',
        )
        logger.info('starting telegram-worker')
        asyncio.run(WorkerApp().run())
