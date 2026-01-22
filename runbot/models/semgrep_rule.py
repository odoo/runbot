from odoo import api, fields, models


class SemgrepRule(models.Model):
    _name = 'runbot.semgrep_rule'
    _description = 'Semgrep Rule'
    _inherit = ['mail.thread']

    name = fields.Char(string='Rule Name', required=True)
    category_id = fields.Many2one('runbot.checker_category', string='Category', required=True, index=True)
    language = fields.Selection([('python', 'Python'), ('javascript', 'JavaScript'), ('generic', 'Generic')], required=True)
    max_version_number = fields.Char(string='Max Odoo Version', help='Maximum exclusive Odoo version this rule applies to')
    min_version_number = fields.Char(string='Min Odoo Version', help='Minimum inclusive Odoo version this rule applies to')
    message = fields.Char(string='Error message', help='Message to display when the rule is triggered', required=True)
    rule = fields.Text("Rule", required=True)
    rule_text = fields.Text("Rule Text", compute='_compute_rule_text')
    severity = fields.Selection([('INFO', 'INFO'), ('WARNING', 'WARNING'), ('ERROR', 'ERROR')], string='Severity', required=True)

    @api.depends('name', 'message', 'severity', 'language', 'rule')
    def _compute_rule_text(self):
        def indent_by(s, by=2):
            indent = " " * by
            return ''.join(
                l if l.isspace() else indent + l
                for l in s.splitlines(keepends=True)
            )

        def count_indent(s):
            for line in s.splitlines(keepends=False):
                if line.isspace():
                    continue
                return len(line) - len(line.lstrip())
            return None

        self.rule_text = ''
        for r in self:
            rule = r.rule
            if not rule:
                continue

            indent = count_indent(rule)
            if indent is None:
                continue

            if indent < 2:
                rule = indent_by(rule, 2 - indent)
                indent = 2

            i_indent = " " * (indent - 2)
            s_indent = " " * indent
            r.rule_text = f"""\
{i_indent}- id: {r.name}
{s_indent}languages: [{r.language}]
{s_indent}severity: {r.severity}
{s_indent}message: {r.message!r}
{rule}
 """


class CheckerCategory(models.Model):
    _name = 'runbot.checker_category'
    _description = 'Checker Category'

    name = fields.Char(string='Category Name', required=True)

    _unique_name = models.Constraint(
        'unique (name)',
        "avoid duplicate Category",
    )
