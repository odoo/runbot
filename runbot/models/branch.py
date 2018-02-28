# -*- coding: utf-8 -*-
import logging
import re
from subprocess import CalledProcessError
from odoo import models, fields, api

_logger = logging.getLogger(__name__)
_re_coverage = re.compile(r'\bcoverage\b')


class runbot_branch(models.Model):

    _name = "runbot.branch"
    _order = 'name'
    _sql_constraints = [('branch_repo_uniq', 'unique (name,repo_id)', 'The branch must be unique per repository !')]

    repo_id = fields.Many2one('runbot.repo', 'Repository', required=True, ondelete='cascade')
    name = fields.Char('Ref Name', required=True)
    branch_name = fields.Char(compute='_get_branch_name', type='char', string='Branch', readonly=1, store=True)
    branch_url = fields.Char(compute='_get_branch_url', type='char', string='Branch url', readonly=1)
    pull_head_name = fields.Char(compute='_get_pull_head_name', type='char', string='PR HEAD name', readonly=1, store=True)
    sticky = fields.Boolean('Sticky')
    coverage = fields.Boolean('Coverage')
    state = fields.Char('Status')
    modules = fields.Char("Modules to Install", help="Comma-separated list of modules to install and test.")
    job_timeout = fields.Integer('Job Timeout (minutes)', help='For default timeout: Mark it zero')
    # test_tags = fields.Char("Test tags", help="Tags for the --test-tags params (same syntax)")  # keep for next version

    @api.depends('name')
    def _get_branch_name(self):
        """compute the branch name based on ref name"""
        for branch in self:
            if branch.name:
                branch.branch_name = branch.name.split('/')[-1]

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

    def create(self, vals):
        vals.setdefault('coverage', _re_coverage.search(vals.get('name') or '') is not None)
        return super(runbot_branch, self).create(vals)

    @api.depends('branch_name')
    def _get_pull_head_name(self):
        """compute pull head name"""
        for branch in self:
            pi = self._get_pull_info()
            if pi:
                self.pull_head_name = pi['head']['ref']

    def _get_branch_quickconnect_url(self, fqdn, dest):
        self.ensure_one()
        r = {}
        r[self.id] = "http://%s/web/login?db=%s-all&login=admin&redirect=/web?debug=1" % (fqdn, dest)
        return r
