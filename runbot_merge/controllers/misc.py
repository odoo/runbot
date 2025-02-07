# -*- coding: utf-8 -*-

from odoo.http import Controller, request, route

try:
    from odoo.addons.saas_worker.util import from_role
except ImportError:
    def from_role(*_, **__):
        return lambda _: None

class MergebotController(Controller):

    @from_role('tx', signed=True)
    @route('/i18n/merge_commit', type='json', auth='public')
    def merge_commit(self, commit_hash, repository, branch, project="RD"):
        """Merge a specific commit hash in a repository
        
        The commit_hash must be known by mergebot (in the git network)
        Used for translation synchronisation from transifex
        """
        repository_id = request.env["runbot_merge.repository"].sudo().search([
            ("name", "=", repository)
        ])
        if not repository_id:
            return {"error": "Repository %r not found" % repository}

        target = request.env["runbot_merge.branch"].sudo().search([
            ("name", "=", branch),
            ("project_id", "=", project)
        ])
        if not target:
            return {"error": "Target branch %s:%s not found" % (branch, target)}

        patch = request.env["runbot_merge.patch"].sudo().create({
            "repository": repository_id.id,
            "target": target.id,
            "commit": commit_hash
        })
        return {"patch": patch.id}
