import logging

from markupsafe import Markup

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    dockerfiles = env['runbot.dockerfile'].search([])
    for dockerfile in dockerfiles:
        if dockerfile.template_id and not dockerfile.layer_ids:
            dockerfile._template_to_layers()  # Upgrade to the latest version of 17 before upgrading to 18

    for dockerfile in dockerfiles:
        if dockerfile.template_id and dockerfile.layer_ids:
            dockerfile.message_post(
                body=Markup('Was using template <a href="/web#id=%s&model=ir.ui.view&view_type=form">%s</a>') % (dockerfile.template_id.id, dockerfile.template_id.name)
            )
            dockerfile.template_id = False
