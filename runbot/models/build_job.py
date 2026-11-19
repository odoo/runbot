import time
from contextlib import contextmanager
from contextvars import ContextVar

from odoo import fields, models

from ..common import s2human

current_job_id = ContextVar('runbot_current_job_id', default=None)  # not in env.context, it can be lost (transactioncache, ...)


class BuildJob(models.Model):
    _name = 'runbot.build.job'
    _description = 'Build Job'
    _log_access = False

    build_id = fields.Many2one('runbot.build', 'Build', required=True, index=True, ondelete='cascade')
    config_step_id = fields.Many2one('runbot.build.config.step', 'Step', ondelete='set null')
    name = fields.Char('Name')  # step display name, can differ from config_step_id.name in dynamic configs
    start_date = fields.Datetime('Start', index=True)  # index for _gc_ir_logging
    log_name = fields.Char('Log file name')  # derived from name, but could theoricaly change in rare cases
    docker_build_result_id = fields.Many2one('runbot.docker_build_result', 'Docker image', ondelete='set null')
    exported_db = fields.Char('Exported database suffix')
    # durations in ms
    duration = fields.Integer('Duration')
    python_time = fields.Integer('Python time', help='Preparation of the job, git and docker pull included')
    git_time = fields.Integer('Git time', help='All git operations')
    docker_pull_time = fields.Integer('Docker pull time')
    docker_time = fields.Integer('Docker time')
    result_time = fields.Integer('Result time', help='Results and statistics')

    @contextmanager
    def _timer(self, field):
        start = time.monotonic()
        try:
            yield
        finally:
            if self:
                self[field] += int((time.monotonic() - start) * 1000)

    def _git_timer(self):
        return self.browse(current_job_id.get())._timer('git_time')

    def _image_tag(self):
        self.ensure_one()
        image_tag = self.docker_build_result_id.dockerfile_id.image_tag
        if image_tag and self.build_id.params_id.config_data.get('docker_use_future'):
            image_tag += '.future'
        return image_tag or False

    def _duration(self):
        self.ensure_one()
        return s2human(self.duration / 1000)

    def _duration_details(self):
        self.ensure_one()
        scheduling_time = max(0, self.duration - self.python_time - self.docker_time - self.result_time)
        times = [
            ('python', self.python_time, [('git', self.git_time), ('docker pull', self.docker_pull_time)]),
            ('docker', self.docker_time, []),
            ('results', self.result_time, []),
            ('scheduling', scheduling_time, []),
        ]

        lines = []
        for label, value, sub_times in times:
            if value:
                lines.append(f'{label}: {value / 1000:.2f}s')
                sub_times = [(sub_label, sub_value) for sub_label, sub_value in sub_times if sub_value]
                for index, (sub_label, sub_value) in enumerate(sub_times):
                    prefix = '└─' if index == len(sub_times) - 1 else '├─'
                    lines.append(f'{prefix} {sub_label}: {sub_value / 1000:.2f}s')
        return '\n'.join(lines)
