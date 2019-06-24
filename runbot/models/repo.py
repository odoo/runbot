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

from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT
from odoo import models, fields, api
from odoo.modules.module import get_module_resource
from odoo.tools import config
from ..common import fqdn, dt2time

_logger = logging.getLogger(__name__)


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
    hook_time = fields.Datetime('Last hook time')
    get_ref_time = fields.Datetime('Last refs db update')
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

    @api.depends('name')
    def _get_path(self):
        """compute the server path of repo from the name"""
        root = self._root()
        for repo in self:
            name = repo.name
            for i in '@:/':
                name = name.replace(i, '_')
            repo.path = os.path.join(root, 'repo', name)

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

    def _git(self, cmd):
        """Execute a git command 'cmd'"""
        for repo in self:
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.debug("git command: %s", ' '.join(cmd))
            return subprocess.check_output(cmd).decode('utf-8')

    def _git_rev_parse(self, branch_name):
        return self._git(['rev-parse', branch_name]).strip()

    def _git_export(self, treeish, dest):
        """Export a git repo to dest"""
        self.ensure_one()
        _logger.debug('checkout %s %s %s', self.name, treeish, dest)
        p1 = subprocess.Popen(['git', '--git-dir=%s' % self.path, 'archive', treeish], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '-xmC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
        p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
        p2.communicate()[0]

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
        if not self.get_ref_time or get_ref_time > dt2time(self.get_ref_time):
            self.get_ref_time = datetime.datetime.fromtimestamp(get_ref_time).strftime(DEFAULT_SERVER_DATETIME_FORMAT)
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
                    builds_to_kill.write({'local_state': 'deathrow'})

                new_build = Build.create(build_info)
                # create a reverse dependency build if needed
                if branch.sticky:
                    for rev_repo in self.search([('dependency_ids', 'in', self.id)]):
                        # find the latest build with the same branch name
                        latest_rev_build = Build.search([('repo_id.id', '=', rev_repo.id), ('branch_id.branch_name', '=', branch.branch_name)], order='id desc', limit=1)
                        if latest_rev_build:
                            _logger.debug('Reverse dependency build %s forced in repo %s by commit %s', latest_rev_build.dest, rev_repo.name, sha[:6])
                            latest_rev_build.build_type = 'indirect'
                            new_build.revdep_build_ids += latest_rev_build._force(message='Rebuild from dependency %s commit %s' % (self.name, sha[:6]))

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
            if repo.mode == 'hook' and (not repo.hook_time or dt2time(repo.hook_time) < fetch_time):
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                            repo.name, int(t0 - fetch_time), int(t0 - dt2time(repo.hook_time)) if repo.hook_time else 'never')
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
        for repo in self:
            try:
                repo._update_git(force)
            except Exception:
                _logger.exception('Fail to update repo %s', repo.name)

    @api.multi
    def _scheduler(self):
        """Schedule builds for the repository"""
        ids = self.ids
        if not ids:
            return
        icp = self.env['ir.config_parameter']
        host = fqdn()
        settings_workers = int(icp.get_param('runbot.runbot_workers', default=6))
        workers = int(icp.get_param('%s.workers' % host, default=settings_workers))
        running_max = int(icp.get_param('runbot.runbot_running_max', default=75))

        Build = self.env['runbot.build']
        domain = [('repo_id', 'in', ids)]
        domain_host = domain + [('host', '=', host)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        build_ids = Build.search(domain_host + [('local_state', 'in', ['testing', 'running', 'deathrow'])])
        build_ids._schedule()
        self.env.cr.commit()

        # launch new tests

        nb_testing = Build.search_count(domain_host + [('local_state', '=', 'testing')])
        available_slots = workers - nb_testing
        reserved_slots = Build.search_count(domain_host + [('local_state', '=', 'pending')])
        assignable_slots = available_slots - reserved_slots
        if available_slots > 0:
            if assignable_slots > 0:  # note: slots have been addapt to be able to force host on pending build. Normally there is no pending with host.
                # commit transaction to reduce the critical section duration
                def allocate_builds(where_clause, limit):
                    self.env.cr.commit()
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

                    self.env.cr.execute(query, {'repo_ids': tuple(ids), 'host': fqdn(), 'limit': limit})
                    return self.env.cr.fetchall()

                allocated = allocate_builds("""AND runbot_build.build_type != 'scheduled'""", assignable_slots)
                _logger.debug('Normal builds %s where allocated to runbot' % allocated)
                weak_slot = assignable_slots - len(allocated) - 1
                if weak_slot > 0:
                    allocated = allocate_builds('', weak_slot)
                    _logger.debug('Scheduled builds %s where allocated to runbot' % allocated)

            pending_build = Build.search(domain_host + [('local_state', '=', 'pending')], limit=available_slots)
            if pending_build:
                pending_build._schedule()
                self.env.cr.commit()

        # terminate and reap doomed build
        build_ids = Build.search(domain_host + [('local_state', '=', 'running')]).ids
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
            open(os.path.join(nginx_dir, 'nginx.conf'), 'wb').write(nginx_config)
            try:
                _logger.debug('reload nginx')
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
        start_time = time.time()
        timeout = self._get_cron_period()
        icp = self.env['ir.config_parameter']
        update_frequency = int(icp.get_param('runbot.runbot_update_frequency', default=10))
        while time.time() - start_time < timeout:
            repos = self.search([('mode', '!=', 'disabled')])
            repos._scheduler()
            self.env.cr.commit()
            self.env.reset()
            self = self.env()[self._name]
            self._reload_nginx()
            time.sleep(update_frequency)
