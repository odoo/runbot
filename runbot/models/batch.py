import time
import logging
import datetime
import subprocess

from odoo import models, fields, api
from ..common import dt2time, s2human_long, pseudo_markdown

_logger = logging.getLogger(__name__)


class Batch(models.Model):
    _name = 'runbot.batch'
    _description = "Bundle batch"

    last_update = fields.Datetime('Last ref update')
    bundle_id = fields.Many2one('runbot.bundle', required=True, index=True, ondelete='cascade')
    commit_link_ids = fields.Many2many('runbot.commit.link')
    commit_ids = fields.Many2many('runbot.commit', compute='_compute_commit_ids')
    slot_ids = fields.One2many('runbot.batch.slot', 'batch_id')
    all_build_ids = fields.Many2many('runbot.build', compute='_compute_all_build_ids', help="Recursive builds")
    state = fields.Selection([('preparing', 'Preparing'), ('ready', 'Ready'), ('done', 'Done'), ('skipped', 'Skipped')])
    hidden = fields.Boolean('Hidden', default=False)
    age = fields.Integer(compute='_compute_age', string='Build age')
    category_id = fields.Many2one('runbot.category', index=True, default=lambda self: self.env.ref('runbot.default_category', raise_if_not_found=False))
    log_ids = fields.One2many('runbot.batch.log', 'batch_id')
    has_warning = fields.Boolean("Has warning")
    base_reference_batch_id = fields.Many2one('runbot.batch')

    @api.depends('slot_ids.build_id')
    def _compute_all_build_ids(self):
        all_builds = self.env['runbot.build'].search([('id', 'child_of', self.slot_ids.build_id.ids)])
        for batch in self:
            batch.all_build_ids = all_builds.filtered_domain([('id', 'child_of', batch.slot_ids.build_id.ids)])

    @api.depends('commit_link_ids')
    def _compute_commit_ids(self):
        for batch in self:
            batch.commit_ids = batch.commit_link_ids.commit_id

    @api.depends('create_date')
    def _compute_age(self):
        """Return the time between job start and now"""
        for batch in self:
            if batch.create_date:
                batch.age = int(time.time() - dt2time(batch.create_date))
            else:
                batch.buildage_age = 0

    def get_formated_age(self):
        return s2human_long(self.age)

    def _url(self):
        self.ensure_one()
        return "/runbot/batch/%s" % self.id

    def _new_commit(self, branch, match_type='new'):
        # if not the same hash for repo:
        commit = branch.head
        self.last_update = fields.Datetime.now()
        for commit_link in self.commit_link_ids:
            # case 1: a commit already exists for the repo (pr+branch, or fast push)
            if commit_link.commit_id.repo_id == commit.repo_id:
                if commit_link.commit_id.id != commit.id:
                    self._log('New head on branch %s during throttle phase: Replacing commit %s with %s', branch.name, commit_link.commit_id.name, commit.name)
                    commit_link.write({'commit_id': commit.id, 'branch_id': branch.id})
                elif not commit_link.branch_id.is_pr and branch.is_pr:
                    commit_link.branch_id = branch  # Try to have a pr instead of branch on commit if possible ?
                break
        else:
            self.write({'commit_link_ids': [(0, 0, {
                'commit_id': commit.id,
                'match_type': match_type,
                'branch_id': branch.id
            })]})

    def _skip(self):
        for batch in self:
            if batch.bundle_id.is_base or batch.state == 'done':
                continue
            batch.state = 'skipped'  # done?
            batch._log('Skipping batch')
            for slot in batch.slot_ids:
                slot.skipped = True
                build = slot.build_id
                if build.global_state in ('running', 'done'):
                    continue
                testing_slots = build.slot_ids.filtered(lambda s: not s.skipped)
                if not testing_slots:
                    if build.global_state == 'pending':
                        build._skip('Newer build found')
                    elif build.global_state in ('waiting', 'testing'):
                        if not build.killable:
                            build.killable = True
                elif slot.link_type == 'created':
                    batches = testing_slots.mapped('batch_id')
                    _logger.info('Cannot skip build %s build is still in use in batches %s', build.id, batches.ids)
                    bundles = batches.mapped('bundle_id') - batch.bundle_id
                    if bundles:
                        batch._log('Cannot kill or skip build %s, build is used in another bundle: %s', build.id, bundles.mapped('name'))

    def _process(self):
        processed = self.browse()
        for batch in self:
            if batch.state == 'preparing' and batch.last_update < fields.Datetime.now() - datetime.timedelta(seconds=60):
                batch._prepare()
                processed |= batch
            elif batch.state == 'ready' and all(slot.build_id.global_state in (False, 'running', 'done') for slot in batch.slot_ids):
                _logger.info('Batch %s is done', self.id)
                batch._log('Batch done')
                batch.state = 'done'
                processed |= batch
        return processed

    def _create_build(self, params):
        """
        Create a build with given params_id if it does not already exists.
        In the case that a very same build already exists that build is returned
        """
        domain = [('params_id', '=', params.id), ('parent_id', '=', False)]
        if self.bundle_id.host_id:
            domain += [('host', '=', self.bundle_id.host_id.name), ('keep_host', '=', True)]
        build = self.env['runbot.build'].search(domain, limit=1, order='id desc')
        link_type = 'matched'
        killed_states = ('skipped', 'killed', 'manually_killed')
        if build and build.local_result not in killed_states and build.global_result not in killed_states:
            if build.killable:
                build.killable = False
        else:
            description = params.trigger_id.description if params.trigger_id.description else False
            link_type = 'created'

            build_type = 'normal'
            if self.category_id != self.env.ref('runbot.default_category'):
                build_type = 'scheduled'
            elif self.bundle_id.priority:
                build_type = 'priority'

            build = self.env['runbot.build'].create({
                'params_id': params.id,
                'description': description,
                'build_type': build_type,
                'no_auto_run': self.bundle_id.no_auto_run,
            })
            if self.bundle_id.host_id:
                build.host = self.bundle_id.host_id.name
                build.keep_host = True

            build._github_status()
        return link_type, build

    def _prepare(self, auto_rebase=False):
        _logger.info('Preparing batch %s', self.id)
        if not self.bundle_id.base_id:
            # in some case the base can be detected lately. If a bundle has no base, recompute the base before preparing
            self.bundle_id._compute_base_id()
        for level, message in self.bundle_id.consistency_warning():
            if level == "warning":
                self.warning("Bundle warning: %s" % message)

        self.state = 'ready'

        bundle = self.bundle_id
        project = bundle.project_id
        if not bundle.version_id:
            _logger.error('No version found on bundle %s in project %s', bundle.name, project.name)

        dockerfile_id = bundle.dockerfile_id or bundle.base_id.dockerfile_id or bundle.project_id.dockerfile_id or bundle.version_id.dockerfile_id
        if not dockerfile_id:
            _logger.error('No dockerfile found !')

        triggers = self.env['runbot.trigger'].search([  # could be optimised for multiple batches. Ormcached method?
            ('project_id', '=', project.id),
            ('category_id', '=', self.category_id.id)
        ]).filtered(
            lambda t: not t.version_domain or \
            self.bundle_id.version_id.filtered_domain(t.get_version_domain())
        )

        pushed_repo = self.commit_link_ids.mapped('commit_id.repo_id')
        dependency_repos = triggers.mapped('dependency_ids')
        all_repos = triggers.mapped('repo_ids') | dependency_repos
        missing_repos = all_repos - pushed_repo

        ######################################
        # Find missing commits
        ######################################
        def fill_missing(branch_commits, match_type):
            if branch_commits:
                for branch, commit in branch_commits.items():  # branch first in case pr is closed.
                    nonlocal missing_repos
                    if commit.repo_id in missing_repos:
                        if not branch.alive:
                            self._log("Skipping dead branch %s" % branch.name)
                            continue
                        values = {
                            'commit_id': commit.id,
                            'match_type': match_type,
                            'branch_id': branch.id,
                        }
                        if match_type.startswith('base'):
                            values['base_commit_id'] = commit.id
                            values['merge_base_commit_id'] = commit.id
                        self.write({'commit_link_ids': [(0, 0, values)]})
                        missing_repos -= commit.repo_id

        # CHECK branch heads consistency
        branch_per_repo = {}
        for branch in bundle.branch_ids.sorted(lambda b: (b.head.id, b.is_pr), reverse=True):
            if branch.alive:
                commit = branch.head
                repo = commit.repo_id
                if repo not in branch_per_repo:
                    branch_per_repo[repo] = branch
                elif branch_per_repo[repo].head != branch.head and branch.alive:
                    obranch = branch_per_repo[repo]
                    self._log("Branch %s and branch %s in repo %s don't have the same head: %s ≠ %s", branch.dname, obranch.dname, repo.name, branch.head.name, obranch.head.name)

        # 1.1 FIND missing commit in bundle heads
        if missing_repos:
            fill_missing({branch: branch.head for branch in bundle.branch_ids.sorted(lambda b: (b.head.id, b.is_pr), reverse=True)}, 'head')

        # 1.2 FIND merge_base info for those commits
        #  use last not preparing batch to define previous repos_heads instead of branches heads:
        #  Will allow to have a diff info on base bundle, compare with previous bundle
        last_base_batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.base_id.id), ('state', '!=', 'preparing'), ('category_id', '=', self.category_id.id), ('id', '!=', self.id)], order='id desc', limit=1)
        base_head_per_repo = {commit.repo_id.id: commit for commit in last_base_batch.commit_ids}
        self._update_commits_infos(base_head_per_repo)  # set base_commit, diff infos, ...

        # 2. FIND missing commit in a compatible base bundle
        if not bundle.is_base:
            merge_base_commits = self.commit_link_ids.mapped('merge_base_commit_id')
            if auto_rebase:
                self.base_reference_batch_id = last_base_batch
            else:
                self.base_reference_batch_id = False
                link_commit = self.env['runbot.commit.link'].search([
                    ('commit_id', 'in', merge_base_commits.ids),
                    ('match_type', 'in', ('new', 'head'))
                ])
                batches = self.env['runbot.batch'].search([
                    ('bundle_id', '=', bundle.base_id.id),
                    ('commit_link_ids', 'in', link_commit.ids),
                    ('state', '!=', 'preparing'),
                    ('category_id', '=', self.category_id.id)
                ]).sorted(lambda b: (len(b.commit_ids & merge_base_commits), b.id), reverse=True)
                if batches:
                    self.base_reference_batch_id = batches[0]

            batch = self.base_reference_batch_id
            if batch:
                if missing_repos:
                    self._log('Using batch [%s](%s) to define missing commits', batch.id, batch._url())
                    fill_missing({link.branch_id: link.commit_id for link in batch.commit_link_ids}, 'base_match')
                # check if all mergebase match reference batch
                batch_exiting_commit = batch.commit_ids.filtered(lambda c: c.repo_id in merge_base_commits.repo_id)
                not_matching = (batch_exiting_commit - merge_base_commits)
                if not_matching and not auto_rebase:
                    message = 'Only %s out of %s merge base matched. You may want to rebase your branches to ensure compatibility' % (len(merge_base_commits)-len(not_matching), len(merge_base_commits))
                    suggestions = [('Tip: rebase %s to %s' % (commit.repo_id.name, commit.name)) for commit in not_matching]
                    self.warning('%s\n%s' % (message, '\n'.join(suggestions)))
            else:
                self._log('No reference batch found to fill missing commits')

        # 3.1 FIND missing commit in base heads
        if missing_repos:
            if not bundle.is_base:
                self._log('Not all commit found in bundle branches and base batch. Fallback on base branches heads.')
            fill_missing({branch: branch.head for branch in self.bundle_id.base_id.branch_ids}, 'base_head')

        # 3.2 FIND missing commit in master base heads
        if missing_repos:  # this is to get an upgrade branch.
            if not bundle.is_base:
                self._log('Not all commit found in current version. Fallback on master branches heads.')
            master_bundle = self.env['runbot.version']._get('master').with_context(project_id=self.bundle_id.project_id.id).base_bundle_id
            fill_missing({branch: branch.head for branch in master_bundle.branch_ids}, 'base_head')

        # 4. FIND missing commit in foreign project
        if missing_repos:
            foreign_projects = dependency_repos.mapped('project_id') - project
            if foreign_projects:
                self._log('Not all commit found. Fallback on foreign base branches heads.')
                foreign_bundles = bundle.search([('name', '=', bundle.name), ('project_id', 'in', foreign_projects.ids)])
                fill_missing({branch: branch.head for branch in foreign_bundles.mapped('branch_ids').sorted('is_pr', reverse=True)}, 'head')
                if missing_repos:
                    foreign_bundles = bundle.search([('name', '=', bundle.base_id.name), ('project_id', 'in', foreign_projects.ids)])
                    fill_missing({branch: branch.head for branch in foreign_bundles.mapped('branch_ids')}, 'base_head')

        # CHECK missing commit
        if missing_repos:
            _logger.warning('Missing repo %s for batch %s', missing_repos.mapped('name'), self.id)

        ######################################
        #  Generate build params
        ######################################
        if auto_rebase:
            for commit_link in self.commit_link_ids:
                commit_link.commit_id = commit_link.commit_id._rebase_on(commit_link.base_commit_id)
        commit_link_by_repos = {commit_link.commit_id.repo_id.id: commit_link for commit_link in self.commit_link_ids}
        bundle_repos = bundle.branch_ids.mapped('remote_id.repo_id')
        version_id = self.bundle_id.version_id.id
        project_id = self.bundle_id.project_id.id
        trigger_customs = {}
        for trigger_custom in self.bundle_id.trigger_custom_ids:
            trigger_customs[trigger_custom.trigger_id] = trigger_custom
        for trigger in triggers:
            trigger_custom = trigger_customs.get(trigger, self.env['runbot.bundle.trigger.custom'])
            trigger_repos = trigger.repo_ids | trigger.dependency_ids
            if trigger_repos & missing_repos:
                self.warning('Missing commit for repo %s for trigger %s', (trigger_repos & missing_repos).mapped('name'), trigger.name)
                continue
            # in any case, search for an existing build
            config = trigger_custom.config_id or trigger.config_id
            extra_params = trigger_custom.extra_params or ''
            config_data = trigger_custom.config_data or {}
            params_value = {
                'version_id':  version_id,
                'extra_params': extra_params,
                'config_id': config.id,
                'project_id': project_id,
                'trigger_id': trigger.id,  # for future reference and access rights
                'config_data': config_data,
                'commit_link_ids': [(6, 0, [commit_link_by_repos[repo.id].id for repo in trigger_repos])],
                'modules': bundle.modules,
                'dockerfile_id': dockerfile_id,
                'create_batch_id': self.id,
                'used_custom_trigger': bool(trigger_custom),
            }
            params_value['builds_reference_ids'] = trigger._reference_builds(bundle)

            params = self.env['runbot.build.params'].create(params_value)

            build = self.env['runbot.build']
            link_type = 'created'
            force_trigger = trigger_custom and trigger_custom.start_mode == 'force'
            skip_trigger = (trigger_custom and trigger_custom.start_mode == 'disabled') or trigger.manual
            should_start = ((trigger.repo_ids & bundle_repos) or bundle.build_all or bundle.sticky)
            if force_trigger or (should_start and not skip_trigger):  # only auto link build if bundle has a branch for this trigger
                link_type, build = self._create_build(params)
            self.env['runbot.batch.slot'].create({
                'batch_id': self.id,
                'trigger_id': trigger.id,
                'build_id': build.id,
                'params_id': params.id,
                'link_type': link_type,
            })

        ######################################
        # SKIP older batches
        ######################################
        default_category = self.env.ref('runbot.default_category')
        if not bundle.sticky and self.category_id == default_category:
            skippable = self.env['runbot.batch'].search([
                ('bundle_id', '=', bundle.id),
                ('state', 'not in', ('done', 'skipped')),
                ('id', '<', self.id),
                ('category_id', '=', default_category.id)
            ])
            skippable._skip()

    def _update_commits_infos(self, base_head_per_repo):
        for link_commit in self.commit_link_ids:
            commit = link_commit.commit_id
            base_head = base_head_per_repo.get(commit.repo_id.id)
            if not base_head:
                self.warning('No base head found for repo %s', commit.repo_id.name)
                continue
            link_commit.base_commit_id = base_head
            merge_base_sha = False
            try:
                link_commit.base_ahead = link_commit.base_behind = 0
                link_commit.file_changed = link_commit.diff_add = link_commit.diff_remove = 0
                link_commit.merge_base_commit_id = commit.id
                if commit.name == base_head.name:
                    continue
                merge_base_sha = commit.repo_id._git(['merge-base', commit.name, base_head.name]).strip()
                merge_base_commit = self.env['runbot.commit']._get(merge_base_sha, commit.repo_id.id)
                link_commit.merge_base_commit_id = merge_base_commit.id

                ahead, behind = commit.repo_id._git(['rev-list', '--left-right', '--count', '%s...%s' % (commit.name, base_head.name)]).strip().split('\t')

                link_commit.base_ahead = int(ahead)
                link_commit.base_behind = int(behind)

                if merge_base_sha == commit.name:
                    continue

                # diff. Iter on --numstat, easier to parse than --shortstat summary
                diff = commit.repo_id._git(['diff', '--numstat', merge_base_sha, commit.name]).strip()
                if diff:
                    for line in diff.split('\n'):
                        link_commit.file_changed += 1
                        add, remove, _ = line.split(None, 2)
                        try:
                            link_commit.diff_add += int(add)
                            link_commit.diff_remove += int(remove)
                        except ValueError:  # binary files
                            pass
            except subprocess.CalledProcessError:
                self.warning('Commit info failed between %s and %s', commit.name, base_head.name)

    def warning(self, message, *args):
        self.has_warning = True
        _logger.warning('batch %s: ' + message, self.id, *args)
        self._log(message, *args, level='WARNING')

    def _log(self, message, *args, level='INFO'):
        message = message % args if args else message
        self.env['runbot.batch.log'].create({
            'batch_id': self.id,
            'message': message,
            'level': level,
        })


class BatchLog(models.Model):
    _name = 'runbot.batch.log'
    _description = 'Batch log'

    batch_id = fields.Many2one('runbot.batch', index=True)
    message = fields.Text('Message')
    level = fields.Char()


    def _markdown(self):
        """ Apply pseudo markdown parser for message.
        """
        self.ensure_one()
        return pseudo_markdown(self.message)



class BatchSlot(models.Model):
    _name = 'runbot.batch.slot'
    _description = 'Link between a bundle batch and a build'
    _order = 'trigger_id,id'

    _fa_link_type = {'created': 'hashtag', 'matched': 'link', 'rebuild': 'refresh'}

    batch_id = fields.Many2one('runbot.batch', index=True)
    trigger_id = fields.Many2one('runbot.trigger', index=True)
    build_id = fields.Many2one('runbot.build', index=True)
    all_build_ids = fields.Many2many('runbot.build', compute='_compute_all_build_ids')
    params_id = fields.Many2one('runbot.build.params', index=True, required=True)
    link_type = fields.Selection([('created', 'Build created'), ('matched', 'Existing build matched'), ('rebuild', 'Rebuild')], required=True)  # rebuild type?
    active = fields.Boolean('Attached', default=True)
    skipped = fields.Boolean('Skipped', default=False)
    # rebuild, what to do: since build can be in multiple batch:
    # - replace for all batch?
    # - only available on batch and replace for batch only?
    # - create a new bundle batch will new linked build?

    @api.depends('build_id')
    def _compute_all_build_ids(self):
        all_builds = self.env['runbot.build'].search([('id', 'child_of', self.build_id.ids)])
        for slot in self:
            slot.all_build_ids = all_builds.filtered_domain([('id', 'child_of', slot.build_id.ids)])

    def fa_link_type(self):
        return self._fa_link_type.get(self.link_type, 'exclamation-triangle')

    def _create_missing_build(self):
        """Create a build when the slot does not have one"""
        self.ensure_one()
        if self.build_id:
            return self.build_id
        self.batch_id._log(f'Trigger {self.trigger_id.name} was started by {self.env.user.name}')
        self.link_type, self.build_id = self.batch_id._create_build(self.params_id)
        return self.build_id
