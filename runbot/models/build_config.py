import base64
import glob
import json
import logging
import fnmatch
import psutil
import re
import shlex
import time
from unidiff import PatchSet
from ..common import now, grep, time2str, rfind, s2human, os, RunbotException, ReProxy, markdown_escape
from ..container import docker_get_gateway_ip, Command
from odoo import models, fields, api, tools
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import file_open
from odoo.tools.safe_eval import safe_eval, test_python_expr, _SAFE_OPCODES, to_opcodes

# adding some additionnal optcode to safe_eval. This is not 100% needed and won't be done in standard but will help
# to simplify some python step by wraping the content in a function to allow return statement and get closer to other
# steps


# There is an issue in unidiff 0.7.3 fixed in 0.7.4
# https://github.com/matiasb/python-unidiff/commit/a3faffc54e5aacaee3ded4565c534482d5cc3465
# Since the unidiff packaged version in noble is 0.7.3
# patching it looks like the easiest solution

from unidiff import patch, VERSION
if VERSION == '0.7.3':
    patch.RE_DIFF_GIT_DELETED_FILE = re.compile(r'^deleted file mode \d+$')
    patch.RE_DIFF_GIT_NEW_FILE = re.compile(r'^new file mode \d+$')


_SAFE_OPCODES |= set(to_opcodes(['LOAD_DEREF', 'STORE_DEREF', 'LOAD_CLOSURE', 'MAKE_CELL', 'COPY_FREE_VARS']))

_logger = logging.getLogger(__name__)

_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING .*'

PYTHON_DEFAULT = "# type python code here\n\n\n\n\n\n"


def filter_all_modules(selector, build, dynamic_vars):
    if selector.split(',', 1)[0] != '*':
        selector = f'*,{selector}'
    return filter_default_modules(selector, build, dynamic_vars)


def filter_default_modules(selector, build, dynamic_vars):
    build._checkout()  # we need to ensure source are exported before _get_modules_to_test
    modules = build._get_modules_to_test(selector)
    return ','.join(modules)


def keep_modified_modules(modules, build, dynamic_vars):
    if build.params_id.config_data.get('skip_modified_modules_filter', False):
        return modules
    modified_modules = build._modified_modules()
    modules = modules.split(',')
    filtered_modules = [module for module in modules if module in modified_modules]
    return ','.join(filtered_modules)


def keep_modified_modules_or_base(modules, build, dynamic_vars):
    bundle = build.params_id.create_batch_id.bundle_id
    if bundle.is_base or bundle.is_staging:
        return modules
    return keep_modified_modules(modules, build, dynamic_vars)


def make_module_test_tags(modules, build, dynamic_vars):
    return ','.join([f'/{module}' for module in modules.split(',')])


def prepend_string(modules, build, dynamic_vars, element):
    if re.match(r'^[\'"].*[\'"]$', element):
        element = element[1:-1]
    else:
        element = dynamic_vars.get(element, element)
    return ','.join([f'{element}{module}' for module in modules.split(',')])


def append_string(modules, build, dynamic_vars, element):
    if re.match(r'^[\'"].*[\'"]$', element):
        element = element[1:-1]
    else:
        element = dynamic_vars.get(element, element)
    return ','.join([f'{module}{element}' for module in modules.split(',')])


class Config(models.Model):
    _name = 'runbot.build.config'
    _description = "Build config"
    _inherit = "mail.thread"

    name = fields.Char('Config name', required=True, tracking=True, help="Unique name for config please use trigram as postfix for custom configs")
    active = fields.Boolean('Active', default=True, tracking=True)

    description = fields.Char('Config description')
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'config_id', copy=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)
    group = fields.Many2one('runbot.build.config', 'Configuration group', help="Group of config's and config steps")
    group_name = fields.Char('Group name', related='group.name')
    step_ids = fields.Many2many('runbot.build.config.step', compute='_compute_step_ids')

    dynamic_config_file_path = fields.Char('Dynamic Config File Path', tracking=True)
    default_dynamic_config = fields.Text('Default Dynamic Config File', tracking=True)
    dynamic_config_extension = fields.Text('Dynamic Config Extend File', tracking=True)

    @api.constrains('default_dynamic_config', 'dynamic_config_extension')
    def _check_dynamic_config(self):
        for record in self:
            try:
                base_config = json.loads(record.default_dynamic_config or '{}')
            except json.JSONDecodeError as e:
                msg = f'Invalid json in field default_dynamic_config: {e.msg} at line {e.lineno} column {e.colno}'
                raise ValidationError(f'Error in field default_dynamic_config: {msg}') from None
            try:
                config_extension = json.loads(record.dynamic_config_extension or '{}')
            except json.JSONDecodeError as e:
                msg = f'Invalid json in field dynamic_config_extension: {e.msg} at line {e.lineno} column {e.colno}'
                raise ValidationError(f'Error in field dynamic_config_extension: {msg}') from None

            extended_config = record._apply_dynamic_config_extension(base_config, config_extension)
            for config in [base_config, extended_config]:
                if config:
                    self._validate_dynamic_config(config)

    def _apply_dynamic_config_extension(self, base_config, extension):
        if not extension:
            return base_config
        for key, extension_value in extension.items():
            base_value = base_config.get(key)
            if isinstance(extension_value, dict) and isinstance(base_value, dict):
                if base_value is not None:
                    base_config[key] = self._apply_dynamic_config_extension(base_value, extension_value)
                continue
            if isinstance(extension_value, list) and len(extension_value) == 2:
                action, action_value = extension_value
                if isinstance(action, str):
                    if action == 'APPEND' and key in base_config:
                        base_config[key] += action_value
                    if action == 'SET':
                        base_config[key] = action_value
                    continue
                    _logger.warning('Unknown action %s for key %s in dynamic config extension', action, key)
            if isinstance(extension_value, list) and isinstance(base_value, list):
                for _extension_value in extension_value:
                    extension_filters = {k[1:]: v for k, v in _extension_value.items() if k.startswith('@')}
                    new_values = {k: v for k, v in _extension_value.items() if not k.startswith('@')}
                    for _base_value in base_value:
                        for filter_key, filter_val in extension_filters.items():
                            if _base_value.get(filter_key) != filter_val:
                                break
                        else:
                            self._apply_dynamic_config_extension(_base_value, new_values)
        return base_config

    def _validate_dynamic_config(self, config):
        def validate(schema, value, path):
            for key, validator in schema.items():
                val = value.get(key)
                if callable(validator):
                    validator(val, f'{path}.{key}')
                else:
                    if val != validator:
                        raise ValidationError(f'{path}.{key} should be {validator}, got {val}')
            for key in value:
                if key not in schema:
                    raise ValidationError(f'Unexpected key {key} in {path}')

        def str_checker(regex):
            def wrapper(modules, path):
                if not isinstance(modules, str):
                    raise ValidationError(f'{path} ({modules}) should be a string')
                if not re.match(regex, modules):
                    raise ValidationError(f'{path} ({modules}) contains invalid characters ({regex})')
            return wrapper

        def REQUIRED(validator):
            def wrapper(value, path):
                if not value:
                    raise ValidationError(f'{path} is required')
                return validator(value, path)
            return wrapper

        def OPTIONAL(validator):
            def wrapper(value, path):
                if value is not None:
                    return validator(value, path)
            return wrapper

        def CONFIG(child, path):
            validate(config_schema, child, path + '[]')

        valid_steps = {}

        def STEP(step, path):
            if (job_type := step.get('job_type')) not in valid_steps:
                raise ValidationError(f'Unknown job_type "{job_type}" in {path}[]')
            step_schema = valid_steps[step.get('job_type')]
            validate(step_schema, step, f'{path}[]')

        def IN(options):
            def wrapper(value, path):
                if value not in options:
                    raise ValidationError(f'{path} should be one of {options}, got {value}')
            return wrapper

        def type_checker(expected_type):
            def wrapper(value, path):
                if not isinstance(value, expected_type):
                    raise ValidationError(f'{path} should be of type {expected_type.__name__}, got {type(value).__name__}')
            return wrapper

        def LIST(validator):
            def wrapper(value, path):
                if not isinstance(value, list):
                    raise ValidationError(f'{path} should be a list')
                for index, item in enumerate(value):
                    validator(item, f'{path}[{index}]')
            return wrapper

        def VARS(vars, path):
            if not isinstance(vars, dict):
                raise ValidationError(f'{path} ({vars}) should be a dict')
            for key, val in vars.items():
                TECHNICAL_NAME(key, f'{path}.{key}')
                STR(val, f'{path}.{key}')

        NAME = str_checker(r'^[\w \-]+$')
        STR = str_checker(r'.+')
        DYNAMIC_VALUE = STR
        TECHNICAL_NAME = str_checker(r'^[a-z0-9_\-]+$')
        BOOL = type_checker(bool)
        INT = type_checker(int)
        COMMAND = str_checker(r'^.*$')

        config_schema = {
            'name': REQUIRED(NAME),
            'vars': OPTIONAL(VARS),
            'steps': REQUIRED(LIST(STEP)),
            'description': OPTIONAL(DYNAMIC_VALUE),
        }
        valid_steps['odoo'] = {
            'name': REQUIRED(NAME),
            'job_type': 'odoo',
            'db_name': OPTIONAL(TECHNICAL_NAME),
            'install_modules': OPTIONAL(DYNAMIC_VALUE),
            'install_default_modules': OPTIONAL(DYNAMIC_VALUE),
            'test_tags': OPTIONAL(DYNAMIC_VALUE),
            'demo_mode': OPTIONAL(IN(['default', 'with_demo', 'without_demo'])),
            'enable_auto_tags': OPTIONAL(BOOL),
            'cpu_limit': OPTIONAL(INT),
            'export_database': OPTIONAL(BOOL),
            'make_stats': OPTIONAL(BOOL),
        }
        valid_steps['create_build'] = {
            'name': REQUIRED(NAME),
            'job_type': 'create_build',
            'children': REQUIRED(LIST(CONFIG)),
            'for_each_vars': OPTIONAL(LIST(VARS)),
            'for_each_module': OPTIONAL(DYNAMIC_VALUE),
            'max_builds': OPTIONAL(INT),
        }
        valid_steps['restore'] = {
            'name': REQUIRED(NAME),
            'job_type': 'restore',
            'db_name': REQUIRED(TECHNICAL_NAME),
            'build_id': OPTIONAL(INT),
            'trigger_id': OPTIONAL(INT),
            'use_current_batch': OPTIONAL(BOOL),
            'zip_url': OPTIONAL(STR),
        }
        valid_steps['command'] = {
            'name': REQUIRED(NAME),
            'job_type': 'command',
            'db_name': OPTIONAL(TECHNICAL_NAME),
            'command': REQUIRED(COMMAND),
            'cpu_limit': OPTIONAL(INT),
            'install_requirements': OPTIONAL(BOOL),
            'export_database': OPTIONAL(BOOL),
            'check_logs': OPTIONAL(LIST(STR)),
            'expected_logs': OPTIONAL(LIST(STR)),
            'make_stats': OPTIONAL(BOOL),
        }
        validate(config_schema, config, 'config')

    @api.model_create_multi
    def create(self, vals_list):
        res = super(Config, self).create(vals_list)
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
        copy.name = f'{self.name} (copy)'
        return copy

    @api.depends('step_order_ids.sequence', 'step_order_ids.step_id')
    def _compute_step_ids(self):
        for config in self:
            config.step_ids = config.step_order_ids.sorted('sequence').mapped('step_id')

    def _check_step_ids_order(self):
        for record in self:
            install_job = False
            for step in record.step_ids:
                if step.job_type == 'install_odoo':
                    install_job = True
                if step.job_type == 'run_odoo':
                    if step != record.step_ids[-1]:
                        raise UserError('Jobs of type run_odoo should be the last one')
                    if not install_job:
                        raise UserError('Jobs of type run_odoo should be preceded by a job of type install_odoo')
            record._check_recursion()

    def _check_recursion(self, visited=None):
        self.ensure_one()
        visited = visited or []
        recursion = False
        if self in visited:
            recursion = True
        visited.append(self)
        if recursion:
            raise UserError('Impossible to save config, recursion detected with path: %s' % ">".join([v.name for v in visited]))
        for step in self.step_ids:
            if step.job_type == 'create_build':
                for create_config in step.create_config_ids:
                    create_config._check_recursion(visited[:])


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
        ('dynamic', 'Dynamic'),
    ]


class ConfigStep(models.Model):
    _name = 'runbot.build.config.step'
    _description = "Config step"
    _inherit = 'mail.thread'

    # general info
    name = fields.Char('Step name', required=True, tracking=True, help="Unique name for step please use trigram as postfix for custom step_ids")
    active = fields.Boolean('Active', default=True, tracking=True)
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
    dockerfile_variant = fields.Char('Docker Variant')
    # install_odoo
    create_db = fields.Boolean('Create Db', default=True, tracking=True)  # future
    custom_db_name = fields.Char('Custom Db Name', tracking=True)  # future
    install_modules = fields.Char('Modules to install', help="List of module patterns to install, use * to install all available modules, prefix the pattern with dash to remove the module.", default='', tracking=True)
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', tracking=True)
    cpu_limit = fields.Integer('Cpu limit', default=3600, tracking=True)
    container_cpus = fields.Integer('Allowed CPUs', help='Allowed container CPUs. Fallback on config parameter if 0.', default=0, tracking=True)
    coverage = fields.Boolean('Coverage', default=False, tracking=True)
    paths_to_omit = fields.Char('Paths to omit from coverage', tracking=True)
    flamegraph = fields.Boolean('Allow Flamegraph', default=False, tracking=True)
    test_enable = fields.Boolean('Test enable', default=True, tracking=True)
    test_tags = fields.Char('Test tags', help="comma separated list of test tags", tracking=True)
    enable_auto_tags = fields.Boolean('Allow auto tag', default=True, tracking=True)
    sub_command = fields.Char('Subcommand', tracking=True)
    extra_params = fields.Char('Extra cmd args', tracking=True)
    additionnal_env = fields.Char('Extra env', help='Example: foo="bar";bar="foo". Cannot contains \' ', tracking=True)
    enable_log_db = fields.Boolean("Enable log db", default=True)
    demo_mode = fields.Selection(
        [('default', 'Default'), ('without_demo', 'Without Demo'), ('with_demo', 'With Demo')],
        "Install demo data", default='default', tracking=True, required=True,
    )
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
    break_before_if_ko = fields.Boolean('Break before this step if build is ko')
    break_after_if_ko = fields.Boolean('Break after this step if build is ko')


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

    def _get_db_name(self, build):
        db_name = self.custom_db_name or self.name
        return re.sub(r'[^a-z0-9\-_]', '_', db_name.lower())

    def sanitized_name(self, build):
        name = self._get_display_name(build) or self.name or ''
        return re.sub(r'[^a-z0-9\-_]', '_', name.lower())

    def _inverse_db_name(self):
        for step in self:
            step.custom_db_name = step.db_name

    def copy(self):
        # remove protection on copy
        copy = super(ConfigStep, self).copy()
        copy._write({'protected': False})
        return copy

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check(vals)
        return super().create(vals_list)

    def write(self, values):
        self._check(values)
        return super(ConfigStep, self).write(values)

    def unlink(self):
        if any(record.protected for record in self):
            raise UserError('Protected step')
        super(ConfigStep, self).unlink()

    def _get_display_name(self, build):
        if self.job_type == 'dynamic':
            steps = build.dynamic_config.get('steps', [])
            index = build.dynamic_active_step_index
            if index < len(steps):
                return steps[index].get('name', self.name)
        return self.name

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
                    _logger.log('%s tried to create an non supported test_param %s' % self.env.user.name, values.get('extra_params'))
                    raise UserError('Invalid extra_params on config step')

    def _run(self, build):
        build.write({'job_start': now(), 'job_end': False})  # state, ...
        log = build._log('run', f'Starting step **{self._get_display_name(build)}** from config **{build.params_id.config_id.name}**', log_type='markdown', level='SEPARATOR')
        result = self._run_step(build)
        if callable(result):  # docker step, should have text logs
            if build.log_list:
                build.log_list = f'{build.log_list},{self.sanitized_name(build)}'
            else:
                build.log_list = self.sanitized_name(build)
            log_url = f'http://{build.host}'
            url = f"{log_url}/runbot/static/build/{build.dest}/logs/{self.sanitized_name(build)}.txt"
            log_link = f'[@icon-file-text]({url})'
            log.message = f'{log.message}  {log_link}'
        return result

    def _run_step(self, build, **kwargs):
        build.log_counter = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_maxlogs', 100)
        run_method = getattr(self, '_run_%s' % self.job_type)
        docker_params = run_method(build, **kwargs)
        if docker_params:
            if 'cpu_limit' not in docker_params:
                max_timeout = int(self.env['ir.config_parameter'].get_param('runbot.runbot_timeout', default=10000))
                docker_params['cpu_limit'] = min(self.cpu_limit, max_timeout)

            container_cpus = float(self.container_cpus or self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_containers_cpus', 0))
            if 'cpus' not in docker_params and container_cpus:
                logical_cpu_count = psutil.cpu_count(logical=True)
                physical_cpu_count = psutil.cpu_count(logical=False)
                docker_params['cpus'] = float((logical_cpu_count / physical_cpu_count) * container_cpus)
            return build._docker_run(self, **docker_params)
        return True

    def _run_create_build(self, build, config_data=None, max_build=200):
        if config_data:
            config_data = {**config_data, **build.params_id.config_data}
        else:
            config_data = build.params_id.config_data
        count = 0
        config_ids = config_data.get('create_config_ids', self.create_config_ids)

        child_data_list = config_data.get('child_data', [{}])
        if not isinstance(child_data_list, list):
            child_data_list = [child_data_list]

        for child_data in child_data_list:
            config_name = child_data.pop('config_name', {})
            description = child_data.pop('description', {})
            for create_config in self.env['runbot.build.config'].browse(child_data.get('config_id', config_ids.ids)):
                child_data_values = {'config_data': {}, **child_data, 'config_id': create_config}
                for _ in range(config_data.get('number_build', self.number_builds)):
                    count += 1
                    if count > max_build:
                        build._logger('Too much build created')
                        build._log('create_build', f'More than {max_build} build created, stopping', level='WARNING')
                        return
                    config_name = config_name or create_config.name
                    child = build._add_child(child_data_values, orphan=self.make_orphan, description=description or config_name)
                    build._log('create_build', 'created with config %s' % config_name, log_type='subbuild', path=str(child.id))

    def _make_python_ctx(self, build):
        return {
            'datetime': tools.safe_eval.datetime,
            'dateutil': tools.safe_eval.dateutil,
            'json': tools.safe_eval.json,
            'b64encode': base64.b64encode,
            'b64decode': base64.b64decode,
            'self': self,
            # 'fields': fields,
            # 'models': models,
            'build': build,
            '_logger': _logger,
            'log_path': build._path('logs', '%s.txt' % self.sanitized_name(build)),
            'glob': glob.glob,
            'Command': Command,
            're': ReProxy,
            'grep': grep,
            'rfind': rfind,
            'json_loads': json.loads,
            'PatchSet': PatchSet,
            'markdown_escape': markdown_escape,
        }

    def _run_python(self, build, force=False):
        eval_ctx = self._make_python_ctx(build)
        eval_ctx['force'] = force
        try:
            safe_eval(self.python_code.strip(), eval_ctx, mode="exec")
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

    def _run_run_odoo(self, build, force=False):
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

        available_options = build._parse_config()

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

        if "--db-template" in available_options:
            icp = self.env['ir.config_parameter']
            db_template = icp.get_param('runbot.runbot_db_template', default='template0')
            cmd += ['--db-template', db_template]

        extra_params = self.extra_params or ''
        if extra_params:
            cmd.extend(shlex.split(extra_params))
        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        if config_env_variables := build.params_id.config_data.get('env_variables', False):
            env_variables += config_env_variables.split(';')

        build_port = build.port
        try:
            self.env['runbot.runbot']._reload_nginx()
        except Exception:
            _logger.exception('An error occured while reloading nginx')
            build._log('', "An error occured while reloading nginx, skipping")
        return dict(cmd=cmd, exposed_ports=[build_port, build_port + 1], ro_volumes=exports, env_variables=env_variables, cpu_limit=None, network_enabled=True)

    def _run_install_odoo(self, build, config_data=None):

        if config_data:
            config_data = {**config_data, **build.params_id.config_data}
        else:
            config_data = build.params_id.config_data
        exports = build._checkout()
        install_module_pattern = config_data.get('install_module_pattern', self.install_modules)
        modules_to_install = build._get_modules_to_test(install_module_pattern)
        mods = ",".join(modules_to_install)
        python_params = []
        py_version = build._get_py_version()
        if self.coverage:
            build.coverage = True
            coverage_extra_params = self._coverage_params(build, modules_to_install)
            python_params = ['-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + coverage_extra_params
        elif self.flamegraph:
            python_params = ['-m', 'flamegraph', '-o', self._perfs_data_path(build)]
        cmd = build._cmd(python_params, py_version, sub_command=self.sub_command, enable_log_db=self.enable_log_db)
        # create db if needed
        db_suffix = config_data.get('db_name') or (build.params_id.dump_db.db_suffix if not self.create_db else False) or self._get_db_name(build)
        db_suffix = re.sub(r'[^a-z0-9\-_]', '_', db_suffix.lower())
        db_name = '%s-%s' % (build.dest, db_suffix)
        if modules_to_install and self.create_db:
            build._local_pg_createdb(db_name)
        cmd += ['-d', db_name]

        # Demo data behavior changed in 18.1 -> demo data became opt-in instead of opt-out
        available_options = build._parse_config()
        # True if build has demo data by default
        demo_installed_by_default = '--with-demo' not in available_options
        demo_mode = config_data.get('demo_mode', self.demo_mode)
        if demo_mode == 'with_demo' and not demo_installed_by_default:
            cmd.append('--with-demo')
        elif demo_mode == 'without_demo' and demo_installed_by_default:
            cmd.append('--without-demo=true')

        # list module to install
        extra_params = build.params_id.extra_params or self.extra_params or ''
        if mods and '-i' not in extra_params:
            cmd += ['-i', mods]
        config_path = build._server("tools/config.py")

        test_enable = config_data.get('test_enable', self.test_enable)
        test_tags = config_data.get('test_tags', self.test_tags)
        enable_auto_tags = config_data.get('enable_auto_tags', self.enable_auto_tags)
        if test_enable:
            cmd.extend(['--test-enable'])

        test_tags_in_extra = '--test-tags' in extra_params

        if (test_enable or test_tags) and "--test-tags" in available_options and not test_tags_in_extra:
            test_tags = [t.strip() for t in (test_tags or '').split(',')]
            if enable_auto_tags and not config_data.get('disable_auto_tags', False):
                if grep(config_path, "[/module][:class]"):
                    auto_tags = self.env['runbot.build.error']._disabling_tags(build)
                    if auto_tags:
                        test_tags += auto_tags

            test_tags = [test_tag for test_tag in test_tags if test_tag]
            if test_tags:
                cmd.extend(['--test-tags', ','.join(test_tags)])
        elif (test_tags_in_extra or self.test_tags) and "--test-tags" not in available_options:
            build._log('test_all', 'Test tags given but not supported')

        if "--screenshots" in available_options:
            cmd.add_config_tuple('screenshots', '/data/build/tests')

        if "--db-template" in available_options:
            icp = self.env['ir.config_parameter']
            db_template = icp.get_param('runbot.runbot_db_template', default='template0')
            cmd.add_config_tuple('db_template', db_template)

        if "--screencasts" in available_options and (self.env['ir.config_parameter'].sudo().get_param('runbot.enable_screencast', False) or config_data.get('screencast', False)):
            cmd.add_config_tuple('screencasts', '/data/build/tests')

        cmd.append('--stop-after-init')  # install job should always finish
        if '--log-level' not in extra_params:
            cmd.append('--log-level=test')
        cmd.append('--max-cron-threads=0')

        if extra_params:
            cmd.extend(shlex.split(extra_params))

        cmd.finals.extend(self._post_install_commands(build, modules_to_install, py_version))  # coverage post, extra-checks, ...

        if config_data.get('export_database', True):
            self._add_zip_generation(build, cmd, db_name)

        if self.flamegraph:
            cmd.finals.append(['flamegraph.pl', '--title', 'Flamegraph %s for build %s' % (self.sanitized_name(build), build.id), self._perfs_data_path(build), '>', self._perfs_data_path(ext='svg')])
            cmd.finals.append(['gzip', '-f', self._perfs_data_path(build)])  # keep data but gz them to save disc space
        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        if config_env_variables := config_data.get('env_variables', False):
            env_variables += config_env_variables.split(';')

        cpu_limit = None
        if config_data.get('cpu_limit'):
            cpu_limit = min(self.cpu_limit, int(config_data['cpu_limit']))

        return dict(cmd=cmd, ro_volumes=exports, cpu_limit=cpu_limit, env_variables=env_variables)

    def _add_zip_generation(self, build, cmd, db_name):
        dump_dir = '/data/build/logs/%s/' % db_name
        sql_dest = '%s/dump.sql' % dump_dir
        filestore_path = '/data/build/datadir/filestore/%s' % db_name
        filestore_dest = '%s/filestore/' % dump_dir
        zip_path = '/data/build/logs/%s.zip' % db_name
        cmd.finals.append(['pg_dump', db_name, '>', sql_dest])
        cmd.finals.append(['cp', '-r', filestore_path, filestore_dest])
        cmd.finals.append(['cd', dump_dir, '&&', 'zip', '-rmq9', zip_path, '*'])
        infos = '{\n    "db_name": "%s",\n    "build_id": %s,\n    "shas": [%s]\n}' % (db_name, build.id, ', '.join(['"%s"' % build_commit.commit_id.dname for build_commit in build.params_id.commit_link_ids]))
        build._write_file('logs/%s/info.json' % db_name, infos)

    def _upgrade_create_childs(self):
        pass

    def _run_configure_upgrade_complement(self, build):
        """
        Parameters:
            - upgrade_dumps_trigger_id:  a configure_upgradestep

        A complement aims to test the exact oposite of an upgrade trigger.
        Ignore configs an categories: only focus on versions.
        """

        base = build.params_id.create_batch_id.bundle_id.base_id
        if not base.to_upgrade_from:
            build._log('_run_configure_upgrade', f'Upgrade from {base.name} is disabled')
            return

        param = build.params_id
        version = param.version_id
        builds_references = param.builds_reference_ids
        builds_references_by_version_id = {b.params_id.version_id.id: b for b in builds_references}
        upgrade_complement_step = build.params_id.trigger_id.upgrade_dumps_trigger_id.upgrade_step_id
        version_domain = build.params_id.trigger_id.upgrade_dumps_trigger_id._get_version_domain()
        valid_targets = build.browse()
        next_versions = version.next_major_version_id | version.next_intermediate_version_ids
        if version_domain:  # filter only on version where trigger is enabled
            next_versions = next_versions.filtered_domain(version_domain)
        if next_versions:
            for next_version in next_versions:
                if version in upgrade_complement_step._get_upgrade_source_versions(next_version):
                    valid_targets |= (builds_references_by_version_id.get(next_version.id) or build.browse())

        filtered_target_builds = build.browse()
        for target in valid_targets:
            base = target.params_id.create_batch_id.bundle_id.base_id
            if base.to_upgrade:
                filtered_target_builds |= target
            else:
                build._log('_run_configure_upgrade', f'Upgrade to {base.name} is disabled')
        valid_targets = filtered_target_builds

        for target in valid_targets:
            build._log('', 'Checking upgrade to [%s](%s)', target.params_id.version_id.name, target.build_url, log_type='markdown')
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

    def _run_configure_upgrade(self, build):
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

        target_builds = build.browse()
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

        # filter target that are not to_upgrade
        filtered_target_builds = build.browse()
        for target_build in target_builds:
            base = target_build.params_id.create_batch_id.bundle_id.base_id
            if base.to_upgrade:
                filtered_target_builds |= target_build
            else:
                build._log('_run_configure_upgrade', f'Upgrade to {base.name} is disabled')
        target_builds = filtered_target_builds

        if target_builds:
            build._log('', 'Testing upgrade targeting %s' % ', '.join(target_builds.mapped('params_id.version_id.name')))
        if not target_builds:
            build._log('_run_configure_upgrade', 'No reference build found with correct target in availables references, skipping. %s' % builds_references.mapped('params_id.version_id.name'))
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

                # filter source that are not to_upgrade_from
                filtered_from_builds = build.browse()
                for from_build in from_builds:
                    base = from_build.params_id.create_batch_id.bundle_id.base_id
                    if base.to_upgrade_from:
                        filtered_from_builds |= from_build
                    else:
                        build._log('_run_configure_upgrade', f'Upgrade from {base.name} is disabled')
                from_builds = filtered_from_builds
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
                            build._log('_run_configure_upgrade', 'No database found for pattern %s' % (upgrade_db.db_pattern), level='ERROR')
                for db in valid_databases:
                    #commit_ids = build.params_id.commit_ids
                    #if commit_ids != target.params_id.commit_ids:
                    #    repo_ids = commit_ids.mapped('repo_id')
                    #    for commit_link in target.params_id.commit_link_ids:
                    #        if commit_link.commit_id.repo_id not in repo_ids:
                    #            additionnal_commit_links |= commit_link
                    #    build._log('', 'Adding sources from build [%s](%s)', target.id, target.build_url, log_type='markdown')

                    child = build._add_child({
                        'upgrade_to_build_id': target.id,
                        'upgrade_from_build_id': source,
                        'dump_db': db.id,
                        'config_id': self.upgrade_config_id,
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

    def _run_test_upgrade(self, build):
        target = build.params_id.upgrade_to_build_id
        commit_ids = build.params_id.commit_ids
        target_commit_ids = target.params_id.commit_ids
        if commit_ids != target_commit_ids:
            target_repo_ids = target_commit_ids.mapped('repo_id')
            for commit in commit_ids:
                if commit.repo_id not in target_repo_ids:
                    target_commit_ids |= commit
            build._log('', 'Adding sources from build [%s](%s)', target.id, target.build_url, log_type='markdown')
        build = build.with_context(defined_commit_ids=target_commit_ids)
        exports = build._checkout()

        db_suffix = build.params_id.config_data.get('db_name') or build.params_id.dump_db.db_suffix
        migrate_db_name = '%s-%s' % (build.dest, db_suffix)  # only ok if restore does not force db_suffix

        migrate_cmd = build._cmd(enable_log_db=self.enable_log_db)
        migrate_cmd += ['-u', 'all']
        migrate_cmd += ['-d', migrate_db_name]
        migrate_cmd += ['--stop-after-init']
        migrate_cmd += ['--max-cron-threads=0']
        upgrade_paths = list(build._get_upgrade_path())
        if upgrade_paths:
            migrate_cmd += ['--upgrade-path', ','.join(upgrade_paths)]

        build._log('run', 'Start migration build %s' % build.dest)

        migrate_cmd.finals.append(['psql', migrate_db_name, '-c', '"SELECT id, name, state FROM ir_module_module WHERE state NOT IN (\'installed\', \'uninstalled\', \'uninstallable\') AND name NOT LIKE \'test_%\' "', '>', '/data/build/logs/modules_states.txt'])

        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        exception_env = self.env['runbot.upgrade.exception']._generate()
        if exception_env:
            env_variables.append(exception_env)
        if config_env_variables := build.params_id.config_data.get('env_variables', False):
            env_variables += config_env_variables.split(';')
        return dict(cmd=migrate_cmd, ro_volumes=exports, env_variables=env_variables, image_tag=target.params_id.dockerfile_id.image_tag)

    def _run_restore(self, build, config_data=None):
        # exports = build._checkout()
        params = build.params_id
        if config_data:
            config_data = {**config_data, **params.config_data}
        else:
            config_data = params.config_data
        dump_db = params.dump_db
        if dump_url := config_data.get('dump_url'):
            zip_name = dump_url.split('/')[-1]
            build._log('_run_restore', f'Restoring db [{zip_name}]({dump_url})', log_type='markdown')
        else:
            reference_build = None
            if restore_build_id := config_data.get('restore_build_id'):
                reference_build = self.env['runbot.build'].browse(int(restore_build_id)).exists()
                if not reference_build:
                    build._log('_run_restore', f'Reference build id {restore_build_id} not found', log_type='markdown', level='ERROR')
                    build._kill(result='ko')
                    return
            elif (dump_trigger_id := config_data.get('dump_trigger_id')):
                dump_trigger = self.env['runbot.trigger'].browse(int(dump_trigger_id))
                if config_data.get('dump_from_current_batch'):
                    reference_batch = build.params_id.create_batch_id
                else:
                    reference_batch = build.params_id.create_batch_id.base_reference_batch_id
                reference_build = reference_batch.slot_ids.filtered(lambda s: s.trigger_id == dump_trigger).mapped('build_id')
            if reference_build:
                dump_suffix = config_data.get('dump_suffix', 'all')
                reference_build = reference_batch.slot_ids.filtered(lambda s: s.trigger_id == dump_trigger).mapped('build_id')
                if not reference_build:
                    build._log('_run_restore', f'No reference build found in batch {reference_batch.id} for trigger {dump_trigger.name}', log_type='markdown', level='ERROR')
                    build._kill(result='ko')
                    return
                if reference_build.local_state not in ('done', 'running'):
                    build._log('_run_restore', f'Reference build [{reference_build.id}]({reference_build.build_url}) is not yet finished, database may not exist', log_type='markdown', level='WARNING')
                dump_db = reference_build.database_ids.filtered(lambda d: d.db_suffix == dump_suffix)
                if not dump_db:
                    build._log('_run_restore', f'No dump with suffix {dump_suffix} found in build [{reference_build.id}]({reference_build.build_url})', log_type='markdown', level='ERROR')
                    build._kill(result='ko')
                    return
            if dump_db:
                download_db_suffix = dump_db.db_suffix
                dump_build = dump_db.build_id
            else:
                download_db_suffix = config_data.get('dump_suffix', self.restore_download_db_suffix or 'all')
                dump_build = build.parent_id
            assert download_db_suffix and dump_build
            download_db_name = '%s-%s' % (dump_build.dest, download_db_suffix)
            zip_name = '%s.zip' % download_db_name
            dump_url = '%s%s' % (dump_build._http_log_url(), zip_name)
            build._log('test-migration', 'Restoring dump [%s](%s) from build [%s](%s)', zip_name, dump_url, dump_build.id, dump_build.build_url, log_type='markdown')
        target_suffix = config_data.get('target_suffix', self.restore_rename_db_suffix or download_db_suffix)
        restore_db_name = '%s-%s' % (build.dest, target_suffix)

        build._local_pg_createdb(restore_db_name)
        cmd = ' && '.join([
            'mkdir /data/build/restore',
            'cd /data/build/restore',
            'wget --retry-on-host-error %s' % dump_url,
            'unzip -q %s' % zip_name,
            'echo "### restoring filestore"',
            'mkdir -p /data/build/datadir/filestore/%s' % restore_db_name,
            'mv filestore/* /data/build/datadir/filestore/%s' % restore_db_name,
            'echo "### restoring db"',
            'psql -q %s < dump.sql' % (restore_db_name),
            'echo "### performing an analyze"',
            'psql -q -d %s -c "ANALYZE;"' % restore_db_name,
            'cd /data/build',
            'echo "### cleaning"',
            'rm -r restore',
            'echo "### listing modules"',
            """psql %s -c "select name from ir_module_module where state = 'installed'" -t -A > /data/build/logs/restore_modules_installed.txt""" % restore_db_name,
            'echo "### restore" "successful"',  # two part string to avoid miss grep
            ])

        return dict(cmd=cmd, network_enabled=True)

    def _reference_builds(self, batch, trigger):
        upgrade_dumps_trigger_id = trigger.upgrade_dumps_trigger_id
        refs_batches = self._reference_batches(batch, trigger)
        refs_builds = refs_batches.mapped('slot_ids').filtered(
            lambda slot: slot.trigger_id == upgrade_dumps_trigger_id
            ).mapped('build_id')
        # should we filter on active? implicit. On match type? on skipped ?
        # is last_"done"_batch enough?
        # TODO active test false and take last done/running build limit 1 -> in case of rebuild
        return refs_builds

    def _is_upgrade_step(self):
        return self.job_type in ('configure_upgrade', 'configure_upgrade_complement')

    def _reference_batches(self, batch, trigger):
        if self.job_type == 'configure_upgrade_complement':
            return self._reference_batches_complement(batch, trigger)
        else:
            return self._reference_batches_upgrade(batch, trigger.upgrade_dumps_trigger_id.category_id.id)

    def _reference_batches_complement(self, batch, trigger):
        bundle = batch.bundle_id
        if not bundle.base_id.to_upgrade_from:
            return self.env['runbot.batch']
        category_id = trigger.upgrade_dumps_trigger_id.category_id.id
        version = bundle.version_id
        next_versions = version.next_major_version_id | version.next_intermediate_version_ids  # TODO filter on trigger version
        target_versions = version.browse()

        upgrade_complement_step = trigger.upgrade_dumps_trigger_id.upgrade_step_id

        if next_versions:
            for next_version in next_versions:
                if bundle.version_id in upgrade_complement_step._get_upgrade_source_versions(next_version):
                    target_versions |= next_version

        base_batch = batch if batch.reference_batch_ids else batch.base_reference_batch_id
        return base_batch.reference_batch_ids.filtered(lambda batch: batch.bundle_id.version_id in target_versions and batch.category_id.id == category_id)

    def _reference_batches_upgrade(self, batch, category_id):
        if not batch.bundle_id.base_id.to_upgrade:
            return self.env['runbot.batch']
        bundle = batch.bundle_id
        target_refs_bundles = self.env['runbot.bundle']
        upgrade_domain = [('to_upgrade_from', '=', True), ('project_id', '=', bundle.project_id.id)]
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
            source_refs_bundles = source_refs_bundles.filtered('to_upgrade_from')

        ref_bundles = target_refs_bundles | source_refs_bundles
        base_batch = batch if batch.reference_batch_ids else batch.base_reference_batch_id
        return base_batch.reference_batch_ids.filtered(lambda batch: batch.bundle_id in ref_bundles and batch.category_id.id == category_id)

    def _log_end(self, build):
        # TODO fixme config data are not the same as the run part in dynamic steps
        job_type = self.job_type
        # in dynamic step, get the real job type
        database_exported = job_type == 'install_odoo'
        config_data = {**build.params_id.config_data}
        if job_type == 'dynamic':
            dynamic_config = build.dynamic_config
            dynamic_active_step_index = build.dynamic_active_step_index
            if dynamic_config and dynamic_config.get("steps", []):
                current_step = dynamic_config["steps"][dynamic_active_step_index]
                job_type = current_step.get('job_type')
                database_exported = current_step.get('export_database', job_type == 'odoo')
                if db_name := current_step.get('db_name'):
                    config_data['db_name'] = db_name
                if job_type == 'odoo':
                    job_type = 'install_odoo'

        if job_type == 'create_build':
            build._logger('Step %s finished in %s' % (self._get_display_name(build), s2human(build.job_time)))
            return

        message = 'Step %s finished in %s'
        args = [self._get_display_name(build), s2human(build.job_time)]
        log_type = 'runbot'
        if database_exported:
            db_suffix = config_data.get('db_name') or (build.params_id.dump_db.db_suffix if not self.create_db else False) or self._get_db_name(build)
            db_suffix = re.sub(r'[^a-z0-9\-_]', '_', db_suffix.lower())
            message += ' [@icon-download](%s%s-%s.zip)'
            args += [build._http_log_url(), build.dest, db_suffix]
            log_type = 'markdown'
        build._log('', message, *args, log_type=log_type)

        if self.coverage:
            xml_url = '%scoverage.xml' % build._http_log_url()
            html_url = 'http://%s/runbot/static/build/%s/coverage/index.html' % (build.host, build.dest)
            message = 'Coverage report: [xml @icon-download](%s), [html @icon-eye](%s)'
            build._log('end_job', message, xml_url, html_url, log_type='markdown')

        if self.flamegraph:
            dat_url = '%sflame_%s.%s' % (build._http_log_url(), self.sanitized_name(build), 'log.gz')
            svg_url = '%sflame_%s.%s' % (build._http_log_url(), self.sanitized_name(build), 'svg')
            message = 'Flamegraph report: [data @icon-download](%s), [svg @icon-eye](%s)'
            build._log('end_job', message, dat_url, svg_url, log_type='markdown')

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

    def _perfs_data_path(self, build, ext='log'):
        return '/data/build/logs/flame_%s.%s' % (self.sanitized_name(build), ext)

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
                    module_path_in_docker = os.sep.join([docker_source_folder, addons_path, module])
                    pattern_to_omit.add('%s/*' % (module_path_in_docker))
        return ['--omit', ','.join(sorted(pattern_to_omit))]

    def _make_results(self, build):
        # TODO fixme config data are not the same as the run part in dynamic steps
        active_job_type = self.job_type
        check_logs = None
        expected_logs = None
        if self.job_type == 'dynamic':
            dynamic_config = build.dynamic_config
            dynamic_active_step_index = build.dynamic_active_step_index
            if not dynamic_config or not (steps := dynamic_config.get("steps", [])):
                build._log('', 'No dynamic config or steps found, skipping', level="WARNING")
                return
            current_step = steps[dynamic_active_step_index]
            if current_step['job_type'] == 'odoo':
                active_job_type = 'install_odoo'
            else:
                active_job_type = current_step['job_type']

            check_logs = current_step.get('check_logs')
            expected_logs = current_step.get('expected_logs')
            if current_step['job_type'] == 'script':
                if check_logs is None:
                    check_logs = ['ERROR', 'WARNING']
                if expected_logs is None:
                    expected_logs = ['scripts executed in']

        log_time = self._get_log_last_write(build)
        if log_time:
            build.job_end = log_time

        if check_logs or expected_logs:
            self._make_custom_result(build, check_logs, expected_logs)
        elif active_job_type == 'python':
            if self.python_result_code and self.python_result_code != PYTHON_DEFAULT:
                self._make_python_results(build)
            elif self.test_enable or self.test_tags:
                self._make_odoo_results(build)
        elif active_job_type == 'install_odoo':
            if self.coverage:
                build.write(self._make_coverage_results(build))
            if not self.sub_command:
                self._make_odoo_results(build)
        elif active_job_type == 'test_upgrade':
            self._make_upgrade_results(build)
        elif active_job_type == 'restore':
            self._make_restore_results(build)

    def _make_python_results(self, build):
        eval_ctx = self._make_python_ctx(build)
        safe_eval(self.python_result_code.strip(), eval_ctx, mode="exec")
        return_value = eval_ctx.get('return_value', {})
        # todo check return_value or write in try except. Example: local result setted to wrong value
        if not isinstance(return_value, dict):
            raise RunbotException('python_result_code must set return_value to a dict values on build')
        build.write(return_value)  # old style support

    def _make_coverage_results(self, build):
        build_values = {}
        build._log('coverage_result', 'Start getting coverage result')
        cov_path = build._path('coverage/index.html')
        if os.path.exists(cov_path):
            with file_open(cov_path, 'r') as f:
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
        build._log('upgrade', 'Getting results for build %s' % build.dest)

        if build.local_result != 'ko':
            checkers = [
                self._check_log,
                self._check_error,
                self._check_module_loaded,
                self._check_module_states,
                self._check_build_ended,
            ]
            if build.local_result != 'warn':
                checkers.append(self._check_warning)
            build.local_result = self._get_checkers_result(build, checkers)

    def _check_module_states(self, build):
        if not build._is_file('logs/modules_states.txt'):
            build._log('', '"logs/modules_states.txt" file not found.', level='ERROR')
            return 'ko'

        content = build._read_file('logs/modules_states.txt') or ''
        if '(0 rows)' not in content:
            build._log('', 'Some modules are not in installed/uninstalled/uninstallable state after migration. \n %s' % content)
            return 'ko'
        return 'ok'

    def _check_log(self, build):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not os.path.isfile(log_path):
            build._log('_make_tests_results', "Log file not found at the end of test job", level="ERROR")
            return 'ko'
        return 'ok'

    def _check_module_loaded(self, build):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not grep(log_path, ".modules.loading: Modules loaded."):
            details = build._get_error_tail_message(log_path)
            build._log('_make_tests_results', f"Modules loaded not found in logs{details}", level="ERROR")
            return 'ko'
        return 'ok'

    def _check_error(self, build, regex=None):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        re_error = regex or r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) .*)$'

        if result := rfind(log_path, re_error):
            build._log('_make_tests_results', 'Error found in logs:\n%s' % '\n'.join(result), level="ERROR")
            return 'ko'

        re_traceback = r'^(?:Traceback \(most recent call last\):)$'
        if result := rfind(log_path, re_traceback):
            # find Traceback, all following indented lines and one last non indented line
            complete_traceback = rfind(log_path, r'^(?:Traceback \(most recent call last\):(?:\n .*)*(?:\n.*)?)')[:10000]
            complete_traceback = complete_traceback or result
            build._log('_make_tests_results', 'Traceback found in logs:\n%s' % '\n'.join(complete_traceback), level="ERROR")
            return 'ko'
        return 'ok'

    def _check_warning(self, build, regex=None):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        regex = regex or _re_warning
        if result := rfind(log_path, regex):
            build._log('_make_tests_results', 'Warning found in logs:\n%s' % '\n'.join(result), level="WARNING")
            return 'warn'
        return 'ok'

    def _check_build_ended(self, build):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not grep(log_path, "Initiating shutdown"):
            details = build._get_error_tail_message(log_path)
            build._log('_make_tests_results', f'No "Initiating shutdown" found in logs.{details}', level="ERROR")
            return 'ko'
        return 'ok'

    def _check_restore_ended(self, build):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not grep(log_path, "### restore successful"):
            build._log('_make_tests_results', 'Restore failed, check text logs for more info', level="ERROR")
            return 'ko'
        return 'ok'

    def _check_expected_log(self, build, expected_log):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not grep(log_path, expected_log):
            details = build._get_error_tail_message(log_path)
            build._log('_make_tests_results', f'No "{expected_log}" found in logs.{details}', level="ERROR")
            return 'ko'
        return 'ok'

    def _get_log_last_write(self, build):
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if os.path.isfile(log_path):
            return time2str(time.localtime(os.path.getmtime(log_path)))

    def _get_checkers_result(self, build, checkers):
        for checker in checkers:
            result = checker(build)
            if result != 'ok':
                return result
        return 'ok'

    def _make_custom_result(self, build, enabled_checkers=None, expected_logs=None):
        build._log('run', 'Getting results for build %s' % build.dest)
        if build.local_result != 'ko':
            checkers = [self._check_log]
            if enabled_checkers is None or "ERROR" in enabled_checkers:
                checkers.append(self._check_error)
            if expected_logs is None:
                expected_logs = [".modules.loading: Modules loaded.", "Initiating shutdown"]
            for expected_log in (expected_logs or []):
                if expected_log:
                    checkers.append(lambda b: self._check_expected_log(b, expected_log))

            if build.local_result != 'warn' and (enabled_checkers is None or "WARNING" in enabled_checkers):
                checkers.append(self._check_warning)

            build.local_result = self._get_checkers_result(build, checkers)

    def _make_odoo_results(self, build):
        build._log('run', 'Getting results for build %s' % build.dest)

        if build.local_result != 'ko':
            checkers = [
                self._check_log,
                self._check_error,
                self._check_module_loaded,
                self._check_build_ended,
            ]
            if build.local_result != 'warn':
                checkers.append(self._check_warning)

            build.local_result = self._get_checkers_result(build, checkers)

    def _make_restore_results(self, build):
        if build.local_result != 'warn':
            checkers = [
                self._check_log,
                self._check_restore_ended,
            ]
            build.local_result = self._get_checkers_result(build, checkers)

    def _make_stats(self, build):
        make_stats = self.make_stats
        current_dynamic_step = self._get_dynamic_step(build) or {}
        if current_dynamic_step and 'make_stats' in current_dynamic_step:
            make_stats = current_dynamic_step['make_stats']
        if not make_stats:  # TODO garbage collect non sticky stat
            return
        build._log('make_stats', 'Getting stats from log file')
        log_path = build._path('logs', '%s.txt' % self.sanitized_name(build))
        if not os.path.exists(log_path):
            build._log('make_stats', 'Log **%s.txt** file not found', self.sanitized_name(build), level='INFO', log_type='markdown')
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
                        'dynamic_step_name': current_dynamic_step.get('name', ''),
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
        return build._modified_files(commit_link_links=commit_link_links)

    def _get_dynamic_step(self, build):
        if self.job_type != 'dynamic':
            return None
        dynamic_config = build.dynamic_config
        if not dynamic_config:
            return None
        steps = dynamic_config.get("steps", [])
        if not steps:
            return None
        if len(steps) <= build.dynamic_active_step_index:
            _logger.error('Invalid dynamic_active_step_index %s, only %s steps defined', build.dynamic_active_step_index, len(steps))
            return None
        current_step = steps[build.dynamic_active_step_index]
        return current_step

    def _run_dynamic(self, build):
        if len(build.ancestors) > 6:
            raise RunbotException('Too many ancestors builds, possible cyclic dynamic build creation')
        if build.parent_id and build.dynamic_config == build.parent_id.dynamic_config:
            raise RunbotException('A child build cannot load the same dynamic config if parent, recursion detected')
        current_step = self._get_dynamic_step(build)
        if not current_step:
            build._log('Dynamic Step', 'No dynamic config or steps found, skipping', level="WARNING")
            return
        if current_step['job_type'] == 'create_build':
            for_each_vars_list = current_step.get('for_each_vars', [{}])
            if 'for_each_module' in current_step:
                modules_vars = []
                for for_each_vars in for_each_vars_list:
                    modules_entry = self._parse_dynamic_entry(current_step['for_each_module'], build, additional_dynamic_vars=for_each_vars)
                    modules = [m.strip() for m in modules_entry.split(',') if m.strip()]
                    for module in modules:
                        module_vars = {**for_each_vars, 'module': module}
                        modules_vars.append(module_vars)
                for_each_vars_list = modules_vars
            parent_vars = {**build.dynamic_config.get('vars', {}), **build.params_id.config_data.get('dynamic_vars', {})}
            child_data_list = []
            for child_index, child in enumerate(current_step.get('children', [])):
                child_vars = child.get('vars', {})
                for for_each_vars in for_each_vars_list:
                    config_name = child.get('name', build.params_id.config_id.name)
                    dynamic_vars = {**parent_vars, **child_vars, **for_each_vars}
                    if 'description' in child:
                        description = self._parse_dynamic_entry(child['description'], build, additional_dynamic_vars=dynamic_vars)
                        # note: we mainly need to provide additional_dynamic_vars because the child is not created yet at this point
                    else:
                        description = config_name
                    child_data = {
                        'config_data': {**build.params_id.config_data.dict, "dynamic_vars": dynamic_vars},
                        'config_id': build.params_id.config_id.id,
                        'dynamic_active_step_index': 0,
                        'dynamic_config_position': f'{build.params_id.dynamic_config_position or ""}/{build.dynamic_active_step_index}.{child_index}',
                        'config_name': config_name,
                        'description': description,
                    }
                    child_data_list.append(child_data)
            return self._run_create_build(
                build,
                {'child_data': child_data_list, 'number_build': current_step.get('number_builds', 1)},
                max_build=min(current_step.get('max_builds', 20), 200),
            )

        if current_step['job_type'] == 'restore':
            config_data = {
                'dump_suffix': self._get_dynamic_db_suffix(current_step),
                'restore_build_id': current_step.get('build_id'),
                'dump_trigger_id': current_step.get('trigger_id'),
                'dump_from_current_batch': current_step.get('use_current_batch'),
                'dump_url': current_step.get('zip_url'),
            }
            return self._run_restore(build, config_data)

        if current_step['job_type'] == 'odoo':
            config_data = {}
            install_modules_pattern = current_step.get('install_default_modules')
            if install_modules_pattern is None:
                install_modules_pattern = current_step.get('install_modules', '')
                if install_modules_pattern.split(',', 1)[0] not in ('*', '-*'):
                    install_modules_pattern = '-*,' + install_modules_pattern
            config_data['install_module_pattern'] = self._parse_dynamic_entry(install_modules_pattern, build)

            if 'test_tags' in current_step:
                config_data['test_tags'] = self._parse_dynamic_entry(current_step.get('test_tags'), build)
            config_data['test_enable'] = bool(current_step.get('test_enable') or current_step.get('test_tags'))

            for key in ('screencast', 'demo_mode', 'enable_auto_tags'):
                if key in current_step:
                    value = current_step[key]
                    config_data[key] = value
            db_suffix = self._get_dynamic_db_suffix(current_step)
            config_data['db_name'] = db_suffix
            return self._run_install_odoo(build, config_data)

        if current_step['job_type'] == 'command':
            exports = build._checkout()
            command_str = current_step.get('command', '')
            db_suffix = self._get_dynamic_db_suffix(current_step)
            db_name = '%s-%s' % (build.dest, db_suffix)
            command = command_str.split(' ')
            values = {
                'db_name': db_name,
                'data_dir': '/data/build/datadir/',
                'addons_path': ",".join(build._get_addons_path()),
                'exports': ",".join(exports.keys()),
                'exports_paths': ",".join(exports.values()),
            }
            command = [shlex.quote(self._parse_dynamic_entry(part, build, values)) for part in command]
            pres = []
            if current_step.get('install_requirements', False):
                pres = build._make_pip_command()
            cmd = Command(pres, command, [])
            if current_step.get('export_database'):
                self._add_zip_generation(build, cmd, db_name)
            cpu_limit = self.cpu_limit
            if current_step.get('cpu_limit'):
                cpu_limit = min(self.cpu_limit, int(current_step['cpu_limit']))

            return dict(cmd=cmd, ro_volumes=exports, cpu_limit=cpu_limit)

        build._log('Dynamic Step', f'Unknown job_type {current_step["job_type"]} in dynamic config', level="ERROR")

    def _get_dynamic_db_suffix(self, step):
        db_suffix = step.get('db_name') or 'all'
        db_suffix = re.sub(r'[^a-z0-9_\-]', '_', db_suffix.lower())
        return db_suffix

    def _parse_dynamic_entry(self, entry, build, additional_dynamic_vars=None):
        """
        transforms a module/test-tags entry dynamically
        """
        dynamic_config = build.dynamic_config

        expression_filters = {
            'filter_all_modules': filter_all_modules,
            'filter_default_modules': filter_default_modules,
            'make_module_test_tags': make_module_test_tags,
            'prepend': prepend_string,
            'append': append_string,
            'modified_modules': keep_modified_modules,
            'modified_modules_or_base': keep_modified_modules_or_base,
        }
        dynamic_vars = {**dynamic_config.get('vars', {}), **build.params_id.config_data.get('dynamic_vars', {}), **(additional_dynamic_vars or {})}

        def parse_expression(match):
            # inspired by jinja but with limited features
            expression = match.group(0)[2:-2]  # remove {{ }}
            parts = expression.split('|')
            value = parts[0]
            if value in dynamic_vars:
                value = dynamic_vars[parts[0]]
            elif value == 'default_modules':
                value = filter_default_modules('', build, dynamic_vars)
            for processor in parts[1:]:
                args = []
                if match := re.match(r'(\w+)\((.+)\)', processor):
                    processor = match.group(1)
                    args = match.group(2).split(',')
                expression_filter = expression_filters.get(processor)
                if not expression_filter:
                    build._log('Dynamic Config', f'Unknown processor {processor} in dynamic config entry {entry}', level="ERROR")
                    return expression
                value = expression_filter(value, build, dynamic_vars, *args)
            return value

        return re.sub(r"\{\{[^}]*\}\}", parse_expression, entry)

    def consume_remaining_tasks(self, build):
        if self.job_type == 'dynamic':
            next_index = build.dynamic_active_step_index + 1
            build.dynamic_active_step_index = next_index
            steps = build.dynamic_config.get('steps', [])
            return next_index < len(steps)
        return False


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

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'sequence' not in vals and vals.get('step_id'):
                vals['sequence'] = self.env['runbot.build.config.step'].browse(vals.get('step_id')).default_sequence
            if self.pool._init:  # do not duplicate entry on install
                existing = self.search([('sequence', '=', vals.get('sequence')), ('config_id', '=', vals.get('config_id')), ('step_id', '=', vals.get('step_id'))])
                if existing:
                    return
        return super().create(vals_list)
