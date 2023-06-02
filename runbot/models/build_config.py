import base64
import glob
import json
import logging
import fnmatch
import re
import shlex
import time
from unidiff import PatchSet
from ..common import now, grep, time2str, rfind, s2human, os, RunbotException
from ..container import docker_get_gateway_ip, Command
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval, test_python_expr, _SAFE_OPCODES, to_opcodes

# adding some additionnal optcode to safe_eval. This is not 100% needed and won't be done in standard but will help
# to simplify some python step by wraping the content in a function to allow return statement and get closer to other
# steps

_SAFE_OPCODES |= set(to_opcodes(['LOAD_DEREF', 'STORE_DEREF', 'LOAD_CLOSURE']))

_logger = logging.getLogger(__name__)

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '

PYTHON_DEFAULT = "# type python code here\n\n\n\n\n\n"

class ReProxy():
    @classmethod
    def match(cls, *args, **kwrags):
        return re.match(*args, **kwrags)

    @classmethod
    def search(cls, *args, **kwrags):
        return re.search(*args, **kwrags)

    @classmethod
    def compile(cls, *args, **kwrags):
        return re.compile(*args, **kwrags)

    @classmethod
    def findall(cls, *args, **kwrags):
        return re.findall(*args, **kwrags)

    VERBOSE = re.VERBOSE
    MULTILINE = re.MULTILINE

class Config(models.Model):
    _name = 'runbot.build.config'
    _description = "Build config"
    _inherit = "mail.thread"

    name = fields.Char('Config name', required=True, tracking=True, help="Unique name for config please use trigram as postfix for custom configs")

    description = fields.Char('Config description')
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'config_id', copy=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)
    group = fields.Many2one('runbot.build.config', 'Configuration group', help="Group of config's and config steps")
    group_name = fields.Char('Group name', related='group.name')

    @api.model_create_single
    def create(self, values):
        res = super(Config, self).create(values)
        res._check_step_ids_order()
        return res

    def write(self, values):
        res = super(Config, self).write(values)
        self._check_step_ids_order()
        return res

    def copy(self):
        # remove protection on copy
        copy = super(Config, self).copy()
        copy.sudo().write({'protected': False})
        return copy

    def unlink(self):
        super(Config, self).unlink()

    def step_ids(self):
        if self:
            self.ensure_one()
        return [ordered_step.step_id for ordered_step in self.step_order_ids.sorted('sequence')]

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

    def _check_recustion(self, visited=None):
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


class ConfigStepUpgradeDb(models.Model):
    _name = 'runbot.config.step.upgrade.db'
    _description = "Config Step Upgrade Db"

    step_id = fields.Many2one('runbot.build.config.step', 'Step')
    config_id = fields.Many2one('runbot.build.config', 'Config')
    db_pattern = fields.Char('Db suffix pattern')
    min_target_version_id = fields.Many2one('runbot.version', "Minimal target version_id")

TYPES = [
        ('install_odoo', 'Test odoo'),
        ('run_odoo', 'Run odoo'),
        ('python', 'Python code'),
        ('create_build', 'Create build'),
        ('configure_upgrade', 'Configure Upgrade'),
        ('configure_upgrade_complement', 'Configure Upgrade Complement'),
        ('test_upgrade', 'Test Upgrade'),
        ('restore', 'Restore'),
    ]
class ConfigStep(models.Model):
    _name = 'runbot.build.config.step'
    _description = "Config step"
    _inherit = 'mail.thread'

    # general info
    name = fields.Char('Step name', required=True, tracking=True, help="Unique name for step please use trigram as postfix for custom step_ids")
    domain_filter = fields.Char('Domain filter', tracking=True)
    description = fields.Char('Config step description')

    job_type = fields.Selection(TYPES, default='install_odoo', required=True, tracking=True, ondelete={t[0]: 'cascade' for t in [TYPES]})
    protected = fields.Boolean('Protected', default=False, tracking=True)
    default_sequence = fields.Integer('Sequence', default=100, tracking=True)  # or run after? # or in many2many rel?
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'step_id')
    group = fields.Many2one('runbot.build.config', 'Configuration group', help="Group of config's and config steps")
    group_name = fields.Char('Group name', related='group.name')
    make_stats = fields.Boolean('Make stats', default=False)
    build_stat_regex_ids = fields.Many2many('runbot.build.stat.regex', string='Stats Regexes')
    # install_odoo
    create_db = fields.Boolean('Create Db', default=True, tracking=True)  # future
    custom_db_name = fields.Char('Custom Db Name', tracking=True)  # future
    install_modules = fields.Char('Modules to install', help="List of module patterns to install, use * to install all available modules, prefix the pattern with dash to remove the module.", default='')
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', tracking=True)
    cpu_limit = fields.Integer('Cpu limit', default=3600, tracking=True)
    coverage = fields.Boolean('Coverage', default=False, tracking=True)
    paths_to_omit = fields.Char('Paths to omit from coverage', tracking=True)
    flamegraph = fields.Boolean('Allow Flamegraph', default=False, tracking=True)
    test_enable = fields.Boolean('Test enable', default=True, tracking=True)
    test_tags = fields.Char('Test tags', help="comma separated list of test tags", tracking=True)
    enable_auto_tags = fields.Boolean('Allow auto tag', default=False, tracking=True)
    sub_command = fields.Char('Subcommand', tracking=True)
    extra_params = fields.Char('Extra cmd args', tracking=True)
    additionnal_env = fields.Char('Extra env', help='Example: foo="bar";bar="foo". Cannot contains \' ', tracking=True)
    enable_log_db = fields.Boolean("Enable log db", default=True)
    # python
    python_code = fields.Text('Python code', tracking=True, default=PYTHON_DEFAULT)
    python_result_code = fields.Text('Python code for result', tracking=True, default=PYTHON_DEFAULT)
    running_job = fields.Boolean('Job final state is running', default=False, help="Docker won't be killed if checked")
    # create_build
    create_config_ids = fields.Many2many('runbot.build.config', 'runbot_build_config_step_ids_create_config_ids_rel', string='New Build Configs', tracking=True, index=True)
    number_builds = fields.Integer('Number of build to create', default=1, tracking=True)

    force_host = fields.Boolean('Use same host as parent for children', default=False, tracking=True)  # future
    make_orphan = fields.Boolean('No effect on the parent result', help='Created build result will not affect parent build result', default=False, tracking=True)

    # upgrade
    # 1. define target
    upgrade_to_master = fields.Boolean() # upgrade niglty + (future migration? no, need last master, not nightly master)
    upgrade_to_current = fields.Boolean(help="If checked, only upgrade to current will be used, other options will be ignored")
    upgrade_to_major_versions = fields.Boolean() # upgrade (no master)
    upgrade_to_all_versions = fields.Boolean() # upgrade niglty (no master)
    upgrade_to_version_ids = fields.Many2many('runbot.version', relation='runbot_upgrade_to_version_ids', string='Forced version to use as target')
    # 2. define source from target
    upgrade_from_current = fields.Boolean(help="If checked, only upgrade from current will be used, other options will be ignored Template should be installed in the same build")
    upgrade_from_previous_major_version = fields.Boolean() # 13.0
    upgrade_from_last_intermediate_version = fields.Boolean() # 13.3
    upgrade_from_all_intermediate_version = fields.Boolean() # 13.2 # 13.1
    upgrade_from_version_ids = fields.Many2many('runbot.version', relation='runbot_upgrade_from_version_ids', string='Forced version to use as source (cartesian with target)')

    upgrade_flat = fields.Boolean("Flat", help="Take all decisions in on build")

    upgrade_config_id = fields.Many2one('runbot.build.config',string='Upgrade Config', tracking=True, index=True)
    upgrade_dbs = fields.One2many('runbot.config.step.upgrade.db', 'step_id', tracking=True)

    restore_download_db_suffix = fields.Char('Download db suffix')
    restore_rename_db_suffix = fields.Char('Rename db suffix')

    commit_limit = fields.Integer('Commit limit', default=50)
    file_limit = fields.Integer('File limit', default=450)

    @api.constrains('python_code')
    def _check_python_code(self):
        return self._check_python_field('python_code')

    @api.constrains('python_result_code')
    def _check_python_result_code(self):
        return self._check_python_field('python_result_code')

    def _check_python_field(self, field_name):
        for step in self.sudo().filtered(field_name):
            msg = test_python_expr(expr=step[field_name].strip(), mode="exec")
            if msg:
                raise ValidationError(msg)

    @api.onchange('sub_command')
    def _onchange_number_builds(self):
        if self.sub_command:
            self.install_modules = '-*'
            self.test_enable = False
            self.create_db = False

    @api.depends('name', 'custom_db_name')
    def _compute_db_name(self):
        for step in self:
            step.db_name = step.custom_db_name or step.name

    def _inverse_db_name(self):
        for step in self:
            step.custom_db_name = step.db_name

    def copy(self):
        # remove protection on copy
        copy = super(ConfigStep, self).copy()
        copy._write({'protected': False})
        return copy

    @api.model_create_single
    def create(self, values):
        self._check(values)
        return super(ConfigStep, self).create(values)

    def write(self, values):
        self._check(values)
        return super(ConfigStep, self).write(values)

    def unlink(self):
        if any(record.protected for record in self):
            raise UserError('Protected step')
        super(ConfigStep, self).unlink()

    def _check(self, values):
        if 'name' in values:
            name_reg = r'^[a-zA-Z0-9\-_]*$'
            if not re.match(name_reg, values.get('name')):
                raise UserError('Name cannot contain special char or spaces exepts "_" and "-"')
        if not self.env.user.has_group('runbot.group_build_config_administrator'):
            if (values.get('job_type') == 'python' or ('python_code' in values and values['python_code'] and values['python_code'] != PYTHON_DEFAULT)):
                raise UserError('cannot create or edit config step of type python code')
            if (values.get('job_type') == 'python' or ('python_result_code' in values and values['python_result_code'] and values['python_result_code'] != PYTHON_DEFAULT)):
                raise UserError('cannot create or edit config step of type python code')
            if (values.get('extra_params')):
                reg = r'^[a-zA-Z0-9\-_ "]*$'
                if not re.match(reg, values.get('extra_params')):
                    _logger.log('%s tried to create an non supported test_param %s' % (self.env.user.name, values.get('extra_params')))
                    raise UserError('Invalid extra_params on config step')

    def _run(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        build.write({'job_start': now(), 'job_end': False})  # state, ...
        log_link = ''
        if self._has_log():
            log_url = f'http://{build.host}'
            url = f"{log_url}/runbot/static/build/{build.dest}/logs/{self.name}.txt"
            log_link = f'[@icon-file-text]({url})'
        build._log('run', 'Starting step **%s** from config **%s** %s' % (self.name, build.params_id.config_id.name, log_link), log_type='markdown', level='SEPARATOR')
        return self._run_step(build, log_path)

    def _run_step(self, build, log_path, **kwargs):
        build.log_counter = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_maxlogs', 100)
        run_method = getattr(self, '_run_%s' % self.job_type)
        docker_params = run_method(build, log_path, **kwargs)
        if docker_params:
            return build._docker_run(**docker_params)
        return True

    def _run_create_build(self, build, log_path):
        count = 0
        config_data = build.params_id.config_data
        config_ids = config_data.get('create_config_ids', self.create_config_ids)

        child_data_list = config_data.get('child_data', [{}])
        if not isinstance(child_data_list, list):
            child_data_list = [child_data_list]

        for child_data in child_data_list:
            for create_config in self.env['runbot.build.config'].browse(child_data.get('config_id', config_ids.ids)):
                _child_data = {'config_data': {}, **child_data, 'config_id': create_config}
                for _ in range(config_data.get('number_build', self.number_builds)):
                    count += 1
                    if count > 200:
                        build._logger('Too much build created')
                        break
                    child = build._add_child(_child_data, orphan=self.make_orphan)
                    build._log('create_build', 'created with config %s' % create_config.name, log_type='subbuild', path=str(child.id))

    def make_python_ctx(self, build):
        return {
            'self': self,
            # 'fields': fields,
            # 'models': models,
            'build': build,
            '_logger': _logger,
            'log_path': build._path('logs', '%s.txt' % self.name),
            'glob': glob.glob,
            'Command': Command,
            're': ReProxy,
            'grep': grep,
            'rfind': rfind,
            'json_loads': json.loads,
            'PatchSet': PatchSet,
        }

    def _run_python(self, build, log_path, force=False):
        eval_ctx = self.make_python_ctx(build)
        eval_ctx['force'] = force
        try:
            safe_eval(self.python_code.strip(), eval_ctx, mode="exec", nocopy=True)
            run = eval_ctx.get('run')
            if run and callable(run):
                return run()
            return eval_ctx.get('docker_params')
        except ValueError as e:
            save_eval_value_error_re = r'<class \'odoo.addons.runbot.models.repo.RunbotException\'>: "(.*)" while evaluating\n.*'
            message = e.args[0]
            groups = re.match(save_eval_value_error_re, message)
            if groups:
                build._log("run", groups[1], level='ERROR')
                build._kill(result='ko')
            else:
                raise

    def _is_docker_step(self):
        if not self:
            return False
        self.ensure_one()
        return self.job_type in ('install_odoo', 'run_odoo', 'restore', 'test_upgrade') or (self.job_type == 'python' and ('docker_params =' in self.python_code or '_run_' in self.python_code))

    def _run_run_odoo(self, build, log_path, force=False):
        if not force:
            if build.parent_id:
                build._log('_run_run_odoo', 'build has a parent, skip run')
                return
            if build.no_auto_run:
                build._log('_run_run_odoo', 'build auto run is disabled, skip run')
                return

        exports = build._checkout()

        build._log('run', 'Start running build %s' % build.dest)
        # run server
        cmd = build._cmd(local_only=False, enable_log_db=self.enable_log_db)

        available_options = build.parse_config()

        if "--workers" in available_options:
            cmd += ["--workers", "2"]

        if "--gevent-port" in available_options:
            cmd += ["--gevent-port", "8070"]

        elif "--longpolling-port" in available_options:
            cmd += ["--longpolling-port", "8070"]

        if "--max-cron-threads" in available_options:
            cmd += ["--max-cron-threads", "1"]

        db_name = build.params_id.config_data.get('db_name') or (build.database_ids[0].db_suffix if build.database_ids else 'all')
        # we need to have at least one job of type install_odoo to run odoo, take the last one for db_name.
        cmd += ['-d', '%s-%s' % (build.dest, db_name)]

        if "--proxy-mode" in available_options:
            cmd += ["--proxy-mode"]

        if "--db-filter" in available_options:
            cmd += ['--db-filter', '%d.*$']

        if "--smtp" in available_options:
            smtp_host = docker_get_gateway_ip()
            if smtp_host:
                cmd += ['--smtp', smtp_host]

        extra_params = self.extra_params or ''
        if extra_params:
            cmd.extend(shlex.split(extra_params))
        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []

        docker_name = build._get_docker_name()
        build_port = build.port
        self.env.cr.commit()  # commit before docker run to be 100% sure that db state is consistent with dockers
        self.invalidate_cache()
        self.env['runbot.runbot']._reload_nginx()
        return dict(cmd=cmd, log_path=log_path, container_name=docker_name, exposed_ports=[build_port, build_port + 1], ro_volumes=exports, env_variables=env_variables)

    def _run_install_odoo(self, build, log_path):
        exports = build._checkout()

        modules_to_install = self._modules_to_install(build)
        mods = ",".join(modules_to_install)
        python_params = []
        py_version = build._get_py_version()
        if self.coverage:
            build.coverage = True
            coverage_extra_params = self._coverage_params(build, modules_to_install)
            python_params = ['-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + coverage_extra_params
        elif self.flamegraph:
            python_params = ['-m', 'flamegraph', '-o', self._perfs_data_path()]
        cmd = build._cmd(python_params, py_version, sub_command=self.sub_command, enable_log_db=self.enable_log_db)
        # create db if needed
        db_suffix = build.params_id.config_data.get('db_name') or (build.params_id.dump_db.db_suffix if not self.create_db else False) or self.db_name
        db_name = '%s-%s' % (build.dest, db_suffix)
        if self.create_db:
            build._local_pg_createdb(db_name)
        cmd += ['-d', db_name]
        # list module to install
        extra_params = build.params_id.extra_params or self.extra_params or ''
        if mods and '-i' not in extra_params:
            cmd += ['-i', mods]
        config_path = build._server("tools/config.py")

        available_options = build.parse_config()
        if self.test_enable:
            if "--test-enable" in available_options:
                cmd.extend(['--test-enable'])
            else:
                build._log('test_all', 'Installing modules without testing', level='WARNING')

        test_tags_in_extra = '--test-tags' in extra_params

        if (self.test_enable or self.test_tags) and "--test-tags" in available_options and not test_tags_in_extra:
            test_tags = []
            custom_tags = build.params_id.config_data.get('test_tags')
            if custom_tags:
                test_tags += custom_tags.replace(' ', '').split(',')
            if self.test_tags:
                test_tags += self.test_tags.replace(' ', '').split(',')
            if self.enable_auto_tags and not build.params_id.config_data.get('disable_auto_tags', False):
                if grep(config_path, "[/module][:class]"):
                    auto_tags = self.env['runbot.build.error'].disabling_tags()
                    if auto_tags:
                        test_tags += auto_tags

            test_tags = [test_tag for test_tag in test_tags if test_tag]
            if test_tags:
                cmd.extend(['--test-tags', ','.join(test_tags)])
        elif (test_tags_in_extra or self.test_tags) and "--test-tags" not in available_options:
            build._log('test_all', 'Test tags given but not supported')

        if "--screenshots" in available_options:
            cmd.add_config_tuple('screenshots', '/data/build/tests')

        if "--screencasts" in available_options and self.env['ir.config_parameter'].sudo().get_param('runbot.enable_screencast', False):
            cmd.add_config_tuple('screencasts', '/data/build/tests')

        cmd.append('--stop-after-init')  # install job should always finish
        if '--log-level' not in extra_params:
            cmd.append('--log-level=test')
        cmd.append('--max-cron-threads=0')

        if extra_params:
            cmd.extend(shlex.split(extra_params))

        cmd.finals.extend(self._post_install_commands(build, modules_to_install, py_version))  # coverage post, extra-checks, ...
        dump_dir = '/data/build/logs/%s/' % db_name
        sql_dest = '%s/dump.sql' % dump_dir
        filestore_path = '/data/build/datadir/filestore/%s' % db_name
        filestore_dest = '%s/filestore/' % dump_dir
        zip_path = '/data/build/logs/%s.zip' % db_name
        cmd.finals.append(['pg_dump', db_name, '>', sql_dest])
        cmd.finals.append(['cp', '-r', filestore_path, filestore_dest])
        cmd.finals.append(['cd', dump_dir, '&&', 'zip', '-rmq9', zip_path, '*'])
        infos = '{\n    "db_name": "%s",\n    "build_id": %s,\n    "shas": [%s]\n}' % (db_name, build.id, ', '.join(['"%s"' % build_commit.commit_id.dname for build_commit in build.params_id.commit_link_ids]))
        build.write_file('logs/%s/info.json' % db_name, infos)

        if self.flamegraph:
            cmd.finals.append(['flamegraph.pl', '--title', 'Flamegraph %s for build %s' % (self.name, build.id), self._perfs_data_path(), '>', self._perfs_data_path(ext='svg')])
            cmd.finals.append(['gzip', '-f', self._perfs_data_path()])  # keep data but gz them to save disc space
        max_timeout = int(self.env['ir.config_parameter'].get_param('runbot.runbot_timeout', default=10000))
        timeout = min(self.cpu_limit, max_timeout)
        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        return dict(cmd=cmd, log_path=log_path, container_name=build._get_docker_name(), cpu_limit=timeout, ro_volumes=exports, env_variables=env_variables)

    def _upgrade_create_childs(self):
        pass

    def _run_configure_upgrade_complement(self, build, *args):
        """
        Parameters:
            - upgrade_dumps_trigger_id:  a configure_upgradestep

        A complement aims to test the exact oposite of an upgrade trigger.
        Ignore configs an categories: only focus on versions.
        """
        param = build.params_id
        version = param.version_id
        builds_references = param.builds_reference_ids
        builds_references_by_version_id = {b.params_id.version_id.id: b for b in builds_references}
        upgrade_complement_step = build.params_id.trigger_id.upgrade_dumps_trigger_id.upgrade_step_id
        version_domain = build.params_id.trigger_id.upgrade_dumps_trigger_id.get_version_domain()
        valid_targets = build.browse()
        next_versions = version.next_major_version_id | version.next_intermediate_version_ids
        if version_domain:  # filter only on version where trigger is enabled
            next_versions = next_versions.filtered_domain(version_domain)
        if next_versions:
            for next_version in next_versions:
                if version in upgrade_complement_step._get_upgrade_source_versions(next_version):
                    valid_targets |= (builds_references_by_version_id.get(next_version.id) or build.browse())

        for target in valid_targets:
            build._log('', 'Checking upgrade to [%s](%s)' % (target.params_id.version_id.name, target.build_url), log_type='markdown')
            for upgrade_db in upgrade_complement_step.upgrade_dbs:
                if not upgrade_db.min_target_version_id or upgrade_db.min_target_version_id.number <= target.params_id.version_id.number:
                    # note: here we don't consider the upgrade_db config here
                    dbs = build.database_ids.sorted('db_suffix')
                    for db in self._filter_upgrade_database(dbs, upgrade_db.db_pattern):
                        child = build._add_child({
                            'upgrade_to_build_id': target.id,
                            'upgrade_from_build_id': build,  # always current build
                            'dump_db': db.id,
                            'config_id': upgrade_complement_step.upgrade_config_id
                        })
                        child.description = 'Testing migration from %s to %s using parent db %s' % (
                            version.name,
                            target.params_id.version_id.name,
                            db.name,
                        )
                        child._log('', 'This build tests change of schema in stable version testing upgrade to %s' % target.params_id.version_id.name)

    def _run_configure_upgrade(self, build, log_path):
        """
        Source/target parameters:
            - upgrade_to_current | (upgrade_to_master + (upgrade_to_major_versions | upgrade_to_all_versions))
            - upgrade_from_previous_major_version + (upgrade_from_all_intermediate_version | upgrade_from_last_intermediate_version)
            - upgrade_dbs
            - upgrade_to_version_ids (use instead of upgrade_to flags)
            - upgrade_from_version_ids (use instead of upgrade_from flags)

        Other parameters
            - upgrade_flat
            - upgrade_config_id

        Create subbuilds with parameters defined for a step of type test_upgrade:
            - upgrade_to_build_id
            - upgrade_from_build_id
            - dump_db
            - config_id (upgrade_config_id)

        If upgrade_flat is False, a level of child will be create for target, source and dbs
        (if there is multiple choices).
        If upgrade_flat is True, all combination will be computed locally and only one level of children will be added to caller build.

        Note:
        - This step should be alone in a config since this config is recursive
        - A typical upgrade_config_id should have a restore step and a test_upgrade step.
        """
        assert len(build.parent_path.split('/')) < 6  # small security to avoid recursion loop, 6 is arbitrary
        param = build.params_id
        end = False
        target_builds = False
        source_builds_by_target = {}
        builds_references = param.builds_reference_ids
        builds_references_by_version_id = {b.params_id.version_id.id: b for b in builds_references}
        if param.upgrade_to_build_id:
            target_builds = param.upgrade_to_build_id
        else:
            if self.upgrade_to_current:
                target_builds = build
            else:
                target_builds = build.browse()
                if self.upgrade_to_version_ids:
                    for version in self.upgrade_to_version_ids:
                        target_builds |= builds_references_by_version_id.get(version.id) or build.browse()
                else:
                    master_build = builds_references.filtered(lambda b: b.params_id.version_id.name == 'master')
                    base_builds = (builds_references - master_build)
                    if self.upgrade_to_master:
                        target_builds = master_build
                    if self.upgrade_to_major_versions:
                        target_builds |= base_builds.filtered(lambda b: b.params_id.version_id.is_major)
                    elif self.upgrade_to_all_versions:
                        target_builds |= base_builds
                target_builds = target_builds.sorted(lambda b: b.params_id.version_id.number)
            if target_builds:
                build._log('', 'Testing upgrade targeting %s' % ', '.join(target_builds.mapped('params_id.version_id.name')))
            if not target_builds:
                build._log('_run_configure_upgrade', 'No reference build found with correct target in availables references, skipping. %s' % builds_references.mapped('params_id.version_id.name'), level='ERROR')
                end = True
            elif len(target_builds) > 1 and not self.upgrade_flat:
                for target_build in target_builds:
                    build._add_child(
                        {'upgrade_to_build_id': target_build.id},
                        description="Testing migration to %s" % target_build.params_id.version_id.name
                    )
                end = True
        if end:
            return  # replace this by a python job friendly solution

        for target_build in target_builds:
            if param.upgrade_from_build_id:
                source_builds_by_target[target_build] = param.upgrade_from_build_id
            else:
                if self.upgrade_from_current:
                    from_builds = build
                else:
                    target_version = target_build.params_id.version_id
                    from_builds = self._get_upgrade_source_builds(target_version, builds_references_by_version_id)
                source_builds_by_target[target_build] = from_builds
                if from_builds:
                    build._log('', 'Defining source version(s) for %s: %s' % (target_build.params_id.version_id.name, ', '.join(source_builds_by_target[target_build].mapped('params_id.version_id.name'))))
                if not from_builds:
                    build._log('_run_configure_upgrade', 'No source version found for %s, skipping' % target_version.name, level='INFO')
                elif not self.upgrade_flat:
                    for from_build in from_builds:
                        build._add_child(
                            {'upgrade_to_build_id': target_build.id, 'upgrade_from_build_id': from_build.id},
                            description="Testing migration from %s to %s" % (from_build.params_id.version_id.name, target_build.params_id.version_id.name)
                        )
                    end = True

        if end:
            return  # replace this by a python job friendly solution

        assert not param.dump_db
        for target, sources in source_builds_by_target.items():
            for source in sources:
                valid_databases = []
                if not self.upgrade_dbs:
                    valid_databases = source.database_ids
                for upgrade_db in self.upgrade_dbs:
                    if not upgrade_db.min_target_version_id or upgrade_db.min_target_version_id.number <= target.params_id.version_id.number:
                        config_id = upgrade_db.config_id
                        dump_builds = build.search([('id', 'child_of', source.id), ('params_id.config_id', '=', config_id.id), ('orphan_result', '=', False)])
                        # this search is not optimal
                        if not dump_builds:
                            build._log('_run_configure_upgrade', 'No child build found with config %s in %s' % (config_id.name, source.id), level='ERROR')
                        dbs = dump_builds.database_ids.sorted('db_suffix')
                        valid_databases += list(self._filter_upgrade_database(dbs, upgrade_db.db_pattern))
                        if not valid_databases:
                            build._log('_run_configure_upgrade', 'No datase found for pattern %s' % (upgrade_db.db_pattern), level='ERROR')
                for db in valid_databases:
                    #commit_ids = build.params_id.commit_ids
                    #if commit_ids != target.params_id.commit_ids:
                    #    repo_ids = commit_ids.mapped('repo_id')
                    #    for commit_link in target.params_id.commit_link_ids:
                    #        if commit_link.commit_id.repo_id not in repo_ids:
                    #            additionnal_commit_links |= commit_link
                    #    build._log('', 'Adding sources from build [%s](%s)' % (target.id, target.build_url), log_type='markdown')

                    child = build._add_child({
                        'upgrade_to_build_id': target.id,
                        'upgrade_from_build_id': source,
                        'dump_db': db.id,
                        'config_id': self.upgrade_config_id
                    })

                    child.description = 'Testing migration from %s to %s using db %s (%s)' % (
                        source.params_id.version_id.name,
                        target.params_id.version_id.name,
                        db.name,
                        config_id.name
                    )
                # TODO log somewhere if no db at all is found for a db_suffix

    def _get_upgrade_source_versions(self, target_version):
        if self.upgrade_from_version_ids:
            return self.upgrade_from_version_ids
        else:
            versions = self.env['runbot.version'].browse()
            if self.upgrade_from_previous_major_version:
                versions |= target_version.previous_major_version_id
            if self.upgrade_from_all_intermediate_version:
                versions |= target_version.intermediate_version_ids
            elif self.upgrade_from_last_intermediate_version:
                if target_version.intermediate_version_ids:
                    versions |= target_version.intermediate_version_ids[-1]
        return versions

    def _get_upgrade_source_builds(self, target_version, builds_references_by_version_id):
        versions = self._get_upgrade_source_versions(target_version)
        from_builds = self.env['runbot.build'].browse()
        for version in versions:
            from_builds |= builds_references_by_version_id.get(version.id) or self.env['runbot.build'].browse()
        return from_builds.sorted(lambda b: b.params_id.version_id.number)

    def _filter_upgrade_database(self, dbs, pattern):
        pat_list = pattern.split(',') if pattern else []
        for db in dbs:
            if any(fnmatch.fnmatch(db.db_suffix, pat) for pat in pat_list):
                yield db

    def _run_test_upgrade(self, build, log_path):
        target = build.params_id.upgrade_to_build_id
        commit_ids = build.params_id.commit_ids
        target_commit_ids = target.params_id.commit_ids
        if commit_ids != target_commit_ids:
            target_repo_ids = target_commit_ids.mapped('repo_id')
            for commit in commit_ids:
                if commit.repo_id not in target_repo_ids:
                    target_commit_ids |= commit
            build._log('', 'Adding sources from build [%s](%s)' % (target.id, target.build_url), log_type='markdown')
        build = build.with_context(defined_commit_ids=target_commit_ids)
        exports = build._checkout()

        db_suffix = build.params_id.config_data.get('db_name') or build.params_id.dump_db.db_suffix
        migrate_db_name = '%s-%s' % (build.dest, db_suffix)  # only ok if restore does not force db_suffix

        migrate_cmd = build._cmd(enable_log_db=self.enable_log_db)
        migrate_cmd += ['-u all']
        migrate_cmd += ['-d', migrate_db_name]
        migrate_cmd += ['--stop-after-init']
        migrate_cmd += ['--max-cron-threads=0']
        # migrate_cmd += ['--upgrades-paths', '/%s' % migration_scripts] upgrades-paths is broken, ln is created automatically in sources

        build._log('run', 'Start migration build %s' % build.dest)
        timeout = self.cpu_limit

        migrate_cmd.finals.append(['psql', migrate_db_name, '-c', '"SELECT id, name, state FROM ir_module_module WHERE state NOT IN (\'installed\', \'uninstalled\', \'uninstallable\') AND name NOT LIKE \'test_%\' "', '>', '/data/build/logs/modules_states.txt'])

        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        exception_env = self.env['runbot.upgrade.exception']._generate()
        if exception_env:
            env_variables.append(exception_env)
        return dict(cmd=migrate_cmd, log_path=log_path, container_name=build._get_docker_name(), cpu_limit=timeout, ro_volumes=exports, env_variables=env_variables, image_tag=target.params_id.dockerfile_id.image_tag)

    def _run_restore(self, build, log_path):
        # exports = build._checkout()
        params = build.params_id

        if 'dump_url' in params.config_data:
            dump_url = params.config_data['dump_url']
            zip_name = dump_url.split('/')[-1]
            build._log('test-migration', 'Restoring db [%s](%s)' % (zip_name, dump_url), log_type='markdown')
            suffix = 'all'
        else:
            download_db_suffix = params.dump_db.db_suffix or self.restore_download_db_suffix
            dump_build = params.dump_db.build_id or build.parent_id
            assert download_db_suffix and dump_build
            download_db_name = '%s-%s' % (dump_build.dest, download_db_suffix)
            zip_name = '%s.zip' % download_db_name
            dump_url = '%s%s' % (dump_build.http_log_url(), zip_name)
            build._log('test-migration', 'Restoring dump [%s](%s) from build [%s](%s)' % (zip_name, dump_url, dump_build.id, dump_build.build_url), log_type='markdown')
        restore_suffix = self.restore_rename_db_suffix or params.dump_db.db_suffix or suffix
        assert restore_suffix
        restore_db_name = '%s-%s' % (build.dest, restore_suffix)

        build._local_pg_createdb(restore_db_name)
        cmd = ' && '.join([
            'mkdir /data/build/restore',
            'cd /data/build/restore',
            'wget %s' % dump_url,
            'unzip -q %s' % zip_name,
            'echo "### restoring filestore"',
            'mkdir -p /data/build/datadir/filestore/%s' % restore_db_name,
            'mv filestore/* /data/build/datadir/filestore/%s' % restore_db_name,
            'echo "### restoring db"',
            'psql -q %s < dump.sql' % (restore_db_name),
            'cd /data/build',
            'echo "### cleaning"',
            'rm -r restore',
            'echo "### listing modules"',
            """psql %s -c "select name from ir_module_module where state = 'installed'" -t -A > /data/build/logs/restore_modules_installed.txt""" % restore_db_name,
            'echo "### restore" "successful"', # two part string to avoid miss grep

            ])

        return dict(cmd=cmd, log_path=log_path, container_name=build._get_docker_name(), cpu_limit=self.cpu_limit)

    def _reference_builds(self, bundle, trigger):
        upgrade_dumps_trigger_id = trigger.upgrade_dumps_trigger_id
        refs_batches = self._reference_batches(bundle, trigger)
        refs_builds = refs_batches.mapped('slot_ids').filtered(
            lambda slot: slot.trigger_id == upgrade_dumps_trigger_id
            ).mapped('build_id')
        # should we filter on active? implicit. On match type? on skipped ?
        # is last_"done"_batch enough?
        # TODO active test false and take last done/running build limit 1 -> in case of rebuild
        return refs_builds

    def _is_upgrade_step(self):
        return self.job_type in ('configure_upgrade', 'configure_upgrade_complement')

    def _reference_batches(self, bundle, trigger):
        if self.job_type == 'configure_upgrade_complement':
            return self._reference_batches_complement(bundle, trigger)
        else:
            return self._reference_batches_upgrade(bundle, trigger.upgrade_dumps_trigger_id.category_id.id)

    def _reference_batches_complement(self, bundle, trigger):
        category_id = trigger.upgrade_dumps_trigger_id.category_id.id
        version = bundle.version_id
        next_versions = version.next_major_version_id | version.next_intermediate_version_ids  # TODO filter on trigger version
        target_versions = version.browse()

        upgrade_complement_step = trigger.upgrade_dumps_trigger_id.upgrade_step_id

        if next_versions and bundle.base_id.to_upgrade:
            for next_version in next_versions:
                if bundle.version_id in upgrade_complement_step._get_upgrade_source_versions(next_version):
                    target_versions |= next_version
        return target_versions.with_context(
            category_id=category_id, project_id=bundle.project_id.id
            ).mapped('base_bundle_id').filtered('to_upgrade').mapped('last_done_batch')

    def _reference_batches_upgrade(self, bundle, category_id):
        target_refs_bundles = self.env['runbot.bundle']
        upgrade_domain = [('to_upgrade', '=', True), ('project_id', '=', bundle.project_id.id)]
        if self.upgrade_to_version_ids:
            target_refs_bundles |= self.env['runbot.bundle'].search(upgrade_domain + [('version_id', 'in', self.upgrade_to_version_ids.ids)])
        else:
            if self.upgrade_to_master:
                target_refs_bundles |= self.env['runbot.bundle'].search(upgrade_domain + [('name', '=', 'master')])
            if self.upgrade_to_all_versions:
                target_refs_bundles |= self.env['runbot.bundle'].search(upgrade_domain + [('name', '!=', 'master')])
            elif self.upgrade_to_major_versions:
                target_refs_bundles |= self.env['runbot.bundle'].search(upgrade_domain + [('name', '!=', 'master'), ('version_id.is_major', '=', True)])

        source_refs_bundles = self.env['runbot.bundle']

        def from_versions(f_bundle):
            nonlocal source_refs_bundles
            if self.upgrade_from_previous_major_version:
                source_refs_bundles |= f_bundle.previous_major_version_base_id
            if self.upgrade_from_all_intermediate_version:
                source_refs_bundles |= f_bundle.intermediate_version_base_ids
            elif self.upgrade_from_last_intermediate_version:
                if f_bundle.intermediate_version_base_ids:
                    source_refs_bundles |= f_bundle.intermediate_version_base_ids[-1]

        if self.upgrade_from_version_ids:
            source_refs_bundles |= self.env['runbot.bundle'].search(upgrade_domain + [('version_id', 'in', self.upgrade_from_version_ids.ids)])
            # this is subject to discussion. should this be smart and filter 'from_versions' or should it be flexible and do all possibilities
        else:
            if self.upgrade_to_current:
                from_versions(bundle)
            for f_bundle in target_refs_bundles:
                from_versions(f_bundle)
            source_refs_bundles = source_refs_bundles.filtered('to_upgrade')

        return (target_refs_bundles | source_refs_bundles).with_context(
            category_id=category_id
            ).mapped('last_done_batch')

    def log_end(self, build):
        if self.job_type == 'create_build':
            build._logger('Step %s finished in %s' % (self.name, s2human(build.job_time)))
            return

        kwargs = dict(message='Step %s finished in %s' % (self.name, s2human(build.job_time)))
        if self.job_type == 'install_odoo':
            kwargs['message'] += ' $$fa-download$$'
            db_suffix = build.params_id.config_data.get('db_name') or (build.params_id.dump_db.db_suffix if not self.create_db else False) or self.db_name
            kwargs['path'] = '%s%s-%s.zip' % (build.http_log_url(), build.dest, db_suffix)
            kwargs['log_type'] = 'link'
        build._log('', **kwargs)

        if self.coverage:
            xml_url = '%scoverage.xml' % build.http_log_url()
            html_url = 'http://%s/runbot/static/build/%s/coverage/index.html' % (build.host, build.dest)
            message = 'Coverage report: [xml @icon-download](%s), [html @icon-eye](%s)' % (xml_url, html_url)
            build._log('end_job', message, log_type='markdown')

        if self.flamegraph:
            dat_url = '%sflame_%s.%s' % (build.http_log_url(), self.name, 'log.gz')
            svg_url = '%sflame_%s.%s' % (build.http_log_url(), self.name, 'svg')
            message = 'Flamegraph report: [data @icon-download](%s), [svg @icon-eye](%s)' % (dat_url, svg_url)
            build._log('end_job', message, log_type='markdown')

    def _modules_to_install(self, build):
        return set(build._get_modules_to_test(modules_patterns=self.install_modules))

    def _post_install_commands(self, build, modules_to_install, py_version=None):
        cmds = []
        if self.coverage:
            py_version = py_version if py_version is not None else build._get_py_version()
            # prepare coverage result
            cov_path = build._path('coverage')
            os.makedirs(cov_path, exist_ok=True)
            cmds.append(['python%s' % py_version, "-m", "coverage", "html", "-d", "/data/build/coverage", "--ignore-errors"])
            cmds.append(['python%s' % py_version, "-m", "coverage", "xml", "-o", "/data/build/logs/coverage.xml", "--ignore-errors"])
        return cmds

    def _perfs_data_path(self, ext='log'):
        return '/data/build/logs/flame_%s.%s' % (self.name, ext)

    def _coverage_params(self, build, modules_to_install):
        pattern_to_omit = set()
        if self.paths_to_omit:
            pattern_to_omit = set(self.paths_to_omit.split(','))
        for commit in build.params_id.commit_ids:
            docker_source_folder = build._docker_source_folder(commit)
            for manifest_file in commit.repo_id.manifest_files.split(','):
                pattern_to_omit.add('*%s' % manifest_file)
            for (addons_path, module, _) in commit._get_available_modules():
                if module not in modules_to_install:
                    # we want to omit docker_source_folder/[addons/path/]module/*
                    module_path_in_docker = os.path.join(docker_source_folder, addons_path, module)
                    pattern_to_omit.add('%s/*' % (module_path_in_docker))
        return ['--omit', ','.join(pattern_to_omit)]

    def _make_results(self, build):
        log_time = self._get_log_last_write(build)
        if log_time:
            build.job_end = log_time
        if self.job_type == 'python' and self.python_result_code and self.python_result_code != PYTHON_DEFAULT:
            build.write(self._make_python_results(build))
        elif self.job_type in ['install_odoo', 'python']:
            if self.coverage:
                build.write(self._make_coverage_results(build))
            if self.test_enable or self.test_tags:
                build.write(self._make_tests_results(build))
        elif self.job_type == 'test_upgrade':
            build.write(self._make_upgrade_results(build))
        elif self.job_type == 'restore':
            build.write(self._make_restore_results(build))

    def _make_python_results(self, build):
        eval_ctx = self.make_python_ctx(build)
        safe_eval(self.python_result_code.strip(), eval_ctx, mode="exec", nocopy=True)
        return_value = eval_ctx.get('return_value', {})
        # todo check return_value or write in try except. Example: local result setted to wrong value
        if not isinstance(return_value, dict):
            raise RunbotException('python_result_code must set return_value to a dict values on build')
        return return_value

    def _make_coverage_results(self, build):
        build_values = {}
        build._log('coverage_result', 'Start getting coverage result')
        cov_path = build._path('coverage/index.html')
        if os.path.exists(cov_path):
            with open(cov_path, 'r') as f:
                data = f.read()
                covgrep = re.search(r'pc_cov.>(?P<coverage>\d+)%', data)
                build_values['coverage_result'] = covgrep and covgrep.group('coverage') or False
                if build_values['coverage_result']:
                    build._log('coverage_result', 'Coverage result: %s' % build_values['coverage_result'])
                else:
                    build._log('coverage_result', 'Coverage result not found', level='WARNING')
        else:
            build._log('coverage_result', 'Coverage file not found', level='WARNING')
        return build_values

    def _make_upgrade_results(self, build):
        build_values = {}
        build._log('upgrade', 'Getting results for build %s' % build.dest)

        if build.local_result != 'ko':
            checkers = [
                self._check_log,
                self._check_module_loaded,
                self._check_error,
                self._check_module_states,
                self._check_build_ended,
                self._check_warning,
            ]
            local_result = self._get_checkers_result(build, checkers)
            build_values['local_result'] = build._get_worst_result([build.local_result, local_result])

        return build_values

    def _check_module_states(self, build):
        if not build.is_file('logs/modules_states.txt'):
            build._log('', '"logs/modules_states.txt" file not found.', level='ERROR')
            return 'ko'

        content = build.read_file('logs/modules_states.txt') or ''
        if '(0 rows)' not in content:
            build._log('', 'Some modules are not in installed/uninstalled/uninstallable state after migration. \n %s' % content)
            return 'ko'
        return 'ok'

    def _check_log(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        if not os.path.isfile(log_path):
            build._log('_make_tests_results', "Log file not found at the end of test job", level="ERROR")
            return 'ko'
        return 'ok'

    def _check_module_loaded(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        if not grep(log_path, ".modules.loading: Modules loaded."):
            build._log('_make_tests_results', "Modules loaded not found in logs", level="ERROR")
            return 'ko'
        return 'ok'

    def _check_error(self, build, regex=None):
        log_path = build._path('logs', '%s.txt' % self.name)
        regex = regex or _re_error
        if rfind(log_path, regex):
            build._log('_make_tests_results', 'Error or traceback found in logs', level="ERROR")
            return 'ko'
        return 'ok'

    def _check_warning(self, build, regex=None):
        log_path = build._path('logs', '%s.txt' % self.name)
        regex = regex or _re_warning
        if rfind(log_path, regex):
            build._log('_make_tests_results', 'Warning found in logs', level="WARNING")
            return 'warn'
        return 'ok'

    def _check_build_ended(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        if not grep(log_path, "Initiating shutdown"):
            build._log('_make_tests_results', 'No "Initiating shutdown" found in logs, maybe because of cpu limit.', level="ERROR")
            return 'ko'
        return 'ok'

    def _check_restore_ended(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        if not grep(log_path, "### restore successful"):
            build._log('_make_tests_results', 'Restore failed, check text logs for more info', level="ERROR")
            return 'ko'
        return 'ok'

    def _get_log_last_write(self, build):
        log_path = build._path('logs', '%s.txt' % self.name)
        if os.path.isfile(log_path):
            return time2str(time.localtime(os.path.getmtime(log_path)))

    def _get_checkers_result(self, build, checkers):
        for checker in checkers:
            result = checker(build)
            if result != 'ok':
                return result
        return 'ok'

    def _make_tests_results(self, build):
        build_values = {}
        build._log('run', 'Getting results for build %s' % build.dest)

        if build.local_result != 'ko':
            checkers = [
                self._check_log,
                self._check_module_loaded,
                self._check_error,
                self._check_build_ended
            ]
            if build.local_result != 'warn':
                checkers.append(self._check_warning)

            local_result = self._get_checkers_result(build, checkers)
            build_values['local_result'] = build._get_worst_result([build.local_result, local_result])
        return build_values

    def _make_restore_results(self, build):
        build_values = {}
        if build.local_result != 'warn':
            checkers = [
                self._check_log,
                self._check_restore_ended
            ]
            local_result = self._get_checkers_result(build, checkers)
            build_values['local_result'] = build._get_worst_result([build.local_result, local_result])
        return build_values

    def _make_stats(self, build):
        if not self.make_stats:  # TODO garbage collect non sticky stat
            return
        build._log('make_stats', 'Getting stats from log file')
        log_path = build._path('logs', '%s.txt' % self.name)
        if not os.path.exists(log_path):
            build._log('make_stats', 'Log **%s.txt** file not found' % self.name, level='INFO', log_type='markdown')
            return
        try:
            regex_ids = self.build_stat_regex_ids
            if not regex_ids:
                regex_ids = regex_ids.search([('generic', '=', True)])
            stats_per_regex = regex_ids._find_in_file(log_path)
            if stats_per_regex:
                build_stats = [
                    {
                        'config_step_id': self.id,
                        'build_id': build.id,
                        'category': category,
                        'values': values,
                    } for category, values in stats_per_regex.items()
                ]
                self.env['runbot.build.stat'].create(build_stats)
        except Exception as e:
            message = '**An error occured while computing statistics of %s:**\n`%s`' % (build.job, str(e).replace('\\n', '\n').replace("\\'", "'"))
            _logger.exception(message)
            build._log('make_stats', message, level='INFO', log_type='markdown')

    def _step_state(self):
        self.ensure_one()
        if self.job_type == 'run_odoo' or (self.job_type == 'python' and self.running_job):
            return 'running'
        return 'testing'

    def _has_log(self):
        self.ensure_one()
        return self._is_docker_step()

    def _check_limits(self, build):
        bundle = build.params_id.create_batch_id.bundle_id
        commit_limit = bundle.commit_limit or self.commit_limit
        file_limit = bundle.file_limit or self.file_limit
        message = 'Limit reached: %s has more than %s %s (%s) and will be skipped. Contact runbot team to increase your limit if it was intended'
        success = True
        for commit_link in build.params_id.commit_link_ids:
            if commit_link.base_ahead > commit_limit:
                build._log('', message % (commit_link.commit_id.name, commit_limit, 'commit', commit_link.base_ahead), level="ERROR")
                build.local_result = 'ko'
                success = False
            if commit_link.file_changed > file_limit:
                build._log('', message % (commit_link.commit_id.name, file_limit, 'modified files', commit_link.file_changed), level="ERROR")
                build.local_result = 'ko'
                success = False
        return success

    def _modified_files(self, build, commit_link_links=None):
        modified_files = {}
        if commit_link_links is None:
            commit_link_links = build.params_id.commit_link_ids
        for commit_link in commit_link_links:
            commit = commit_link.commit_id
            modified = commit.repo_id._git(['diff', '--name-only', '%s..%s' % (commit_link.merge_base_commit_id.name, commit.name)])
            if modified:
                files = [('%s/%s' % (build._docker_source_folder(commit), file)) for file in modified.split('\n') if file]
                modified_files[commit_link] = files
        return modified_files


class ConfigStepOrder(models.Model):
    _name = 'runbot.build.config.step.order'
    _description = "Config step order"
    _order = 'sequence, id'
    # a kind of many2many rel with sequence

    sequence = fields.Integer('Sequence', required=True)
    config_id = fields.Many2one('runbot.build.config', 'Config', required=True, ondelete='cascade')
    step_id = fields.Many2one('runbot.build.config.step', 'Config Step', required=True, ondelete='cascade')

    @api.onchange('step_id')
    def _onchange_step_id(self):
        self.sequence = self.step_id.default_sequence

    @api.model_create_single
    def create(self, values):
        if 'sequence' not in values and values.get('step_id'):
            values['sequence'] = self.env['runbot.build.config.step'].browse(values.get('step_id')).default_sequence
        if self.pool._init:  # do not duplicate entry on install
            existing = self.search([('sequence', '=', values.get('sequence')), ('config_id', '=', values.get('config_id')), ('step_id', '=', values.get('step_id'))])
            if existing:
                return
        return super(ConfigStepOrder, self).create(values)
