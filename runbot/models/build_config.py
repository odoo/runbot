import glob
import logging
import os
import re
import shlex
from ..common import now, grep, get_py_version
from ..container import docker_run, docker_get_gateway_ip, build_odoo_cmd
from odoo import models, fields, api, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval, test_python_expr

_logger = logging.getLogger(__name__)

class Config(models.Model):
    _name = "runbot.build.config"
    _inherit = "mail.thread"

    name = fields.Char('Config name', required=True, unique=True, tracking=True, help="Unique name for config please use trigram as postfix for custom configs")
    description = fields.Char('Config description')
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'config_id')
    update_github_state = fields.Boolean('Notify build state to github', default=False, tracking=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)

    @api.model
    def create(self, values):
        res = super(Config, self).create(values)
        res._check_step_ids_order()
        return res
        
    def write(self, values):
        res = super(Config, self).write(values)
        self._check_step_ids_order()
        return res

    def copy(self):
        #remove protection on copy
        copy = super(Config, self).copy()
        copy.sudo().write({'protected':False})
        return copy

    def unlink(self):
        super(Config, self).unlink()

    def step_ids(self):
        self.ensure_one()
        return [ordered_step.step_id for ordered_step in self.step_order_ids]

    def _check_step_ids_order(self):
        install_job = False
        step_ids = self.step_ids()
        for step in step_ids:
            if step.job_type == 'install_odoo':
                install_job = True
            if step.job_type == 'run_odoo':
                if step != step_ids[-1]:
                    raise UserError('Jobs of type run_odoo should be the last one')
                if not install_job:
                    raise UserError('Jobs of type run_odoo should be preceded by a job of type install_odoo')
        self._check_recustion()

    def _check_recustion(self, visited=None): #  todo test
        visited = visited or []
        recursion = False
        if self in visited:
            recursion = True 
        visited.append(self)
        if recursion:
            raise UserError('Impossible to save config, recursion detected with path: %s' % ">".join([v.name for v in visited]))
        for step in self.step_ids():
            if step.job_type == 'create_build':
                for create_config in step.create_config_ids:
                    create_config._check_recustion(visited[:])

class ConfigStep(models.Model):
    _name = 'runbot.build.config.step'
    _inherit = 'mail.thread'

    #general info
    name = fields.Char('Step name', required=True, unique=True, tracking=True, help="Unique name for step please use trigram as postfix for custom step_ids")
        #nome need to be unique art least in one config, mainly to keep database name/coverage result folder unique. 
        #todo needs to sanitize it
    job_type = fields.Selection([
        ('install_odoo', 'Test odoo'),
        ('run_odoo', 'Run odoo'),
        ('python', 'Python code'),
        ('create_build', 'Create build'),
    ], default='install_odoo', required=True, tracking=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)
    default_sequence = fields.Integer('Sequence', default=100, tracking=True) # or run after? # or in many2many rel?
    #install_odoo
    create_db = fields.Boolean('Create Db', default=True, tracking=True)  # future
    custom_db_name = fields.Char('Custom Db Name', tracking=True)  # future
    install_modules = fields.Char('Modules to install', help="List of module to install, use * for all modules")
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', tracking=True)
    cpu_limit = fields.Integer('Cpu limit', default=3600, tracking=True)
    coverage = fields.Boolean('Coverage', dafault=False, tracking=True)
    test_enable = fields.Boolean('Test enable', default=False, tracking=True)
    test_tags = fields.Char('Test tags', help="comma separated list of test tags")
    extra_params = fields.Char('Extra cmd args', tracking=True)
    # python
    python_code = fields.Text('Python code', tracking=True, default="# type python code here\n\n\n\n\n\n")
    running_job = fields.Boolean('Job final state is running', default=False, help="Docker won't be killed if checked")
    # create_build
    create_config_ids = fields.Many2many('runbot.build.config', 'runbot_build_config_step_ids_create_config_ids_rel', string='New Build Configs', tracking=True, index=True)
    number_builds = fields.Integer('Number of build to create', default=1, tracking=True)
    hide_build = fields.Boolean('Hide created build in frontend', default=True, tracking=True)
    force_build = fields.Boolean("As a forced rebuild, don't use duplicate detection", default=False, tracking=True)
    force_host = fields.Boolean('Use same host as parent for children', default=False, tracking=True)  # future

    @api.constrains('python_code')
    def _check_python_code(self):
        #if self.env.user.id == SUPERUSER_ID: # let super user do whatever he wants?
        #    return
        for step in self.sudo().filtered('python_code'):
            msg = test_python_expr(expr=step.python_code.strip(), mode="exec")
            if msg:
                raise ValidationError(msg)

    @api.depends('name', 'custom_db_name')
    def _compute_db_name(self):
        for step in self:
            step.db_name = step.custom_db_name or step.name

    def _inverse_db_name(self):
        for step in self:
            step.custom_db_name = step.db_name

    def copy(self):
        #remove protection on copy
        copy = super(ConfigStep, self).copy()
        copy._write({'protected':False})
        return copy

    @api.model
    def create(self, values):
        self._check(values)
        return super(ConfigStep, self).create(values)

    def write(self, values):
        #if self.protected and not (self.env.user.has_group('runbot.group_build_config_administrator') and values.get('protected') is False): # or check that it is used on any config linked to a repo or a sticky branch? 
        #    raise UserError('Record is protected, protection can only be removed by Job Administrators')
        self._check(values)
        return super(ConfigStep, self).write(values)

    def unlink(self):
        if self.protected:
            raise UserError('Protected step')
        super(ConfigStep, self).unlink()

    def _check(self, values):
        if 'name' in values:
            name_reg = r'^[a-zA-Z0-9\-_]*$'
            if not re.match(name_reg, values.get('name')):
                raise UserError('Name cannot contain special char or spaces exepts "_" and "-"')
        if not self.env.user.has_group('runbot.group_build_config_administrator'):
            if (values.get('job_type') == 'python' or ('python_code' in values and values['python_code'])):
                raise UserError('cannot create or edit config step of type python code')
            if (values.get('extra_params')):
                reg = r'^[a-zA-Z0-9\-_ "]*$'
                if not re.match(reg, values.get('extra_params')):
                    _logger.log('%s tried to create an non supported test_param %s' % (self.env.user.name, values.get('extra_params')))
                    raise UserError('Invalid extra_params on config step')

    def _run(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        build.write({'job_start': now(), 'job_end': False}) # state, ...
        build._log('run', 'Starting step %s from config %s' % (self.name, build.run_config_id.name), level='SEPARATOR')
        return self._run_step(log_path, build)

    def _run_step(self, log_path, build):
        if self.job_type == 'run_odoo':
            return self._run_odoo_run(build, log_path)
        if self.job_type == 'install_odoo':
            return self._run_odoo_install(build, log_path)
        elif self.job_type == 'python':
            return self._run_python(build, log_path)
        elif self.job_type == 'create_build':
            return self._create_build(build, log_path)

    def _create_build(self, build, log_path):
        # todo: kill all subbuild when killing build
        # should we add a limit to avoid explosion of configs? 
        Build = self.env['runbot.build']
        if self.force_build:
            Build = Build.with_context(force_rebuild=True)

        count = 0
        for create_config in self.create_config_ids:
            for _ in range(self.number_builds):
                count +=1
                if count > 200:
                    build._logger('Too much build created')
                    break
                children = Build.create({
                    'dependency_ids': [(4, did.id) for did in build.dependency_ids],
                    'run_config_id': create_config.id,
                    'parent_id': build.id,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'build_type': build.build_type,
                    'date': build.date,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                    'hidden': self.hide_build,
                })
                build._log('create_build', 'created with config %s' % create_config.name, log_type='subbuild', path=str(children.id))

    def _run_python(self, build, log_path):
        eval_ctx = {'self': self, 'build': build, 'log_path': log_path}
        return safe_eval(self.sudo().code.strip(), eval_ctx, mode="exec", nocopy=True)

    def _run_odoo_install(self, build, log_path):
        cmd, _ = build._cmd()
        # create db if needed
        db_name = "%s-%s" % (build.dest, self.db_name)
        if self.create_db:
            build._local_pg_createdb(db_name)
        cmd += ['-d', db_name]
        # list module to install
        modules_to_install = set([mod.strip() for mod in self.install_modules.split(',')])
        if '*' in modules_to_install:
            modules_to_install.remove('*')
            default_mod = set([mod.strip() for mod in build.modules.split(',')])
            modules_to_install = default_mod | modules_to_install
            #todo add without support
        mods = ",".join(modules_to_install)
        if mods:
            cmd += ['-i', mods]
        if self.test_enable:
            if grep(build._server("tools/config.py"), "test-enable"):
                cmd.extend(['--test-enable'])
            else: 
                build._log('test_all', 'Installing modules without testing', level='WARNING')
        if self.test_tags:
                test_tags = self.test_tags.replace(' ','')
                cmd.extend(['--test-tags', test_tags])

        cmd += ['--stop-after-init'] # install job should always finish
        cmd += ['--log-level=test', '--max-cron-threads=0']

        if self.extra_params:
            cmd.extend(shlex.split(self.extra_params))
        if self.coverage:
            build.coverage = True
            available_modules = [  # todo extract this to build methos
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            module_to_omit = set(available_modules) - modules_to_install
            omit = ['--omit', ','.join('*addons/%s/*' % m for m in module_to_omit) + '*__manifest__.py']
            py_version = get_py_version(build)
            cmd = [ py_version, '-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + omit + cmd

            # prepare coverage result
            cov_path = build._path('coverage/test_mail_cov_xdo')
            os.makedirs(cov_path, exist_ok=True)
            cmdcov = ['&&', py_version, "-m", "coverage", "html", "-d", "/data/build/coverage", "--ignore-errors"]
            cmd += cmdcov
        max_timeout = int(self.env['ir.config_parameter'].get_param('runbot.runbot_timeout', default=10000))
        timeout = min(self.cpu_limit, max_timeout)
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name(), cpu_limit=timeout)

    def _run_odoo_run(self, build, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        # run server
        cmd, mods = build._cmd()
        if os.path.exists(build._server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "8070"]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        db_name = [step.db_name for step in build.run_config_id.step_ids() if step.job_type=='install_odoo'][-1]
        # we need to have at least one job of type install_odoo to run odoo, take the last one for db_name.
        cmd += ['-d', '%s-%s' % (build.dest, db_name)]

        if grep(build._server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter', '%d.*$']
            else:
                cmd += ['--db-filter', '%s.*$' % build.dest]
        smtp_host = docker_get_gateway_ip()
        if smtp_host:
            cmd += ['--smtp', smtp_host]
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name(), exposed_ports = [build.port, build.port + 1])

    def _job_state(self):
        self.ensure_one()
        if self.job_type == 'run_odoo' or (self.job_type == 'python' and self.running_job):
            return 'running'
        return 'testing'

    def _has_log(self):
        self.ensure_one()
        return self.job_type != 'create_build'

class ConfigStepOrder(models.Model):
    _name = 'runbot.build.config.step.order'
    _order = 'sequence, id'
    # a kind of many2many rel with sequence

    sequence = fields.Integer('Sequence', required=True)
    config_id = fields.Many2one('runbot.build.config', 'Config', required=True, ondelete='cascade')
    step_id = fields.Many2one('runbot.build.config.step', 'Config Step', required=True, ondelete='cascade')

    @api.onchange('step_id')
    def _onchange_step_id(self):
        self.sequence = self.step_id.default_sequence

    @api.model
    def create(self, values):
        if not 'sequence' in values and values.get('step_id'):
            values['sequence'] = self.env['runbot.build.config.step'].browse(values.get('step_id')).default_sequence
        if self.pool._init: # do not duplicate entry on install
            existing = self.search([('sequence', '=', values.get('sequence')), ('config_id', '=', values.get('config_id')), ('step_id', '=', values.get('step_id'))])
            if existing:
                return
        return super(ConfigStepOrder, self).create(values)