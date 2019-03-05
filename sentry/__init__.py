import logging
from os import environ

import sentry_sdk
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
from sentry_sdk.integrations.logging import LoggingIntegration
from odoo import http

def load_sentry():
    sentry_sdk.init(
        environ['SENTRY_DSN'],
        integrations=[
            # note: if the colorformatter is enabled, sentry gets lost
            # and classifies everything as errors because it fails to
            # properly classify levels as the colorformatter injects
            # the ANSI color codes right into LogRecord.levelname
            LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
        ]
    )
    http.root = SentryWsgiMiddleware(http.root)
