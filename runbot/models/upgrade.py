import re
from odoo import models, fields, api
from odoo.exceptions import UserError


class UpgradeExceptions(models.Model):
    _name = 'runbot.upgrade.exception'
    _description = 'Upgrade exception'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _mail_post_access = 'read'

    active = fields.Boolean('Active', default=True, tracking=True)
    elements = fields.Text('Elements', required=True, tracking=True)
    bundle_id = fields.Many2one('runbot.bundle', index=True)
    create_build_id = fields.Many2one('runbot.build', 'Build')
    pr_ids = fields.Many2many('runbot.branch', string='Pull requests', default=lambda self: self.default_pr_ids())
    info = fields.Text('Info')
    team_id = fields.Many2one('runbot.team', 'Assigned team', index=True)
    message = fields.Text('Upgrade exception message', compute="_compute_message", store=True)

    def action_post_message(self):
        if not self.env.user.has_group('runbot.group_runbot_upgrade_exception_manager'):
            raise UserError('You are not allowed to send messages')
        for pr in self.pr_ids:
            pr.remote_id.sudo()._github('/repos/:owner/:repo/issues/%s/comments' % pr.name, {'body': self.message})

    def action_auto_rebuild(self):
        if not self.env.user.has_group('runbot.group_runbot_upgrade_exception_manager'):
            raise UserError('You are not allowed to rebuild templates')
        builds = self.create_build_id.parent_id.children_ids if self.create_build_id.parent_id else self.create_build_id
        for build in builds:
            if not build.orphan_result and build.local_result != 'ok':
                build.sudo()._rebuild()

    @api.depends('create_date')
    def _compute_message(self):
        message_layout = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_upgrade_exception_message')
        for exception in self:
            exception.message = message_layout.format(exception=exception, base_url=exception.get_base_url())

    def _generate(self):
        exceptions = self.search([])
        if exceptions:
            return 'suppress_upgrade_warnings=%s' % (','.join(exceptions.mapped('elements'))).replace(' ', '').replace('\n', ',')
        return False

    def default_pr_ids(self):
        bundle_id = self.env.context.get('default_bundle_id')
        if bundle_id:
            return self.env['runbot.branch'].search([('bundle_id', '=', bundle_id), ('is_pr', '=', True), ('alive', '=', True)])


class UpgradeRegex(models.Model):
    _name = 'runbot.upgrade.regex'
    _description = 'Upgrade regex'

    active = fields.Boolean('Active', default=True)
    prefix = fields.Char('Type')
    regex = fields.Char('Regex')


class BuildResult(models.Model):
    _inherit = 'runbot.build'

    def _parse_upgrade_errors(self):
        ir_logs = self.env['ir.logging'].search([('level', 'in', ('ERROR', 'WARNING', 'CRITICAL')), ('type', '=', 'server'), ('build_id', 'in', self.ids)])

        upgrade_regexes = self.env['runbot.upgrade.regex'].search([])
        exception = {}
        for log in ir_logs:
            for upgrade_regex in upgrade_regexes:
                m = re.search(upgrade_regex.regex, log.message)
                if m:
                    exception['%s:%s' % (upgrade_regex.prefix, m.groups()[0])] = None
        exception = list(exception)
        if exception:
            bundle = False
            batches = self.top_parent.slot_ids.mapped('batch_id')
            if batches:
                bundle = batches[0].bundle_id.id
            res = {
                'name': 'Upgrade Exception',
                'type': 'ir.actions.act_window',
                'res_model': 'runbot.upgrade.exception',
                'view_mode': 'form',
                'context': {
                    'default_elements': '\n'.join(exception),
                    'default_bundle_id': bundle,
                    'default_create_build_id': self.id,
                    'default_info': 'Automatically generated from build %s' % self.id
                }
            }
            return res
        else:
            raise UserError('Nothing found here')


class UpgradeMatrix(models.Model):
    _name = 'runbot.upgrade.matrix'
    _description = 'Upgrade matrix'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Name', required=True)
    project_id = fields.Many2one('runbot.project', 'Project', required=True)
    entry_ids = fields.One2many('runbot.upgrade.matrix.entry', 'matrix_id', 'Entries')
    auto_update = fields.Boolean('Auto update', default=True, help="Automatically update the matrix entries enabled state when new versions are created")

    # fields defining default behaviour to generate the matix
    upgrade_to_major_versions = fields.Boolean()
    upgrade_to_all_versions = fields.Boolean()
    upgrade_from_previous_major_version = fields.Boolean()
    upgrade_from_last_intermediate_version = fields.Boolean()
    upgrade_from_all_intermediate_version = fields.Boolean()
    step_ids = fields.One2many('runbot.build.config.step', 'upgrade_matrix_id', 'Upgrade steps', readonly=True)

    matrix_summary = fields.Text('Matrix summary', compute='_compute_matrix_summary', store=True, tracking=True)

    @api.depends('entry_ids.from_version_id', 'entry_ids.to_version_id', 'entry_ids.enabled')
    def _compute_matrix_summary(self):
        for matrix in self:
            matrix.matrix_summary = ''
            versions = {}
            lines = []
            for entry in self.entry_ids.sorted(lambda e: (e.to_version_id.number, e.from_version_id.number)):
                if entry.enabled:
                    versions.setdefault(entry.to_version_id, []).append(entry.from_version_id.number)
                else:
                    versions.setdefault(entry.to_version_id, []).append('-')
            for to_version, from_versions in versions.items():
                from_versions_string = ', '.join(sorted(from_versions))
                lines.append(f'{to_version.number} - ({from_versions_string})')
            matrix.matrix_summary = '\n'.join(lines)

    def update_matrix_entries(self):
        for metric in self:
            metric._update_matrix_entries()

    def _update_matrix_entries(self):
        self.ensure_one()
        existing_entries = self.with_context(active_test=False).entry_ids
        entries_per_versions = {(e.from_version_id.id, e.to_version_id.id): e for e in existing_entries}

        # get all versions
        versions = self.env['runbot.bundle'].search([('project_id', '=', self.project_id.id), ('is_base', '=', True), ('sticky', '=', True)]).mapped('version_id').sorted('number')
        valid_transitions = []
        for target_version in versions:
            compatible_versions = target_version.intermediate_version_ids | target_version.previous_major_version_id
            for source_version in compatible_versions:
                valid_transitions.append((source_version, target_version))
                if (source_version.id, target_version.id) not in entries_per_versions:
                    if target_version == source_version:
                        continue
                    self.env['runbot.upgrade.matrix.entry'].create({
                        'matrix_id': self.id,
                        'from_version_id': source_version.id,
                        'to_version_id': target_version.id,
                    })

        for existing_entry in existing_entries:
            if (existing_entry.from_version_id, existing_entry.to_version_id) not in valid_transitions:
                self.message_post(body=f'Removed upgrade matrix entry from {existing_entry.from_version_id.number} to {existing_entry.to_version_id.number} as no longer valid transition')
                existing_entry.unlink()
                existing_entries -= existing_entry

        if self.auto_update:
            existing_entries._update_enabled()

    def reset_matrix_enabled(self):
        for matrix in self:
            matrix.entry_ids._update_enabled(force=True)

    def _get_target_versions(self):
        return self.entry_ids.filtered(lambda e: e.enabled).mapped('to_version_id')

    def _get_target_versions_from(self, from_version):
        return self.entry_ids.filtered(lambda e: e.enabled and e.from_version_id == from_version).mapped('to_version_id')

    def _get_source_versions_to(self, to_version):
        return self.entry_ids.filtered(lambda e: e.enabled and e.to_version_id == to_version).mapped('from_version_id')


class UpgradeMatrixEntry(models.Model):
    _name = 'runbot.upgrade.matrix.entry'
    _description = 'Upgrade matrix entry'
    _order = 'to_version_number desc, from_version_number desc, id desc'

    matrix_id = fields.Many2one('runbot.upgrade.matrix', 'Matrix', required=True, ondelete='cascade')
    from_version_id = fields.Many2one('runbot.version', 'From version', required=True, ondelete='cascade')
    to_version_id = fields.Many2one('runbot.version', 'To version', required=True, ondelete='cascade')
    from_version_number = fields.Char(related='from_version_id.number', string="To version number", store=True)
    to_version_number = fields.Char(related='to_version_id.number', string="From version number", store=True)
    target_bundle_id = fields.Many2one('runbot.bundle', compute='_compute_target_bundle_id', store=True)
    enabled = fields.Boolean('Enabled', default=True)
    active = fields.Boolean('Active', compute='_compute_active', store=True)
    manually_edited = fields.Boolean('Manually edited', default=False)

    _runbot_unique_matrix_entry = models.Constraint(
        'unique(matrix_id, from_version_id, to_version_id)',
        "Matrix entry already exists",
    )

    @api.onchange('enabled')
    def _onchange_enabled(self):
        self.manually_edited = True

    def create(self, vals):
        entry = super().create(vals)
        entry._update_enabled()

    @api.depends('to_version_id', 'matrix_id.project_id')
    def _compute_target_bundle_id(self):
        for entry in self:
            entry.target_bundle_id = entry.to_version_id.with_context(project_id=entry.matrix_id.project_id.id).base_bundle_id

    @api.depends('target_bundle_id.sticky')
    def _compute_active(self):
        for entry in self:
            entry.active = entry.target_bundle_id.sticky

    def _update_enabled(self, force=False):
        for entry in self:
            if entry.manually_edited and not force:
                continue
            entry.manually_edited = False

            matrix = entry.matrix_id
            to_enabled = False
            from_enabled = False

            if matrix.upgrade_to_all_versions or (matrix.upgrade_to_major_versions and entry.to_version_id.is_major):
                to_enabled = True

            if not to_enabled:
                entry.enabled = False
                continue

            if (
                matrix.upgrade_from_all_intermediate_version or
                (matrix.upgrade_from_last_intermediate_version and entry.to_version_id.intermediate_version_ids and entry.from_version_id == entry.to_version_id.intermediate_version_ids[-1]) or
                (matrix.upgrade_from_previous_major_version and entry.from_version_id == entry.to_version_id.previous_major_version_id)
            ):
                from_enabled = True
            entry.enabled = to_enabled and from_enabled
