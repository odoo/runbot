from odoo import models, api, fields
from odoo.addons.runbot.fields import JsonDictField


class TestModelParent(models.Model):
    _name = 'runbot.test.model.parent'
    _inherit = ['runbot.public.model.mixin']
    _description = 'parent'

    private_field = fields.Boolean(public=False)

    field_bool = fields.Boolean(public=True)
    field_integer = fields.Integer(public=True)
    field_float = fields.Float(public=True)
    field_char = fields.Char(public=True)
    field_text = fields.Text(public=True)
    field_html = fields.Html(public=True)
    field_date = fields.Date(public=True)
    field_datetime = fields.Datetime(public=True)
    field_selection = fields.Selection(selection=[('a', 'foo'), ('b', 'bar')], public=True)
    field_json = JsonDictField(public=True)

    field_many2one = fields.Many2one('runbot.test.model.parent', public=True)
    field_one2many = fields.One2many('runbot.test.model.child', 'parent_id', public=True)
    field_one2many_private = fields.One2many('runbot.test.model.child.private', 'parent_id', public=True)
    field_many2many = fields.Many2many(
        'runbot.test.model.parent', relation='test_model_relation', public=True,
        column1='col_1', column2='col_2',
    )

    field_one2many_computed = fields.One2many('runbot.test.model.child', compute='_compute_one2many', public=True)
    field_many2many_computed = fields.Many2many('runbot.test.model.parent', compute='_compute_many2many', public=True)

    @api.model
    def _api_request_allow_direct_access(self):
        return False

    @api.depends()
    def _compute_one2many(self):
        self.field_one2many_computed = self.env['runbot.test.model.child'].search([])

    @api.depends()
    def _compute_many2many(self):
        for rec in self:
            rec.field_many2many_computed = self.env['runbot.test.model.parent'].search([
                ('id', '!=', rec.id),
            ])


class TestModelChild(models.Model):
    _name = 'runbot.test.model.child'
    _inherit = ['runbot.public.model.mixin']
    _description = 'child'

    parent_id = fields.Many2one('runbot.test.model.parent', required=True, public=True)

    data = fields.Integer(public=True)

    @api.model
    def _api_request_allow_direct_access(self):
        return False


class TestPrivateModelChild(models.Model):
    _name = 'runbot.test.model.child.private'
    _description = 'Private Child'

    parent_id = fields.Many2one('runbot.test.model.parent', required=True)

    data = fields.Integer()
