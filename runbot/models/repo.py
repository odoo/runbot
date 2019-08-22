# -*- coding: utf-8 -*-
import datetime
import dateutil
import json
import logging
import os
import random
import re
import requests
import signal
import subprocess
import time
import glob
import shutil

from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT
from odoo import models, fields, api, registry
from odoo.modules.module import get_module_resource
from odoo.tools import config
from ..common import fqdn, dt2time, Commit
from psycopg2.extensions import TransactionRollbackError
_logger = logging.getLogger(__name__)

class HashMissingException(Exception):
    pass

class ArchiveFailException(Exception):
    pass

class runbot_repo(models.Model):

    _name = "runbot.repo"

    name = fields.Char('Repository', required=True)
    short_name = fields.Char('Repository', compute='_compute_short_name', store=False, readonly=True)
    sequence = fields.Integer('Sequence')
    path = fields.Char(compute='_get_path', string='Directory', readonly=True)
    base = fields.Char(compute='_get_base_url', string='Base URL', readonly=True)  # Could be renamed to a more explicit name like base_url
    nginx = fields.Boolean('Nginx')
    mode = fields.Selection([('disabled', 'Disabled'),
                             ('poll', 'Poll'),
                             ('hook', 'Hook')],
                            default='poll',
                            string="Mode", required=True, help="hook: Wait for webhook on /runbot/hook/<id> i.e. github push event")
    hook_time = fields.Float('Last hook time')
    get_ref_time = fields.Float('Last refs db update')
    duplicate_id = fields.Many2one('runbot.repo', 'Duplicate repo', help='Repository for finding duplicate builds')
    modules = fields.Char("Modules to install", help="Comma-separated list of modules to install and test.")
    modules_auto = fields.Selection([('none', 'None (only explicit modules list)'),
                                     ('repo', 'Repository modules (excluding dependencies)'),
                                     ('all', 'All modules (including dependencies)')],
                                    default='all',
                                    string="Other modules to install automatically")

    dependency_ids = fields.Many2many(
        'runbot.repo', 'runbot_repo_dep_rel', column1='dependant_id', column2='dependency_id',
        string='Extra dependencies',
        help="Community addon repos which need to be present to run tests.")
    token = fields.Char("Github token", groups="runbot.group_runbot_admin")
    group_ids = fields.Many2many('res.groups', string='Limited to groups')

    repo_config_id = fields.Many2one('runbot.build.config', 'Run Config')
    config_id = fields.Many2one('runbot.build.config', 'Run Config', compute='_compute_config_id', inverse='_inverse_config_id')

    server_files = fields.Char('Server files', help='Comma separated list of possible server files')  # odoo-bin,openerp-server,openerp-server.py
    manifest_files = fields.Char('Addons files', help='Comma separated list of possible addons files', default='__manifest__.py')
    addons_paths = fields.Char('Addons files', help='Comma separated list of possible addons path', default='')

    def _compute_config_id(self):
        for repo in self:
            if repo.repo_config_id:
                repo.config_id = repo.repo_config_id
            else:
                repo.config_id = self.env.ref('runbot.runbot_build_config_default')

    def _inverse_config_id(self):
        for repo in self:
            repo.repo_config_id = repo.config_id

    def _root(self):
        """Return root directory of repository"""
        default = os.path.join(os.path.dirname(__file__), '../static')
        return os.path.abspath(default)

    def _source_path(self, sha, *path):
        """
        returns the absolute path to the source folder of the repo (adding option *path)
        """
        self.ensure_one()
        return os.path.join(self._root(), 'sources', self._get_repo_name_part(), sha, *path)

    @api.depends('name')
    def _get_path(self):
        """compute the server path of repo from the name"""
        root = self._root()
        for repo in self:
            repo.path = os.path.join(root, 'repo', repo._sanitized_name(repo.name))

    @api.model
    def _sanitized_name(self, name):
        for i in '@:/':
            name = name.replace(i, '_')
        return name

    @api.depends('name')
    def _get_base_url(self):
        for repo in self:
            name = re.sub('.+@', '', repo.name)
            name = re.sub('^https://', '', name)  # support https repo style
            name = re.sub('.git$', '', name)
            name = name.replace(':', '/')
            repo.base = name

    @api.depends('name', 'base')
    def _compute_short_name(self):
        for repo in self:
            repo.short_name = '/'.join(repo.base.split('/')[-2:])

    def _get_repo_name_part(self):
        self.ensure_one()
        return self._sanitized_name(self.name.split('/')[-1])

    def _git(self, cmd):
        """Execute a git command 'cmd'"""
        self.ensure_one()
        _logger.debug("git command: git (dir %s) %s", self.short_name, ' '.join(cmd))
        cmd = ['git', '--git-dir=%s' % self.path] + cmd
        return subprocess.check_output(cmd).decode('utf-8')

    def _git_rev_parse(self, branch_name):
        return self._git(['rev-parse', branch_name]).strip()

    def _git_export(self, sha):
        """Export a git repo into a sources"""
        # TODO add automated tests
        self.ensure_one()
        export_path = self._source_path(sha)

        if os.path.isdir(export_path):
            _logger.info('git export: checkouting to %s (already exists)' % export_path)
            return export_path

        if not self._hash_exists(sha):
            self._update(force=True)
        if not self._hash_exists(sha):
            try:
                result = self._git(['fetch', 'origin', sha])
            except:
                pass
        if not self._hash_exists(sha):
            raise HashMissingException()

        _logger.info('git export: checkouting to %s (new)' % export_path)
        os.makedirs(export_path)

        p1 = subprocess.Popen(['git', '--git-dir=%s' % self.path, 'archive', sha], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '-xmC', export_path], stdin=p1.stdout, stdout=subprocess.PIPE)
        p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
        (out, err) = p2.communicate()
        if err:
            raise ArchiveFailException(err)

        # TODO get result and fallback on cleaing in case of problem
        return export_path

    def _hash_exists(self, commit_hash):
        """ Verify that a commit hash exists in the repo """
        self.ensure_one()
        try:
            self._git(['cat-file', '-e', commit_hash])
        except subprocess.CalledProcessError:
            return False
        return True

    def _github(self, url, payload=None, ignore_errors=False, nb_tries=2):
        """Return a http request to be sent to github"""
        for repo in self:
            if not repo.token:
                return
            match_object = re.search('([^/]+)/([^/]+)/([^/.]+(.git)?)', repo.base)
            if match_object:
                url = url.replace(':owner', match_object.group(2))
                url = url.replace(':repo', match_object.group(3))
                url = 'https://api.%s%s' % (match_object.group(1), url)
                session = requests.Session()
                session.auth = (repo.token, 'x-oauth-basic')
                session.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
                try_count = 0
                while try_count < nb_tries:
                    try:
                        if payload:
                            response = session.post(url, data=json.dumps(payload))
                        else:
                            response = session.get(url)
                        response.raise_for_status()
                        if try_count > 0:
                            _logger.info('Success after %s tries' % (try_count + 1))
                        return response.json()
                    except Exception as e:
                        try_count += 1
                        if try_count < nb_tries:
                            time.sleep(2)
                        else:
                            if ignore_errors:
                                _logger.exception('Ignored github error %s %r (try %s/%s)' % (url, payload, try_count + 1, nb_tries))
                            else:
                                raise

    def _get_fetch_head_time(self):
        self.ensure_one()
        fname_fetch_head = os.path.join(self.path, 'FETCH_HEAD')
        if os.path.exists(fname_fetch_head):
            return os.path.getmtime(fname_fetch_head)

    def _get_refs(self):
        """Find new refs
        :return: list of tuples with following refs informations:
        name, sha, date, author, author_email, subject, committer, committer_email
        """
        self.ensure_one()

        get_ref_time = self._get_fetch_head_time()
        if not self.get_ref_time or get_ref_time > self.get_ref_time:
            self.get_ref_time = get_ref_time
            fields = ['refname', 'objectname', 'committerdate:iso8601', 'authorname', 'authoremail', 'subject', 'committername', 'committeremail']
            fmt = "%00".join(["%(" + field + ")" for field in fields])
            git_refs = self._git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
            git_refs = git_refs.strip()
            return [tuple(field for field in line.split('\x00')) for line in git_refs.split('\n')]
        else:
            return []

    def _find_or_create_branches(self, refs):
        """Parse refs and create branches that does not exists yet
        :param refs: list of tuples returned by _get_refs()
        :return: dict {branch.name: branch.id}
        The returned structure contains all the branches from refs newly created
        or older ones.
        """
        Branch = self.env['runbot.branch']
        self.env.cr.execute("""
            WITH t (branch) AS (SELECT unnest(%s))
          SELECT t.branch, b.id
            FROM t LEFT JOIN runbot_branch b ON (b.name = t.branch)
           WHERE b.repo_id = %s;
        """, ([r[0] for r in refs], self.id))
        ref_branches = {r[0]: r[1] for r in self.env.cr.fetchall()}

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            if not ref_branches.get(name):
                _logger.debug('repo %s found new branch %s', self.name, name)
                new_branch = Branch.create({'repo_id': self.id, 'name': name})
                ref_branches[name] = new_branch.id
        return ref_branches

    def _find_new_commits(self, refs, ref_branches):
        """Find new commits in bare repo
        :param refs: list of tuples returned by _get_refs()
        :param ref_branches: dict structure {branch.name: branch.id}
                             described in _find_or_create_branches
        """
        self.ensure_one()
        Branch = self.env['runbot.branch']
        Build = self.env['runbot.build']
        icp = self.env['ir.config_parameter']
        max_age = int(icp.get_param('runbot.runbot_max_age', default=30))

        self.env.cr.execute("""
            WITH t (build, branch_id) AS (SELECT unnest(%s), unnest(%s))
          SELECT b.name, b.branch_id
            FROM t LEFT JOIN runbot_build b ON (b.name = t.build) AND (b.branch_id = t.branch_id)
        """, ([r[1] for r in refs], [ref_branches[r[0]] for r in refs]))
        # generate a set of tuples (branch_id, sha)
        builds_candidates = {(r[1], r[0]) for r in self.env.cr.fetchall()}

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            branch = Branch.browse(ref_branches[name])

            # skip the build for old branches (Could be checked before creating the branch in DB ?)
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(days=max_age) < datetime.datetime.now():
                continue

            # create build (and mark previous builds as skipped) if not found
            if not (branch.id, sha) in builds_candidates:
                if branch.no_auto_build or branch.no_build:
                    continue
                _logger.debug('repo %s branch %s new build found revno %s', self.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }
                if not branch.sticky:
                    # pending builds are skipped as we have a new ref
                    builds_to_skip = Build.search(
                        [('branch_id', '=', branch.id), ('local_state', '=', 'pending')],
                        order='sequence asc')
                    builds_to_skip._skip(reason='New ref found')
                    if builds_to_skip:
                        build_info['sequence'] = builds_to_skip[0].sequence
                    # testing builds are killed
                    builds_to_kill = Build.search([
                        ('branch_id', '=', branch.id),
                        ('local_state', '=', 'testing'),
                        ('committer', '=', committer)
                    ])
                    for btk in builds_to_kill:
                        btk._log('repo._update_git', 'Build automatically killed, newer build found.', level='WARNING')
                    builds_to_kill.write({'requested_action': 'deathrow'})

                new_build = Build.create(build_info)
                # create a reverse dependency build if needed
                if branch.sticky:
                    for rev_repo in self.search([('dependency_ids', 'in', self.id)]):
                        # find the latest build with the same branch name
                        latest_rev_build = Build.search([('build_type', '=', 'normal'), ('hidden', '=', 'False'), ('repo_id.id', '=', rev_repo.id), ('branch_id.branch_name', '=', branch.branch_name)], order='id desc', limit=1)
                        if latest_rev_build:
                            _logger.debug('Reverse dependency build %s forced in repo %s by commit %s', latest_rev_build.dest, rev_repo.name, sha[:6])
                            indirect = latest_rev_build._force(message='Rebuild from dependency %s commit %s' % (self.name, sha[:6]))
                            if not indirect:
                                _logger.exception('Failed to create indirect for %s from %s in repo %s', new_build, latest_rev_build, rev_repo)
                            else:
                                indirect.build_type = 'indirect'
                                new_build.revdep_build_ids += indirect

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', self.id), ('local_state', '=', 'pending')]
        icp = self.env['ir.config_parameter']
        running_max = int(icp.get_param('runbot.runbot_running_max', default=75))
        builds_to_be_skipped = Build.search(skippable_domain, order='sequence desc', offset=running_max)
        builds_to_be_skipped._skip()

    @api.multi
    def _create_pending_builds(self):
        """ Find new commits in physical repos"""
        refs = {}
        ref_branches = {}
        for repo in self:
            try:
                ref = repo._get_refs()
                max_age = int(self.env['ir.config_parameter'].get_param('runbot.runbot_max_age', default=30))
                good_refs = [r for r in ref if dateutil.parser.parse(r[2][:19]) + datetime.timedelta(days=max_age) > datetime.datetime.now()]
                if good_refs:
                    refs[repo] = good_refs
            except Exception:
                _logger.exception('Fail to get refs for repo %s', repo.name)
            if repo in refs:
                ref_branches[repo] = repo._find_or_create_branches(refs[repo])

        # keep _find_or_create_branches separated from build creation to ease
        # closest branch detection
        for repo in self:
            if repo in refs:
                repo._find_new_commits(refs[repo], ref_branches[repo])

    def _clone(self):
        """ Clone the remote repo if needed """
        self.ensure_one()
        repo = self
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            _logger.info("Cloning repository '%s' in '%s'" % (repo.name, repo.path))
            subprocess.call(['git', 'clone', '--bare', repo.name, repo.path])

    def _update_git(self, force):
        """ Update the git repo on FS """
        self.ensure_one()
        repo = self
        _logger.debug('repo %s updating branches', repo.name)

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        self._clone()

        # check for mode == hook
        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if not force and os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook' and (not repo.hook_time or repo.hook_time < fetch_time):
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                            repo.name, int(t0 - fetch_time), int(t0 - repo.hook_time) if repo.hook_time else 'never')
                return

        self._update_fetch_cmd()

    def _update_fetch_cmd(self):
        # Extracted from update_git to be easily overriden in external module
        self.ensure_one()
        repo = self
        repo._git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*', '+refs/pull/*/head:refs/pull/*'])

    @api.multi
    def _update(self, force=True):
        """ Update the physical git reposotories on FS"""
        for repo in reversed(self):
            try:
                repo._update_git(force)
            except Exception:
                _logger.exception('Fail to update repo %s', repo.name)

    @api.multi
    def _scheduler(self, host=None):
        """Schedule builds for the repository"""
        ids = self.ids
        if not ids:
            return
        icp = self.env['ir.config_parameter']
        host = host or self.env['runbot.host']._get_current()
        workers = host.get_nb_worker()
        running_max = int(icp.get_param('runbot.runbot_running_max', default=75))
        assigned_only = host.assigned_only

        Build = self.env['runbot.build']
        domain = [('repo_id', 'in', ids)]
        domain_host = domain + [('host', '=', host.name)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        build_ids = Build.search(domain_host + ['|', ('local_state', 'in', ['testing', 'running']), ('requested_action', 'in', ['wake_up', 'deathrow'])])
        build_ids._schedule()
        self.env.cr.commit()
        self.invalidate_cache()

        # launch new tests

        nb_testing = Build.search_count(domain_host + [('local_state', '=', 'testing')])
        available_slots = workers - nb_testing
        reserved_slots = Build.search_count(domain_host + [('local_state', '=', 'pending')])
        assignable_slots = (available_slots - reserved_slots) if not assigned_only else 0
        if available_slots > 0:
            if assignable_slots > 0:  # note: slots have been addapt to be able to force host on pending build. Normally there is no pending with host.
                # commit transaction to reduce the critical section duration
                def allocate_builds(where_clause, limit):
                    self.env.cr.commit()
                    self.invalidate_cache()
                    # self-assign to be sure that another runbot instance cannot self assign the same builds
                    query = """UPDATE
                                    runbot_build
                                SET
                                    host = %%(host)s
                                WHERE
                                    runbot_build.id IN (
                                        SELECT runbot_build.id
                                        FROM runbot_build
                                        LEFT JOIN runbot_branch
                                        ON runbot_branch.id = runbot_build.branch_id
                                        WHERE
                                            runbot_build.repo_id IN %%(repo_ids)s
                                            AND runbot_build.local_state = 'pending'
                                            AND runbot_build.host IS NULL
                                            %s
                                        ORDER BY
                                            array_position(array['normal','rebuild','indirect','scheduled']::varchar[], runbot_build.build_type) ASC,
                                            runbot_branch.sticky DESC,
                                            runbot_branch.priority DESC,
                                            runbot_build.sequence ASC
                                        FOR UPDATE OF runbot_build SKIP LOCKED
                                        LIMIT %%(limit)s
                                    )
                                RETURNING id""" % where_clause

                    self.env.cr.execute(query, {'repo_ids': tuple(ids), 'host': host.name, 'limit': limit})
                    return self.env.cr.fetchall()

                allocated = allocate_builds("""AND runbot_build.build_type != 'scheduled'""", assignable_slots)
                if allocated:
                    _logger.debug('Normal builds %s where allocated to runbot' % allocated)
                weak_slot = assignable_slots - len(allocated) - 1
                if weak_slot > 0:
                    allocated = allocate_builds('', weak_slot)
                    if allocated:
                        _logger.debug('Scheduled builds %s where allocated to runbot' % allocated)

            pending_build = Build.search(domain_host + [('local_state', '=', 'pending')], limit=available_slots)
            if pending_build:
                pending_build._schedule()

        # terminate and reap doomed build
        build_ids = Build.search(domain_host + [('local_state', '=', 'running')], order='job_start desc').ids
        # sort builds: the last build of each sticky branch then the rest
        sticky = {}
        non_sticky = []
        for build in Build.browse(build_ids):
            if build.branch_id.sticky and build.branch_id.id not in sticky:
                sticky[build.branch_id.id] = build.id
            else:
                non_sticky.append(build.id)
        build_ids = list(sticky.values())
        build_ids += non_sticky
        # terminate extra running builds
        Build.browse(build_ids)[running_max:]._kill()
        Build.browse(build_ids)._reap()

    def _domain(self):
        return self.env.get('ir.config_parameter').get_param('runbot.runbot_domain', fqdn())

    def _reload_nginx(self):
        settings = {}
        settings['port'] = config.get('http_port')
        settings['runbot_static'] = os.path.join(get_module_resource('runbot', 'static'), '')
        nginx_dir = os.path.join(self._root(), 'nginx')
        settings['nginx_dir'] = nginx_dir
        settings['re_escape'] = re.escape
        settings['fqdn'] = fqdn()
        nginx_repos = self.search([('nginx', '=', True)], order='id')
        if nginx_repos:
            settings['builds'] = self.env['runbot.build'].search([('repo_id', 'in', nginx_repos.ids), ('local_state', '=', 'running'), ('host', '=', fqdn())])

            nginx_config = self.env['ir.ui.view'].render_template("runbot.nginx_config", settings)
            os.makedirs(nginx_dir, exist_ok=True)
            content = None
            with open(os.path.join(nginx_dir, 'nginx.conf'), 'rb') as f:
                content = f.read()
            if content != nginx_config:
                _logger.debug('reload nginx')
                with open(os.path.join(nginx_dir, 'nginx.conf'), 'wb') as f:
                    f.write(nginx_config)
                try:
                    pid = int(open(os.path.join(nginx_dir, 'nginx.pid')).read().strip(' \n'))
                    os.kill(pid, signal.SIGHUP)
                except Exception:
                    _logger.debug('start nginx')
                    if subprocess.call(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf']):
                        # obscure nginx bug leaving orphan worker listening on nginx port
                        if not subprocess.call(['pkill', '-f', '-P1', 'nginx: worker']):
                            _logger.debug('failed to start nginx - orphan worker killed, retrying')
                            subprocess.call(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf'])
                        else:
                            _logger.debug('failed to start nginx - failed to kill orphan worker - oh well')

    def _get_cron_period(self, min_margin=120):
        """ Compute a randomized cron period with a 2 min margin below
        real cron timeout from config.
        """
        cron_limit = config.get('limit_time_real_cron')
        req_limit = config.get('limit_time_real')
        cron_timeout = cron_limit if cron_limit > -1 else req_limit
        return cron_timeout - (min_margin + random.randint(1, 60))

    def _cron_fetch_and_schedule(self, hostname):
        """This method have to be called from a dedicated cron on a runbot
        in charge of orchestration.
        """
        if hostname != fqdn():
            return 'Not for me'
        start_time = time.time()
        timeout = self._get_cron_period()
        icp = self.env['ir.config_parameter']
        update_frequency = int(icp.get_param('runbot.runbot_update_frequency', default=10))
        while time.time() - start_time < timeout:
            repos = self.search([('mode', '!=', 'disabled')])
            repos._update(force=False)
            repos._create_pending_builds()

            self.env.cr.commit()
            self.invalidate_cache()
            time.sleep(update_frequency)

    def _cron_fetch_and_build(self, hostname):
        """ This method have to be called from a dedicated cron
        created on each runbot instance.
        """
        if hostname != fqdn():
            return 'Not for me'
        host = self.env['runbot.host']._get_current()
        host.last_start_loop = fields.Datetime.now()
        self.env.cr.commit()
        start_time = time.time()
        # 1. source cleanup
        # -> Remove sources when no build is using them
        # (could be usefull to keep them for wakeup but we can checkout them again if not forced push)
        self.env['runbot.repo']._source_cleanup()
        # 2. db and log cleanup
        # -> Keep them as long as possible
        self.env['runbot.build']._local_cleanup()

        timeout = self._get_cron_period()
        icp = self.env['ir.config_parameter']
        update_frequency = int(icp.get_param('runbot.runbot_update_frequency', default=10))
        while time.time() - start_time < timeout:
            repos = self.search([('mode', '!=', 'disabled')])
            try:
                repos._scheduler(host)
                host.last_success = fields.Datetime.now()
                self.env.cr.commit()
                self.env.reset()
                self = self.env()[self._name]
                self._reload_nginx()
                time.sleep(update_frequency)
            except TransactionRollbackError:
                _logger.exception('Trying to rollback')
                self.env.cr.rollback()
                self.env.reset()
                time.sleep(random.uniform(0, 1))
            except Exception as e:
                with registry(self._cr.dbname).cursor() as cr:  # user another cursor since transaction will be rollbacked
                    message = str(e)
                    chost = host.with_env(self.env(cr=cr))
                    if chost.last_exception == message:
                        chost.exception_count += 1
                    else:
                        chost.with_env(self.env(cr=cr)).last_exception = str(e)
                        chost.exception_count = 1
                raise

        if host.last_exception:
            host.last_exception = ""
            host.exception_count = 0
        host.last_end_loop = fields.Datetime.now()

    def _source_cleanup(self):
        try:
            if self.pool._init:
                return
            _logger.info('Source cleaning')
            # we can remove a source only if no build are using them as name or rependency_ids aka as commit
            cannot_be_deleted_builds = self.env['runbot.build'].search([('host', '=', fqdn()), ('local_state', 'not in', ('done', 'duplicate'))])
            cannot_be_deleted_path = set()
            for build in cannot_be_deleted_builds:
                for commit in build._get_all_commit():
                    cannot_be_deleted_path.add(commit._source_path())

            to_delete = set()
            to_keep = set()
            repos = self.search([('mode', '!=', 'disabled')])
            for repo in repos:
                repo_source = os.path.join(repo._root(), 'sources', repo._get_repo_name_part(), '*')
                for source_dir in glob.glob(repo_source):
                    if source_dir not in cannot_be_deleted_path:
                        to_delete.add(source_dir)
                    else:
                        to_keep.add(source_dir)

            # we are comparing cannot_be_deleted_path with to keep to sensure that the algorithm is working, we want to avoid to erase file by mistake
            # note: it is possible that a parent_build is in testing without checkouting sources, but it should be exceptions
            if to_delete:
                if cannot_be_deleted_path == to_keep:
                    to_delete = list(to_delete)
                    to_keep = list(to_keep)
                    cannot_be_deleted_path = list(cannot_be_deleted_path)
                    for source_dir in to_delete:
                        _logger.info('Deleting source: %s' % source_dir)
                        assert 'static' in source_dir
                        shutil.rmtree(source_dir)
                    _logger.info('%s/%s source folder where deleted (%s kept)' % (len(to_delete), len(to_delete+to_keep), len(to_keep)))
                else:
                    _logger.warning('Inconsistency between sources and database: \n%s \n%s' % (cannot_be_deleted_path-to_keep, to_keep-cannot_be_deleted_path))

        except:
            _logger.error('An exception occured while cleaning sources')
            pass
