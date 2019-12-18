# -*- coding: utf-8 -*-
import fnmatch
import glob
import logging
import pwd
import re
import shutil
import subprocess
import time
import datetime
from ..common import dt2time, fqdn, now, grep, uniq_list, local_pgadmin_cursor, s2human, Commit, dest_reg, os
from ..container import docker_build, docker_stop, docker_state, Command
from odoo.addons.runbot.models.repo import RunbotException
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.tools import appdirs
from collections import defaultdict
from subprocess import CalledProcessError

_logger = logging.getLogger(__name__)

result_order = ['ok', 'warn', 'ko', 'skipped', 'killed', 'manually_killed']
state_order = ['pending', 'testing', 'waiting', 'running', 'duplicate', 'done']


def make_selection(array):
    def format(string):
        return (string, string.replace('_', ' ').capitalize())
    return [format(elem) if isinstance(elem, str) else elem for elem in array]


class runbot_build(models.Model):
    _name = "runbot.build"
    _order = 'id desc'
    _rec_name = 'id'

    branch_id = fields.Many2one('runbot.branch', 'Branch', required=True, ondelete='cascade', index=True)
    repo_id = fields.Many2one(related='branch_id.repo_id', readonly=True, store=True)
    name = fields.Char('Revno', required=True)
    host = fields.Char('Host')
    port = fields.Integer('Port')
    dest = fields.Char(compute='_compute_dest', type='char', string='Dest', readonly=1, store=True)
    domain = fields.Char(compute='_compute_domain', type='char', string='URL')
    date = fields.Datetime('Commit date')
    author = fields.Char('Author')
    author_email = fields.Char('Author Email')
    committer = fields.Char('Committer')
    committer_email = fields.Char('Committer Email')
    subject = fields.Text('Subject')
    sequence = fields.Integer('Sequence')
    log_ids = fields.One2many('ir.logging', 'build_id', string='Logs')
    error_log_ids = fields.One2many('ir.logging', 'build_id', domain=[('level', 'in', ['WARNING', 'ERROR', 'CRITICAL'])], string='Error Logs')

    # state machine

    global_state = fields.Selection(make_selection(state_order), string='Status', compute='_compute_global_state', store=True)
    local_state = fields.Selection(make_selection(state_order), string='Build Status', default='pending', required=True, oldname='state', index=True)
    global_result = fields.Selection(make_selection(result_order), string='Result', compute='_compute_global_result', store=True)
    local_result = fields.Selection(make_selection(result_order), string='Build Result', oldname='result')
    triggered_result = fields.Selection(make_selection(result_order), string='Triggered Result')  # triggered by db only

    requested_action = fields.Selection([('wake_up', 'To wake up'), ('deathrow', 'To kill')], string='Action requested', index=True)

    nb_pending = fields.Integer("Number of pending in queue", default=0)
    nb_testing = fields.Integer("Number of test slot use", default=0)
    nb_running = fields.Integer("Number of test slot use", default=0)

    # should we add a stored field for children results?
    active_step = fields.Many2one('runbot.build.config.step', 'Active step')
    job = fields.Char('Active step display name', compute='_compute_job')
    job_start = fields.Datetime('Job start')
    job_end = fields.Datetime('Job end')
    build_start = fields.Datetime('Build start')
    build_end = fields.Datetime('Build end')
    job_time = fields.Integer(compute='_compute_job_time', string='Job time')
    build_time = fields.Integer(compute='_compute_build_time', string='Job time')
    build_age = fields.Integer(compute='_compute_build_age', string='Build age')
    duplicate_id = fields.Many2one('runbot.build', 'Corresponding Build', index=True)
    revdep_build_ids = fields.Many2many('runbot.build', 'runbot_rev_dep_builds',
                                        column1='rev_dep_id', column2='dependent_id',
                                        string='Builds that depends on this build')
    extra_params = fields.Char('Extra cmd args')
    coverage = fields.Boolean('Code coverage was computed for this build')
    coverage_result = fields.Float('Coverage result', digits=(5, 2))
    build_type = fields.Selection([('scheduled', 'This build was automatically scheduled'),
                                   ('rebuild', 'This build is a rebuild'),
                                   ('normal', 'normal build'),
                                   ('indirect', 'Automatic rebuild'),
                                   ],
                                  default='normal',
                                  string='Build type')
    parent_id = fields.Many2one('runbot.build', 'Parent Build', index=True)
    # should we add a has children stored boolean?
    hidden = fields.Boolean("Don't show build on main page", default=False)  # index?
    children_ids = fields.One2many('runbot.build', 'parent_id')
    dependency_ids = fields.One2many('runbot.build.dependency', 'build_id')

    config_id = fields.Many2one('runbot.build.config', 'Run Config', required=True, default=lambda self: self.env.ref('runbot.runbot_build_config_default', raise_if_not_found=False))
    real_build = fields.Many2one('runbot.build', 'Real Build', help="duplicate_id or self", compute='_compute_real_build')
    log_list = fields.Char('Comma separted list of step_ids names with logs', compute="_compute_log_list", store=True)
    orphan_result = fields.Boolean('No effect on the parent result', default=False)

    commit_path_mode = fields.Selection([('rep_sha', 'repo name + sha'),
                                         ('soft', 'repo name only'),
                                         ],
                                        default='soft',
                                        string='Source export path mode')
    build_url = fields.Char('Build url', compute='_compute_build_url', store=False)
    build_error_ids = fields.Many2many('runbot.build.error', 'runbot_build_error_ids_runbot_build_rel', string='Errors')
    keep_running = fields.Boolean('Keep running', help='Keep running')
    log_counter = fields.Integer('Log Lines counter', default=100)

    @api.depends('config_id')
    def _compute_log_list(self):  # storing this field because it will be access trhoug repo viewn and keep track of the list at create
        for build in self:
            build.log_list = ','.join({step.name for step in build.config_id.step_ids() if step._has_log()})

    @api.depends('nb_testing', 'nb_pending', 'local_state', 'duplicate_id.global_state')
    def _compute_global_state(self):
        # could we use nb_pending / nb_testing ? not in a compute, but in a update state method
        for record in self:
            if record.duplicate_id:
                record.global_state = record.duplicate_id.global_state
            else:
                waiting_score = record._get_state_score('waiting')
                if record._get_state_score(record.local_state) < waiting_score or record.nb_pending + record.nb_testing == 0:
                    record.global_state = record.local_state
                else:
                    record.global_state = 'waiting'

    def _get_youngest_state(self, states):
        index = min([self._get_state_score(state) for state in states])
        return state_order[index]

    def _get_state_score(self, result):
        return state_order.index(result)

    # random note: need to count hidden in pending and testing build displayed in frontend

    @api.depends('children_ids.global_result', 'local_result', 'duplicate_id.global_result', 'children_ids.orphan_result')
    def _compute_global_result(self):
        for record in self:
            if record.duplicate_id:
                record.global_result = record.duplicate_id.global_result
            elif record.local_result and record._get_result_score(record.local_result) >= record._get_result_score('ko'):
                record.global_result = record.local_result
            else:
                children_ids = [child for child in record.children_ids if not child.orphan_result]
                if children_ids:
                    children_result = record._get_worst_result([child.global_result for child in children_ids], max_res='ko')
                    if record.local_result:
                        record.global_result = record._get_worst_result([record.local_result, children_result])
                    else:
                        record.global_result = children_result
                else:
                    record.global_result = record.local_result

    def _get_worst_result(self, results, max_res=False):
        results = [result for result in results if result]  # filter Falsy values
        index = max([self._get_result_score(result) for result in results]) if results else 0
        if max_res:
            return result_order[min([index, self._get_result_score(max_res)])]
        return result_order[index]

    def _get_result_score(self, result):
        return result_order.index(result)

    def _update_nb_children(self, new_state, old_state=None):
        # could be interresting to update state in batches.
        tracked_count_list = ['pending', 'testing', 'running']
        if (new_state not in tracked_count_list and old_state not in tracked_count_list) or new_state == old_state:
            return

        for record in self:
            values = {}
            if old_state in tracked_count_list:
                values['nb_%s' % old_state] = record['nb_%s' % old_state] - 1
            if new_state in tracked_count_list:
                values['nb_%s' % new_state] = record['nb_%s' % new_state] + 1

            record.write(values)
            if record.parent_id:
                record.parent_id._update_nb_children(new_state, old_state)

    @api.depends('real_build.active_step')
    def _compute_job(self):
        for build in self:
            build.job = build.real_build.active_step.name

    @api.depends('duplicate_id')
    def _compute_real_build(self):
        for build in self:
            build.real_build = build.duplicate_id or build

    def copy(self, values=None):
        raise UserError("Cannot duplicate build!")

    def create(self, vals):
        branch = self.env['runbot.branch'].search([('id', '=', vals.get('branch_id', False))])  # branche 10174?
        if branch.no_build:
            return self.env['runbot.build']
        vals['config_id'] = vals['config_id'] if 'config_id' in vals else branch.config_id.id
        build_id = super(runbot_build, self).create(vals)
        build_id._update_nb_children(build_id.local_state)
        extra_info = {'sequence': build_id.id if not build_id.sequence else build_id.sequence}
        context = self.env.context

        # compute dependencies
        repo = build_id.repo_id
        dep_create_vals = []
        nb_deps = len(repo.dependency_ids)
        params = build_id._get_params()
        build_id._log('create', 'Build created') # mainly usefull to log creation time
        if not vals.get('dependency_ids'):
            for extra_repo in repo.dependency_ids:
                repo_name = extra_repo.short_name
                last_commit = params['dep'][repo_name]  # not name
                if last_commit:
                    match_type = 'params'
                    build_closest_branch = False
                    message = 'Dependency for repo %s defined in commit message' % (repo_name)
                else:
                    (build_closest_branch, match_type) = build_id.branch_id._get_closest_branch(extra_repo.id)
                    closest_name = build_closest_branch.name
                    closest_branch_repo = build_closest_branch.repo_id
                    last_commit = closest_branch_repo._git_rev_parse(closest_name)
                    message = 'Dependency for repo %s defined from closest branch %s' % (repo_name, closest_name)
                try:
                    commit_oneline = extra_repo._git(['show', '--pretty="%H -- %s"', '-s', last_commit]).strip()
                except CalledProcessError:
                    commit_oneline = 'Commit %s not found on build creation' % last_commit
                    # possible that oneline fail if given from commit message. Do it on build? or keep this information

                build_id._log('create', '%s: %s' % (message, commit_oneline))

                dep_create_vals.append({
                    'build_id': build_id.id,
                    'dependecy_repo_id': extra_repo.id,
                    'closest_branch_id': build_closest_branch and build_closest_branch.id,
                    'dependency_hash': last_commit,
                    'match_type': match_type,
                })

        for dep_vals in dep_create_vals:
            self.env['runbot.build.dependency'].sudo().create(dep_vals)

        if not context.get('force_rebuild'):  # not vals.get('build_type') == rebuild': could be enough, but some cron on runbot are using this ctx key, to do later
            # detect duplicate
            duplicate_id = None
            domain = [
                ('repo_id', 'in', (build_id.repo_id.duplicate_id.id, build_id.repo_id.id)),  # before, was only looking in repo.duplicate_id looks a little better to search in both
                ('id', '!=', build_id.id),
                ('name', '=', build_id.name),
                ('duplicate_id', '=', False),
                # ('build_type', '!=', 'indirect'),  # in case of performance issue, this little fix may improve performance a little but less duplicate will be detected when pushing an empty branch on repo with duplicates
                '|', ('local_result', '=', False), ('local_result', '!=', 'skipped'),  # had to reintroduce False posibility for selections
                ('config_id', '=', build_id.config_id.id),
                ('extra_params', '=', build_id.extra_params),
            ]
            candidates = self.search(domain)
            if candidates and nb_deps:
                # check that all depedencies are matching.

                # Note: We avoid to compare closest_branch_id, because the same hash could be found on
                # 2 different branches (pr + branch).
                # But we may want to ensure that the hash is comming from the right repo, we dont want to compare community
                # hash with enterprise hash.
                # this is unlikely to happen so branch comparaison is disabled
                self.env.cr.execute("""
                    SELECT DUPLIDEPS.build_id
                    FROM runbot_build_dependency as DUPLIDEPS
                    JOIN runbot_build_dependency as BUILDDEPS
                    ON BUILDDEPS.dependency_hash = DUPLIDEPS.dependency_hash
                    AND BUILDDEPS.build_id = %s
                    AND DUPLIDEPS.build_id in %s
                    GROUP BY DUPLIDEPS.build_id
                    HAVING COUNT(DUPLIDEPS.*) = %s
                    ORDER BY DUPLIDEPS.build_id  -- remove this in case of performance issue, not so usefull
                    LIMIT 1
                """, (build_id.id, tuple(candidates.ids), nb_deps))
                filtered_candidates_ids = self.env.cr.fetchall()

                if filtered_candidates_ids:
                    duplicate_id = filtered_candidates_ids[0]
            else:
                duplicate_id = candidates[0].id if candidates else False

            if duplicate_id:
                extra_info.update({'local_state': 'duplicate', 'duplicate_id': duplicate_id})
                # maybe update duplicate priority if needed

            docker_source_folders = set()
            for commit in build_id._get_all_commit():
                docker_source_folder = build_id._docker_source_folder(commit)
                if docker_source_folder in docker_source_folders:
                    extra_info['commit_path_mode'] = 'rep_sha'
                    continue
                docker_source_folders.add(docker_source_folder)

        build_id.write(extra_info)
        if build_id.local_state == 'duplicate' and build_id.duplicate_id.global_state in ('running', 'done'):
            build_id._github_status()
        return build_id

    def write(self, values):
        # some validation to ensure db consistency
        if 'local_state' in values:
            build_by_old_values = defaultdict(lambda: self.env['runbot.build'])
            for record in self:
                build_by_old_values[record.local_state] += record
            for local_state, builds in build_by_old_values.items():
                builds._update_nb_children(values.get('local_state'), local_state)
        assert 'state' not in values
        local_result = values.get('local_result')
        for build in self:
            assert not local_result or local_result == self._get_worst_result([build.local_result, local_result])  # dont write ok on a warn/error build
        res = super(runbot_build, self).write(values)
        for build in self:
            assert bool(not build.duplicate_id) ^ (build.local_state == 'duplicate')  # don't change duplicate state without removing duplicate id.
        return res

    def update_build_end(self):
        for build in self:
            build.build_end = now()
            if build.parent_id and build.parent_id.local_state in ('running', 'done'):
                    build.parent_id.update_build_end()

    @api.depends('name', 'branch_id.name')
    def _compute_dest(self):
        for build in self:
            if build.name:
                nickname = build.branch_id.name.split('/')[2]
                nickname = re.sub(r'"|\'|~|\:', '', nickname)
                nickname = re.sub(r'_|/|\.', '-', nickname)
                build.dest = ("%05d-%s-%s" % (build.id, nickname[:32], build.name[:6])).lower()

    @api.depends('repo_id', 'port', 'dest', 'host', 'duplicate_id.domain')
    def _compute_domain(self):
        domain = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_domain', fqdn())
        for build in self:
            if build.duplicate_id:
                build.domain = build.duplicate_id.domain
            elif build.repo_id.nginx:
                build.domain = "%s.%s" % (build.dest, build.host)
            else:
                build.domain = "%s:%s" % (domain, build.port)

    def _compute_build_url(self):
        for build in self:
            build.build_url = "/runbot/build/%s" % build.id

    @api.depends('job_start', 'job_end', 'duplicate_id.job_time')
    def _compute_job_time(self):
        """Return the time taken by the tests"""
        for build in self:
            if build.duplicate_id:
                build.job_time = build.duplicate_id.job_time
            elif build.job_end:
                build.job_time = int(dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                build.job_time = int(time.time() - dt2time(build.job_start))

    @api.depends('build_start', 'build_end', 'duplicate_id.build_time')
    def _compute_build_time(self):
        for build in self:
            if build.duplicate_id:
                build.build_time = build.duplicate_id.build_time
            elif build.build_end and build.global_state != 'waiting':
                build.build_time = int(dt2time(build.build_end) - dt2time(build.build_start))
            elif build.build_start:
                build.build_time = int(time.time() - dt2time(build.build_start))

    @api.depends('job_start', 'duplicate_id.build_age')
    def _compute_build_age(self):
        """Return the time between job start and now"""
        for build in self:
            if build.duplicate_id:
                build.build_age = build.duplicate_id.build_age
            elif build.job_start:
                build.build_age = int(time.time() - dt2time(build.build_start))

    def _get_params(self):
        try:
            message = self.repo_id._git(['show', '-s', self.name])
        except CalledProcessError:
            _logger.error('Error getting params for %s', self.name)
            message = ''
        params = defaultdict(lambda: defaultdict(str))
        if message:
            regex = re.compile(r'^[\t ]*Runbot-dependency: ([A-Za-z0-9\-_]+/[A-Za-z0-9\-_]+):([0-9A-Fa-f\-]*) *(#.*)?$', re.M)  # dep:repo:hash #comment
            for result in re.findall(regex, message):
                params['dep'][result[0]] = result[1]
        return params

    def _copy_dependency_ids(self):
        return [(0, 0, {
            'match_type': dep.match_type,
            'closest_branch_id': dep.closest_branch_id and dep.closest_branch_id.id,
            'dependency_hash': dep.dependency_hash,
            'dependecy_repo_id': dep.dependecy_repo_id.id,
        }) for dep in self.dependency_ids]

    def _force(self, message=None, exact=False):
        """Force a rebuild and return a recordset of forced builds"""
        forced_builds = self.env['runbot.build']
        for build in self:
            pending_ids = self.search([('local_state', '=', 'pending')], order='id', limit=1)
            if pending_ids:
                sequence = pending_ids[0].id
            else:
                sequence = self.search([], order='id desc', limit=1)[0].id
            # Force it now
            if build.local_state in ['running', 'done', 'duplicate']:
                values = {
                    'sequence': sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'date': build.date,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'build_type': 'rebuild',
                }
                if exact:
                    values.update({
                        'config_id': build.config_id.id,
                        'extra_params': build.extra_params,
                        'orphan_result': build.orphan_result,
                        'dependency_ids': build._copy_dependency_ids(),
                    })
                    #  if replace: ?
                    if build.parent_id:
                        values.update({
                            'parent_id': build.parent_id.id,  # attach it to parent
                            'hidden': build.hidden,
                        })
                        build.orphan_result = True  # set result of build as orphan

                new_build = build.with_context(force_rebuild=True).create(values)
                forced_builds |= new_build
                user = request.env.user if request else self.env.user
                new_build._log('rebuild', 'Rebuild initiated by %s (%s)%s' % (user.name, 'exact' if exact else 'default', (' :%s' % message) if message else ''))
        return forced_builds

    def _skip(self, reason=None):
        """Mark builds ids as skipped"""
        if reason:
            self._logger('skip %s', reason)
        self.write({'local_state': 'done', 'local_result': 'skipped', 'duplicate_id': False})

    def _build_from_dest(self, dest):
        if dest_reg.match(dest):
            return self.browse(int(dest.split('-')[0]))
        return self.browse()

    def _filter_to_clean(self, dest_list, label):
        icp = self.env['ir.config_parameter']
        max_days_main = int(icp.get_param('runbot.db_gc_days', default=30))
        max_days_child = int(icp.get_param('runbot.db_gc_days_child', default=15))

        dest_by_builds_ids = defaultdict(list)
        ignored = set()
        for dest in dest_list:
            build = self._build_from_dest(dest)
            if build:
                dest_by_builds_ids[build.id].append(dest)
            else:
                ignored.add(dest)
        if ignored:
            _logger.debug('%s (%s) not deleted because not dest format', label, " ".join(list(ignored)))
        builds = self.browse(dest_by_builds_ids)
        existing = builds.exists()
        remaining = (builds - existing)
        if remaining:
            dest_list = [dest for sublist in [dest_by_builds_ids[rem_id] for rem_id in remaining.ids] for dest in sublist]
            _logger.debug('(%s) (%s) not deleted because no corresponding build found' % (label, " ".join(dest_list)))
        for build in existing:
            if fields.Datetime.from_string(build.job_end or build.create_date) + datetime.timedelta(days=(max_days_main if not build.parent_id else max_days_child)) < datetime.datetime.now():
                if build.local_state == 'done':
                    for db in dest_by_builds_ids[build.id]:
                        yield db
                elif build.local_state != 'running':
                    _logger.warning('db (%s) not deleted because state is not done' % " ".join(dest_by_builds_ids[build.id]))

    def _local_cleanup(self, force=False):
        """
        Remove datadir and drop databases of build older than db_gc_days or db_gc_days_child.
        If force is set to True, does the same cleaning based on recordset without checking build age.
        """
        if self.pool._init:
            return
        _logger.debug('Local cleaning')

        _filter = self._filter_to_clean
        additionnal_condition_str = ''

        if force is True:
            def filter_ids(dest_list, label):
                for dest in dest_list:
                    build = self._build_from_dest(dest)
                    if build and build in self:
                        yield dest
                    elif not build:
                        _logger.debug('%s (%s) skipped because not dest format', label, dest)
            _filter = filter_ids
            additionnal_conditions = []
            for _id in self.exists().ids:
                additionnal_conditions.append("datname like '%s-%%'" % _id)
            if additionnal_conditions:
                additionnal_condition_str = 'AND (%s)' % ' OR '.join(additionnal_conditions)

        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("""
                SELECT datname
                    FROM pg_database
                    WHERE pg_get_userbyid(datdba) = current_user
                    %s
            """ % additionnal_condition_str)
            existing_db = [d[0] for d in local_cr.fetchall()]

        for db in _filter(dest_list=existing_db, label='db'):
            self._logger('Removing database')
            self._local_pg_dropdb(db)

        root = self.env['runbot.repo']._root()
        builds_dir = os.path.join(root, 'build')

        if force is True:
            dests = [build.dest for build in self]
        else:
            dests = _filter(dest_list=os.listdir(builds_dir), label='workspace')

        for dest in dests:
            build_dir = os.path.join(builds_dir, dest)
            for f in os.listdir(build_dir):
                path = os.path.join(build_dir, f)
                if os.path.isdir(path) and f != 'logs':
                    shutil.rmtree(path)
                elif f == 'logs':
                    log_path = os.path.join(build_dir, 'logs')
                    for f in os.listdir(log_path):
                        log_file_path = os.path.join(log_path, f)
                        if os.path.isdir(log_file_path):
                            shutil.rmtree(log_file_path)
                        elif not f.endswith('.txt'):
                            os.unlink(log_file_path)

    def _find_port(self):
        # currently used port
        ids = self.search([('local_state', 'not in', ['pending', 'done']), ('host', '=', fqdn())])
        ports = set(i['port'] for i in ids.read(['port']))

        # starting port
        icp = self.env['ir.config_parameter']
        port = int(icp.get_param('runbot.runbot_starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 3
        return port

    def _logger(self, *l):
        l = list(l)
        for build in self:
            l[0] = "%s %s" % (build.dest, l[0])
            _logger.debug(*l)

    def _get_docker_name(self):
        self.ensure_one()
        return '%s_%s' % (self.dest, self.active_step.name)

    def _init_pendings(self, host):
        for build in self:
            if build.local_state != 'pending':
                raise UserError("Build %s is not pending" % build.id)
            if build.host != host.name:
                raise UserError("Build %s does not have correct host" % build.id)
            # allocate port and schedule first job
            values = {
                'port': self._find_port(),
                'job_start': now(),
                'build_start': now(),
                'job_end': False,
            }
            values.update(build._next_job_values())
            build.write(values)
            if not build.active_step:
                build._log('_schedule', 'No job in config, doing nothing')
                continue
            try:
                build._log('_schedule', 'Init build environment with config %s ' % build.config_id.name)
                # notify pending build - avoid confusing users by saying nothing
                build._github_status()
                os.makedirs(build._path('logs'), exist_ok=True)
                build._log('_schedule', 'Building docker image')
                docker_build(build._path('logs', 'docker_build.txt'), build._path())
            except Exception:
                _logger.exception('Failed initiating build %s', build.dest)
                build._log('_schedule', 'Failed initiating build')
                build._kill(result='ko')
                continue
            build._run_job()

    def _process_requested_actions(self):
        for build in self:
            if build.requested_action == 'deathrow':
                result = None
                if build.local_state != 'running' and build.global_result not in ('warn', 'ko'):
                    result = 'manually_killed'
                build._kill(result=result)
                continue

            if build.requested_action == 'wake_up':
                if docker_state(build._get_docker_name(), build._path()) == 'RUNNING':
                    build.write({'requested_action': False, 'local_state': 'running'})
                    build._log('wake_up', 'Waking up failed, docker is already running', level='SEPARATOR')
                elif not os.path.exists(build._path()):
                    build.write({'requested_action': False, 'local_state': 'done'})
                    build._log('wake_up', 'Impossible to wake-up, build dir does not exists anymore', level='SEPARATOR')
                else:
                    try:
                        log_path = build._path('logs', 'wake_up.txt')

                        port = self._find_port()
                        build.write({
                            'job_start': now(),
                            'job_end': False,
                            'active_step': False,
                            'requested_action': False,
                            'local_state': 'running',
                            'port': port,
                        })
                        build._log('wake_up', 'Waking up build', level='SEPARATOR')
                        self.env['runbot.build.config.step']._run_odoo_run(build, log_path)
                        # reload_nginx will be triggered by _run_odoo_run
                    except Exception:
                        _logger.exception('Failed to wake up build %s', build.dest)
                        build._log('_schedule', 'Failed waking up build', level='ERROR')
                        build.write({'requested_action': False, 'local_state': 'done'})
                continue

    def _schedule(self):
        """schedule the build"""
        icp = self.env['ir.config_parameter']
        for build in self:
            if build.local_state not in ['testing', 'running']:
                raise UserError("Build %s is not testing/running: %s" % (build.id, build.local_state))
            if build.local_state == 'testing':
                # failfast in case of docker error (triggered in database)
                if build.triggered_result and not build.active_step.ignore_triggered_result:
                    worst_result = self._get_worst_result([build.triggered_result, build.local_result])
                    if  worst_result != build.local_result:
                        build.local_result = build.triggered_result
                        build._github_status()  # failfast
            # check if current job is finished
            _docker_state = docker_state(build._get_docker_name(), build._path())
            if _docker_state == 'RUNNING':
                timeout = min(build.active_step.cpu_limit, int(icp.get_param('runbot.runbot_timeout', default=10000)))
                if build.local_state != 'running' and build.job_time > timeout:
                    build._log('_schedule', '%s time exceeded (%ss)' % (build.active_step.name if build.active_step else "?", build.job_time))
                    build._kill(result='killed')
                continue
            elif _docker_state == 'UNKNOWN' and (build.local_state == 'running' or build.active_step._is_docker_step()):
                if build.job_time < 5:
                    continue
                elif build.job_time < 60:
                    _logger.debug('container "%s" seems too take a while to start', build._get_docker_name())
                    continue
                else:
                    build._log('_schedule', 'Docker not started after 60 seconds, skipping', level='ERROR')
            # No job running, make result and select nex job
            build_values = {
                'job_end': now(),
            }
            # make result of previous job
            try:
                results = build.active_step._make_results(build)
            except Exception as e:
                if isinstance(e, RunbotException):
                    message = e.args[0]
                else:
                    message = 'An error occured while computing results of %s:\n %s' % (build.job, str(e).replace('\\n', '\n').replace("\\'", "'"))
                    _logger.exception(message)
                build._log('_make_results', message, level='ERROR')
                results = {'local_result': 'ko'}

            build_values.update(results)

            build.active_step.log_end(build)

            build_values.update(build._next_job_values())  # find next active_step or set to done

            ending_build = build.local_state not in ('done', 'running') and build_values.get('local_state') in ('done', 'running')
            if ending_build:
                build.update_build_end()

            build.write(build_values)
            if ending_build:
                build._github_status()
                if not build.local_result:  # Set 'ok' result if no result set (no tests job on build)
                    build.local_result = 'ok'
                    build._logger("No result set, setting ok by default")
            build._run_job()

    def _run_job(self):
        # run job
        for build in self:
            if build.local_state != 'done':
                build._logger('running %s', build.active_step.name)
                os.makedirs(build._path('logs'), exist_ok=True)
                os.makedirs(build._path('datadir'), exist_ok=True)
                try:
                    build.active_step._run(build)  # run should be on build?
                except Exception as e:
                    if isinstance(e, RunbotException):
                        message = e.args[0]
                    else:
                        message = '%s failed running step %s:\n %s' % (build.dest, build.job, str(e).replace('\\n', '\n').replace("\\'", "'"))
                    _logger.exception(message)
                    build._log("run", message, level='ERROR')
                    build._kill(result='ko')

    def _path(self, *l, **kw):
        """Return the repo build path"""
        self.ensure_one()
        build = self
        root = self.env['runbot.repo']._root()
        return os.path.join(root, 'build', build.dest, *l)

    def http_log_url(self):
        return 'http://%s/runbot/static/build/%s/logs/' % (self.host, self.dest)

    def _server(self, *path):
        """Return the absolute path to the direcory containing the server file, adding optional *path"""
        self.ensure_one()
        commit = self._get_server_commit()
        if os.path.exists(commit._source_path('odoo')):
            return commit._source_path('odoo', *path)
        return commit._source_path('openerp', *path)

    def _get_available_modules(self, commit):
        for manifest_file_name in commit.repo.manifest_files.split(','):  # '__manifest__.py' '__openerp__.py'
            for addons_path in (commit.repo.addons_paths or '').split(','):  # '' 'addons' 'odoo/addons'
                sep = os.path.join(addons_path, '*')
                for manifest_path in glob.glob(commit._source_path(sep, manifest_file_name)):
                    module = os.path.basename(os.path.dirname(manifest_path))
                    yield (addons_path, module, manifest_file_name)

    def _docker_source_folder(self, commit):
        # in case some build have commits with the same repo name (ex: foo/bar, foo-ent/bar)
        # it can be usefull to uniquify commit export path using hash
        if self.commit_path_mode == 'rep_sha':
            return '%s-%s' % (commit.repo._get_repo_name_part(), commit.sha[:8])
        else:
            return commit.repo._get_repo_name_part()

    def _checkout(self, commits=None):
        self.ensure_one()  # will raise exception if hash not found, we don't want to fail for all build.
        # checkout branch
        exports = {}
        for commit in commits or self._get_all_commit():
            build_export_path = self._docker_source_folder(commit)
            if build_export_path in exports:
                self._log('_checkout', 'Multiple repo have same export path in build, some source may be missing for %s' % build_export_path, level='ERROR')
                self._kill(result='ko')
            exports[build_export_path] = commit.export()
        return exports

    def _get_repo_available_modules(self, commits=None):
        available_modules = []
        repo_modules = []
        for commit in commits or self._get_all_commit():
            for (addons_path, module, manifest_file_name) in self._get_available_modules(commit):
                if commit.repo == self.repo_id:
                    repo_modules.append(module)
                if module in available_modules:
                    self._log(
                        'Building environment',
                        '%s is a duplicated modules (found in "%s")' % (module, commit._source_path(addons_path, module, manifest_file_name)),
                        level='WARNING'
                    )
                else:
                    available_modules.append(module)
        return repo_modules, available_modules

    def _get_modules_to_test(self, commits=None, modules_patterns=''):
        self.ensure_one()  # will raise exception if hash not found, we don't want to fail for all build.

        # checkout branch
        repo_modules, available_modules = self._get_repo_available_modules(commits=commits)

        patterns_list = []
        for pats in [self.repo_id.modules, self.branch_id.modules, modules_patterns]:
            patterns_list += [p.strip() for p in (pats or '').split(',')]

        if self.repo_id.modules_auto == 'all':
            default_repo_modules = available_modules
        elif self.repo_id.modules_auto == 'repo':
            default_repo_modules = repo_modules
        else:
            default_repo_modules = []

        modules_to_install = set(default_repo_modules)
        for pat in patterns_list:
            if pat.startswith('-'):
                pat = pat.strip('- ')
                modules_to_install -= {mod for mod in modules_to_install if fnmatch.fnmatch(mod, pat)}
            else:
                modules_to_install |= {mod for mod in available_modules if fnmatch.fnmatch(mod, pat)}

        return sorted(modules_to_install)

    def _local_pg_dropdb(self, dbname):
        with local_pgadmin_cursor() as local_cr:
            pid_col = 'pid' if local_cr.connection.server_version >= 90200 else 'procpid'
            query = 'SELECT pg_terminate_backend({}) FROM pg_stat_activity WHERE datname=%s'.format(pid_col)
            local_cr.execute(query, [dbname])
            local_cr.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname) for pn in 'OpenERP Odoo'.split()]
        cmd = ['rm', '-rf'] + paths
        _logger.debug(' '.join(cmd))
        subprocess.call(cmd)

    def _local_pg_createdb(self, dbname):
        self._local_pg_dropdb(dbname)
        _logger.debug("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % dbname)

    def _log(self, func, message, level='INFO', log_type='runbot', path='runbot'):
        self.ensure_one()
        _logger.debug("Build %s %s %s", self.id, func, message)
        self.env['ir.logging'].create({
            'build_id': self.id,
            'level': level,
            'type': log_type,
            'name': 'odoo.runbot',
            'message': message,
            'path': path,
            'func': func,
            'line': '0',
        })

    def _kill(self, result=None):
        host = fqdn()
        for build in self:
            if build.host != host:
                continue
            build._log('kill', 'Kill build %s' % build.dest)
            docker_stop(build._get_docker_name(), build._path())
            v = {'local_state': 'done', 'requested_action': False, 'active_step': False, 'duplicate_id': False, 'job_end': now()}  # what if duplicate? state done?
            if not build.build_end:
                v['build_end'] = now()
            if result:
                v['local_result'] = result
            build.write(v)
            self.env.cr.commit()
            build._github_status()
            self.invalidate_cache()

    def _ask_kill(self):
        # todo xdo, should we kill or skip children builds? it looks like yes, but we need to be carefull if subbuild can be duplicates
        self.ensure_one()
        user = request.env.user if request else self.env.user
        uid = user.id
        build = self
        if build.duplicate_id:
            if build.duplicate_id.branch_id.sticky:
                build._skip()
                build._log('_ask_kill', 'Skipping build %s, requested by %s (user #%s)(duplicate of sticky, kill duplicate)' % (build.dest, user.name, uid))
                return
            build = build.duplicate_id  # if duplicate is not sticky, most likely a pr, kill other build
        if build.local_state == 'pending':
            build._skip()
            build._log('_ask_kill', 'Skipping build %s, requested by %s (user #%s)' % (build.dest, user.name, uid))
        elif build.local_state in ['testing', 'running']:
            build.requested_action = 'deathrow'
            build._log('_ask_kill', 'Killing build %s, requested by %s (user #%s)' % (build.dest, user.name, uid))
        for child in build.children_ids:  # should we filter build that are target of a duplicate_id?
            if not child.duplicate_id:
                child._ask_kill()

    def _wake_up(self):
        build = self.real_build
        if build.local_state != 'done':
            build._log('wake_up', 'Impossibe to wake up, state is not done')
        else:
            build.requested_action = 'wake_up'

    def _get_all_commit(self):
        return [Commit(self.repo_id, self.name)] + [Commit(dep._get_repo(), dep.dependency_hash) for dep in self.dependency_ids]

    def _get_server_commit(self, commits=None):
        """
        returns a Commit() of the first repo containing server files found in commits or in build commits
        the commits param is not used in code base but could be usefull for jobs and crons
        """
        for commit in (commits or self._get_all_commit()):
            if commit.repo.server_files:
                return commit
        raise ValidationError('No repo found with defined server_files')

    def _get_addons_path(self, commits=None):
        for commit in (commits or self._get_all_commit()):
            source_path = self._docker_source_folder(commit)
            for addons_path in (commit.repo.addons_paths or '').split(','):
                if os.path.isdir(commit._source_path(addons_path)):
                    yield os.path.join(source_path, addons_path).strip(os.sep)

    def _get_server_info(self, commit=None):
        server_dir = False
        server = False
        commit = commit or self._get_server_commit()
        for server_file in commit.repo.server_files.split(','):
            if os.path.isfile(commit._source_path(server_file)):
                return (commit, server_file)
        _logger.error('None of %s found in commit, actual commit content:\n %s' % (commit.repo.server_files, os.listdir(commit._source_path())))
        raise RunbotException('No server found in %s' % commit)

    def _cmd(self, python_params=None, py_version=None, local_only=True):
        """Return a list describing the command to start the build
        """
        self.ensure_one()
        build = self
        python_params = python_params or []
        py_version = py_version if py_version is not None else build._get_py_version()
        pres = []
        for commit in self._get_all_commit():
            if os.path.isfile(commit._source_path('requirements.txt')):
                repo_dir = self._docker_source_folder(commit)
                requirement_path = os.path.join(repo_dir, 'requirements.txt')
                pres.append(['sudo', 'pip%s' % py_version, 'install', '-r', '%s' % requirement_path])

        addons_paths = self._get_addons_path()
        (server_commit, server_file) = self._get_server_info()
        server_dir = self._docker_source_folder(server_commit)

        # commandline
        cmd = ['python%s' % py_version] + python_params + [os.path.join(server_dir, server_file), '--addons-path', ",".join(addons_paths)]
        # options
        config_path = build._server("tools/config.py")
        if grep(config_path, "no-xmlrpcs"):  # move that to configs ?
            cmd.append("--no-xmlrpcs")
        if grep(config_path, "no-netrpc"):
            cmd.append("--no-netrpc")

        command = Command(pres, cmd, [])

        # use the username of the runbot host to connect to the databases
        command.add_config_tuple('db_user', '%s' % pwd.getpwuid(os.getuid()).pw_name)

        if local_only:
            if grep(config_path, "--http-interface"):
                command.add_config_tuple("http_interface", "127.0.0.1")
            elif grep(config_path, "--xmlrpc-interface"):
                command.add_config_tuple("xmlrpc_interface", "127.0.0.1")

        if grep(config_path, "log-db"):
            logdb_uri = self.env['ir.config_parameter'].get_param('runbot.runbot_logdb_uri')
            logdb = self.env.cr.dbname
            if logdb_uri and grep(build._server('sql_db.py'), 'allow_uri'):
                logdb = '%s' % logdb_uri
            command.add_config_tuple("log_db", "%s" % logdb)
            if grep(build._server('tools/config.py'), 'log-db-level'):
                command.add_config_tuple("log_db_level", '25')

        if grep(config_path, "data-dir"):
            datadir = build._path('datadir')
            if not os.path.exists(datadir):
                os.mkdir(datadir)
            command.add_config_tuple("data_dir", '/data/build/datadir')

        return command

    def _github_status_notify_all(self, status):
        """Notify each repo with a status"""
        self.ensure_one()
        if self.config_id.update_github_state:
            repos = {b.repo_id for b in self.search([('name', '=', self.name)])}
            for repo in repos:
                _logger.debug("github updating %s status %s to %s in repo %s", status['context'], self.name, status['state'], repo.name)
                try:
                    repo._github('/repos/:owner/:repo/statuses/%s' % self.name, status, ignore_errors=True)
                except Exception:
                    self._log('_github_status_notify_all', 'Status notification failed for "%s" in repo "%s"' % (self.name, repo.name))

    def _github_status(self):
        """Notify github of failed/successful builds"""
        for build in self:
            if build.parent_id:
                build.parent_id._github_status()
            elif build.config_id.update_github_state:
                runbot_domain = self.env['runbot.repo']._domain()
                desc = "runbot build %s" % (build.dest,)

                if build.global_result in ('ko', 'warn'):
                    state = 'failure'
                elif build.global_state == 'testing':
                    state = 'pending'
                elif build.global_state in ('running', 'done'):
                    state = 'error'
                    if build.global_result == 'ok':
                        state = 'success'
                else:
                    _logger.debug("skipping github status for build %s ", build.id)
                    continue
                desc += " (runtime %ss)" % (build.job_time,)

                status = {
                    "state": state,
                    "target_url": "http://%s/runbot/build/%s" % (runbot_domain, build.id),
                    "description": desc,
                    "context": "ci/runbot"
                }
                build._github_status_notify_all(status)

    def _next_job_values(self):
        self.ensure_one()
        step_ids = self.config_id.step_ids()
        if not step_ids:  # no job to do, build is done
            return {'active_step': False, 'local_state': 'done'}

        if not self.active_step and self.local_state != 'pending':
            # means that a step has been run manually without using config
            return {'active_step': False, 'local_state': 'done'}

        next_index = step_ids.index(self.active_step) + 1 if self.active_step else 0
        if next_index >= len(step_ids):  # final job, build is done
            return {'active_step': False, 'local_state': 'done'}

        new_step = step_ids[next_index]  # job to do, state is job_state (testing or running)
        return {'active_step': new_step.id, 'local_state': new_step._step_state()}

    def _get_py_version(self):
        """return the python name to use from build instance"""
        (server_commit, server_file) = self._get_server_info()
        server_path = server_commit._source_path(server_file)
        with open(server_path, 'r') as f:
            if f.readline().strip().endswith('python3'):
                return '3'
        return ''

    def _parse_logs(self):
        """ Parse build logs to classify errors """
        BuildError = self.env['runbot.build.error']
        # only parse logs from builds in error and not already scanned
        builds_to_scan = self.search([('id', 'in', self.ids), ('local_result', '=', 'ko'), ('build_error_ids', '=', False)])
        ir_logs = self.env['ir.logging'].search([('level', '=', 'ERROR'), ('type', '=', 'server'), ('build_id', 'in', builds_to_scan.ids)])
        BuildError._parse_logs(ir_logs)

    def read_file(self, file, mode='r'):
        file_path = self._path(file)
        try:
            with open(file_path, mode) as f:
                return f.read()
        except Exception as e:
            self._log('readfile', 'exception: %s' % e)
            return False

    def write_file(self, file, data, mode='w'):
        file_path = self._path(file)
        file_dir = os.path.split(file_path)[0]
        os.makedirs(file_dir, exist_ok=True)
        try:
            with open(file_path, mode) as f:
                f.write(data)
        except Exception as e:
            self._log('write_file', 'exception: %s' % e)
            return False

    def make_dirs(self, dir_path):
        full_path = self._path(dir_path)
        try:
            os.makedirs(full_path, exist_ok=True)
        except Exception as e:
            self._log('make_dirs', 'exception: %s' % e)
            return False

    def build_type_label(self):
        self.ensure_one()
        return dict(self.fields_get('build_type', 'selection')['build_type']['selection']).get(self.build_type, self.build_type)

    def get_formated_job_time(self):
        return s2human(self.job_time)

    def get_formated_build_time(self):
        return s2human(self.build_time)

    def get_formated_build_age(self):
        return s2human(self.build_age)

    def sorted_revdep_build_ids(self):
        return sorted(self.revdep_build_ids, key=lambda build: build.repo_id.name)
