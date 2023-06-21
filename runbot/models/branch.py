# -*- coding: utf-8 -*-
import logging
import re

from collections import defaultdict
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class Branch(models.Model):
    _name = 'runbot.branch'
    _description = "Branch"
    _order = 'name'
    _rec_name = 'dname'

    _sql_constraints = [('branch_repo_uniq', 'unique (name,remote_id)', 'The branch must be unique per repository !')]

    name = fields.Char('Name', required=True)
    remote_id = fields.Many2one('runbot.remote', 'Remote', required=True, ondelete='cascade', index=True)

    head = fields.Many2one('runbot.commit', 'Head Commit', index=True)
    head_name = fields.Char('Head name', related='head.name', store=True)

    reference_name = fields.Char(compute='_compute_reference_name', string='Bundle name', store=True)
    bundle_id = fields.Many2one('runbot.bundle', 'Bundle', compute='_compute_bundle_id', store=True, ondelete='cascade', index=True)

    is_pr = fields.Boolean('IS a pr', required=True)
    pr_title = fields.Char('Pr Title')
    pr_body = fields.Char('Pr Body')
    pr_author = fields.Char('Pr Author')

    pull_head_name = fields.Char(compute='_compute_branch_infos', string='PR HEAD name', readonly=1, store=True)
    pull_head_remote_id = fields.Many2one('runbot.remote', 'Pull head repository', compute='_compute_branch_infos', store=True, index=True)
    target_branch_name = fields.Char(compute='_compute_branch_infos', string='PR target branch', store=True)
    reviewers = fields.Char('Reviewers')

    reflog_ids = fields.One2many('runbot.ref.log', 'branch_id')

    branch_url = fields.Char(compute='_compute_branch_url', string='Branch url', readonly=1)
    dname = fields.Char('Display name', compute='_compute_dname', search='_search_dname')

    alive = fields.Boolean('Alive', default=True)
    draft = fields.Boolean('Draft', compute='_compute_branch_infos', store=True)

    @api.depends('name', 'remote_id.short_name')
    def _compute_dname(self):
        for branch in self:
            branch.dname = '%s:%s' % (branch.remote_id.short_name, branch.name)

    def _search_dname(self, operator, value):
        if ':' not in value:
            return [('name', operator, value)]
        repo_short_name, branch_name = value.split(':')
        owner, repo_name = repo_short_name.split('/')
        return ['&', ('remote_id', '=', self.env['runbot.remote'].search([('owner', '=', owner), ('repo_name', '=', repo_name)]).id), ('name', operator, branch_name)]

    @api.depends('name', 'is_pr', 'target_branch_name', 'pull_head_name', 'pull_head_remote_id')
    def _compute_reference_name(self):
        """
        Unique reference for a branch inside a bundle.
            - branch_name for branches
            - branch name part of pull_head_name for pr if remote is known
            - pull_head_name (organisation:branch_name) for external pr
        """
        for branch in self:
            if branch.is_pr:
                _, name = branch.pull_head_name.split(':')
                if branch.pull_head_remote_id:
                    reference_name = name
                else:
                    reference_name = branch.pull_head_name  # repo is not known, not in repo list must be an external pr, so use complete label
                    #if ':patch-' in branch.pull_head_name:
                    #    branch.reference_name = '%s~%s' % (branch.pull_head_name, branch.name)
            else:
                reference_name = branch.name
            forced_version = branch.remote_id.repo_id.single_version  # we don't add a depend on repo.single_version to avoid mass recompute of existing branches
            if forced_version and not reference_name.startswith(f'{forced_version.name}-'):
                reference_name = f'{forced_version.name}---{reference_name}'
            branch.reference_name = reference_name

    @api.depends('name')
    def _compute_branch_infos(self, pull_info=None):
        """compute branch_url, pull_head_name and target_branch_name based on name"""
        name_to_remote = {}
        prs = self.filtered(lambda branch: branch.is_pr)
        pull_info_dict = {}
        if not pull_info and len(prs) > 30:  # this is arbitrary, we should store # page on remote
            pr_per_remote = defaultdict(list)
            for pr in prs:
                pr_per_remote[pr.remote_id].append(pr)
            for remote, prs in pr_per_remote.items():
                _logger.info('Getting info in %s for %s pr using page scan', remote.name, len(prs))
                pr_names = set([pr.name for pr in prs])
                count = 0
                for result in remote._github('/repos/:owner/:repo/pulls?state=all&sort=updated&direction=desc', ignore_errors=True, recursive=True):
                    for info in result:
                        number = str(info.get('number'))
                        pr_names.discard(number)
                        pull_info_dict[(remote, number)] = info
                    count += 1
                    if not pr_names:
                        break
                    if count > 100:
                        _logger.info('Not all pr found after 100 pages: remaining: %s', pr_names)
                        break

        for branch in self:
            #branch.target_branch_name = False
            #branch.pull_head_name = False
            #branch.pull_head_remote_id = False
            if branch.name:
                pi = branch.is_pr and (pull_info or pull_info_dict.get((branch.remote_id, branch.name)) or branch._get_pull_info())
                if pi:
                    try:
                        branch.alive = pi.get('state', False) != 'closed'
                        branch.target_branch_name = pi['base']['ref']
                        branch.pull_head_name = pi['head']['label']
                        branch.pr_title = pi['title']
                        branch.draft = pi.get('draft', False) or branch.pr_title and (branch.pr_title.startswith('[DRAFT]') or branch.pr_title.startswith('[WIP]'))
                        branch.pr_body = pi['body']
                        branch.pr_author = pi['user']['login']
                        pull_head_repo_name = False
                        if pi['head'].get('repo'):
                            pull_head_repo_name = pi['head']['repo'].get('full_name')
                            if pull_head_repo_name not in name_to_remote:
                                owner, repo_name = pull_head_repo_name.split('/')
                                name_to_remote[pull_head_repo_name] = self.env['runbot.remote'].search([('owner', '=', owner), ('repo_name', '=', repo_name)], limit=1)
                            branch.pull_head_remote_id = name_to_remote[pull_head_repo_name]
                    except (TypeError, AttributeError):
                        _logger.exception('Error for pr %s using pull_info %s', branch.name, pi)
                        raise

    @api.depends('name', 'remote_id.base_url', 'is_pr')
    def _compute_branch_url(self):
        """compute the branch url based on name"""
        for branch in self:
            if branch.name:
                if branch.is_pr:
                    branch.branch_url = "https://%s/pull/%s" % (branch.remote_id.base_url, branch.name)
                else:
                    branch.branch_url = "https://%s/tree/%s" % (branch.remote_id.base_url, branch.name)
            else:
                branch.branch_url = ''

    @api.depends('reference_name', 'remote_id.repo_id.project_id')
    def _compute_bundle_id(self):
        for branch in self:
            dummy = branch.remote_id.repo_id.project_id.dummy_bundle_id
            if branch.bundle_id == dummy:
                continue
            name = branch.reference_name
            project = branch.remote_id.repo_id.project_id or self.env.ref('runbot.main_project')
            project.ensure_one()
            bundle = self.env['runbot.bundle'].search([('name', '=', name), ('project_id', '=', project.id)])
            need_new_base = not bundle and branch.match_is_base(name)
            if (bundle.is_base or need_new_base) and branch.remote_id != branch.remote_id.repo_id.main_remote_id:
                _logger.warning('Trying to add a dev branch to base bundle, falling back on dummy bundle')
                bundle = dummy
            elif name and branch.remote_id and branch.remote_id.repo_id._is_branch_forbidden(name):
                _logger.warning('Trying to add a forbidden branch, falling back on dummy bundle')
                bundle = dummy
            elif bundle.is_base and branch.is_pr:
                _logger.warning('Trying to add pr to base bundle, falling back on dummy bundle')
                bundle = dummy
            elif not bundle:
                values = {
                    'name': name,
                    'project_id': project.id,
                }
                if need_new_base:
                    values['is_base'] = True

                if branch.is_pr and branch.target_branch_name:  # most likely external_pr, use target as version
                    base = self.env['runbot.bundle'].search([
                        ('name', '=', branch.target_branch_name),
                        ('is_base', '=', True),
                        ('project_id', '=', project.id)
                    ])
                    if base:
                        values['defined_base_id'] = base.id
                if name:
                    bundle = self.env['runbot.bundle'].create(values)  # this prevent creating a branch in UI
            branch.bundle_id = bundle

    @api.model_create_multi
    def create(self, value_list):
        branches = super().create(value_list)
        for branch in branches:
            if branch.head:
                self.env['runbot.ref.log'].create({'commit_id': branch.head.id, 'branch_id': branch.id})
        return branches

    def write(self, values):
        if 'head' in values:
            head = self.head
        super().write(values)
        if 'head' in values and head != self.head:
            self.env['runbot.ref.log'].create({'commit_id': self.head.id, 'branch_id': self.id})

    def _get_pull_info(self):
        self.ensure_one()
        remote = self.remote_id
        if self.is_pr:
            _logger.info('Getting info for %s', self.name)
            return remote._github('/repos/:owner/:repo/pulls/%s' % self.name, ignore_errors=False) or {}  # TODO catch and send a managable exception
        return {}

    def ref(self):
        return 'refs/%s/%s/%s' % (
            self.remote_id.remote_name,
            'pull' if self.is_pr else 'heads',
            self.name
        )

    def recompute_infos(self, payload=None):
        """ public method to recompute infos on demand """
        was_draft = self.draft
        was_alive = self.alive
        init_target_branch_name = self.target_branch_name
        self._compute_branch_infos(payload)
        if self.target_branch_name != init_target_branch_name:
            _logger.info('retargeting %s to %s', self.name, self.target_branch_name)
            base = self.env['runbot.bundle'].search([
                ('name', '=', self.target_branch_name),
                ('is_base', '=', True),
                ('project_id', '=', self.remote_id.repo_id.project_id.id)
            ])
            if base and self.bundle_id.defined_base_id != base:
                _logger.info('Changing base of bundle %s to %s(%s)', self.bundle_id, base.name, base.id)
                self.bundle_id.defined_base_id = base.id
                self.bundle_id._force()

        if self.draft:
            self.reviewers = ''  # reset reviewers on draft

        if was_alive and not self.alive and self.bundle_id.for_next_freeze:
            if not any(branch.alive and branch.is_pr for branch in self.bundle_id.branch_ids):
                self.bundle_id.for_next_freeze = False

        if (not self.draft and was_draft) or (self.alive and not was_alive) or (self.target_branch_name != init_target_branch_name and self.alive):
            self.bundle_id._force()

    @api.model
    def match_is_base(self, name):
        """match against is_base_regex ir.config_parameter"""
        if not name:
            return False
        icp = self.env['ir.config_parameter'].sudo()
        regex = icp.get_param('runbot.runbot_is_base_regex', False)
        if regex:
            return re.match(regex, name)


class RefLog(models.Model):
    _name = 'runbot.ref.log'
    _description = 'Ref log'
    _log_access = False

    commit_id = fields.Many2one('runbot.commit', index=True)
    branch_id = fields.Many2one('runbot.branch', index=True)
    date = fields.Datetime(default=fields.Datetime.now)
