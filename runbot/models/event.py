import ast

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class Event(models.Model):
    _name = 'runbot.event'
    _description = "Impactful event"
    _inherit = 'mail.thread'
    _order = 'date desc, id desc'

    name = fields.Char('Title', required=True, tracking=True)
    date = fields.Datetime('Date', required=True, default=fields.Datetime.now, index=True, tracking=True,
                           help="When the change was applied, can be adapted to when it was really effective")
    event_type = fields.Selection([
        ('code', 'Code'),
        ('configuration', 'Configuration'),
        ('deployment', 'Deployment'),
        ('infrastructure', 'Infrastructure'),
        ('other', 'Other'),
    ], string='Type', required=True, default='other', tracking=True)
    description = fields.Text('Explanation')
    impact_errors = fields.Boolean('Impacts errors', tracking=True,
                                   help="Displayed in the error history graph and listed on the errors")
    impact_stats = fields.Boolean('Impacts stats', tracking=True,
                                  help="Displayed as a vertical line in the stats graph")
    version_domain = fields.Char('Version Domain', tracking=True,
                                 help="Only displayed for the matching versions, all versions if empty")

    @api.constrains('version_domain')
    def _check_version_domain(self):
        version = self.env['runbot.version'].search([], limit=1)
        for event in self:
            try:
                event._match_version(version)
            except Exception as e:
                raise ValidationError(f"Unable to validate version domain: {e}")

    def _get_version_domain(self):
        self.ensure_one()
        return ast.literal_eval(self.version_domain) if self.version_domain else []

    def _match_version(self, version):
        self.ensure_one()
        return not self.version_domain or bool(version.filtered_domain(self._get_version_domain()))

    def _filter_versions(self, versions):
        """ Events matching at least one of the given versions """
        return self.filtered(lambda event: not event.version_domain or any(event._match_version(version) for version in versions))

    @api.model
    def _get_events(self, impact, start=None, end=None, versions=None):
        """ Events impacting `impact` ('errors' or 'stats') in ]start, end], matching at least one of `versions` if given """
        domain = [(f'impact_{impact}', '=', True)]
        if start:
            domain.append(('date', '>', start))
        if end:
            domain.append(('date', '<=', end))
        events = self.search(domain)
        if versions is not None:
            events = events._filter_versions(versions)
        return events

    def _serialize(self):
        return [{
            'id': event.id,
            'name': event.name,
            'date': fields.Datetime.to_string(event.date),
            'event_type': event.event_type,
            'description': event.description or '',
            'impact_errors': event.impact_errors,
            'impact_stats': event.impact_stats,
            'version_domain': event.version_domain or False,
        } for event in self]
