# -*- coding: utf-8 -*-
import datetime
import json
import logging
import re
import subprocess
import time

import requests

from pathlib import Path

from odoo import models, fields, api
from ..common import os, RunbotException, _make_github_session
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)


def _sanitize(name):
    for i in '@:/':
        name = name.replace(i, '_')
    return name


class Trigger(models.Model):
    """
    List of repo parts that must be part of the same bundle
    """

    _name = 'runbot.trigger'
    _inherit = 'mail.thread'
    _description = 'Triggers'

    _order = 'sequence, id'

    sequence = fields.Integer('Sequence')
    name = fields.Char("Name")
    description = fields.Char("Description", help="Informative description")
    project_id = fields.Many2one('runbot.project', string="Project id", required=True, default=lambda self: self.env.ref('runbot.main_project', raise_if_not_found=False))
    repo_ids = fields.Many2many('runbot.repo', relation='runbot_trigger_triggers', string="Triggers", domain="[('project_id', '=', project_id)]")
    dependency_ids = fields.Many2many('runbot.repo', relation='runbot_trigger_dependencies', string="Dependencies")
    config_id = fields.Many2one('runbot.build.config', string="Config", required=True)
    batch_dependent = fields.Boolean('Batch Dependent', help="Force adding batch in build parameters to make it unique and give access to bundle")

    ci_context = fields.Char("Ci context", default='ci/runbot', tracking=True)
    category_id = fields.Many2one('runbot.category', default=lambda self: self.env.ref('runbot.default_category', raise_if_not_found=False))
    version_domain = fields.Char(string="Version domain")
    hide = fields.Boolean('Hide trigger on main page')
    manual = fields.Boolean('Only start trigger manually', default=False)

    upgrade_dumps_trigger_id = fields.Many2one('runbot.trigger', string='Template/complement trigger', tracking=True)
    upgrade_step_id = fields.Many2one('runbot.build.config.step', compute="_compute_upgrade_step_id", store=True)
    ci_url = fields.Char("ci url")
    ci_description = fields.Char("ci description")
    has_stats = fields.Boolean('Has a make_stats config step', compute="_compute_has_stats", store=True)

    team_ids = fields.Many2many('runbot.team', string="Runbot Teams", help="Teams responsible of this trigger, mainly usefull for nightly")
    active = fields.Boolean("Active", default=True)

    @api.depends('config_id.step_order_ids.step_id.make_stats')
    def _compute_has_stats(self):
        for trigger in self:
            trigger.has_stats = any(trigger.config_id.step_order_ids.step_id.mapped('make_stats'))

    @api.depends('upgrade_dumps_trigger_id', 'config_id', 'config_id.step_order_ids.step_id.job_type')
    def _compute_upgrade_step_id(self):
        for trigger in self:
            trigger.upgrade_step_id = False
            if trigger.upgrade_dumps_trigger_id:
                trigger.upgrade_step_id = self._upgrade_step_from_config(trigger.config_id)

    def _upgrade_step_from_config(self, config):
        upgrade_step = next((step_order.step_id for step_order in config.step_order_ids if step_order.step_id._is_upgrade_step()), False)
        if not upgrade_step:
            upgrade_step = next((step_order.step_id for step_order in config.step_order_ids if step_order.step_id.job_type == 'python'), False)
        if not upgrade_step:
            raise UserError('Upgrade trigger should have a config with step of type Configure Upgrade')
        return upgrade_step

    def _reference_builds(self, bundle):
        self.ensure_one()
        if self.upgrade_step_id:  # this is an upgrade trigger, add corresponding builds
            custom_config = next((trigger_custom.config_id for trigger_custom in bundle.trigger_custom_ids if trigger_custom.trigger_id == self), False)
            step = self._upgrade_step_from_config(custom_config) if custom_config else self.upgrade_step_id
            refs_builds = step._reference_builds(bundle, self)
            return [(4, b.id) for b in refs_builds]
        return []

    def get_version_domain(self):
        if self.version_domain:
            return safe_eval(self.version_domain)
        return []


class Remote(models.Model):
    """
    Regroups repo and it duplicates (forks): odoo+odoo-dev for each repo
    """
    _name = 'runbot.remote'
    _description = 'Remote'
    _order = 'sequence, id'
    _inherit = 'mail.thread'

    name = fields.Char('Url', required=True, tracking=True)
    repo_id = fields.Many2one('runbot.repo', required=True, tracking=True)

    owner = fields.Char(compute='_compute_base_infos', string='Repo Owner', store=True, readonly=True, tracking=True)
    repo_name = fields.Char(compute='_compute_base_infos', string='Repo Name', store=True, readonly=True, tracking=True)
    repo_domain = fields.Char(compute='_compute_base_infos', string='Repo domain', store=True, readonly=True, tracking=True)

    base_url = fields.Char(compute='_compute_base_url', string='Base URL', readonly=True, tracking=True)

    short_name = fields.Char('Short name', compute='_compute_short_name', tracking=True)
    remote_name = fields.Char('Remote name', compute='_compute_remote_name', tracking=True)

    sequence = fields.Integer('Sequence', tracking=True)
    fetch_heads = fields.Boolean('Fetch branches', default=True, tracking=True)
    fetch_pull = fields.Boolean('Fetch PR', default=False, tracking=True)
    send_status = fields.Boolean('Send status', default=False, tracking=True)

    token = fields.Char("Github token", groups="runbot.group_runbot_admin")

    @api.depends('name')
    def _compute_base_infos(self):
        for remote in self:
            name = re.sub('.+@', '', remote.name)
            name = re.sub('^https://', '', name)  # support https repo style
            name = re.sub('.git$', '', name)
            name = name.replace(':', '/')
            s = name.split('/')
            remote.repo_domain = s[-3]
            remote.owner = s[-2]
            remote.repo_name = s[-1]

    @api.depends('repo_domain', 'owner', 'repo_name')
    def _compute_base_url(self):
        for remote in self:
            remote.base_url = '%s/%s/%s' % (remote.repo_domain, remote.owner, remote.repo_name)

    @api.depends('name', 'base_url')
    def _compute_short_name(self):
        for remote in self:
            remote.short_name = '/'.join(remote.base_url.split('/')[-2:])

    def _compute_remote_name(self):
        for remote in self:
            remote.remote_name = _sanitize(remote.short_name)

    def create(self, values_list):
        remote = super().create(values_list)
        if not remote.repo_id.main_remote_id:
            remote.repo_id.main_remote_id = remote
        remote._cr.postcommit.add(remote.repo_id._update_git_config)
        return remote

    def write(self, values):
        res = super().write(values)
        self._cr.postcommit.add(self.repo_id._update_git_config)
        return res

    def _github(self, url, payload=None, ignore_errors=False, nb_tries=2, recursive=False, session=None):
        generator = self.sudo()._github_generator(url, payload=payload, ignore_errors=ignore_errors, nb_tries=nb_tries, recursive=recursive, session=session)
        if recursive:
            return generator
        result = list(generator)
        return result[0] if result else False

    def _github_generator(self, url, payload=None, ignore_errors=False, nb_tries=2, recursive=False, session=None):
        """Return a http request to be sent to github"""
        for remote in self:
            if remote.owner and remote.repo_name and remote.repo_domain:
                url = url.replace(':owner', remote.owner)
                url = url.replace(':repo', remote.repo_name)
                url = 'https://api.%s%s' % (remote.repo_domain, url)
                session = session or _make_github_session(remote.token)
                while url:
                    if recursive:
                        _logger.info('Getting page %s', url)
                    try_count = 0
                    while try_count < nb_tries:
                        try:
                            if payload:
                                response = session.post(url, data=json.dumps(payload))
                            else:
                                response = session.get(url)
                            response.raise_for_status()
                            if try_count > 0:
                                _logger.info('Success after %s tries', (try_count + 1))
                            if recursive:
                                link = response.headers.get('link')
                                url = False
                                if link:
                                    url = {link.split(';')[1]: link.split(';')[0] for link in link.split(',')}.get(' rel="next"')
                                if url:
                                    url = url.strip('<> ')
                                yield response.json()
                                break
                            else:
                                yield response.json()
                                return
                        except requests.HTTPError:
                            try_count += 1
                            if try_count < nb_tries:
                                time.sleep(2)
                            else:
                                if ignore_errors:
                                    _logger.exception('Ignored github error %s %r (try %s/%s)', url, payload, try_count, nb_tries)
                                    url = False
                                else:
                                    raise


class Repo(models.Model):

    _name = 'runbot.repo'
    _description = "Repo"
    _order = 'sequence, id'
    _inherit = 'mail.thread'

    name = fields.Char("Name", tracking=True)  # odoo/enterprise/upgrade/security/runbot/design_theme
    identity_file = fields.Char("Identity File", help="Identity file to use with git/ssh", groups="runbot.group_runbot_admin")
    main_remote_id = fields.Many2one('runbot.remote', "Main remote", tracking=True)
    remote_ids = fields.One2many('runbot.remote', 'repo_id', "Remotes")
    project_id = fields.Many2one('runbot.project', required=True, tracking=True,
                                 help="Default bundle project to use when pushing on this repos",
                                 default=lambda self: self.env.ref('runbot.main_project', raise_if_not_found=False))
    # -> not verry usefull, remove it? (iterate on projects or contraints triggers:
    # all trigger where a repo is used must be in the same project.
    modules = fields.Char("Modules to install", help="Comma-separated list of modules to install and test.", tracking=True)
    server_files = fields.Char('Server files', help='Comma separated list of possible server files', tracking=True)  # odoo-bin,openerp-server,openerp-server.py
    manifest_files = fields.Char('Manifest files', help='Comma separated list of possible manifest files', default='__manifest__.py', tracking=True)
    addons_paths = fields.Char('Addons paths', help='Comma separated list of possible addons path', default='', tracking=True)

    sequence = fields.Integer('Sequence', tracking=True)
    path = fields.Char(compute='_get_path', string='Directory', readonly=True)
    mode = fields.Selection([('disabled', 'Disabled'),
                             ('poll', 'Poll'),
                             ('hook', 'Hook')],
                            default='poll',
                            string="Mode", required=True, help="hook: Wait for webhook on /runbot/hook/<id> i.e. github push event", tracking=True)
    hook_time = fields.Float('Last hook time', compute='_compute_hook_time')
    last_processed_hook_time = fields.Float('Last processed hook time')
    get_ref_time = fields.Float('Last refs db update', compute='_compute_get_ref_time')
    trigger_ids = fields.Many2many('runbot.trigger', relation='runbot_trigger_triggers', readonly=True)
    single_version = fields.Many2one('runbot.version', "Single version", help="Limit the repo to a single version for non versionned repo")
    forbidden_regex = fields.Char('Forbidden regex', help="Regex that forid bundle creation if branch name is matching", tracking=True)
    invalid_branch_message = fields.Char('Forbidden branch message', tracking=True)

    def _compute_get_ref_time(self):
        self.env.cr.execute("""
            SELECT repo_id, time FROM runbot_repo_reftime
            WHERE id IN (
                SELECT max(id) FROM runbot_repo_reftime
                WHERE repo_id = any(%s) GROUP BY repo_id
            )
        """, [self.ids])
        times = dict(self.env.cr.fetchall())
        for repo in self:
            repo.get_ref_time = times.get(repo.id, 0)

    def _compute_hook_time(self):
        self.env.cr.execute("""
            SELECT repo_id, time FROM runbot_repo_hooktime
            WHERE id IN (
                SELECT max(id) FROM runbot_repo_hooktime
                WHERE repo_id = any(%s) GROUP BY repo_id
            )
        """, [self.ids])
        times = dict(self.env.cr.fetchall())

        for repo in self:
            repo.hook_time = times.get(repo.id, 0)

    def set_hook_time(self, value):
        for repo in self:
            self.env['runbot.repo.hooktime'].create({'time': value, 'repo_id': repo.id})
        self.invalidate_cache()

    def set_ref_time(self, value):
        for repo in self:
            self.env['runbot.repo.reftime'].create({'time': value, 'repo_id': repo.id})
        self.invalidate_cache()

    def _gc_times(self):
        self.env.cr.execute("""
            DELETE from runbot_repo_reftime WHERE id NOT IN (
                SELECT max(id) FROM runbot_repo_reftime GROUP BY repo_id
            )
        """)
        self.env.cr.execute("""
            DELETE from runbot_repo_hooktime WHERE id NOT IN (
                SELECT max(id) FROM runbot_repo_hooktime GROUP BY repo_id
            )
        """)

    @api.depends('name')
    def _get_path(self):
        """compute the server path of repo from the name"""
        root = self.env['runbot.runbot']._root()
        for repo in self:
            repo.path = os.path.join(root, 'repo', _sanitize(repo.name))

    def _git(self, cmd, errors='strict'):
        """Execute a git command 'cmd'"""
        self.ensure_one()
        config_args = []
        if self.identity_file:
            config_args = ['-c', 'core.sshCommand=ssh -i %s/.ssh/%s' % (str(Path.home()), self.identity_file)]
        cmd = ['git', '-C', self.path] + config_args + cmd
        _logger.info("git command: %s", ' '.join(cmd))
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode(errors=errors)

    def _fetch(self, sha):
        if not self._hash_exists(sha):
            self._update(force=True)
            if not self._hash_exists(sha):
                for remote in self.remote_ids:
                    try:
                        self._git(['fetch', remote.remote_name, sha])
                        _logger.info('Success fetching specific head %s on %s', sha, remote)
                        break
                    except subprocess.CalledProcessError:
                        pass
                if not self._hash_exists(sha):
                    raise RunbotException("Commit %s is unreachable. Did you force push the branch?" % sha)

    def _hash_exists(self, commit_hash):
        """ Verify that a commit hash exists in the repo """
        self.ensure_one()
        try:
            self._git(['cat-file', '-e', commit_hash])
        except subprocess.CalledProcessError:
            return False
        return True

    def _is_branch_forbidden(self, branch_name):
        self.ensure_one()
        if self.forbidden_regex:
            return re.match(self.forbidden_regex, branch_name)
        return False

    def _get_fetch_head_time(self):
        self.ensure_one()
        fname_fetch_head = os.path.join(self.path, 'FETCH_HEAD')
        if os.path.exists(fname_fetch_head):
            return os.path.getmtime(fname_fetch_head)
        return 0

    def _get_refs(self, max_age=30, ignore=None):
        """Find new refs
        :return: list of tuples with following refs informations:
        name, sha, date, author, author_email, subject, committer, committer_email
        """
        self.ensure_one()
        get_ref_time = round(self._get_fetch_head_time(), 4)
        commit_limit = time.time() - (60 * 60 * 24 * max_age)
        if not self.get_ref_time or get_ref_time > self.get_ref_time:
            try:
                self.set_ref_time(get_ref_time)
                fields = ['refname', 'objectname', 'committerdate:unix', 'authorname', 'authoremail', 'subject', 'committername', 'committeremail']
                fmt = "%00".join(["%(" + field + ")" for field in fields])
                cmd = ['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/*/heads/*']
                if any(remote.fetch_pull for remote in self.remote_ids):
                    cmd.append('refs/*/pull/*')
                git_refs = self._git(cmd)
                git_refs = git_refs.strip()
                if not git_refs:
                    return []
                refs = [tuple(field for field in line.split('\x00')) for line in git_refs.split('\n')]
                refs = [r for r in refs if not re.match(r'^refs/[\w-]+/heads/\d+$', r[0])]  # remove branches with interger names to avoid confusion with pr names
                refs = [r for r in refs if int(r[2]) > commit_limit or self.env['runbot.branch'].match_is_base(r[0].split('/')[-1])]
                if ignore:
                    refs = [r for r in refs if r[0].split('/')[-1] not in ignore]
                return refs
            except Exception:
                _logger.exception('Fail to get refs for repo %s', self.name)
                self.env['runbot.runbot'].warning('Fail to get refs for repo %s', self.name)
        return []

    def _find_or_create_branches(self, refs):
        """Parse refs and create branches that does not exists yet
        :param refs: list of tuples returned by _get_refs()
        :return: dict {branch.name: branch.id}
        The returned structure contains all the branches from refs newly created
        or older ones.
        """

        # FIXME WIP
        names = [r[0].split('/')[-1] for r in refs]
        branches = self.env['runbot.branch'].search([('name', 'in', names), ('remote_id', 'in', self.remote_ids.ids)])
        ref_branches = {branch.ref(): branch for branch in branches}
        new_branch_values = []
        for ref_name, sha, date, author, author_email, subject, committer, committer_email in refs:
            if not ref_branches.get(ref_name):
                # format example:
                # refs/ruodoo-dev/heads/12.0-must-fail
                # refs/ruodoo/pull/1
                _, remote_name, branch_type, name = ref_name.split('/')
                remote_id = self.remote_ids.filtered(lambda r: r.remote_name == remote_name).id
                if not remote_id:
                    _logger.warning('Remote %s not found', remote_name)
                    continue
                new_branch_values.append({'remote_id': remote_id, 'name': name, 'is_pr': branch_type == 'pull'})
                # TODO catch error for pr info. It may fail for multiple raison. closed? external? check corner cases
                _logger.info('new branch %s found in %s', name, self.name)
        if new_branch_values:
            _logger.info('Creating new branches')
            new_branches = self.env['runbot.branch'].create(new_branch_values)
            for branch in new_branches:
                ref_branches[branch.ref()] = branch
        return ref_branches

    def _find_new_commits(self, refs, ref_branches):
        """Find new commits in bare repo
        :param refs: list of tuples returned by _get_refs()
        :param ref_branches: dict structure {branch.name: branch.id}
                             described in _find_or_create_branches
        """
        self.ensure_one()

        for ref_name, sha, date, author, author_email, subject, committer, committer_email in refs:
            branch = ref_branches[ref_name]
            if branch.head_name != sha:  # new push on branch
                _logger.info('repo %s branch %s new commit found: %s', self.name, branch.name, sha)

                commit = self.env['runbot.commit']._get(sha, self.id, {
                        'author': author,
                        'author_email': author_email,
                        'committer': committer,
                        'committer_email': committer_email,
                        'subject': subject,
                        'date': datetime.datetime.fromtimestamp(int(date)),
                    })
                branch.head = commit
                if not branch.alive:
                    if branch.is_pr:
                        _logger.info('Recomputing infos of dead pr %s', branch.name)
                        branch._compute_branch_infos()
                    else:
                        branch.alive = True

                if branch.reference_name and branch.remote_id and branch.remote_id.repo_id._is_branch_forbidden(branch.reference_name):
                    message = "This branch name is incorrect. Branch name should be prefixed with a valid version"
                    message = branch.remote_id.repo_id.invalid_branch_message or message
                    branch.head._github_status(False, "Branch naming", 'failure', False, message)

                bundle = branch.bundle_id
                if bundle.no_build:
                    continue

                if bundle.last_batch.state != 'preparing':
                    preparing = self.env['runbot.batch'].create({
                        'last_update': fields.Datetime.now(),
                        'bundle_id': bundle.id,
                        'state': 'preparing',
                    })
                    bundle.last_batch = preparing

                if bundle.last_batch.state == 'preparing':
                    bundle.last_batch._new_commit(branch)

    def _update_batches(self, force=False, ignore=None):
        """ Find new commits in physical repos"""
        updated = False
        for repo in self:
            if repo.remote_ids and self._update(poll_delay=30 if force else 60*5):
                max_age = int(self.env['ir.config_parameter'].get_param('runbot.runbot_max_age', default=30))
                ref = repo._get_refs(max_age, ignore=ignore)
                ref_branches = repo._find_or_create_branches(ref)
                repo._find_new_commits(ref, ref_branches)
                updated = True
        return updated

    def _update_git_config(self):
        """ Update repo git config file """
        for repo in self:
            if repo.mode == 'disabled':
                _logger.info(f'skipping disabled repo {repo.name}')
                continue
            if os.path.isdir(os.path.join(repo.path, 'refs')):
                git_config_path = os.path.join(repo.path, 'config')
                template_params = {'repo': repo}
                git_config = self.env['ir.ui.view']._render_template("runbot.git_config", template_params)
                with open(git_config_path, 'w') as config_file:
                    config_file.write(str(git_config))
                _logger.info('Config updated for repo %s' % repo.name)
            else:
                _logger.info('Repo not cloned, skiping config update for %s' % repo.name)

    def _git_init(self):
        """ Clone the remote repo if needed """
        self.ensure_one()
        repo = self
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            _logger.info("Initiating repository '%s' in '%s'" % (repo.name, repo.path))
            git_init = subprocess.run(['git', 'init', '--bare', repo.path], stderr=subprocess.PIPE)
            if git_init.returncode:
                _logger.warning('Git init failed with code %s and message: "%s"', git_init.returncode, git_init.stderr)
                return
            self._update_git_config()
            return True

    def _update_git(self, force=False, poll_delay=5*60):
        """ Update the git repo on FS """
        self.ensure_one()
        repo = self
        if not repo.remote_ids:
            return False
        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        force = self._git_init() or force

        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if not force and os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook':
                if not repo.hook_time or (repo.last_processed_hook_time and repo.hook_time <= repo.last_processed_hook_time):
                    return False
                repo.last_processed_hook_time = repo.hook_time
            if repo.mode == 'poll':
                if (time.time() < fetch_time + poll_delay):
                    return False

        _logger.info('Updating repo %s', repo.name)
        return self._update_fetch_cmd()

    def _update_fetch_cmd(self):
        # Extracted from update_git to be easily overriden in external module
        self.ensure_one()
        try_count = 0
        success = False
        delay = 0
        while not success and try_count < 5:
            time.sleep(delay)
            try:
                self._git(['fetch', '-p', '--all', ])
                success = True
            except subprocess.CalledProcessError as e:
                try_count += 1
                delay = delay * 1.5 if delay else 0.5
                if try_count > 4:
                    message = 'Failed to fetch repo %s: %s' % (self.name, e.output.decode())
                    host = self.env['runbot.host']._get_current()
                    host.message_post(body=message)
                    icp = self.env['ir.config_parameter'].sudo()
                    if icp.get_param('runbot.runbot_disable_host_on_fetch_failure'):
                        self.env['runbot.runbot'].warning('Host %s got reserved because of fetch failure' % host.name)
                        _logger.exception(message)
                        host.disable()
        return success

    def _update(self, force=False, poll_delay=5*60):
        """ Update the physical git reposotories on FS"""
        self.ensure_one()
        try:
            return self._update_git(force, poll_delay)
        except Exception:
            _logger.exception('Fail to update repo %s', self.name)

    def _get_module(self, file):
        for addons_path in (self.addons_paths or '').split(','):
            base_path = f'{self.name}/{addons_path}'
            if file.startswith(base_path):
                return file[len(base_path):].strip('/').split('/')[0]


class RefTime(models.Model):
    _name = 'runbot.repo.reftime'
    _description = "Repo reftime"
    _log_access = False

    time = fields.Float('Time', index=True, required=True)
    repo_id = fields.Many2one('runbot.repo', 'Repository', required=True, ondelete='cascade')


class HookTime(models.Model):
    _name = 'runbot.repo.hooktime'
    _description = "Repo hooktime"
    _log_access = False

    time = fields.Float('Time')
    repo_id = fields.Many2one('runbot.repo', 'Repository', required=True, ondelete='cascade')
