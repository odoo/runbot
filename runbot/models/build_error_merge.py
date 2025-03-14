from odoo import models, fields, api
from odoo.osv import expression


class BuildErrorMerge(models.Model):
    _name = 'runbot.build.error.merge'
    _description = 'Error Merge patterns'
    _inherit = ['mail.thread']

    active = fields.Boolean('Active', default=True)
    name = fields.Char('Name', required=True)
    merge_filter_ids = fields.One2many('runbot.build.error.merge.filters', 'error_merge_id', 'Merge Lines')
    description = fields.Char('Description', compute='_compute_description', store=True, tracking=True)
    oneline_description = fields.Char('One Line Description', compute='_compute_description_online')
    auto_merge = fields.Boolean('Auto Merge', default=False)
    matching_contents_ids = fields.One2many('runbot.build.error.content', compute='_compute_matching_contents_ids', string='Matching Contents')

    def web_read(self, *arg, **kwargs):
        return super(BuildErrorMerge, self.with_context(error_merge_ids=self.ids)).web_read(*arg, **kwargs)

    def _compute_matching_contents_ids(self):
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
    def _compute_description_online(self):
        for record in self:
            record.oneline_description = record.description.replace('\n', ', ')

    def _get_read_group_params(self):
        domain = [('error_active', '=', True)]
        for filter in self.merge_filter_ids:
            domain = expression.AND([domain, [(filter.field_name, '!=', False)]])
        groups = self.merge_filter_ids.mapped('field_name')
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
        result = [expression.FALSE_LEAF]
        for record in self:
            if all(error_content[f.field_name] for f in record.merge_filter_ids):
                merge_domain = [(f.field_name, '=', error_content[f.field_name]) for f in record.merge_filter_ids]
                result = expression.OR([result, merge_domain])
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
        domain, groups = self._get_read_group_params()

        all_ids = []
        for result in self._get_matching_groups():
            all_ids += result[-1]
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'list,form',
            'res_model': 'runbot.build.error.content',
            'domain': [('id', 'in', all_ids)],
            'context': {'group_by': groups},
        }

    def action_auto_merge(self):
        for merge_rule in self:
            for result in merge_rule._get_matching_groups():
                error_content_ids = result[-1]
                error_content = self.env['runbot.build.error.content'].browse(error_content_ids)
                error_content.matching_contents_ids.merge()


class BuildErrorMergeFilter(models.Model):
    _name = 'runbot.build.error.merge.filters'
    _description = 'Error Merge patterns filters'

    field_id = fields.Many2one('ir.model.fields', 'Field', domain=[('model_id.model', '=', 'runbot.build.error.content')], required=True, ondelete='cascade')
    field_name = fields.Char('Field Name', related='field_id.name', store=True, readonly=True)
    error_merge_id = fields.Many2one('runbot.build.error.merge', 'Error Merge', required=True)
