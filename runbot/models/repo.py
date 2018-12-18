# -*- coding: utf-8 -*-
import datetime
import dateutil
import json
import logging
import os
import re
import requests
import signal
import subprocess
import time

from odoo import models, fields, api
from odoo.modules.module import get_module_resource
from odoo.tools import config
from ..common import fqdn, dt2time

_logger = logging.getLogger(__name__)


class runbot_repo(models.Model):

    _name = "runbot.repo"

    name = fields.Char('Repository', required=True)
    # branch_ids = fields.One2many('runbot.branch', inverse_name='repo_id') # keep for next version
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
    duplicate_id = fields.Many2one('runbot.repo', 'Duplicate repo', help='Repository for finding duplicate builds')
    modules = fields.Char("Modules to install", help="Comma-separated list of modules to install and test.")
    modules_auto = fields.Selection([('none', 'None (only explicit modules list)'),
                                     ('repo', 'Repository modules (excluding dependencies)'),
                                     ('all', 'All modules (including dependencies)')],
                                    default='repo',
                                    string="Other modules to install automatically")

    dependency_ids = fields.Many2many(
        'runbot.repo', 'runbot_repo_dep_rel', column1='dependant_id', column2='dependency_id',
        string='Extra dependencies',
        help="Community addon repos which need to be present to run tests.")
    token = fields.Char("Github token", groups="runbot.group_runbot_admin")
    group_ids = fields.Many2many('res.groups', string='Limited to groups')

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

    def _git(self, cmd):
        """Execute a git command 'cmd'"""
        for repo in self:
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.info("git command: %s", ' '.join(cmd))
            return subprocess.check_output(cmd).decode('utf-8')

    def _git_export(self, treeish, dest):
        """Export a git repo to dest"""
        self.ensure_one()
        _logger.debug('checkout %s %s %s', self.name, treeish, dest)
        p1 = subprocess.Popen(['git', '--git-dir=%s' % self.path, 'archive', treeish], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '-xmC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
        p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
        p2.communicate()[0]

    def _github(self, url, payload=None, ignore_errors=False):
        """Return a http request to be sent to github"""
        for repo in self:
            if not repo.token:
                return
            try:
                match_object = re.search('([^/]+)/([^/]+)/([^/.]+(.git)?)', repo.base)
                if match_object:
                    url = url.replace(':owner', match_object.group(2))
                    url = url.replace(':repo', match_object.group(3))
                    url = 'https://api.%s%s' % (match_object.group(1), url)
                    session = requests.Session()
                    session.auth = (repo.token, 'x-oauth-basic')
                    session.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
                    if payload:
                        response = session.post(url, data=json.dumps(payload))
                    else:
                        response = session.get(url)
                    response.raise_for_status()
                    return response.json()
            except Exception:
                if ignore_errors:
                    _logger.exception('Ignored github error %s %r', url, payload)
                else:
                    raise

    def _update_git(self):
        """ Update the git repo on FS """
        self.ensure_one()
        repo = self
        _logger.debug('repo %s updating branches', repo.name)

        icp = self.env['ir.config_parameter']
        max_age = int(icp.get_param('runbot.runbot_max_age', default=30))

        Build = self.env['runbot.build']
        Branch = self.env['runbot.branch']

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            _logger.info("Cloning repository '%s' in '%s'" % (repo.name, repo.path))
            subprocess.call(['git', 'clone', '--bare', repo.name, repo.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook' and repo.hook_time and dt2time(repo.hook_time) < fetch_time:
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                              repo.name, int(t0 - fetch_time), int(t0 - dt2time(repo.hook_time)))
                return

        repo._git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*', '+refs/pull/*/head:refs/pull/*'])

        fields = ['refname', 'objectname', 'committerdate:iso8601', 'authorname', 'authoremail', 'subject', 'committername', 'committeremail']
        fmt = "%00".join(["%(" + field + ")" for field in fields])
        git_refs = repo._git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[field for field in line.split('\x00')] for line in git_refs.split('\n')]

        self.env.cr.execute("""
            WITH t (branch) AS (SELECT unnest(%s))
          SELECT t.branch, b.id
            FROM t LEFT JOIN runbot_branch b ON (b.name = t.branch)
           WHERE b.repo_id = %s;
        """, ([r[0] for r in refs], repo.id))
        ref_branches = {r[0]: r[1] for r in self.env.cr.fetchall()}

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            # create or get branch
            # branch = repo.branch_ids.search([('name', '=', name), ('repo_id', '=', repo.id)])
            # if not branch:
            #    _logger.debug('repo %s found new branch %s', repo.name, name)
            #    branch = self.branch_ids.create({
            #        'repo_id': repo.id,
            #        'name': name})
            # keep for next version with a branch_ids field

            if ref_branches.get(name):
                branch_id = ref_branches[name]
            else:
                _logger.debug('repo %s found new branch %s', repo.name, name)
                branch_id = Branch.create({'repo_id': repo.id, 'name': name}).id
            branch = Branch.browse([branch_id])[0]

            # skip the build for old branches (Could be checked before creating the branch in DB ?)
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(days=max_age) < datetime.datetime.now():
                continue

            # create build (and mark previous builds as skipped) if not found
            build_ids = Build.search([('branch_id', '=', branch.id), ('name', '=', sha)])
            if not build_ids:
                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                    'coverage': branch.coverage,
                }
                if not branch.sticky:
                    # pending builds are skipped as we have a new ref
                    builds_to_skip = Build.search(
                        [('branch_id', '=', branch.id), ('state', '=', 'pending')],
                        order='sequence asc')
                    builds_to_skip._skip(reason='New ref found')
                    if builds_to_skip:
                        build_info['sequence'] = builds_to_skip[0].sequence
                    # testing builds are killed
                    builds_to_kill = Build.search([
                        ('branch_id', '=', branch.id),
                        ('state', '=', 'testing'),
                        ('committer', '=', committer)
                    ])
                    builds_to_kill.write({'state': 'deathrow'})
                    for btk in builds_to_kill:
                        btk._log('repo._update_git', 'Build automatically killed, newer build found.')

                new_build = Build.create(build_info)
                # create a reverse dependency build if needed
                if branch.sticky:
                    for rev_repo in self.search([('dependency_ids', 'in', self.id)]):
                        # find the latest build with the same branch name
                        latest_rev_build = Build.search([('repo_id.id', '=', rev_repo.id), ('branch_id.branch_name', '=', branch.branch_name)], order='id desc', limit=1)
                        if latest_rev_build:
                            _logger.debug('Reverse dependency build %s forced in repo %s by commit %s', latest_rev_build.dest, rev_repo.name, sha[:6])
                            latest_rev_build.build_type = 'indirect'
                            new_build.revdep_build_ids += latest_rev_build._force(message='Rebuild from dependency %s commit %s' % (repo.name, sha[:6]))

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', repo.id), ('state', '=', 'pending')]
        icp = self.env['ir.config_parameter']
        running_max = int(icp.get_param('runbot.runbot_running_max', default=75))
        builds_to_be_skipped = Build.search(skippable_domain, order='sequence desc', offset=running_max)
        builds_to_be_skipped._skip()

    def _update(self, repos):
        """ Update the physical git reposotories on FS"""
        for repo in repos:
            try:
                repo._update_git()
            except Exception:
                _logger.exception('Fail to update repo %s', repo.name)

    def _scheduler(self, ids=None):
        """Schedule builds for the repository"""
        icp = self.env['ir.config_parameter']
        workers = int(icp.get_param('runbot.runbot_workers', default=6))
        running_max = int(icp.get_param('runbot.runbot_running_max', default=75))
        host = fqdn()

        Build = self.env['runbot.build']
        domain = [('repo_id', 'in', ids), ('branch_id.job_type', '!=', 'none')]
        domain_host = domain + [('host', '=', host)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        build_ids = Build.search(domain_host + [('state', 'in', ['testing', 'running', 'deathrow'])])
        build_ids._schedule()

        # launch new tests
        testing = Build.search_count(domain_host + [('state', '=', 'testing')])
        pending = Build.search_count(domain + [('state', '=', 'pending')])

        while testing < workers and pending > 0:

            # find sticky / priority pending build if any, otherwise, last pending (by id, not by sequence) will do the job

            pending_ids = Build.search(domain + [('state', '=', 'pending'), '|', ('branch_id.sticky', '=', True), ('branch_id.priority', '=', True)], limit=1)
            if not pending_ids:
                pending_ids = Build.search(domain + [('state', '=', 'pending')], order="sequence", limit=1)

            pending_ids._schedule()

            # compute the number of testing and pending jobs again
            testing = Build.search_count(domain_host + [('state', '=', 'testing')])
            pending = Build.search_count(domain + [('state', '=', 'pending')])

        # terminate and reap doomed build
        build_ids = Build.search(domain_host + [('state', '=', 'running')]).ids
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
            settings['builds'] = self.env['runbot.build'].search([('repo_id', 'in', nginx_repos.ids), ('state', '=', 'running'), ('host', '=', fqdn())])

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

    def _cron(self):
        repos = self.search([('mode', '!=', 'disabled')])
        self._update(repos)
        self._scheduler(repos.ids)
        self._reload_nginx()
