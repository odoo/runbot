from . import models, controllers

def _check_citext(env):
    env.cr.execute("select 1 from pg_extension where extname = 'citext'")
    if not env.cr.rowcount:
        try:
            env.cr.execute('create extension citext')
        except Exception:
            raise AssertionError("runbot_merge needs the citext extension")
