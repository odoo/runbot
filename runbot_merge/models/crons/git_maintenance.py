import logging
import subprocess

from odoo import models
from ...git import get_local


_gc = logging.getLogger(__name__)
class GC(models.TransientModel):
    _name = 'runbot_merge.maintenance'
    _description = "Weekly maintenance of... cache repos?"

    def _run(self):
        # lock out crons which use the local repo cache to avoid concurrency
        # issues while we're GC-ing it
        Stagings = self.env['runbot_merge.stagings']
        crons = self.env.ref('runbot_merge.staging_cron', Stagings) | self.env.ref('forwardport.port_forward', Stagings)
        if crons:
            self.env.cr.execute("""
                SELECT 1 FROM ir_cron
                WHERE id = any(%s)
                FOR UPDATE
            """, [crons.ids])

        # run on all repos with a forwardport target (~ forwardport enabled)
        for repo in self.env['runbot_merge.repository'].search([]):
            repo_git = get_local(repo, prefix=None)
            if not repo:
                continue

            _gc.info('Running maintenance on %s', repo.name)
            r = repo_git\
                .stdout(True)\
                .with_config(stderr=subprocess.STDOUT, text=True, check=False)\
                .gc(aggressive=True, prune='now')
            if r.returncode:
                _gc.warning("Maintenance failure (status=%d):\n%s", r.returncode, r.stdout)
