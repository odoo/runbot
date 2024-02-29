# -*- coding: utf-8 -*-
import hashlib
import logging
import re

from collections import defaultdict
from dateutil.relativedelta import relativedelta
from markupsafe import Markup
from werkzeug.urls import url_join
from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class BuildErrorLink(models.Model):
    _name = 'runbot.build.error.link'
    _description = 'Build Build Error Extended Relation'
    _order = 'log_date desc, build_id desc'

    build_id = fields.Many2one('runbot.build', required=True, index=True)
    build_error_id = fields.Many2one('runbot.build.error', required=True, index=True, ondelete='cascade')
    log_date = fields.Datetime(string='Log date')
    host = fields.Char(related='build_id.host')
    dest = fields.Char(related='build_id.dest')
    version_id = fields.Many2one(related='build_id.version_id')
    trigger_id = fields.Many2one(related='build_id.trigger_id')
    description = fields.Char(related='build_id.description')
    build_url = fields.Char(related='build_id.build_url')

    _sql_constraints = [
        ('error_build_rel_unique', 'UNIQUE (build_id, build_error_id)', 'A link between a build and an error must be unique'),
    ]


class BuildError(models.Model):

    _name = "runbot.build.error"
    _description = "Build error"

    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = "id"

    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    summary = fields.Char('Content summary', compute='_compute_summary', store=False)
    module_name = fields.Char('Module name')  # name in ir_logging
    file_path = fields.Char('File Path')  # path in ir logging
    function = fields.Char('Function name')  # func name in ir logging
    fingerprint = fields.Char('Error fingerprint', index=True)
    random = fields.Boolean('underterministic error', tracking=True)
    responsible = fields.Many2one('res.users', 'Assigned fixer', tracking=True)
    team_id = fields.Many2one('runbot.team', 'Assigned team', tracking=True)
    fixing_commit = fields.Char('Fixing commit', tracking=True)
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', tracking=True, domain=[('is_pr', '=', True)])
    fixing_pr_alive = fields.Boolean('Fixing PR alive', related='fixing_pr_id.alive')
    fixing_pr_url = fields.Char('Fixing PR url', related='fixing_pr_id.branch_url')
    build_error_link_ids = fields.One2many('runbot.build.error.link', 'build_error_id')
    children_build_error_link_ids = fields.One2many('runbot.build.error.link', compute='_compute_children_build_error_link_ids')
    build_ids = fields.Many2many('runbot.build', compute= '_compute_build_ids')
    bundle_ids = fields.One2many('runbot.bundle', compute='_compute_bundle_ids')
    version_ids = fields.One2many('runbot.version', compute='_compute_version_ids', string='Versions', search='_search_version')
    trigger_ids = fields.Many2many('runbot.trigger', compute='_compute_trigger_ids', string='Triggers', search='_search_trigger_ids')
    active = fields.Boolean('Active (not fixed)', default=True, tracking=True)
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags')
    build_count = fields.Integer(compute='_compute_build_counts', string='Nb seen', store=True)
    parent_id = fields.Many2one('runbot.build.error', 'Linked to', index=True)
    child_ids = fields.One2many('runbot.build.error', 'parent_id', string='Child Errors', context={'active_test': False})
    children_build_ids = fields.Many2many('runbot.build', compute='_compute_children_build_ids', string='Children builds')
    error_history_ids = fields.Many2many('runbot.build.error', compute='_compute_error_history_ids', string='Old errors', context={'active_test': False})
    first_seen_build_id = fields.Many2one('runbot.build', compute='_compute_first_seen_build_id', string='First Seen build')
    first_seen_date = fields.Datetime(string='First Seen Date', compute='_compute_seen_date', store=True)
    last_seen_build_id = fields.Many2one('runbot.build', compute='_compute_last_seen_build_id', string='Last Seen build', store=True)
    last_seen_date = fields.Datetime(string='Last Seen Date', compute='_compute_seen_date', store=True)
    test_tags = fields.Char(string='Test tags', help="Comma separated list of test_tags to use to reproduce/remove this error", tracking=True)

    @api.constrains('test_tags')
    def _check_test_tags(self):
        for build_error in self:
            if build_error.test_tags and '-' in build_error.test_tags:
                raise ValidationError('Build error test_tags should not be negated')

    @api.model_create_multi
    def create(self, vals_list):
        cleaners = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        for vals in vals_list:
            content = vals.get('content')
            cleaned_content = cleaners._r_sub(content)
            vals.update({
                'cleaned_content': cleaned_content,
                'fingerprint': self._digest(cleaned_content)
            })
        records = super().create(vals_list)
        records.action_assign()
        return records

    def write(self, vals):
        if 'active' in vals:
            for build_error in self:
                (build_error.child_ids - self).write({'active': vals['active']})
                if not (self.env.su or self.user_has_groups('runbot.group_runbot_admin')):
                    if build_error.test_tags:
                        raise UserError("This error as a test-tag and can only be (de)activated by admin")
                    if not vals['active'] and build_error.last_seen_date + relativedelta(days=1) > fields.Datetime.now():
                        raise UserError("This error broke less than one day ago can only be deactivated by admin")
        if 'cleaned_content' in vals:
            vals.update({'fingerprint': self._digest(vals['cleaned_content'])})
        result = super(BuildError, self).write(vals)
        if vals.get('parent_id'):
            for build_error in self:
                parent = build_error.parent_id
                if build_error.test_tags:
                    if parent.test_tags and not self.env.su:
                        raise UserError(f"Cannot parent an error with test tags: {build_error.test_tags}")
                    elif not parent.test_tags:
                        parent.sudo().test_tags = build_error.test_tags
                        build_error.sudo().test_tags = False
                if build_error.responsible:
                    if parent.responsible and parent.responsible != build_error.responsible and not self.env.su:
                        raise UserError(f"Error {parent.id} as already a responsible ({parent.responsible}) cannot assign {build_error.responsible}")
                    else:
                        parent.responsible = build_error.responsible
                        build_error.responsible = False
                if build_error.team_id:
                    if not parent.team_id:
                        parent.team_id = build_error.team_id
                    build_error.team_id = False
        return result

    @api.depends('build_error_link_ids')
    def _compute_build_ids(self):
        for record in self:
            record.build_ids = record.build_error_link_ids.mapped('build_id')

    @api.depends('build_error_link_ids')
    def _compute_children_build_error_link_ids(self):
        for record in self:
            record.children_build_error_link_ids = record.build_error_link_ids | record.child_ids.build_error_link_ids

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_build_counts(self):
        for build_error in self:
            build_error.build_count = len(build_error.build_ids | build_error.mapped('child_ids.build_ids'))

    @api.depends('build_ids')
    def _compute_bundle_ids(self):
        for build_error in self:
            top_parent_builds = build_error.build_ids.mapped(lambda rec: rec and rec.top_parent)
            build_error.bundle_ids = top_parent_builds.mapped('slot_ids').mapped('batch_id.bundle_id')

    @api.depends('children_build_ids')
    def _compute_version_ids(self):
        for build_error in self:
            build_error.version_ids = build_error.children_build_ids.version_id 

    @api.depends('children_build_ids')
    def _compute_trigger_ids(self):
        for build_error in self:
            build_error.trigger_ids = build_error.children_build_ids.trigger_id

    @api.depends('content')
    def _compute_summary(self):
        for build_error in self:
            build_error.summary = build_error.content[:80]


    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_children_build_ids(self):
        for build_error in self:
            all_builds = build_error.build_ids | build_error.mapped('child_ids.build_ids')
            build_error.children_build_ids = all_builds.sorted(key=lambda rec: rec.id, reverse=True)

    @api.depends('children_build_ids')
    def _compute_last_seen_build_id(self):
        for build_error in self:
            build_error.last_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[0] or False

    @api.depends('build_error_link_ids', 'child_ids.build_error_link_ids')
    def _compute_seen_date(self):
        for build_error in self:
            error_dates = (build_error.build_error_link_ids | build_error.child_ids.build_error_link_ids).mapped('log_date')
            build_error.first_seen_date = error_dates and min(error_dates)
            build_error.last_seen_date = error_dates and max(error_dates)

    @api.depends('children_build_ids')
    def _compute_first_seen_build_id(self):
        for build_error in self:
            build_error.first_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[-1] or False

    @api.depends('fingerprint', 'child_ids.fingerprint')
    def _compute_error_history_ids(self):
        for error in self:
            fingerprints = [error.fingerprint] + [rec.fingerprint for rec in error.child_ids]
            error.error_history_ids = self.search([('fingerprint', 'in', fingerprints), ('active', '=', False), ('id', '!=', error.id or False)])

    @api.model
    def _digest(self, s):
        """
        return a hash 256 digest of the string s
        """
        return hashlib.sha256(s.encode()).hexdigest()

    @api.model
    def _parse_logs(self, ir_logs):
        if not ir_logs:
            return
        regexes = self.env['runbot.error.regex'].search([])
        search_regs = regexes.filtered(lambda r: r.re_type == 'filter')
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')

        hash_dict = defaultdict(self.env['ir.logging'].browse)
        for log in ir_logs:
            if search_regs._r_search(log.message):
                continue
            fingerprint = self._digest(cleaning_regs._r_sub(log.message))
            hash_dict[fingerprint] |= log

        build_errors = self.env['runbot.build.error']
        # add build ids to already detected errors
        existing_errors = self.env['runbot.build.error'].search([('fingerprint', 'in', list(hash_dict.keys())), ('active', '=', True)])
        existing_fingerprints = existing_errors.mapped('fingerprint')
        build_errors |= existing_errors
        for build_error in existing_errors:
            logs = hash_dict[build_error.fingerprint]
            # update filepath if it changed. This is optionnal and mainly there in case we adapt the OdooRunner log 
            if logs[0].path != build_error.file_path:
                build_error.file_path = logs[0].path
                build_error.function = logs[0].func

        # create an error for the remaining entries
        for fingerprint, logs in hash_dict.items():
            if fingerprint in existing_fingerprints:
                continue
            new_build_error = self.env['runbot.build.error'].create({
                'content': logs[0].message,
                'module_name': logs[0].name.removeprefix('odoo.').removeprefix('addons.'),
                'file_path': logs[0].path,
                'function': logs[0].func,
            })
            build_errors |= new_build_error
            existing_fingerprints.append(fingerprint)

        for build_error in build_errors:
            logs = hash_dict[build_error.fingerprint]
            for rec in logs:
                if rec.build_id not in build_error.build_error_link_ids.build_id:
                    self.env['runbot.build.error.link'].create({
                        'build_id': rec.build_id.id,
                        'build_error_id': build_error.id,
                        'log_date': rec.create_date
                    })

        if build_errors:
            window_action = {
                "type": "ir.actions.act_window",
                "res_model": "runbot.build.error",
                "views": [[False, "tree"]],
                "domain": [('id', 'in', build_errors.ids)]
            }
            if len(build_errors) == 1:
                window_action["views"] = [[False, "form"]]
                window_action["res_id"] = build_errors.id
            return window_action

    @api.model
    def _test_tags_list(self):
        active_errors = self.search([('test_tags', '!=', False)])
        test_tag_list = active_errors.mapped('test_tags')
        return [test_tag for error_tags in test_tag_list for test_tag in (error_tags).split(',')]

    @api.model
    def _disabling_tags(self):
        return ['-%s' % tag for tag in self._test_tags_list()]

    def _search_version(self, operator, value):
        exclude_domain = []
        if operator == '=':
            exclude_ids = self.env['runbot.build.error'].search([('version_ids', '!=', value)])
            exclude_domain = [('id', 'not in', exclude_ids.ids)]
        return [('build_error_link_ids.version_id', operator, value)] + exclude_domain

    def _search_trigger_ids(self, operator, value):
        return [('build_error_link_ids.trigger_id', operator, value)]

    def _get_form_url(self):
        self.ensure_one()
        return url_join(self.get_base_url(), f'/web#id={self.id}&model=runbot.build.error&view_type=form')

    def _get_form_link(self):
        self.ensure_one()
        return Markup(f'<a href="%s">%s</a>') % (self._get_form_url(), self.id)

    def _merge(self):
        if len(self) < 2:
            return
        _logger.debug('Merging errors %s', self)
        base_error = self[0]
        base_linked = self[0].parent_id or self[0]
        for error in self[1:]:
            assert base_error.fingerprint == error.fingerprint, f'Errors {base_error.id} and {error.id} have a different fingerprint'
            if error.test_tags and not base_linked.test_tags:
                base_linked.test_tags = error.test_tags
                if not base_linked.active and error.active:
                    base_linked.active = True
                base_error.message_post(body=Markup('⚠ test-tags inherited from error %s') % error._get_form_link())
            elif base_linked.test_tags and error.test_tags and base_linked.test_tags != error.test_tags:
                base_error.message_post(body=Markup('⚠ trying to merge errors with different test-tags from %s tag: "%s"') % (error._get_form_link(), error.test_tags))
                error.message_post(body=Markup('⚠ trying to merge errors with different test-tags from %s tag: "%s"') % (base_error._get_form_link(), base_error.test_tags))
                continue

            for build_error_link in error.build_error_link_ids:
                if build_error_link.build_id not in base_error.build_error_link_ids.build_id:
                    build_error_link.build_error_id = base_error
                else:
                    # as the relation already exists and was not transferred we can remove the old one
                    build_error_link.unlink()

            if error.responsible and not base_linked.responsible:
                base_error.responsible = error.responsible
            elif base_linked.responsible and error.responsible and base_linked.responsible != error.responsible:
                base_linked.message_post(body=Markup('⚠ responsible in merged error %s was "%s" and different from this one') % (error._get_form_link(), error.responsible.name))

            if error.team_id and not base_error.team_id:
                base_error.team_id = error.team_id

            base_error.message_post(body=Markup('Error %s was merged into this one') % error._get_form_link())
            error.message_post(body=Markup('Error was merged into %s') % base_linked._get_form_link())
            error.child_ids.parent_id = base_error
            error.active = False

    ####################
    #   Actions
    ####################

    def action_link_errors(self):
        """ Link errors with the first one of the recordset
        choosing parent in error with responsible, random bug and finally fisrt seen
        """
        if len(self) < 2:
            return
        self = self.with_context(active_test=False)
        build_errors = self.search([('id', 'in', self.ids)], order='responsible asc, random desc, id asc')
        build_errors[1:].write({'parent_id': build_errors[0].id})

    def action_clean_content(self):
        _logger.info('Cleaning %s build errors', len(self))
        cleaning_regs = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])

        changed_fingerprints = set()
        for build_error in self:
            fingerprint_before = build_error.fingerprint
            build_error.cleaned_content = cleaning_regs._r_sub(build_error.content)
            if fingerprint_before != build_error.fingerprint:
                changed_fingerprints.add(build_error.fingerprint)

        # merge identical errors
        errors_by_fingerprint = self.env['runbot.build.error'].search([('fingerprint', 'in', list(changed_fingerprints))])
        for fingerprint in changed_fingerprints:
            errors_to_merge = errors_by_fingerprint.filtered(lambda r: r.fingerprint == fingerprint)
            errors_to_merge._merge()

    def action_assign(self):
        if not any((not record.responsible and not record.team_id and record.file_path and not record.parent_id) for record in self):
            return
        teams = self.env['runbot.team'].search(['|', ('path_glob', '!=', False), ('module_ownership_ids', '!=', False)])
        repos = self.env['runbot.repo'].search([])
        for record in self:
            if not record.responsible and not record.team_id and record.file_path and not record.parent_id:
                team = teams._get_team(record.file_path, repos)
                if team:
                    record.team_id = team


class BuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"
    _description = "Build error tag"

    name = fields.Char('Tag')
    error_ids = fields.Many2many('runbot.build.error', string='Errors')


class ErrorRegex(models.Model):

    _name = "runbot.error.regex"
    _description = "Build error regex"
    _inherit = "mail.thread"
    _rec_name = 'id'
    _order = 'sequence, id'

    regex = fields.Char('Regular expression')
    re_type = fields.Selection([('filter', 'Filter out'), ('cleaning', 'Cleaning')], string="Regex type")
    sequence = fields.Integer('Sequence', default=100)
    replacement = fields.Char('Replacement string', help="String used as a replacment in cleaning. '%' if not set")

    def _r_sub(self, s):
        """ replaces patterns from the recordset by replacement's or '%' in the given string """
        for c in self:
            s = re.sub(c.regex, c.replacement or '%', s)
        return s

    def _r_search(self, s):
        """ Return True if one of the regex is found in s """
        for filter in self:
            if re.search(filter.regex, s):
                return True
        return False


class ErrorBulkWizard(models.TransientModel):
    _name = 'runbot.error.bulk.wizard'
    _description = "Errors Bulk Wizard"

    team_id = fields.Many2one('runbot.team', 'Assigned team')
    responsible_id = fields.Many2one('res.users', 'Assigned fixer')
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', domain=[('is_pr', '=', True)])
    fixing_commit = fields.Char('Fixing commit')
    archive = fields.Boolean('Close error (archive)', default=False)
    chatter_comment = fields.Text('Chatter Comment')

    @api.onchange('fixing_commit', 'chatter_comment')
    def _onchange_commit_comment(self):
        for record in self:
            if record.fixing_commit or record.chatter_comment:
                record.archive = True

    def action_submit(self):
        error_ids = self.env['runbot.build.error'].browse(self.env.context.get('active_ids'))
        if error_ids:
            if self.team_id:
                error_ids['team_id'] = self.team_id
            if self.responsible_id:
                error_ids['responsible'] = self.responsible_id
            if self.fixing_pr_id:
                error_ids['fixing_pr_id'] = self.fixing_pr_id
            if self.fixing_commit:
                error_ids['fixing_commit'] = self.fixing_commit
            if self.archive:
                error_ids['active'] = False
            if self.chatter_comment:
                for build_error in error_ids:
                    build_error.message_post(body=Markup('%s') % self.chatter_comment, subject="Bullk Wizard Comment")
