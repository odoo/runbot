import pathlib

from odoo.tools.appdirs import user_cache_dir


def migrate(_cr, _version):
    # avoid needing to re-clone our repo unnecessarily
    pathlib.Path(user_cache_dir('forwardport')).rename(
        pathlib.Path(user_cache_dir('mergebot')))
