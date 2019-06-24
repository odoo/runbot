# -*- coding: utf-8 -*-
import glob
import logging
import os
import pwd
import re
import shutil
import subprocess
import time
from ..common import dt2time, fqdn, now, grep, uniq_list, local_pgadmin_cursor, s2human
from ..container import docker_build, docker_stop, docker_is_running
from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import appdirs
from collections import defaultdict
from subprocess import CalledProcessError

_logger = logging.getLogger(__name__)

result_order = ['ok', 'warn', 'ko', 'skipped', 'killed', 'manually_killed']
state_order = ['pending', 'testing', 'waiting', 'running', 'deathrow', 'duplicate', 'done']


def make_selection(array):
    def format(string):
        return (string, string.replace('_', ' ').capitalize())
    return [format(elem) if isinstance(elem, str) else elem for elem in array]


class runbot_build(models.Model):
    _name = "runbot.build"
    _order = 'id desc'

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
    modules = fields.Char("Modules to Install")

    # state machine

    global_state = fields.Selection(make_selection(state_order), string='Status', compute='_compute_global_state', store=True)
    local_state = fields.Selection(make_selection(state_order), string='Build Status', default='pending', required=True, oldname='state', index=True)
    global_result = fields.Selection(make_selection(result_order), string='Result', compute='_compute_global_result', store=True)
    local_result = fields.Selection(make_selection(result_order), string='Build Result', oldname='result')
    triggered_result = fields.Selection(make_selection(result_order), string='Triggered Result')  # triggered by db only

    nb_pending = fields.Integer("Number of pending in queue", default=0)
    nb_testing = fields.Integer("Number of test slot use", default=0)
    nb_running = fields.Integer("Number of test slot use", default=0)

    # should we add a stored field for children results?
    pid = fields.Integer('Pid')
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
    server_match = fields.Selection([('builtin', 'This branch includes Odoo server'),
                                     ('match', 'This branch includes Odoo server'),
                                     ('default', 'No match found - defaults to master')],
                                    string='Server branch matching')
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
                    build_closets_branch = False
                    message = 'Dependency for repo %s defined in commit message' % (repo_name)
                else:
                    (build_closets_branch, match_type) = build_id.branch_id._get_closest_branch(extra_repo.id)
                    closest_name = build_closets_branch.name
                    closest_branch_repo = build_closets_branch.repo_id
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
                    'closest_branch_id': build_closets_branch.id,
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
                    --AND BUILDDEPS.closest_branch_id = DUPLIDEPS.closest_branch_id -- only usefull if we are affraid of hash collision in different branches
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

        build_id.write(extra_info)
        if build_id.local_state == 'duplicate' and build_id.duplicate_id.global_state in ('running', 'done'):  # and not build_id.parent_id:
            build_id._github_status()

        build_id._update_nb_children(build_id.local_state)
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

    def _end_test(self):
        for build in self:
            if build.parent_id and build.global_state in ('running', 'done'):
                global_result = build.global_result
                loglevel = 'OK' if global_result == 'ok' else 'WARNING' if global_result == 'warn' else 'ERROR'
                build.parent_id._log('children_build', 'returned a "%s" result ' % (global_result), level=loglevel, log_type='subbuild', path=self.id)
                if build.parent_id.local_state in ('running', 'done'):
                    build.parent_id._end_test()

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
            elif build.build_end:
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
        message = False
        try:
            message = self.repo_id._git(['show', '-s', self.name])
        except CalledProcessError:
            pass  # todo remove this try catch and make correct patch for _git
        params = defaultdict(lambda: defaultdict(str))
        if message:
            regex = re.compile(r'^[\t ]*dep=([A-Za-z0-9\-_]+/[A-Za-z0-9\-_]+):([0-9A-Fa-f\-]*) *(#.*)?$', re.M)  # dep:repo:hash #comment
            for result in re.findall(regex, message):
                params['dep'][result[0]] = result[1]
        return params

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
            if build.local_state in ['running', 'done', 'duplicate', 'deathrow']:
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
                    'modules': build.modules,
                    'build_type': 'rebuild',
                }
                if exact:
                    values.update({
                        'config_id': build.config_id.id,
                        'extra_params': build.extra_params,
                        'dependency_ids': build.dependency_ids,
                        'server_match': build.server_match,
                        'orphan_result': build.orphan_result,
                    })
                    #if replace: ?
                    if build.parent_id:
                        values.update({
                            'parent_id': build.parent_id.id,  # attach it to parent
                            'hidden': build.hidden,
                        })
                        build.orphan_result = True  # set result of build as orphan

                new_build = build.with_context(force_rebuild=True).create(values)
                forced_builds |= new_build
                user = request.env.user if request else self.env.user
                new_build._log('rebuild', 'Rebuild initiated by %s' % user.name)
                if message:
                    new_build._log('rebuild', new_build)
        return forced_builds

    def _skip(self, reason=None):
        """Mark builds ids as skipped"""
        if reason:
            self._logger('skip %s', reason)
        self.write({'local_state': 'done', 'local_result': 'skipped', 'duplicate_id': False})

    def _local_cleanup(self):
        for build in self:
            # Cleanup the *local* cluster
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname LIKE %s
                """, [build.dest + '%'])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

        # cleanup: find any build older than 7 days.
        root = self.env['runbot.repo']._root()
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        if builds:
            self.env.cr.execute("""
                SELECT dest
                  FROM runbot_build
                 WHERE dest IN %s
                   AND (local_state != 'done' OR job_end > (now() - interval '7 days'))
            """, [tuple(builds)])  # todo xdo not covered by tests
            actives = set(b[0] for b in self.env.cr.fetchall())

            for b in builds:
                path = os.path.join(build_dir, b)
                if b not in actives and os.path.isdir(path) and os.path.isabs(path):
                    shutil.rmtree(path)

        # cleanup old unused databases
        self.env.cr.execute("select id from runbot_build where local_state in ('testing', 'running')")  # todo xdo not coversed by tests
        db_ids = [id[0] for id in self.env.cr.fetchall()]
        if db_ids:
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname ~ '^[0-9]+-.*'
                       AND SUBSTRING(datname, '^([0-9]+)-.*')::int not in %s

                """, [tuple(db_ids)])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

    def _find_port(self):
        # currently used port
        ids = self.search([('local_state', 'not in', ['pending', 'done'])])
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

    def _schedule(self):
        """schedule the build"""
        icp = self.env['ir.config_parameter']
        # For retro-compatibility, keep this parameter in seconds

        for build in self:
            self.env.cr.commit()  # commit between each build to minimise transactionnal errors due to state computations
            if build.local_state == 'deathrow':
                build._kill(result='manually_killed')
                continue

            if build.local_state == 'pending':
                # allocate port and schedule first job
                port = self._find_port()
                values = {
                    'host': fqdn(), # or ip? of false? 
                    'port': port,
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
                    build._checkout()
                    build._log('_schedule', 'Building docker image')
                    docker_build(build._path('logs', 'docker_build.txt'), build._path())
                except Exception:
                    _logger.exception('Failed initiating build %s', build.dest)
                    build._log('_schedule', 'Failed initiating build')
                    build._kill(result='ko')
                    continue
            else:  # testing/running build
                if build.local_state == 'testing':
                    # failfast in case of docker error (triggered in database)
                    if (not build.local_result or build.local_result == 'ok') and build.triggered_result:
                        build.local_result = build.triggered_result
                        build._github_status()  # failfast
                # check if current job is finished
                if docker_is_running(build._get_docker_name()):
                    timeout = min(build.active_step.cpu_limit, int(icp.get_param('runbot.runbot_timeout', default=10000)))
                    if build.local_state != 'running' and build.job_time > timeout:
                        build._log('_schedule', '%s time exceeded (%ss)' % (build.active_step.name if build.active_step else "?", build.job_time))
                        build._kill(result='killed')
                    continue
                # No job running, make result and select nex job
                build_values = {
                    'job_end': now(),
                }
                # make result of previous job
                try:
                    results = build.active_step._make_results(build)
                except:
                    _logger.exception('An error occured while computing results')
                    build._log('_make_results', 'An error occured while computing results', level='ERROR')
                    results = {'local_state': 'ko'}
                build_values.update(results)

                # Non running build in
                notify_end_job = build.active_step.job_type != 'create_build'

                build_values.update(build._next_job_values())  # find next active_step or set to done
                ending_build = build.local_state not in ('done', 'running') and build_values.get('local_state') in ('done', 'running')
                if ending_build:
                    build_values['build_end'] = now()

                step_end_message = 'Step %s finished in %s' % (build.job, s2human(build.job_time))
                build.write(build_values)

                if ending_build:
                    build._github_status()
                    # build._end_test()
                    if not build.local_result:  # Set 'ok' result if no result set (no tests job on build)
                        build.local_result = 'ok'
                        build._logger("No result set, setting ok by default")

                if notify_end_job:
                    build._log('end_job', step_end_message)
                else:
                    build._logger(step_end_message)

            # run job
            pid = None
            if build.local_state != 'done':
                build._logger('running %s', build.active_step.name)
                os.makedirs(build._path('logs'), exist_ok=True)
                os.makedirs(build._path('datadir'), exist_ok=True)
                try:
                    pid = build.active_step._run(build)  # run should be on build?
                    build.write({'pid': pid})  # no really usefull anymore with dockers
                except Exception as e:
                    message = '%s failed running step %s:\n %s' % (build.dest, build.job, str(e).replace('\\n', '\n').replace("\\'", "'"))
                    _logger.exception(message)
                    build._log("run", message, level='ERROR')
                    build._kill(result='ko')
                    continue

            # cleanup only needed if it was not killed
            if build.local_state == 'done':
                build._local_cleanup()

    def _path(self, *l, **kw):
        """Return the repo build path"""
        self.ensure_one()
        build = self
        root = self.env['runbot.repo']._root()
        return os.path.join(root, 'build', build.dest, *l)

    def _server(self, *l, **kw):  # not really build related, specific to odoo version, could be a data
        """Return the build server path"""
        self.ensure_one()
        build = self
        if os.path.exists(build._path('odoo')):
            return build._path('odoo', *l)
        return build._path('openerp', *l)

    def _filter_modules(self, modules, available_modules, explicit_modules):
        blacklist_modules = set(['auth_ldap', 'document_ftp', 'base_gengo',
                                 'website_gengo', 'website_instantclick',
                                 'pad', 'pad_project', 'note_pad',
                                 'pos_cache', 'pos_blackbox_be'])

        mod_filter = lambda m: (
            m in available_modules and
            (m in explicit_modules or (not m.startswith(('hw_', 'theme_', 'l10n_')) and
                                       m not in blacklist_modules))
        )
        return uniq_list(filter(mod_filter, modules))

    def _checkout(self):
        self.ensure_one()  # will raise exception if hash not found, we don't want to fail for all build.
        # starts from scratch
        build = self
        if os.path.isdir(build._path()):
            shutil.rmtree(build._path())

        # runbot log path
        os.makedirs(build._path("logs"), exist_ok=True)
        os.makedirs(build._server('addons'), exist_ok=True)

        # update repo if needed
        if not build.repo_id._hash_exists(build.name):
            build.repo_id._update()

        # checkout branch
        build.branch_id.repo_id._git_export(build.name, build._path())

        has_server = os.path.isfile(build._server('__init__.py'))
        server_match = 'builtin'

        # build complete set of modules to install
        modules_to_move = []
        modules_to_test = ((build.branch_id.modules or '') + ',' +
                            (build.repo_id.modules or ''))
        modules_to_test = list(filter(None, modules_to_test.split(',')))  # ???
        explicit_modules = set(modules_to_test)
        _logger.debug("manual modules_to_test for build %s: %s", build.dest, modules_to_test)

        if not has_server:
            if build.repo_id.modules_auto == 'repo':
                modules_to_test += [
                    os.path.basename(os.path.dirname(a))
                    for a in (glob.glob(build._path('*/__openerp__.py')) +
                                glob.glob(build._path('*/__manifest__.py')))
                ]
                _logger.debug("local modules_to_test for build %s: %s", build.dest, modules_to_test)

            # todo make it backward compatible, or create migration script?
            for build_dependency in build.dependency_ids:
                closest_branch = build_dependency.closest_branch_id
                latest_commit = build_dependency.dependency_hash
                repo = closest_branch.repo_id or build_dependency.repo_id
                closest_name = closest_branch.name or 'no_branch'
                if build_dependency.match_type == 'default':
                    server_match = 'default'
                elif server_match != 'default':
                    server_match = 'match'

                build._log(
                    '_checkout', 'Checkouting %s from %s' % (closest_name, repo.name)
                )

                if not repo._hash_exists(latest_commit):
                    repo._update(force=True)
                if not repo._hash_exists(latest_commit):
                    repo._git(['fetch', 'origin', latest_commit])
                if not repo._hash_exists(latest_commit):
                    build._log('_checkout', "Dependency commit %s in repo %s is unreachable" % (latest_commit, repo.name))
                    raise Exception

                repo._git_export(latest_commit, build._path())

            # Finally mark all addons to move to openerp/addons
            modules_to_move += [
                os.path.dirname(module)
                for module in (glob.glob(build._path('*/__openerp__.py')) +
                                glob.glob(build._path('*/__manifest__.py')))
            ]

        # move all addons to server addons path
        for module in uniq_list(glob.glob(build._path('addons/*')) + modules_to_move):
            basename = os.path.basename(module)
            addon_path = build._server('addons', basename)
            if os.path.exists(addon_path):
                build._log(
                    'Building environment',
                    'You have duplicate modules in your branches "%s"' % basename
                )
                if os.path.islink(addon_path) or os.path.isfile(addon_path):
                    os.remove(addon_path)
                else:
                    shutil.rmtree(addon_path)
            shutil.move(module, build._server('addons'))

        available_modules = [
            os.path.basename(os.path.dirname(a))
            for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                        glob.glob(build._server('addons/*/__manifest__.py')))
        ]
        if build.repo_id.modules_auto == 'all' or (build.repo_id.modules_auto != 'none' and has_server):
            modules_to_test += available_modules

        modules_to_test = self._filter_modules(modules_to_test,
                                                set(available_modules), explicit_modules)
        _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
        build.write({'server_match': server_match,
                        'modules': ','.join(modules_to_test)})

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

    def _reap(self):
        while True:
            try:
                pid, status, rusage = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

    def _kill(self, result=None):
        host = fqdn()
        for build in self:
            if build.host != host:
                continue
            build._log('kill', 'Kill build %s' % build.dest)
            docker_stop(build._get_docker_name())
            v = {'local_state': 'done', 'active_step': False, 'duplicate': False, 'build_end': now()}  # what if duplicate? state done?
            if not build.job_end:
                v['job_end'] = now()
            if result:
                v['local_result'] = result
            build.write(v)
            self.env.cr.commit()
            build._github_status()
            build._local_cleanup()

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
            build.write({'local_state': 'deathrow'})
            build._log('_ask_kill', 'Killing build %s, requested by %s (user #%s)' % (build.dest, user.name, uid))
        for child in build.children_ids:  # should we filter build that are target of a duplicate_id?
            if not build.duplicate_id and build.local_state != 'done':
                child._ask_kill()

    def _cmd(self):  # why not remove build.modules output ?
        """Return a tuple describing the command to start the build
        First part is list with the command and parameters
        Second part is a list of Odoo modules
        """
        self.ensure_one()
        build = self
        bins = [
            'odoo-bin',                 # >= 10.0
            'openerp-server',           # 9.0, 8.0
            'openerp-server.py',        # 7.0
            'bin/openerp-server.py',    # < 7.0
        ]
        for odoo_bin in bins:
            if os.path.isfile(build._path(odoo_bin)):
                break

        # commandline
        cmd = [ os.path.join('/data/build', odoo_bin), ]
        # options
        if grep(build._server("tools/config.py"), "no-xmlrpcs"):  # move that to configs ?
            cmd.append("--no-xmlrpcs") 
        if grep(build._server("tools/config.py"), "no-netrpc"):
            cmd.append("--no-netrpc")
        if grep(build._server("tools/config.py"), "log-db"):
            logdb_uri = self.env['ir.config_parameter'].get_param('runbot.runbot_logdb_uri')
            logdb = self.env.cr.dbname
            if logdb_uri and grep(build._server('sql_db.py'), 'allow_uri'):
                logdb = '%s' % logdb_uri
            cmd += ["--log-db=%s" % logdb]
            if grep(build._server('tools/config.py'), 'log-db-level'):
                cmd += ["--log-db-level", '25']

        if grep(build._server("tools/config.py"), "data-dir"):
            datadir = build._path('datadir')
            if not os.path.exists(datadir):
                os.mkdir(datadir)
            cmd += ["--data-dir", '/data/build/datadir']

        # use the username of the runbot host to connect to the databases
        cmd += ['-r %s' % pwd.getpwuid(os.getuid()).pw_name]

        return cmd, build.modules

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
            if build.config_id.update_github_state:
                runbot_domain = self.env['runbot.repo']._domain()
                desc = "runbot build %s" % (build.dest,)
                if build.local_state == 'testing':
                    state = 'pending'
                elif build.local_state in ('running', 'done'):
                    state = 'error'
                else:
                    continue
                desc += " (runtime %ss)" % (build.job_time,)
                if build.local_result == 'ok':
                    state = 'success'
                if build.local_result in ('ko', 'warn'):
                    state = 'failure'
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

        next_index = step_ids.index(self.active_step) + 1 if self.active_step else 0
        if next_index >= len(step_ids):  # final job, build is done
            return {'active_step': False, 'local_state': 'done'}

        new_step = step_ids[next_index]  # job to do, state is job_state (testing or running)
        return {'active_step': new_step.id, 'local_state': new_step._step_state()}

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
