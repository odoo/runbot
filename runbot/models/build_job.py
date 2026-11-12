from odoo import fields, models

from ..common import s2human


class BuildJob(models.Model):
    """Execution of a config step (runbot.build.config.step) in a build."""
    _name = 'runbot.build.job'
    _description = 'Build Job'
    _order = 'id'
    _log_access = False

    build_id = fields.Many2one('runbot.build', 'Build', required=True, index=True, ondelete='cascade')
    config_step_id = fields.Many2one('runbot.build.config.step', 'Step', ondelete='set null')
    name = fields.Char('Name')  # step display name, can differ from config_step_id.name in dynamic configs
    start_date = fields.Datetime('Start')
    log_name = fields.Char('Log file name')
    image_tag = fields.Char('Docker image tag')
    image_id = fields.Char('Docker image id')
    exported_db = fields.Char('Exported database suffix')
    # durations in ms
    duration = fields.Integer('Duration')
    python_time = fields.Integer('Python time', help='Preparation of the job, checkout and docker pull included')
    checkout_time = fields.Integer('Checkout time')
    docker_pull_time = fields.Integer('Docker pull time')
    docker_time = fields.Integer('Docker time')
    result_time = fields.Integer('Result time', help='Results and statistics')

    def _duration(self):
        self.ensure_one()
        return s2human(self.duration / 1000)
