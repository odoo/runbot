# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Business Applications
#    Copyright (C) 2004-2012 OpenERP S.A. (<http://openerp.com>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.osv import fields, osv

class runbot_config_settings(osv.osv_memory):
    _name = 'runbot.config.settings'
    _inherit = 'res.config.settings'
    _columns = {
        'default_workers': fields.integer('Total Number of Workers'),
        'default_running_max': fields.integer('Maximum Number of Running Builds'),
        'default_timeout': fields.integer('Default Timeout (in seconds)'),
        'default_starting_port': fields.integer('Starting Port for Running Builds'),
        'default_domain': fields.char('Runbot Domain'),
    }

    def get_default_parameters(self, cr, uid, fields, context=None):
        icp = self.pool['ir.config_parameter']
        workers = icp.get_param(cr, uid, 'runbot.workers', default=6)
        running_max = icp.get_param(cr, uid, 'runbot.running_max', default=75)
        timeout = icp.get_param(cr, uid, 'runbot.timeout', default=1800)
        starting_port = icp.get_param(cr, uid, 'runbot.starting_port', default=2000)
        runbot_domain = icp.get_param(cr, uid, 'runbot.domain', default='runbot.odoo.com')
        return {
            'default_workers': int(workers),
            'default_running_max': int(running_max),
            'default_timeout': int(timeout),
            'default_starting_port': int(starting_port),
            'default_domain': runbot_domain,
        }

    def set_default_parameters(self, cr, uid, ids, context=None):
        config = self.browse(cr, uid, ids[0], context)
        icp = self.pool['ir.config_parameter']
        icp.set_param(cr, uid, 'runbot.workers', config.default_workers)
        icp.set_param(cr, uid, 'runbot.running_max', config.default_running_max)
        icp.set_param(cr, uid, 'runbot.timeout', config.default_timeout)
        icp.set_param(cr, uid, 'runbot.starting_port', config.default_starting_port)
        icp.set_param(cr, uid, 'runbot.domain', config.default_domain)


# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
