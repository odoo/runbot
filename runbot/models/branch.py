# -*- coding: utf-8 -*-
import logging
import re
import time
from subprocess import CalledProcessError
from odoo import models, fields, api

_logger = logging.getLogger(__name__)
_re_patch = re.compile(r'.*patch-\d+$')


class runbot_branch(models.Model):

    _name = "runbot.branch"
    _order = 'name'
    _sql_constraints = [('branch_repo_uniq', 'unique (name,repo_id)', 'The branch must be unique per repository !')]

    repo_id = fields.Many2one('runbot.repo', 'Repository', required=True, ondelete='cascade')
    duplicate_repo_id = fields.Many2one('runbot.repo', 'Duplicate Repository', related='repo_id.duplicate_id',)
    name = fields.Char('Ref Name', required=True)
    branch_name = fields.Char(compute='_get_branch_infos', string='Branch', readonly=1, store=True)
    branch_url = fields.Char(compute='_get_branch_url', string='Branch url', readonly=1)
    pull_head_name = fields.Char(compute='_get_branch_infos', string='PR HEAD name', readonly=1, store=True)
    target_branch_name = fields.Char(compute='_get_branch_infos', string='PR target branch', store=True)
    sticky = fields.Boolean('Sticky')
    closest_sticky = fields.Many2one('runbot.branch', compute='_compute_closest_sticky', string='Closest sticky')
    defined_sticky = fields.Many2one('runbot.branch', string='Force sticky')
    previous_version = fields.Many2one('runbot.branch', compute='_compute_previous_version', string='Previous version branch')
    intermediate_stickies = fields.Many2many('runbot.branch', compute='_compute_intermediate_stickies', string='Intermediates stickies')
    coverage_result = fields.Float(compute='_compute_coverage_result', type='Float', string='Last coverage', store=False)  # non optimal search in loop, could we store this result ? or optimise
    state = fields.Char('Status')
    modules = fields.Char("Modules to Install", help="Comma-separated list of modules to install and test.")
    priority = fields.Boolean('Build priority', default=False)
    no_build = fields.Boolean("Forbid creation of build on this branch", default=False)
    no_auto_build = fields.Boolean("Don't automatically build commit on this branch", default=False)
    rebuild_requested = fields.Boolean("Request a rebuild", help="Rebuild the latest commit even when no_auto_build is set.", default=False)

    branch_config_id = fields.Many2one('runbot.build.config', 'Run Config')
    config_id = fields.Many2one('runbot.build.config', 'Run Config', compute='_compute_config_id', inverse='_inverse_config_id')

    @api.depends('sticky', 'defined_sticky', 'target_branch_name', 'name')
    # won't be recompute if a new branch is marked as sticky or sticky is removed, but should be ok if not stored
    def _compute_closest_sticky(self):
        for branch in self:
            if branch.sticky:
                branch.closest_sticky = branch
            elif branch.defined_sticky:
                branch.closest_sticky = branch.defined_sticky # be carefull with loop
            elif branch.target_branch_name:
                corresping_branch = self.search([('branch_name', '=', branch.target_branch_name), ('repo_id', '=', branch.repo_id.id)])
                branch.closest_sticky = corresping_branch.closest_sticky
            else:
                repo_ids = (branch.repo_id | branch.repo_id.duplicate_id).ids
                self.env.cr.execute("select id from runbot_branch where sticky = 't' and repo_id = any(%s) and %s like name||'%%'", (repo_ids, branch.name or ''))
                branch.closest_sticky = self.browse(self.env.cr.fetchone())

    @api.depends('closest_sticky.previous_version')
    def _compute_previous_version(self):
        for branch in self:
            if branch.closest_sticky == branch:
                repo_ids = (branch.repo_id | branch.repo_id.duplicate_id).ids
                domain = [('branch_name', 'like', '%.0'), ('sticky', '=', True), ('branch_name', '!=', 'master'), ('repo_id', 'in', repo_ids)]
                if branch.branch_name != 'master':
                    domain += [('id', '<', branch.id)]
                branch.previous_version = self.search(domain, limit=1, order='id desc')
            else:
                branch.previous_version = branch.closest_sticky.previous_version

    @api.depends('previous_version', 'closest_sticky.intermediate_stickies')
    def _compute_intermediate_stickies(self):
        for branch in self:
            if branch.closest_sticky == branch:
                if not branch.previous_version:
                    continue
                repo_ids = (branch.repo_id | branch.repo_id.duplicate_id).ids
                domain = [('id', '>', branch.previous_version.id), ('sticky', '=', True), ('branch_name', '!=', 'master'), ('repo_id', 'in', repo_ids)]
                if branch.closest_sticky.branch_name != 'master':
                    domain += [('id', '<', branch.closest_sticky.id)]
                branch.intermediate_stickies = [(6, 0, self.search(domain, order='id desc').ids)]
            else:
                branch.intermediate_stickies = [(6, 0, branch.closest_sticky.intermediate_stickies.ids)]

    def _compute_config_id(self):
        for branch in self:
            if branch.branch_config_id:
                branch.config_id = branch.branch_config_id
            else:
                branch.config_id = branch.repo_id.config_id

    def _inverse_config_id(self):
        for branch in self:
            branch.branch_config_id = branch.config_id

    @api.depends('name')
    def _get_branch_infos(self):
        """compute branch_name, branch_url, pull_head_name and target_branch_name based on name"""
        for branch in self:
            if branch.name:
                branch.branch_name = branch.name.split('/')[-1]
                pi = branch._get_pull_info()
                if pi:
                    branch.target_branch_name = pi['base']['ref']
                    if not _re_patch.match(pi['head']['label']):
                        # label is used to disambiguate PR with same branch name
                        branch.pull_head_name = pi['head']['label']

    @api.depends('branch_name')
    def _get_branch_url(self):
        """compute the branch url based on branch_name"""
        for branch in self:
            if branch.name:
                if re.match('^[0-9]+$', branch.branch_name):
                    branch.branch_url = "https://%s/pull/%s" % (branch.repo_id.base, branch.branch_name)
                else:
                    branch.branch_url = "https://%s/tree/%s" % (branch.repo_id.base, branch.branch_name)

    def _get_pull_info(self):
        self.ensure_one()
        repo = self.repo_id
        if repo.token and self.name.startswith('refs/pull/'):
            pull_number = self.name[len('refs/pull/'):]
            return repo._github('/repos/:owner/:repo/pulls/%s' % pull_number, ignore_errors=True) or {}
        return {}

    def _is_on_remote(self):
        # check that a branch still exists on remote
        self.ensure_one()
        branch = self
        repo = branch.repo_id
        try:
            repo._git(['ls-remote', '-q', '--exit-code', repo.name, branch.name])
        except CalledProcessError:
            return False
        return True

    @api.model
    def create(self, vals):
        if not vals.get('config_id') and ('use-coverage' in (vals.get('name') or '')):
            coverage_config = self.env.ref('runbot.runbot_build_config_test_coverage', raise_if_not_found=False)
            if coverage_config:
                vals['config_id'] = coverage_config

        return super(runbot_branch, self).create(vals)

    def _get_last_coverage_build(self):
        """ Return the last build with a coverage value > 0"""
        self.ensure_one()
        return self.env['runbot.build'].search([
            ('branch_id.id', '=', self.id),
            ('local_state', 'in', ['done', 'running']),
            ('coverage_result', '>=', 0.0),
        ], order='sequence desc', limit=1)

    def _compute_coverage_result(self):
        """ Compute the coverage result of the last build in branch """
        for branch in self:
            last_build = branch._get_last_coverage_build()
            branch.coverage_result = last_build.coverage_result or 0.0

    def _get_closest_branch(self, target_repo_id):
        """
            Return branch id of the closest branch based on name or pr informations.
        """
        self.ensure_one()
        Branch = self.env['runbot.branch']

        repo = self.repo_id
        name = self.pull_head_name or self.branch_name

        target_repo = self.env['runbot.repo'].browse(target_repo_id)

        target_repo_ids = [target_repo.id]
        r = target_repo.duplicate_id
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        _logger.debug('Search closest of %s (%s) in repos %r', name, repo.name, target_repo_ids)

        def sort_by_repo(branch):
            return (
                not branch.sticky,      # sticky first
                target_repo_ids.index(branch.repo_id[0].id),
                -1 * len(branch.branch_name),  # little change of logic here, was only sorted on branch_name in prefix matching case before
                -1 * branch.id
            )

        # 1. same name, not a PR
        if not self.pull_head_name:  # not a pr
            domain = [
                ('repo_id', 'in', target_repo_ids),
                ('branch_name', '=', self.branch_name),
                ('name', '=like', 'refs/heads/%'),
            ]
            targets = Branch.search(domain, order='id DESC')
            targets = sorted(targets, key=sort_by_repo)
            if targets and targets[0]._is_on_remote():
                return (targets[0], 'exact')

        # 2. PR with head name equals
        if self.pull_head_name:
            domain = [
                ('repo_id', 'in', target_repo_ids),
                ('pull_head_name', '=', self.pull_head_name),
                ('name', '=like', 'refs/pull/%'),
            ]
            pulls = Branch.search(domain, order='id DESC')
            pulls = sorted(pulls, key=sort_by_repo)
            for pull in Branch.browse([pu['id'] for pu in pulls]):
                pi = pull._get_pull_info()
                if pi.get('state') == 'open':
                    if ':' in self.pull_head_name:
                        (repo_name, pr_branch_name) = self.pull_head_name.split(':')
                        repo = self.env['runbot.repo'].browse(target_repo_ids).filtered(lambda r: ':%s/' % repo_name in r.name)
                        # most of the time repo will be pull.repo_id.duplicate_id, but it is still possible to have a pr pointing the same repo
                        if repo:
                            pr_branch_ref = 'refs/heads/%s' % pr_branch_name
                            pr_branch = self._get_or_create_branch(repo.id, pr_branch_ref)
                            # use _get_or_create_branch in case a pr is scanned before pull_head_name branch.
                            return (pr_branch, 'exact PR')
                    return (pull, 'exact PR')

        # 4.Match a PR in enterprise without community PR
        # Moved before 3 because it makes more sense
        if self.pull_head_name:
            if self.name.startswith('refs/pull'):
                if ':' in self.pull_head_name:
                    (repo_name, pr_branch_name) = self.pull_head_name.split(':')
                    repos = self.env['runbot.repo'].browse(target_repo_ids).filtered(lambda r: ':%s/' % repo_name in r.name)
                else:
                    pr_branch_name = self.pull_head_name
                    repos = target_repo
                if repos:
                    duplicate_branch_name = 'refs/heads/%s' % pr_branch_name
                    domain = [
                        ('repo_id', 'in', tuple(repos.ids)),
                        ('branch_name', '=', pr_branch_name),
                        ('pull_head_name', '=', False),
                    ]
                    targets = Branch.search(domain, order='id DESC')
                    targets = sorted(targets, key=sort_by_repo)
                    if targets and targets[0]._is_on_remote():
                        return (targets[0], 'no PR')

        # 3. Match a branch which is the dashed-prefix of current branch name
        if not self.pull_head_name:
            if '-' in self.branch_name:
                name_start = 'refs/heads/%s' % self.branch_name.split('-')[0]
                domain = [('repo_id', 'in', target_repo_ids), ('name', '=like', '%s%%' % name_start)]
                branches = Branch.search(domain, order='id DESC')
                branches = sorted(branches, key=sort_by_repo)
                for branch in branches:
                    if self.branch_name.startswith('%s-' % branch.branch_name) and branch._is_on_remote():
                        return (branch, 'prefix')

        # 5. last-resort value
        if self.target_branch_name:
            default_target_ref = 'refs/heads/%s' % self.target_branch_name
            default_branch = self.search([('repo_id', 'in', target_repo_ids), ('name', '=', default_target_ref)], limit=1)
            if default_branch:
                return (default_branch, 'pr_target')

        default_target_ref = 'refs/heads/master'
        default_branch = self.search([('repo_id', 'in', target_repo_ids), ('name', '=', default_target_ref)], limit=1)
        # we assume that master will always exists
        return (default_branch, 'default')

    def _branch_exists(self, branch_id):
        Branch = self.env['runbot.branch']
        branch = Branch.search([('id', '=', branch_id)])
        if branch and branch[0]._is_on_remote():
            return True
        return False

    def _get_or_create_branch(self, repo_id, name):
        res = self.search([('repo_id', '=', repo_id), ('name', '=', name)], limit=1)
        if res:
            return res
        _logger.warning('creating missing branch %s', name)
        Branch = self.env['runbot.branch']
        branch = Branch.create({'repo_id': repo_id, 'name': name})
        return branch

    def toggle_request_branch_rebuild(self):
        for branch in self:
            if not branch.rebuild_requested:
                branch.rebuild_requested = True
                branch.repo_id.write({'hook_time': time.time()})
            else:
                branch.rebuild_requested = False
