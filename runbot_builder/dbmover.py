#!/usr/bin/python3
import argparse
import contextlib
import logging
import psycopg2
import os
import re
import shutil
import sys

from collections import defaultdict
from logging.handlers import WatchedFileHandler

LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger('odoo.addons.runbot').setLevel(logging.DEBUG)
logging.addLevelName(25, "!NFO")

_logger = logging.getLogger(__name__)

DBRE = r'^(?P<build_id>\d+)-.+-[0-9a-f]{6}-?(?P<db_suffix>.*)$'


@contextlib.contextmanager
def local_pgadmin_cursor():
    cnx = None
    try:
        cnx = psycopg2.connect("dbname=postgres")
        cnx.autocommit = True  # required for admin commands
        yield cnx.cursor()
    finally:
        if cnx:
            cnx.close()


def list_local_dbs():
    with local_pgadmin_cursor() as local_cr:
        local_cr.execute("""
            SELECT datname
                FROM pg_database
                WHERE pg_get_userbyid(datdba) = current_user
        """)
        return [d[0] for d in local_cr.fetchall()]


def _local_pg_rename_db(dbname, new_db_name):
    with local_pgadmin_cursor() as local_cr:
        pid_col = 'pid' if local_cr.connection.server_version >= 90200 else 'procpid'
        query = 'SELECT pg_terminate_backend({}) FROM pg_stat_activity WHERE datname=%s'.format(pid_col)
        local_cr.execute(query, [dbname])
        local_cr.execute("ALTER DATABASE \"%s\" RENAME TO \"%s\";" % (dbname, new_db_name))


class RunbotClient():

    def __init__(self, env):
        self.env = env

    def rename_build_dirs(self, args):
        builds_root = os.path.join(self.env['runbot.runbot']._root(), 'build')
        builds_backup_root = os.path.join(self.env['runbot.runbot']._root(), 'build-backup')
        if not args.dry_run:
            try:
                _logger.info('Backup build dir in "%s"', builds_backup_root)
                shutil.copytree(builds_root, builds_backup_root, copy_function=os.link)
            except FileExistsError:
                _logger.info('Backup path "%s" already exists, skipping', builds_backup_root)

        build_dirs = {}
        leftovers = []
        for dir_name in os.listdir(builds_root):
            match = re.match(DBRE, dir_name)
            if match and match['db_suffix'] == '':
                build_dirs[match['build_id']] = dir_name
            else:
                leftovers.append(dir_name)

        for build in self.env['runbot.build'].search([('id', 'in', list(build_dirs.keys()))]):
            origin_dir = build_dirs[str(build.id)]
            origin_path = os.path.join(builds_root, origin_dir)
            if origin_dir == build.dest:
                _logger.info('Skip moving %s, already moved', build.dest)
                continue
            _logger.info('Moving "%s" --> "%s"', origin_dir, build.dest)
            if args.dry_run:
                continue
            dest_path = os.path.join(builds_root, build.dest)
            os.rename(origin_path, dest_path)

        for leftover in leftovers:
            _logger.info("leftover: %s", leftover)

    def rename_databases(self, args):
        total_db = 0
        db_names = defaultdict(dict)
        leftovers = []
        for local_db_name in list_local_dbs():
            match = re.match(DBRE, local_db_name)
            if match and match['db_suffix'] != '':
                db_names[match['build_id']][match['db_suffix']] = local_db_name
            else:
                leftovers.append(local_db_name)
            total_db += 1

        nb_matching = 0
        ids = [int(i) for i in db_names.keys()]
        builds = self.env['runbot.build'].search([('id', 'in', ids)])
        for build in builds:
            for suffix in db_names[str(build.id)].keys():
                origin_name = db_names[str(build.id)][suffix]
                dest_name = "%s-%s" % (build.dest, suffix)
                nb_matching += 1
                _logger.info('Renaming database "%s" --> "%s"', origin_name, dest_name)
                if args.dry_run:
                    continue
                _local_pg_rename_db(origin_name, dest_name)

        _logger.info("Found %s databases", total_db)
        _logger.info("Found %s matching databases", nb_matching)
        _logger.info("Leftovers: %s", len(leftovers))
        _logger.info("Builds not found : %s", len(set(ids) - set(builds.ids)))


def run():
    # parse args
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db_host', default='127.0.0.1')
    parser.add_argument('--db_port', default='5432')
    parser.add_argument('--db_user')
    parser.add_argument('--db_password')
    parser.add_argument('-d', '--database', default='runbot_upgrade', help='name of runbot db')
    parser.add_argument('--logfile', default=False)
    parser.add_argument('-n', '--dry-run', action='store_true')
    args = parser.parse_args()
    if args.logfile:
        dirname = os.path.dirname(args.logfile)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname)

        handler = WatchedFileHandler(args.logfile)
        formatter = logging.Formatter(LOG_FORMAT)
        handler.setFormatter(formatter)
        _logger.parent.handlers.clear()
        _logger.parent.addHandler(handler)

    # configure odoo
    sys.path.append(args.odoo_path)
    import odoo
    _logger.info("Starting upgrade move script using database %s", args.database)
    odoo.tools.config['db_host'] = args.db_host
    odoo.tools.config['db_port'] = args.db_port
    odoo.tools.config['db_user'] = args.db_user
    odoo.tools.config['db_password'] = args.db_password
    addon_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
    config_addons_path = odoo.tools.config['addons_path']
    odoo.tools.config['addons_path'] = ','.join([config_addons_path, addon_path])

    # create environment
    registry = odoo.registry(args.database)
    with odoo.api.Environment.manage():
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            runbot_client = RunbotClient(env)
            runbot_client.rename_build_dirs(args)
            runbot_client.rename_databases(args)


if __name__ == '__main__':
    run()
    _logger.info("All done")
