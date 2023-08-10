import logging
from os import environ

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware

from odoo import http
from odoo.addons.base.models.ir_cron import ir_cron
from odoo.http import WebRequest

from .exceptions import FastForwardError, MergeError, Unmergeable


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
        auto_session_tracking=False,
        # traces_sample_rate=1.0,
        integrations=[
            # note: if the colorformatter is enabled, sentry gets lost
            # and classifies everything as errors because it fails to
            # properly classify levels as the colorformatter injects
            # the ANSI color codes right into LogRecord.levelname
            LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
        ],
        before_send=event_filter,
        # apparently not in my version of the sdk
        # functions_to_trace = []
    )
    http.root = SentryWsgiMiddleware(http.root)
    instrument_odoo()

def instrument_odoo():
    """Monkeypatches odoo core to copy odoo metadata into sentry for more
    informative events
    """
    # add user to wsgi request context
    old_call_function = WebRequest._call_function
    def _call_function(self, *args, **kwargs):
        if self.uid:
            sentry_sdk.set_user({
                'id': self.uid,
                'email': self.env.user.email,
                'username': self.env.user.login,
            })
        else:
            sentry_sdk.set_user({'username': '<public>'})
        return old_call_function(self, *args, **kwargs)
    WebRequest._call_function = _call_function

    # create transaction for tracking crons, add user to that
    old_callback = ir_cron._callback
    def _callback(self, cron_name, server_action_id, job_id):
        sentry_sdk.start_transaction(name=f"cron {cron_name}")
        sentry_sdk.set_user({
            'id': self.env.user.id,
            'email': self.env.user.email,
            'username': self.env.user.login,
        })
        return old_callback(self, cron_name, server_action_id, job_id)
    ir_cron._callback = _callback

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


