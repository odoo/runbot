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
