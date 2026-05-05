import base64
import fnmatch
import glob
import json
import logging
import re
import shlex
import time

import psutil
import requests
from unidiff import VERSION, PatchSet, patch

from odoo import api, fields, models, tools
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import _SAFE_OPCODES, safe_eval, test_python_expr, to_opcodes

from ..common import (
    ReProxy,
    RunbotException,
    TestTagsParser,
    grep,
    markdown_escape,
    now,
    os,
    rfind,
    s2human,
    time2str,
)
from ..container import Command, docker_get_gateway_ip

# There is an issue in unidiff 0.7.3 fixed in 0.7.4
# https://github.com/matiasb/python-unidiff/commit/a3faffc54e5aacaee3ded4565c534482d5cc3465
# Since the unidiff packaged version in noble is 0.7.3
# patching it looks like the easiest solution

if VERSION == '0.7.3':
    patch.RE_DIFF_GIT_DELETED_FILE = re.compile(r'^deleted file mode \d+$')
    patch.RE_DIFF_GIT_NEW_FILE = re.compile(r'^new file mode \d+$')

# adding some additionnal optcode to safe_eval. This is not 100% needed and won't be done in standard but will help
# to simplify some python step by wraping the content in a function to allow return statement and get closer to other
# steps

_SAFE_OPCODES |= set(to_opcodes(['LOAD_DEREF', 'STORE_DEREF', 'LOAD_CLOSURE', 'MAKE_CELL', 'COPY_FREE_VARS']))

_logger = logging.getLogger(__name__)

_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING .*'

PYTHON_DEFAULT = "# type python code here\n\n\n\n\n\n"


def filter_all_modules(selector, build, dynamic_vars):
    if selector.split(',', 1)[0] != '*':
        selector = f'*,{selector}'
    return filter_default_modules(selector, build, dynamic_vars)


def get_dependencies(modules, build, dynamic_vars, depth=None):
    depth = int(depth) if depth else None
    modules = modules.split(',')
    dependant = set(build._get_modules_dependencies(modules, depth)) - set(modules)
    return ','.join(sorted(dependant))


def get_dependant(modules, build, dynamic_vars, depth=None):
    depth = int(depth) if depth else None
    modules = modules.split(',')
    dependant = set(build._get_dependant_modules(modules, depth)) - set(modules)
    return ','.join(sorted(dependant))


def filter_default_modules(selector, build, dynamic_vars):
    modules = build._get_modules_to_test(selector)
    return ','.join(modules)


def select_existing_modules(selector, build, dynamic_vars):
    selector = f'-*,{selector}'
    return filter_default_modules(selector, build, dynamic_vars)


def keep_modified_modules(modules, build, dynamic_vars, *defaults):
    if build.params_id.config_data.get('skip_modified_modules_filter', False):
        return modules
    if defaults:
        defaults = [d[1:-1] if re.match(r'^[\'"].*[\'"]$', d) else d for d in defaults]
    modified_modules = build._modified_modules(defaults=defaults)
    modules = modules.split(',')
    filtered_modules = [module for module in modules if module in modified_modules]
    return ','.join(filtered_modules)


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


def union(modules, build, dynamic_vars, element):
    if re.match(r'^[\'"].*[\'"]$', element):
        element = element[1:-1]
    else:
        element = dynamic_vars.get(element, element)
    element = element.strip()
    modules = set(modules.split(',')) if modules else set()
    new_modules = set(element.split(',')) if element else set()
    return ','.join(sorted(modules | new_modules))


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
            if isinstance(vars, list):
                for item in vars:
                    VARS(item, path)
            else:
                if not isinstance(vars, dict):
                    raise ValidationError(f'{path} ({vars}) should be a dict')
                for key, val in vars.items():
                    TECHNICAL_NAME(key, f'{path}.{key}')
                    STR(val, f'{path}.{key}')

        NAME = str_checker(r'^[\w \-]+$')
        STR = str_checker(r'.*')
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
            'log': OPTIONAL(DYNAMIC_VALUE),
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
            'extra_params': OPTIONAL(DYNAMIC_VALUE),
            'cpu_limit': OPTIONAL(INT),
            'export_database': OPTIONAL(BOOL),
            'make_stats': OPTIONAL(BOOL),
            'log': OPTIONAL(DYNAMIC_VALUE),
        }
        valid_steps['create_build'] = {
            'name': REQUIRED(NAME),
            'job_type': 'create_build',
            'children': REQUIRED(LIST(CONFIG)),
            'for_each_vars': OPTIONAL(LIST(VARS)),
            'for_each_module': OPTIONAL(DYNAMIC_VALUE),
            'max_builds': OPTIONAL(INT),
            'if': OPTIONAL(DYNAMIC_VALUE),
            'log': OPTIONAL(DYNAMIC_VALUE),
        }
        valid_steps['restore'] = {
            'name': REQUIRED(NAME),
            'job_type': 'restore',
            'db_name': REQUIRED(TECHNICAL_NAME),
            'build_id': OPTIONAL(INT),
            'trigger_id': OPTIONAL(INT),
            'use_current_batch': OPTIONAL(BOOL),
            'zip_url': OPTIONAL(STR),
            'log': OPTIONAL(DYNAMIC_VALUE),
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
            'log': OPTIONAL(DYNAMIC_VALUE),
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


TYPES = [
        ('install_odoo', 'Test odoo'),
        ('run_odoo', 'Run odoo'),
        ('python', 'Python code'),
        ('create_build', 'Create build'),
        ('configure_upgrade', 'Configure Upgrade'),
        ('test_upgrade', 'Test Upgrade'),
        ('restore', 'Restore'),
        ('dynamic', 'Dynamic'),
        ('semgrep', 'Semgrep'),
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
    dockerfile_id = fields.Many2one('runbot.dockerfile', string='Dockerfile')
    dockerfile_variant = fields.Char('Docker Variant')
    # install_odoo
    create_db = fields.Boolean('Create Db', default=True, tracking=True)  # future
    custom_db_name = fields.Char('Custom Db Name', tracking=True)  # future
    install_modules = fields.Char('Modules to install', help="List of module patterns to install, use * to install all available modules, prefix the pattern with dash to remove the module.", default='', tracking=True)
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', tracking=True)
    cpu_limit = fields.Integer('Cpu limit', default=3600, tracking=True)
    container_cpus = fields.Integer('Allowed CPUs', help='Allowed container CPUs. Fallback on config parameter if 0.', default=0, tracking=True)
    coverage = fields.Boolean('Coverage', default=False, tracking=True)
    coverage_branch = fields.Boolean('Coverage branch', default=False, tracking=True)
    coverage_concurrency = fields.Boolean('Coverage concurrency', default=False, tracking=True)
    coverage_test_context = fields.Boolean('Coverage test context', default=False, tracking=True)
    coverage_make_report = fields.Boolean('Make coverage report', default=False, tracking=True)
    paths_to_omit = fields.Char('Paths to omit from coverage', tracking=True)
    flamegraph = fields.Boolean('Allow Flamegraph', default=False, tracking=True)
    test_enable = fields.Boolean('Test enable', default=True, tracking=True)
    test_tags = fields.Char('Test tags', help="new line (or comma) separated list of test tags", tracking=True)
    enable_auto_tags = fields.Boolean('Allow auto tag', default=True, tracking=True)
    sub_command = fields.Char('Subcommand', tracking=True)
    extra_params = fields.Char('Extra cmd args', tracking=True)
    additionnal_env = fields.Char('Extra env', help='Example: foo=bar;bar=foo. Cannot contains \' ', tracking=True)
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

    upgrade_matrix_id = fields.Many2one('runbot.upgrade.matrix', 'Upgrade matrix', tracking=True)
    upgrade_current = fields.Boolean('Upgrade current version only', help='Only upgrade from and to current version', default=True, tracking=True)
    skip_current = fields.Boolean('Upgrade other version only', help='Only upgrade from and to other versions (blacklist current version)', default=True, tracking=True)
    # TODO maybe remove this fields in the future, should all work in the same build
    upgrade_from_bellow = fields.Boolean('Upgrade from bellow', help="Standard upgrade behaviour", default=True, tracking=True)
    upgrade_to_above = fields.Boolean('Upgrade to above', help="Will behave as a complement", default=True, tracking=True)
    upgrade_from_base = fields.Boolean('Upgrade from base', help="Allow upgrade from base version to current", default=False, tracking=True)
    allow_similar_build_quick_result = fields.Boolean('Allow similar build quick result', help="Allow to find result on a similar build with the same parameters, and mark the result and state when creating the child build", default=False, tracking=True)

    upgrade_flat = fields.Boolean("Flat", help="Take all decisions in on build")

    upgrade_config_id = fields.Many2one('runbot.build.config', string='Upgrade Config', tracking=True, index=True)
    upgrade_dbs = fields.One2many('runbot.config.step.upgrade.db', 'step_id', tracking=True)

    restore_download_db_suffix = fields.Char('Download db suffix')
    restore_rename_db_suffix = fields.Char('Rename db suffix')

    semgrep_category = fields.Many2one('runbot.checker_category', string='Semgrep Category', tracking=True)
    custom_link = fields.Char('Custom link for semgrep codes', tracking=True)
    disable_nosem = fields.Boolean('Disable nosem', default=False, tracking=True)

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
        def log(*args, **kwargs):
            args = [str(arg) for arg in args]
            build._log(self.name, *args, **kwargs)
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
            'TestTagsParser': TestTagsParser,
            'requests': requests.Session(),
            'log': log,
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
        if self.coverage or config_data.get('coverage'):
            build.coverage = True
            python_params = ['-m', 'coverage', 'run', '--source', '/data/build']
            if config_data.get('coverage_branch', self.coverage_branch):
                python_params += ['--branch']
            if config_data.get('coverage_concurrency', self.coverage_concurrency):
                python_params += ['--concurrency=thread']
            python_params += self._coverage_params(build, config_data)
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

        extra_params = config_data.get('extra_params', build.params_id.extra_params or self.extra_params or '')
        # list module to install
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
            test_tags = [t.strip() for t in TestTagsParser(test_tags or '').filter_specs]
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

        cmd.finals.extend(self._post_install_commands(build, config_data, py_version))  # coverage post, extra-checks, ...

        if config_data.get('export_database', True):
            self._add_zip_generation(build, cmd, db_name)

        if self.flamegraph:
            cmd.finals.append(['flamegraph.pl', '--title', 'Flamegraph %s for build %s' % (self.sanitized_name(build), build.id), self._perfs_data_path(build), '>', self._perfs_data_path(ext='svg')])
            cmd.finals.append(['gzip', '-f', self._perfs_data_path(build)])  # keep data but gz them to save disc space
        env_variables = self.additionnal_env.split(';') if self.additionnal_env else []
        if config_env_variables := config_data.get('env_variables', False):
            env_variables += config_env_variables.split(';')

        if config_data.get('coverage_test_context', self.coverage_test_context):
            env_variables.append("COVERAGE_DYNAMIC_CONTEXT=test_function")

        cpu_limit = None
        if config_data.get('cpu_limit'):
            cpu_limit = min(self.cpu_limit, int(config_data['cpu_limit']))
        if cpu_limit and config_data.get('cpu_limit_factor'):
            cpu_limit = int(cpu_limit * float(config_data['cpu_limit_factor']))
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

    def _run_configure_upgrade(self, build):
        """
        Parameters
            - upgrade_matrix_id
            - upgrade_current
            - skip_current
            - upgrade_from_bellow
            - upgrade_to_above
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
        source_builds_by_target = {}
        template_builds = build._upgrade_builds_references()
        template_builds_by_version_id = {b.params_id.version_id.id: b for b in template_builds}

        target_builds = build.browse()
        only_current = self.upgrade_current
        upgrade_from_bellow = self.upgrade_from_bellow
        upgrade_to_above = self.upgrade_to_above

        def get_reference_builds_for_versions(versions):
            refs = self.env['runbot.build'].browse()
            for version in versions:
                ref_build = template_builds_by_version_id.get(version.id)
                if ref_build:
                    refs |= ref_build
                else:
                    urls = [f'[{build_id}](/runbot/build/{build_id})' for build_id in template_builds.ids]
                    build._log('_run_configure_upgrade', f'No reference build found for version {version.name} in {",".join(urls)}', level='WARNING', log_type='markdown')
            return refs

        if param.upgrade_to_build_id:
            target_builds = param.upgrade_to_build_id
        else:
            valid_target_versions = self.upgrade_matrix_id._get_target_versions()
            if only_current:
                if upgrade_from_bellow or self.upgrade_from_base:
                    if param.version_id in valid_target_versions:
                        target_builds |= build.get_current_batch_template()
                if upgrade_to_above:
                    target_versions = self.upgrade_matrix_id._get_target_versions_from(param.version_id)
                    # for target version, we don't want a template build, but an upgrade one
                    target_builds |= get_reference_builds_for_versions(target_versions)
            else:
                for version in valid_target_versions:
                    if self.skip_current and version == param.version_id:
                        continue
                    if version == param.version_id:
                        target_builds |= build.get_current_batch_template()
                    else:
                        target_builds |= get_reference_builds_for_versions([version])

        if target_builds:
            build._log('', 'Testing upgrade targeting %s' % ', '.join(target_builds.mapped('params_id.version_id.name')))
        if not target_builds:
            build._log('_run_configure_upgrade', 'No reference build found with correct target in availables references, skipping. %s' % template_builds.mapped('params_id.version_id.name'))
            return
        elif len(target_builds) > 1 and not self.upgrade_flat:
            for target_build in target_builds:
                build._add_child(
                    {'upgrade_to_build_id': target_build.id},
                    description="Testing migration to %s" % target_build.params_id.version_id.name,
                )
            return

        for target_build in target_builds:
            if param.upgrade_from_build_id:
                source_builds_by_target[target_build] = param.upgrade_from_build_id
            else:
                target_version = target_build.params_id.version_id
                source_builds = build.browse()
                valid_source_versions = self.upgrade_matrix_id._get_source_versions_to(target_version)
                if only_current:
                    # we expect target_build to be a valid source for "current"
                    if param.version_id in valid_source_versions:
                        if upgrade_to_above:
                            source_builds |= build.get_current_batch_template()
                    if self.upgrade_from_base:
                        source_builds |= get_reference_builds_for_versions(param.version_id)
                    if upgrade_from_bellow and target_build.params_id.version_id == param.version_id:
                        source_builds |= get_reference_builds_for_versions(valid_source_versions)
                else:
                    for version in valid_source_versions:
                        if self.skip_current and version == param.version_id:
                            continue
                        if version == param.version_id:
                            source_builds |= build.get_current_batch_template()
                        else:
                            source_builds |= get_reference_builds_for_versions([version])

                if source_builds:
                    build._log('', 'Defining source version(s) for %s: %s' % (target_version.name, ', '.join(source_builds.mapped('params_id.version_id.name'))))
                if not source_builds:
                    build._log('_run_configure_upgrade', 'No source version found for %s, skipping' % target_version.name, level='WARNING')
                elif not self.upgrade_flat:
                    for source_build in source_builds:
                        source_description = source_build.params_id.version_id.name
                        target_description = target_build.params_id.version_id.name
                        if source_build.create_batch_id == build.create_batch_id:
                            source_description += f'current ({source_description})'
                        if target_build.create_batch_id == build.create_batch_id:
                            target_description += f'current ({target_description})'
                        build._add_child(
                            {'upgrade_to_build_id': target_build.id, 'upgrade_from_build_id': source_build.id},
                            description="Testing migration from %s to %s" % (source_description, target_description)
                        )
                    return
                source_builds_by_target[target_build] = source_builds

        assert not param.dump_db
        # we need to define the correct upgrade commits to use. They are not always the upgrade commits from the build itself
        additional_commits_links = self.env['runbot.commit.link']
        single_version_repos = (build.trigger_id.repo_ids | build.trigger_id.dependency_ids).filtered('single_version')

        # for stable, use the upgrade commits from the corresponding master build
        repo_per_version = {}
        for repo in single_version_repos:
            repo_per_version.setdefault(repo.single_version, []).append(repo)
        for repo_version, repos in repo_per_version.items():
            reference_batch = build.params_id.create_batch_id
            if repo_version != build.params_id.version_id:
                # for stable, use the upgrade commits from the corresponding master batch
                reference_batches = reference_batch.reference_batch_ids or reference_batch.base_reference_batch_id.reference_batch_ids
                reference_batch = reference_batches.filtered(lambda b: b.bundle_id.version_id == repo_version)
                build._log('', f'Using batch [{reference_batch.id}](/runbot/batch/{reference_batch.id}) to select {repo_version.name} upgrade commits', log_type='markdown')
            repo_commit = reference_batch.commit_link_ids.filtered(lambda cl: cl.commit_id.repo_id in repos)
            if not repo_commit:
                build._log('_run_configure_upgrade', f'No commit found for repo {repo.name} in batch {reference_batch.id}', level='ERROR')
            additional_commits_links |= repo_commit
        for target, sources in source_builds_by_target.items():
            if target != build:
                build._log('', f'Using build [{target.id}](/runbot/build/{target.id}) to select {target.version_id.name} commits', log_type='markdown')
            target_commits_link = target.params_id.commit_link_ids.filtered(lambda cl: cl.commit_id.repo_id not in single_version_repos)
            # small note: in master additional_commits_links and target_commits_link both comme from the current batch
            target_commits_link |= additional_commits_links
            for source in sources:
                valid_databases = []
                if not self.upgrade_dbs:  # TODO cleanup
                    valid_databases = source.database_ids
                for upgrade_db in self.upgrade_dbs:
                    config_id = upgrade_db.config_id
                    dump_builds = build.search([('id', 'child_of', source.id), ('params_id.config_id', '=', config_id.id), ('orphan_result', '=', False)])
                    # this search is not optimal
                    if not dump_builds:
                        build._log('_run_configure_upgrade', 'No build found with config %s in %s' % (config_id.name, source.id), level='ERROR')
                    dbs = dump_builds.database_ids.sorted('db_suffix')
                    valid_databases += list(self._filter_upgrade_database(dbs, upgrade_db.db_pattern))
                    if not valid_databases:
                        build._log('_run_configure_upgrade', 'No database found for pattern %s' % (upgrade_db.db_pattern), level='ERROR')

                for db in valid_databases:
                    child = build._add_child({
                        'upgrade_to_build_id': None,
                        'upgrade_from_build_id': source.id,
                        'dump_db': db.id,
                        'config_id': self.upgrade_config_id,
                        'builds_reference_ids': False,  # remove builds_reference_ids since now upgrade_to_build_id and upgrade_from_build_id are set
                        'commit_link_ids': target_commits_link.ids,
                        'version_id': target.params_id.version_id.id,
                        'trigger_id': None,
                        'dockerfile_id': target.params_id.dockerfile_id.id,
                    })
                    source_description = source.params_id.version_id.name
                    target_description = target.params_id.version_id.name
                    if source in build.create_batch_id.slot_ids.build_id:
                        source_description += ' (current)'
                    if target in build.create_batch_id.slot_ids.build_id:
                        target_description += ' (current)'
                    child.description = 'Testing migration from **%s** to **%s** using db %s' % (
                        source_description,
                        target_description,
                        db.name,
                    )

                    if self.allow_similar_build_quick_result:
                        existing_done_build = next((build for build in child.params_id.build_ids.sorted('id') if build.global_state == 'done' and build.global_result == 'ok'), None)
                        if not existing_done_build:
                            existing_done_build = next((build for build in child.params_id.build_ids.sorted('id') if build.global_state == 'done' and build.local_result not in ('skipped', 'killed')), None)
                        if existing_done_build:
                            child._log('', 'A similar [build](%s) has been found, marking as done directly', existing_done_build.build_url, log_type='markdown')
                            child.local_state = 'done'
                            child.local_result = existing_done_build.local_result

    def _filter_upgrade_database(self, dbs, pattern):
        pat_list = pattern.split(',') if pattern else []
        for db in dbs:
            if any(fnmatch.fnmatch(db.db_suffix, pat) for pat in pat_list):
                yield db

    def _run_test_upgrade(self, build):
        target = build.params_id.upgrade_to_build_id  # TODO remove
        target_commit_ids = build_commit_ids = build.params_id.commit_ids
        if target:
            target_commit_ids = target.params_id.commit_ids
            if build_commit_ids != target_commit_ids:
                target_repo_ids = target_commit_ids.mapped('repo_id')
                for commit in build_commit_ids:
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
        return dict(cmd=migrate_cmd, ro_volumes=exports, env_variables=env_variables, image_tag=build.params_id.dockerfile_id.image_tag)

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

        if self.coverage and self.coverage_make_report:
            json_url = f'http://{build.host}/runbot/static/build/{build.dest}/logs/coverage/coverage.json'
            html_url = f'http://{build.host}/runbot/static/build/{build.dest}/logs/coverage/'
            message = 'Coverage report: [json @icon-download](%s), [html @icon-eye](%s)'
            build._log('end_job', message, json_url, html_url, log_type='markdown')

        if self.flamegraph:
            dat_url = '%sflame_%s.%s' % (build._http_log_url(), self.sanitized_name(build), 'log.gz')
            svg_url = '%sflame_%s.%s' % (build._http_log_url(), self.sanitized_name(build), 'svg')
            message = 'Flamegraph report: [data @icon-download](%s), [svg @icon-eye](%s)'
            build._log('end_job', message, dat_url, svg_url, log_type='markdown')

    def _post_install_commands(self, build, config_data, py_version):
        cmds = []
        if config_data.get('coverage_make_report', (self.coverage and self.coverage_make_report)):
            cmds.append(['python%s' % py_version, "-m", "coverage", "html", "-d", "/data/build/logs/coverage", "--ignore-errors"])
            cmds.append(['python%s' % py_version, "-m", "coverage", "json", "-o", "/data/build/logs/coverage.json", "--ignore-errors"])
        if config_data.get('coverage', self.coverage):
            cmds.append(['mv', "/data/build/.coverage", f"/data/build/logs/coverage.{build.id}.{int(time.time())}"])
        return cmds

    def _perfs_data_path(self, build, ext='log'):
        return '/data/build/logs/flame_%s.%s' % (self.sanitized_name(build), ext)

    def _coverage_params(self, build, config_data):
        pattern_to_omit = set()
        if self.paths_to_omit:
            pattern_to_omit |= set(self.paths_to_omit.split(','))
        if config_data.get('paths_to_omit'):
            pattern_to_omit |= set(config_data.get('paths_to_omit').split(','))
        if pattern_to_omit:
            return ['--omit', ','.join(sorted(pattern_to_omit))]
        return []

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
            if not self.sub_command:
                self._make_odoo_results(build)
        elif active_job_type == 'test_upgrade':
            self._make_upgrade_results(build)
        elif active_job_type == 'restore':
            self._make_restore_results(build)
        elif active_job_type == 'semgrep':
            self._make_semgrep_results(build)

    def _make_python_results(self, build):
        eval_ctx = self._make_python_ctx(build)
        safe_eval(self.python_result_code.strip(), eval_ctx, mode="exec")
        return_value = eval_ctx.get('return_value', {})
        # todo check return_value or write in try except. Example: local result setted to wrong value
        if not isinstance(return_value, dict):
            raise RunbotException('python_result_code must set return_value to a dict values on build')
        build.write(return_value)  # old style support

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

        if build.local_result == 'warn':
            return 'warn'  # don't check traceback for build in waning since the traceback could come from a warning message

        re_traceback = r'^(?:Traceback \(most recent call last\):)$'
        if result := rfind(log_path, re_traceback):
            # find Traceback, all following indented lines and one last non indented line
            complete_traceback = rfind(log_path, r'^(?:.+\n)?(?:Traceback \(most recent call last\):(?:\n .*)*(?:\n.*)?)')
            if complete_traceback:
                def is_lower_level(tb):
                    first_line = tb.split('\n')[0]
                    for level in ('_WARNING', '_ERROR', '_CRITICAL'):
                        if level in first_line:
                            return True
                    return False
                result = [tb for tb in complete_traceback if not is_lower_level(tb)]
                if not result:
                    return 'ok'  # all tracebacks were in a lower login level, ignore them
            build._log('_make_tests_results', 'Traceback found in logs:\n%s' % ('\n'.join(result))[:10000], level="ERROR")
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

        config_vars_list = build.dynamic_config.get('vars', {})
        if not isinstance(config_vars_list, list):
            config_vars_list = [config_vars_list]
        raw_vars = {}
        for config_vars in config_vars_list:
            raw_vars.update(config_vars)

        raw_vars.update(build.params_id.config_data.get('dynamic_vars', {}))
        dynamic_vars = {}
        # dynamic_vars can either be raw value like 'account', value to evaluate lazily in anothed dynamic value like 'account->!mail'
        # or dynamic value that we want to evaluate early like '{{*|filter_all_modules|modified_modules}}' (between {{}})
        # this loop will evalute the third category
        # this alows to evaluate only once an expression that could be expensive to use it in multiple dynamic values
        # this also allow to clarify the config by chaining vars definition
        # TODO check ordering
        for key, value in raw_vars.items():
            dynamic_vars[key] = self._parse_dynamic_entry(value, build, dynamic_vars=dynamic_vars)

        current_step = self._get_dynamic_step(build)
        if not current_step:
            build._log('Dynamic Step', 'No dynamic config or steps found, skipping', level="WARNING")
            return
        if current_step.get('log'):
            text = self._parse_dynamic_entry(current_step['log'], build, dynamic_vars=dynamic_vars)
            build._log('_run_dynamic', text)
        if current_step['job_type'] == 'create_build':
            for_each_vars_list = current_step.get('for_each_vars', [{}])
            if 'for_each_module' in current_step:
                modules_vars = []
                for for_each_vars in for_each_vars_list:
                    modules_entry = self._parse_dynamic_entry(current_step['for_each_module'], build, dynamic_vars={**dynamic_vars, **for_each_vars})
                    modules = [m.strip() for m in modules_entry.split(',') if m.strip()]
                    for module in modules:
                        module_vars = {**for_each_vars, 'module': module}
                        modules_vars.append(module_vars)
                for_each_vars_list = modules_vars

            child_data_list = []
            for child_index, child in enumerate(current_step.get('children', [])):
                child_vars = child.get('vars', {})
                for for_each_vars in for_each_vars_list:
                    config_name = child.get('name', build.params_id.config_id.name)
                    raw_dynamic_vars = {**dynamic_vars, **for_each_vars, **child_vars}
                    child_dynamic_vars = {}
                    # evaluate for_each_vars
                    for key, value in raw_dynamic_vars.items():
                        child_dynamic_vars[key] = self._parse_dynamic_entry(value, build, dynamic_vars=child_dynamic_vars)
                    if 'if' in current_step:
                        condition = self._parse_dynamic_entry(current_step['if'], build, dynamic_vars=child_dynamic_vars)
                        if not condition:
                            continue
                    if 'description' in child:
                        description = self._parse_dynamic_entry(child['description'], build, dynamic_vars=child_dynamic_vars)
                        # note: we mainly need to provide additional_dynamic_vars because the child is not created yet at this point
                    else:
                        description = config_name
                    # filter vars not prefixed with _ to simplify child values
                    if child.get('log'):
                        text = self._parse_dynamic_entry(child['log'], build, dynamic_vars=child_dynamic_vars)
                        build._log('_run_dynamic', text)
                    public_child_dynamic_vars = {key: value for key, value in child_dynamic_vars.items() if not key.startswith('_')}
                    child_data = {
                        'config_data': {**build.params_id.config_data.dict, "dynamic_vars": public_child_dynamic_vars},
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
            config_data['install_module_pattern'] = self._parse_dynamic_entry(install_modules_pattern, build, dynamic_vars)

            if 'test_tags' in current_step:
                config_data['test_tags'] = self._parse_dynamic_entry(current_step.get('test_tags'), build, dynamic_vars)
            config_data['test_enable'] = bool(current_step.get('test_enable') or current_step.get('test_tags'))

            if 'extra_params' in current_step:
                config_data['extra_params'] = self._parse_dynamic_entry(current_step.get('extra_params'), build, dynamic_vars)

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
                **dynamic_vars,
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

    def _parse_dynamic_entry(self, entry, build, dynamic_vars):
        """
        transforms a module/test-tags entry dynamically
        """
        expression_filters = {
            'filter_all_modules': filter_all_modules,
            'filter_default_modules': filter_default_modules,
            'make_module_test_tags': make_module_test_tags,
            'select_existing_modules': select_existing_modules,
            'get_dependencies': get_dependencies,
            'get_dependant': get_dependant,
            'prepend': prepend_string,
            'append': append_string,
            'modified_modules': keep_modified_modules,
            'union': union,
        }
        dynamic_vars = dynamic_vars or {}

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

    def _run_semgrep(self, build):
        if not self._check_limits(build):
            return

        rules = self.env['runbot.semgrep_rule'].search([
            ("category_id", '=', self.semgrep_category.id),
            '|', ("min_version_number", '=', False), ("min_version_number", "<=", build.params_id.version_id.number),
            '|', ('max_version_number', '=', False), ('max_version_number', '>', build.params_id.version_id.number),
        ])
        if not rules:
            return

        for rule in rules:
            build._write_file(f"rules/{rule.name}.yaml", "rules:\n" + rule.rule_text)

        exports = build._checkout()

        files = []
        targets = []
        for link in build.params_id.commit_link_ids:
            # filtering section for progressive CI (style & security)
            modified = link.commit_id.repo_id._git([
                'diff',
                '%s..%s' % (link.merge_base_commit_id.name, link.commit_id.name),
                '--',
                '*.py',
                '*.js',
            ])
            for patched_file in PatchSet(modified.splitlines(keepends=True)):
                target = patched_file.target_file.removeprefix('b/')
                if target.startswith(('setup/',)):
                    continue
                target = link.commit_id.repo_id.name + '/' + target

                before = len(targets)
                targets.extend(
                    f"{target}:{line.target_line_no}"
                    for hunk in patched_file
                    for line in hunk
                    if line.is_added
                )
                # only look at file if it has additions
                if len(targets) > before:
                    files.append(target)

        if not files:
            build._log("", "Nothing to scan.")
            return

        build._log("", f"checking {len(targets)} lines in {len(files)} files")

        # add empty ignore file, otherwise semgrep ignores test directories by default
        build._write_file(".semgrepignore", "")
        build._write_file(f"logs/{self.name}-files_list.txt", "\n".join(files))
        build._write_file("targets", "\n".join(targets))

        cmd = f"semgrep scan {'--disable-nosem' if self.disable_nosem else ''} -c /data/build/rules --json --timeout=0 --verbose $(cat logs/{self.name}-files_list.txt) > /data/build/results.json"

        return {
            "cmd": cmd,
            "container_name": build._get_docker_name(),
            "ro_volumes": exports,
        }

    def _make_semgrep_results(self, build):
        step_result = "ok"
        if build._is_file("targets"):
            targets = set(build._read_file("targets").splitlines(keepends=False))
            f = build._read_file("results.json")
            semgrep_result = json.loads(f) if f else {}
        else:
            targets = set()
            semgrep_result = {}

        repo = {
            link.commit_id.repo_id.name: (link.branch_id.remote_id.base_url, link.commit_id)
            for link in build.params_id.commit_link_ids
        }

        # some of the lints can catch the same issue multiple times on the same line, and semgrep does not dedup
        seen = set()

        # rules results
        for result in semgrep_result.get('results', ()):
            _, _, code = result['check_id'].rpartition('.')
            start = result['start']['line']
            matches = targets & {
                f"{result['path']}:{start}"
                for line in range(result['start']['line'], result['end']['line'] + 1)
            }
            if not matches:
                continue

            if all((target, code) in seen for target in matches):
                continue
            seen.update((target, code) for target in matches)

            repo_name, path = result['path'].split('/', 1)
            filename = f"{path}:{start}"
            repo_base_url, commit = repo[repo_name]
            commit_hash = commit.name

            # FIXME: should be a code block :(
            extra = result['extra']
            # snippet = extra['lines'] #"\n".join(f'{line}' for line in extra['lines'].splitlines(keepends=False))
            file = commit._read_source(path, mode='rb')
            snippet = file[result['start']['offset']:result['end']['offset']].decode()

            codelink = f"{code}: {extra['message']}\n"
            if self.custom_link:
                # message may be sensitive, do not display, show snippet on same line if single line, otherwise block below
                codelink = f"[{code} 🔗]({self.custom_link}#{code}): "
            if '\n' in snippet:
                snippet = '\n' + snippet

            build._log(
                "semgrep",
                f"""\
        [%s](https://%s/blob/%s/%s#L%s-L%s)
        {codelink}`%s`
            """, filename, repo_base_url, commit_hash, path, result['start']['line'], result['end']['line'], snippet,
                level=extra['severity'],
                log_type='markdown',
            )
            if extra['severity'] != 'INFO':
                step_result = "ko"

        # internal semgrep errors
        for err in semgrep_result.get('errors', ()):
            build._log("semgrep", err.get('message') or str(err), log_type='markdown')

        build['local_result'] = build._get_worst_result([build.local_result, step_result])


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
