import logging
from os import environ

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware

from odoo import http
from runbot_merge.exceptions import FastForwardError, Mismatch, MergeError, Unmergeable


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
        setup_sentry(dsn)
    except Exception:
        logger.exception("DSN found, failed to enable sentry...")
    else:
        logger.info("DSN found, sentry enabled...")


def setup_sentry(dsn):
    sentry_sdk.init(
        dsn,
        traces_sample_rate=1.0,
        integrations=[
            # note: if the colorformatter is enabled, sentry gets lost
            # and classifies everything as errors because it fails to
            # properly classify levels as the colorformatter injects
            # the ANSI color codes right into LogRecord.levelname
            LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
        ],
        before_send=event_filter,
    )
    http.root = SentryWsgiMiddleware(http.root)

dummy_record = logging.LogRecord(name="", level=logging.NOTSET, pathname='', lineno=0, msg='', args=(), exc_info=None)
# mapping of exception types to predicates, if the predicate returns `True` the
# exception event should be suppressed
SUPPRESS_EXCEPTION = {
    # Someone else deciding to push directly to the branch (which is generally
    # what leads to this error) is not really actionable.
    #
    # Other possibilities are more structural and thus we probably want to know:
    # - other 422 Unprocessable github errors (likely config issues):
    #   - reference does not exist
    #   - object does not exist
    #   - object is not a commit
    #   - branch protection issue
    # - timeout on ref update (github probably dying)
    # - other HTTP error (also github probably dying)
    #
    # might be worth using richer exceptions to make this clearer, and easier to classify
    FastForwardError: lambda e: 'not a fast forward' in str(e.__cause__),
    # Git conflict when merging (or non-json response which is weird),
    # notified on PR
    MergeError: lambda _: True,
    # Failed preconditions on merging, notified on PR
    Unmergeable: lambda _: True,
}
def event_filter(event, hint):
    # event['level'], event['logger'], event['logentry'], event['exception']
    # known hints: log_record: LogRecord, exc_info: (type, BaseExeption, Traceback) | None
    exc_info = hint.get('exc_info') or hint.get('log_record', dummy_record).exc_info
    if exc_info:
        etype, exc, _ = exc_info
        if SUPPRESS_EXCEPTION.get(etype, lambda _: False)(exc):
            return None


