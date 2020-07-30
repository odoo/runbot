import logging
from os import environ

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware

from odoo import http
from . import models, controllers

def delegate(self, attr):
    return getattr(self.app, attr)
SentryWsgiMiddleware.__getattr__ = delegate

def enable_sentry():
    logger = logging.getLogger('runbot_merge')

    dsn = environ.get('SENTRY_DSN')
    if not dsn:
        logger.info("No DSN found, skipping sentry...")
        return

    try:
        sentry_sdk.init(
            dsn,
            integrations=[
                # note: if the colorformatter is enabled, sentry gets lost
                # and classifies everything as errors because it fails to
                # properly classify levels as the colorformatter injects
                # the ANSI color codes right into LogRecord.levelname
                LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
            ]
        )
        http.root = SentryWsgiMiddleware(http.root)
    except Exception:
        logger.exception("DSN found, failed to enable sentry...")
    else:
        logger.info("DSN found, sentry enabled...")

def _check_citext(cr):
    cr.execute("select 1 from pg_extension where extname = 'citext'")
    if not cr.rowcount:
        try:
            cr.execute('create extension citext')
        except Exception:
            raise AssertionError("runbot_merge needs the citext extension")
