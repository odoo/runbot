import base64
import glob
import logging
import re
import shlex
import time
from ..common import now, grep, time2str, rfind, Commit, s2human, os
from ..container import docker_run, docker_get_gateway_ip, Command
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval, test_python_expr
from odoo.addons.runbot.models.repo import RunbotException

_logger = logging.getLogger(__name__)

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '

PYTHON_DEFAULT = "# type python code here\n\n\n\n\n\n"


class Config(models.Model):
    _name = "runbot.build.config"
    _inherit = "mail.thread"

    name = fields.Char('Config name', required=True, unique=True, track_visibility='onchange', help="Unique name for config please use trigram as postfix for custom configs")
    description = fields.Char('Config description')
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'config_id')
    update_github_state = fields.Boolean('Notify build state to github', default=False, track_visibility='onchange')
    protected = fields.Boolean('Protected', default=False, track_visibility='onchange')
    group = fields.Many2one('runbot.build.config', 'Configuration group', help="Group of config's and config steps")
    group_name = fields.Char(related='group.name')

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
        # remove protection on copy
        copy = super(Config, self).copy()
        copy.sudo().write({'protected': False})
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

    def _check_recustion(self, visited=None):  # todo test
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

    # general info
    name = fields.Char('Step name', required=True, unique=True, track_visibility='onchange', help="Unique name for step please use trigram as postfix for custom step_ids")
    job_type = fields.Selection([
        ('install_odoo', 'Test odoo'),
        ('run_odoo', 'Run odoo'),
        ('python', 'Python code'),
        ('create_build', 'Create build'),
    ], default='install_odoo', required=True, track_visibility='onchange')
    protected = fields.Boolean('Protected', default=False, track_visibility='onchange')
    default_sequence = fields.Integer('Sequence', default=100, track_visibility='onchange')  # or run after? # or in many2many rel?
    step_order_ids = fields.One2many('runbot.build.config.step.order', 'step_id')
    group = fields.Many2one('runbot.build.config', 'Configuration group', help="Group of config's and config steps")
    group_name = fields.Char('Group name', related='group.name')
    # install_odoo
    create_db = fields.Boolean('Create Db', default=True, track_visibility='onchange')  # future
    custom_db_name = fields.Char('Custom Db Name', track_visibility='onchange')  # future
    install_modules = fields.Char('Modules to install', help="List of module patterns to install, use * to install all available modules, prefix the pattern with dash to remove the module.", default='')
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', track_visibility='onchange')
    cpu_limit = fields.Integer('Cpu limit', default=3600, track_visibility='onchange')
    coverage = fields.Boolean('Coverage', default=False, track_visibility='onchange')
    flamegraph = fields.Boolean('Allow Flamegraph', default=False, track_visibility='onchange')
    test_enable = fields.Boolean('Test enable', default=True, track_visibility='onchange')
    test_tags = fields.Char('Test tags', help="comma separated list of test tags", track_visibility='onchange')
    enable_auto_tags = fields.Boolean('Allow auto tag', default=False, track_visibility='onchange')
    extra_params = fields.Char('Extra cmd args', track_visibility='onchange')
    additionnal_env = fields.Char('Extra env', help='Example: foo="bar",bar="foo". Cannot contains \' ', track_visibility='onchange')
    # python
    python_code = fields.Text('Python code', track_visibility='onchange', default=PYTHON_DEFAULT)
    python_result_code = fields.Text('Python code for result', track_visibility='onchange', default=PYTHON_DEFAULT)
    ignore_triggered_result = fields.Boolean('Ignore error triggered in logs', track_visibility='onchange', default=False)
    running_job = fields.Boolean('Job final state is running', default=False, help="Docker won't be killed if checked")
    # create_build
    create_config_ids = fields.Many2many('runbot.build.config', 'runbot_build_config_step_ids_create_config_ids_rel', string='New Build Configs', track_visibility='onchange', index=True)
    number_builds = fields.Integer('Number of build to create', default=1, track_visibility='onchange')
    hide_build = fields.Boolean('Hide created build in frontend', default=True, track_visibility='onchange')
    force_build = fields.Boolean("As a forced rebuild, don't use duplicate detection", default=False, track_visibility='onchange')
    force_host = fields.Boolean('Use same host as parent for children', default=False, track_visibility='onchange')  # future
    make_orphan = fields.Boolean('No effect on the parent result', help='Created build result will not affect parent build result', default=False, track_visibility='onchange')

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

    @api.onchange('number_builds')
    def _onchange_number_builds(self):
        if self.number_builds > 1:
            self.force_build = True
        else:
            self.force_build = False

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

    @api.model
    def create(self, values):
        self._check(values)
        return super(ConfigStep, self).create(values)

    def write(self, values):
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
        build._log('run', 'Starting step %s from config %s' % (self.name, build.config_id.name), level='SEPARATOR')
        return self._run_step(build, log_path)

    def _run_step(self, build, log_path):
        build.log_counter = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_maxlogs', 100)
        if self.job_type == 'run_odoo':
            return self._run_odoo_run(build, log_path)
        if self.job_type == 'install_odoo':
            return self._run_odoo_install(build, log_path)
        elif self.job_type == 'python':
            return self._run_python(build, log_path)
        elif self.job_type == 'create_build':
            return self._create_build(build, log_path)

    def _create_build(self, build, log_path):
        Build = self.env['runbot.build']
        if self.force_build:
            Build = Build.with_context(force_rebuild=True)

        count = 0
        for create_config in self.create_config_ids:
            for _ in range(self.number_builds):
                count += 1
                if count > 200:
                    build._logger('Too much build created')
                    break
                children = Build.create({
                    'dependency_ids': build._copy_dependency_ids(),
                    'config_id': create_config.id,
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
                    'hidden': self.hide_build,
                    'orphan_result': self.make_orphan,
                })
                build._log('create_build', 'created with config %s' % create_config.name, log_type='subbuild', path=str(children.id))

    def make_python_ctx(self, build):
        return {
            'self': self,
            'fields': fields,
            'models': models,
            'build': build,
            'docker_run': docker_run,
            '_logger': _logger,
            'log_path': build._path('logs', '%s.txt' % self.name),
            'glob': glob.glob,
            'Command': Command,
            'Commit': Commit,
            'base64': base64,
            're': re,
            'time': time,
            'grep': grep,
            'rfind': rfind,
        }
    def _run_python(self, build, log_path):  # TODO rework log_path after checking python steps, compute on build
        eval_ctx = self.make_python_ctx(build)
        try:
            safe_eval(self.python_code.strip(), eval_ctx, mode="exec", nocopy=True)
        except RunbotException as e:
            message = e.args[0]
            build._log("run", message, level='ERROR')
            build._kill(result='ko')


    def _is_docker_step(self):
        if not self:
            return False
        self.ensure_one()
        return self.job_type in ('install_odoo', 'run_odoo') or (self.job_type == 'python' and 'docker_run(' in self.python_code)

    def _run_odoo_run(self, build, log_path):
        exports = build._checkout()
        # update job_start AFTER checkout to avoid build being killed too soon if checkout took some time and docker take some time to start
        build.job_start = now()

        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        # run server
        cmd = build._cmd(local_only=False)
        if os.path.exists(build._get_server_commit()._source_path('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "8070"]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        db_name = [step.db_name for step in build.config_id.step_ids() if step.job_type == 'install_odoo'][-1]
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

        docker_name = build._get_docker_name()
        build_path = build._path()
        build_port = build.port
        self.env.cr.commit()  # commit before docker run to be 100% sure that db state is consistent with dockers
        self.invalidate_cache()
        res = docker_run(cmd, log_path, build_path, docker_name, exposed_ports=[build_port, build_port + 1], ro_volumes=exports)
        build.repo_id._reload_nginx()
        return res

    def _run_odoo_install(self, build, log_path):
        exports = build._checkout()
        # update job_start AFTER checkout to avoid build being killed too soon if checkout took some time and docker take some time to start
        build.job_start = now()

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
        cmd = build._cmd(python_params, py_version)
        # create db if needed
        db_name = "%s-%s" % (build.dest, self.db_name)
        if self.create_db:
            build._local_pg_createdb(db_name)
        cmd += ['-d', db_name]
        # list module to install
        extra_params = build.extra_params or self.extra_params or ''
        if mods and '-i' not in extra_params:
            cmd += ['-i', mods]
        config_path = build._server("tools/config.py")
        if self.test_enable:
            if grep(config_path, "test-enable"):
                cmd.extend(['--test-enable'])
            else:
                build._log('test_all', 'Installing modules without testing', level='WARNING')
        test_tags_in_extra = '--test-tags' in extra_params
        if self.test_tags or test_tags_in_extra:
            if grep(config_path, "test-tags"):
                if not test_tags_in_extra:
                    test_tags = self.test_tags.replace(' ', '')
                    if self.enable_auto_tags:
                        auto_tags = self.env['runbot.build.error'].disabling_tags()
                        test_tags = ','.join(test_tags.split(',') + auto_tags)
                    cmd.extend(['--test-tags', test_tags])
            else:
                build._log('test_all', 'Test tags given but not supported')
        elif self.enable_auto_tags and self.test_enable:
            if grep(config_path, "[/module][:class]"):
                auto_tags = self.env['runbot.build.error'].disabling_tags()
                if auto_tags:
                    test_tags = ','.join(auto_tags)
                    cmd.extend(['--test-tags', test_tags])

        if grep(config_path, "--screenshots"):
            cmd.add_config_tuple('screenshots', '/data/build/tests')

        cmd.append('--stop-after-init')  # install job should always finish
        if '--log-level' not in extra_params:
            cmd.append('--log-level=test')
        cmd.append('--max-cron-threads=0')

        if extra_params:
            cmd.extend(shlex.split(extra_params))

        cmd.posts.append(self._post_install_command(build, modules_to_install, py_version))  # coverage post, extra-checks, ...
        dump_dir = '/data/build/logs/%s/' % db_name
        sql_dest = '%s/dump.sql' % dump_dir
        filestore_path = '/data/build/datadir/filestore/%s' % db_name
        filestore_dest = '%s/filestore/' % dump_dir
        zip_path = '/data/build/logs/%s.zip' % db_name
        cmd.finals.append(['pg_dump', db_name, '>', sql_dest])
        cmd.finals.append(['cp', '-r', filestore_path, filestore_dest])
        cmd.finals.append(['cd', dump_dir, '&&', 'zip', '-rmq9', zip_path, '*'])
        infos = '{\n    "db_name": "%s",\n    "build_id": %s,\n    "shas": [%s]\n}' % (db_name, build.id, ', '.join(['"%s"' % commit for commit in build._get_all_commit()]))
        build.write_file('logs/%s/info.json' % db_name, infos)

        if self.flamegraph:
            cmd.finals.append(['flamegraph.pl', '--title', 'Flamegraph %s for build %s' % (self.name, build.id), self._perfs_data_path(), '>', self._perfs_data_path(ext='svg')])
            cmd.finals.append(['gzip', '-f', self._perfs_data_path()])  # keep data but gz them to save disc space
        max_timeout = int(self.env['ir.config_parameter'].get_param('runbot.runbot_timeout', default=10000))
        timeout = min(self.cpu_limit, max_timeout)
        env_variables = self.additionnal_env.split(',') if self.additionnal_env else []
        return docker_run(cmd, log_path, build._path(), build._get_docker_name(), cpu_limit=timeout, ro_volumes=exports, env_variables=env_variables)

    def log_end(self, build):
        if self.job_type == 'create_build':
            build._logger('Step %s finished in %s' % (self.name, s2human(build.job_time)))
            return

        kwargs = dict(message='Step %s finished in %s' % (self.name, s2human(build.job_time)))
        if self.job_type == 'install_odoo':
            kwargs['message'] += ' $$fa-download$$'
            kwargs['path'] = '%s%s-%s.zip' % (build.http_log_url(), build.dest, self.db_name)
            kwargs['log_type'] = 'link'
        build._log('', **kwargs)

        if self.flamegraph:
            link = self._perf_data_url(build, 'log.gz')
            message = 'Flamegraph data: $$fa-download$$'
            build._log('end_job', message, log_type='link', path=link)

            link = self._perf_data_url(build, 'svg')
            message = 'Flamegraph svg: $$fa-download$$'
            build._log('end_job', message, log_type='link', path=link)

    def _modules_to_install(self, build):
        return set(build._get_modules_to_test(modules_patterns=self.install_modules))

    def _post_install_command(self, build, modules_to_install, py_version=None):
        if self.coverage:
            py_version = py_version if py_version is not None else build._get_py_version()
            # prepare coverage result
            cov_path = build._path('coverage')
            os.makedirs(cov_path, exist_ok=True)
            return ['python%s' % py_version, "-m", "coverage", "html", "-d", "/data/build/coverage", "--ignore-errors"]
        return []

    def _perfs_data_path(self, ext='log'):
        return '/data/build/logs/flame_%s.%s' % (self.name, ext)

    def _perf_data_url(self, build, ext='log'):
        return '%sflame_%s.%s' % (build.http_log_url(), self.name, ext)

    def _coverage_params(self, build, modules_to_install):
        pattern_to_omit = set()
        for commit in build._get_all_commit():
            docker_source_folder = build._docker_source_folder(commit)
            for manifest_file in commit.repo.manifest_files.split(','):
                pattern_to_omit.add('*%s' % manifest_file)
            for (addons_path, module, _) in build._get_available_modules(commit):
                if module not in modules_to_install:
                    # we want to omit docker_source_folder/[addons/path/]module/*
                    module_path_in_docker = os.path.join(docker_source_folder, addons_path, module)
                    pattern_to_omit.add('%s/*' % (module_path_in_docker))
        return ['--omit', ','.join(pattern_to_omit)]

    def _make_results(self, build):
        build_values = {}
        log_time = self._get_log_last_write(build)
        if log_time:
            build_values['job_end'] = log_time
        if self.job_type == 'python' and self.python_result_code and self.python_result_code != PYTHON_DEFAULT:
            build_values.update(self._make_python_results(build))
        elif self.job_type in ['install_odoo', 'python']:
            if self.coverage:
                build_values.update(self._make_coverage_results(build))
            if self.test_enable or self.test_tags:
                build_values.update(self._make_tests_results(build))
        return build_values

    def _make_python_results(self, build):
        eval_ctx = self.make_python_ctx(build)
        safe_eval(self.python_result_code.strip(), eval_ctx, mode="exec", nocopy=True)
        return_value = eval_ctx.get('return_value')
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
                self._check_warning,
                self._check_build_ended
            ]
            local_result = self._get_checkers_result(build, checkers)
            build_values['local_result'] = build._get_worst_result([build.local_result, local_result])
        return build_values

    def _step_state(self):
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
        if 'sequence' not in values and values.get('step_id'):
            values['sequence'] = self.env['runbot.build.config.step'].browse(values.get('step_id')).default_sequence
        if self.pool._init:  # do not duplicate entry on install
            existing = self.search([('sequence', '=', values.get('sequence')), ('config_id', '=', values.get('config_id')), ('step_id', '=', values.get('step_id'))])
            if existing:
                return
        return super(ConfigStepOrder, self).create(values)
