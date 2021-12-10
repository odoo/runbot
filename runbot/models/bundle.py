import time
import logging
import datetime
import subprocess

from collections import defaultdict
from odoo import models, fields, api, tools
from ..common import dt2time, s2human_long

_logger = logging.getLogger(__name__)


class Bundle(models.Model):
    _name = 'runbot.bundle'
    _description = "Bundle"

    name = fields.Char('Bundle name', required=True, help="Name of the base branch")
    project_id = fields.Many2one('runbot.project', required=True, index=True)
    branch_ids = fields.One2many('runbot.branch', 'bundle_id')

    # custom behaviour
    no_build = fields.Boolean('No build')
    no_auto_run = fields.Boolean('No run')
    build_all = fields.Boolean('Force all triggers')
    modules = fields.Char("Modules to install", help="Comma-separated list of modules to install and test.")

    batch_ids = fields.One2many('runbot.batch', 'bundle_id')
    last_batch = fields.Many2one('runbot.batch', index=True, domain=lambda self: [('category_id', '=', self.env.ref('runbot.default_category').id)])
    last_batchs = fields.Many2many('runbot.batch', 'Last batchs', compute='_compute_last_batchs')
    last_done_batch = fields.Many2many('runbot.batch', 'Last batchs', compute='_compute_last_done_batch')

    sticky = fields.Boolean('Sticky', compute='_compute_sticky', store=True, index=True)
    is_base = fields.Boolean('Is base', index=True)
    defined_base_id = fields.Many2one('runbot.bundle', 'Forced base bundle', domain="[('project_id', '=', project_id), ('is_base', '=', True)]")
    base_id = fields.Many2one('runbot.bundle', 'Base bundle', compute='_compute_base_id', store=True)
    to_upgrade = fields.Boolean('To upgrade', compute='_compute_to_upgrade', store=True, index=False)

    version_id = fields.Many2one('runbot.version', 'Version', compute='_compute_version_id', store=True)
    version_number = fields.Char(related='version_id.number', store=True, index=True)

    previous_major_version_base_id = fields.Many2one('runbot.bundle', 'Previous base bundle', compute='_compute_relations_base_id')
    intermediate_version_base_ids = fields.Many2many('runbot.bundle', 'Intermediate base bundles', compute='_compute_relations_base_id')

    priority = fields.Boolean('Build priority', default=False)

    # Custom parameters
    trigger_custom_ids = fields.One2many('runbot.bundle.trigger.custom', 'bundle_id')
    host_id = fields.Many2one('runbot.host', compute="_compute_host_id", store=True)
    dockerfile_id = fields.Many2one('runbot.dockerfile', index=True, help="Use a custom Dockerfile")
    commit_limit = fields.Integer("Commit limit")
    file_limit = fields.Integer("File limit")

    @api.depends('name')
    def _compute_host_id(self):
        assigned_only = None
        runbots = {}
        for bundle in self.filtered('name'):
            elems = bundle.name.split('-')
            for elem in elems:
                if elem.startswith('runbot'):
                    if elem.replace('runbot', '') == '_x':
                        if assigned_only is None:
                            assigned_only = self.env['runbot.host'].search([('assigned_only', '=', True)], limit=1)
                        bundle.host_id = assigned_only or False
                    elif elem.replace('runbot', '').isdigit():
                        if elem not in runbots:
                            runbots[elem] = self.env['runbot.host'].search([('name', 'like', '%s%%' % elem)], limit=1)
                        bundle.host_id = runbots[elem] or False

    @api.depends('sticky')
    def _compute_make_stats(self):
        for bundle in self:
            bundle.make_stats = bundle.sticky

    @api.depends('is_base')
    def _compute_sticky(self):
        for bundle in self:
            bundle.sticky = bundle.is_base

    @api.depends('is_base')
    def _compute_to_upgrade(self):
        for bundle in self:
            bundle.to_upgrade = bundle.is_base

    @api.depends('name', 'is_base', 'defined_base_id', 'base_id.is_base', 'project_id')
    def _compute_base_id(self):
        for bundle in self:
            if bundle.is_base:
                bundle.base_id = bundle
                continue
            if bundle.defined_base_id:
                bundle.base_id = bundle.defined_base_id
                continue
            project_id = bundle.project_id.id
            master_base = False
            fallback = False
            for bid, bname in self._get_base_ids(project_id):
                if bundle.name.startswith('%s-' % bname):
                    bundle.base_id = self.browse(bid)
                    break
                elif bname == 'master':
                    master_base = self.browse(bid)
                elif not fallback or fallback.id < bid:
                    fallback = self.browse(bid)
            else:
                bundle.base_id = master_base or fallback

    @tools.ormcache('project_id')
    def _get_base_ids(self, project_id):
        return [(b.id, b.name) for b in self.search([('is_base', '=', True), ('project_id', '=', project_id)])]

    @api.depends('is_base', 'base_id.version_id')
    def _compute_version_id(self):
        for bundle in self.sorted(key='is_base', reverse=True):
            if not bundle.is_base:
                bundle.version_id = bundle.base_id.version_id
                continue
            bundle.version_id = self.env['runbot.version']._get(bundle.name)

    @api.depends('version_id')
    def _compute_relations_base_id(self):
        for bundle in self:
            bundle = bundle.with_context(project_id=bundle.project_id.id)
            bundle.previous_major_version_base_id = bundle.version_id.previous_major_version_id.base_bundle_id
            bundle.intermediate_version_base_ids = bundle.version_id.intermediate_version_ids.mapped('base_bundle_id')

    @api.depends_context('category_id')
    def _compute_last_batchs(self):
        if self:
            batch_ids = defaultdict(list)
            category_id = self.env.context.get('category_id', self.env['ir.model.data'].xmlid_to_res_id('runbot.default_category'))
            self.env.cr.execute("""
                SELECT
                    id
                FROM (
                    SELECT
                        batch.id AS id,
                        row_number() OVER (PARTITION BY batch.bundle_id order by batch.id desc) AS row
                    FROM
                        runbot_bundle bundle INNER JOIN runbot_batch batch ON bundle.id=batch.bundle_id
                    WHERE
                        bundle.id in %s
                        AND batch.category_id = %s
                    ) AS bundle_batch
                WHERE
                    row <= 4
                ORDER BY row, id desc
                """, [tuple(self.ids), category_id]
            )
            batchs = self.env['runbot.batch'].browse([r[0] for r in self.env.cr.fetchall()])
            for batch in batchs:
                batch_ids[batch.bundle_id.id].append(batch.id)

            for bundle in self:
                bundle.last_batchs = [(6, 0, batch_ids[bundle.id])]

    @api.depends_context('category_id')
    def _compute_last_done_batch(self):
        if self:
            # self.env['runbot.batch'].flush()
            for bundle in self:
                bundle.last_done_batch = False
            category_id = self.env.context.get('category_id', self.env['ir.model.data'].xmlid_to_res_id('runbot.default_category'))
            self.env.cr.execute("""
                SELECT
                    id
                FROM (
                    SELECT
                        batch.id AS id,
                        row_number() OVER (PARTITION BY batch.bundle_id order by batch.id desc) AS row
                    FROM
                        runbot_bundle bundle INNER JOIN runbot_batch batch ON bundle.id=batch.bundle_id
                    WHERE
                        bundle.id in %s
                        AND batch.state = 'done'
                        AND batch.category_id = %s
                    ) AS bundle_batch
                WHERE
                    row = 1
                ORDER BY row, id desc
                """, [tuple(self.ids), category_id]
            )
            batchs = self.env['runbot.batch'].browse([r[0] for r in self.env.cr.fetchall()])
            for batch in batchs:
                batch.bundle_id.last_done_batch = batch

    def _url(self):
        self.ensure_one()
        return "/runbot/bundle/%s" % self.id


    def create(self, values_list):
        res = super().create(values_list)
        if res.is_base:
            model = self.browse()
            model._get_base_ids.clear_cache(model)
        return res

    def write(self, values):
        super().write(values)
        if 'is_base' in values:
            model = self.browse()
            model._get_base_ids.clear_cache(model)

    def _force(self, category_id=None):
        self.ensure_one()
        if self.last_batch.state == 'preparing':
            return
        values = {
            'last_update': fields.Datetime.now(),
            'bundle_id': self.id,
            'state': 'preparing',
        }
        if category_id:
            values['category_id'] = category_id
        new = self.env['runbot.batch'].create(values)
        self.last_batch = new
        return new

    def consistency_warning(self):
        if self.defined_base_id:
            return [('info', 'This bundle has a forced base: %s' % self.defined_base_id.name)]
        warnings = []
        if not self.base_id:
            warnings.append(('warning', 'No base defined on this bundle'))
        else:
            for branch in self.branch_ids:
                if branch.is_pr and branch.target_branch_name != self.base_id.name:
                    if branch.target_branch_name.startswith(self.base_id.name):
                        warnings.append(('info', 'PR %s targeting a non base branch: %s' % (branch.dname, branch.target_branch_name)))
                    else:
                        warnings.append(('warning' if branch.alive else 'info', 'PR %s targeting wrong version: %s (expecting %s)' % (branch.dname, branch.target_branch_name, self.base_id.name)))
                elif not branch.is_pr and not branch.name.startswith(self.base_id.name) and not self.defined_base_id:
                    warnings.append(('warning', 'Branch %s not starting with version name (%s)' % (branch.dname, self.base_id.name)))
        return warnings

    def branch_groups(self):
        self.branch_ids.sorted(key=lambda b: (b.remote_id.repo_id.sequence, b.remote_id.repo_id.id, b.is_pr))
        branch_groups = {repo: [] for repo in self.branch_ids.mapped('remote_id.repo_id').sorted('sequence')}
        for branch in self.branch_ids.sorted(key=lambda b: (b.is_pr)):
            branch_groups[branch.remote_id.repo_id].append(branch)
        return branch_groups


class BundleTriggerCustomisation(models.Model):
    _name = 'runbot.bundle.trigger.custom'
    _description = 'Custom trigger'

    trigger_id = fields.Many2one('runbot.trigger', domain="[('project_id', '=', bundle_id.project_id)]")
    bundle_id = fields.Many2one('runbot.bundle')
    config_id = fields.Many2one('runbot.build.config')
    extra_params = fields.Char("Custom parameters")

    _sql_constraints = [
        (
            "bundle_custom_trigger_unique",
            "unique (bundle_id, trigger_id)",
            "Only one custom trigger per trigger per bundle is allowed",
        )
    ]
