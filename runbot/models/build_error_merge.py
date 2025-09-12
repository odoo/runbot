import logging

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


class BuildErrorMerge(models.Model):
    _name = 'runbot.build.error.merge'
    _description = 'Error Merge patterns'
    _inherit = ['mail.thread']

    active = fields.Boolean('Active', default=True)
    name = fields.Char('Name', required=True)
    merge_filter_ids = fields.One2many('runbot.build.error.merge.filters', 'error_merge_id', 'Merge Lines')
    description = fields.Char('Description', compute='_compute_description', store=True, tracking=True)
    oneline_description = fields.Char('One Line Description', compute='_compute_description_oneline')
    auto_merge = fields.Boolean('Auto Merge', default=False)
    matching_contents_ids = fields.One2many('runbot.build.error.content', compute='_compute_matching_contents_ids', string='Matching Contents')

    def web_read(self, *arg, **kwargs):
        # ensure that the Auto merge descriptor in the error merge form view matching contents is properly computed
        return super(BuildErrorMerge, self.with_context(error_merge_ids=self.ids)).web_read(*arg, **kwargs)

    def _compute_matching_contents_ids(self):
        self.matching_contents_ids = False
        for record in self:
            all_ids = []
            for result in record._get_matching_groups():
                all_ids += result[-1]
                record.matching_contents_ids = self.env['runbot.build.error.content'].browse(all_ids)

    @api.depends('merge_filter_ids.field_name')
    def _compute_description(self):
        for record in self:
            record.description = '\n'.join(f.field_name for f in record.merge_filter_ids)

    @api.depends('description')
    def _compute_description_oneline(self):
        for record in self:
            record.oneline_description = record.description.replace('\n', ', ')

    def _get_read_group_params(self):
        domain = [('error_active', '=', True)]
        for filter in self.merge_filter_ids:
            domain = Domain.AND([domain, [(filter.field_name, '!=', False)]])
        groups = [merge_filter.field_name for merge_filter in self.merge_filter_ids]
        assert groups

        return (
            domain,
            groups,
        )

    def _get_matching_groups(self):
        domain, groups = self._get_read_group_params()
        return self.env['runbot.build.error.content']._read_group(
            domain,
            groups,
            ['id:array_agg'],
            [('error_id:count_distinct', '>', 1)],
        )

    def _get_similar_domain(self, error_content):
        result = [('fingerprint', '=', error_content.fingerprint)]
        for record in self:
            if all(error_content[f.field_name] for f in record.merge_filter_ids):
                merge_domain = [(f.field_name, '=', error_content[f.field_name]) for f in record.merge_filter_ids]
                result = Domain.OR([result, merge_domain])
        return result

    def action_summary(self):
        self.ensure_one()
        return {
            'name': 'Error Candidates',
            'type': 'ir.actions.act_url',
            'url': f"/runbot/error/merge/result/{self.id}",
        }

    def action_search_error_content_matches(self):
        self.ensure_one()
        _domain, groups = self._get_read_group_params()

        all_ids = []
        for result in self._get_matching_groups():
            all_ids += result[-1]
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'list,form',
            'res_model': 'runbot.build.error.content',
            'domain': [('id', 'in', all_ids)],
            'context': {'group_by': groups},
            'id': self.env.ref('runbot.open_view_build_error_content_tree').id,
            'name': 'Error merge groups',
        }

    def action_auto_merge(self):
        if not self.env.user.has_group('runbot.group_runbot_admin'):
            raise AccessError('You must be an admin to perform this action.')
        all_error_content_ids = []
        for merge_rule in self:
            summary = []
            for result in merge_rule._get_matching_groups():
                error_content_ids = result[-1]
                all_error_content_ids += error_content_ids
                error_content = self.env['runbot.build.error.content'].browse(error_content_ids)
                all_errors = error_content.error_id
                base_error = error_content.sudo().action_link_errors_contents()  # note: executed as sudo to skip multiple responsible checks
                other_errors = all_errors - base_error
                _logger.info('Auto merging error contents %s. This will result in merging errors %s in error %s ', error_content_ids, other_errors.ids, base_error.id)
                other_errors_links = Markup(', ').join([error._get_form_link() for error in other_errors])
                desc = error_content[0].with_context(error_merge_ids=merge_rule.ids).auto_merge_descriptor
                summary.append(Markup('Merging %s into %s for %s') % (other_errors_links, base_error._get_form_link(), desc))
            merge_rule.message_post(body=Markup("<br/>").join(summary))
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'list,form',
            'res_model': 'runbot.build.error.content',
            'domain': [('id', 'in', all_error_content_ids)],
            'name': 'Auto Merged Errors',
        }


class BuildErrorMergeFilter(models.Model):
    _name = 'runbot.build.error.merge.filters'
    _description = 'Error Merge patterns filters'

    field_id = fields.Many2one('ir.model.fields', 'Field', domain=[('model_id.model', '=', 'runbot.build.error.content')], required=True, ondelete='cascade')
    field_name = fields.Char('Field Name', related='field_id.name', store=True, readonly=True)
    error_merge_id = fields.Many2one('runbot.build.error.merge', 'Error Merge', required=True)
