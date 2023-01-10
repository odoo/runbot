from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    projects = env['runbot.project'].search([])
    for project in projects:
        if not project.master_bundle_id:
            master = env['runbot.bundle'].search([('name', '=', 'master'), ('project_id', '=', project.id)], limit=1)
            if not master:
                master = env['runbot.bundle'].create({
                    'name': 'master',
                    'project_id': project.id,
                    'is_base': True,
                })
            project.master_bundle_id = master

        if not project.dummy_bundle_id:
            dummy = env['runbot.bundle'].search([('name', '=', 'Dummy'), ('project_id', '=', project.id)], limit=1)
            if not dummy:
                dummy = env['runbot.bundle'].create({
                    'name': 'Dummy',
                    'project_id': project.id,
                    'no_build': True,
                })
            else:
                dummy.no_build = True
            project.dummy_bundle_id = dummy
