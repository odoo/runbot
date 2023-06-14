from . import models, controllers
from .sentry import enable_sentry

def _check_citext(cr):
    cr.execute("select 1 from pg_extension where extname = 'citext'")
    if not cr.rowcount:
        try:
            cr.execute('create extension citext')
        except Exception:
            raise AssertionError("runbot_merge needs the citext extension")
