from odoo.http import Controller, request, route

from .utils import from_role

class MergebotController(Controller):

    @from_role('tx', signed=True)
    @route('/i18n/merge_commit', type='json', auth='public')
    def merge_commit(self, commit_hash, repository, branch, project="RD", callback_url=None):
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
            ("project_id.name", "=", project)
        ])
        if not target:
            return {"error": f"Target branch {project}:{branch} not found"}

        vals = {
            "repository": repository_id.id,
            "target": target.id,
            "commit": commit_hash,
        }
        if callback_url:
            vals["callback_url"] = callback_url
        patch = request.env["runbot_merge.patch"].sudo().create(vals)
        return {"patch": patch.id}
