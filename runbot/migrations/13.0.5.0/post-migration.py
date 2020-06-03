# -*- coding: utf-8 -*-

from odoo.api import Environment
from odoo import SUPERUSER_ID
import logging
import progressbar
from collections import defaultdict
import datetime

def _bar(total):
    b = progressbar.ProgressBar(maxval=total, \
        widgets=[progressbar.Bar('=', '[', ']'), ' ', progressbar.Percentage()])
    b.start()
    return b

_logger = logging.getLogger(__name__)


class RunbotMigrationException(Exception):
    pass


def migrate(cr, version):
    env = Environment(cr, SUPERUSER_ID, {})

    # monkey patch github to raise an exception during migration to avoid problems
    def _github():
        raise RunbotMigrationException('_github call')

    env['runbot.remote']._github = _github

    # some checks:
    for keyword in ('real_build', 'duplicate_id', '_get_all_commit', '_get_repo', '_copy_dependency_ids', 'Commit', '_get_repo_available_modules'):
        matches = env['runbot.build.config.step'].search([('python_code', 'like', keyword)])
        if matches:
            _logger.warning('Some python steps found with %s ref: %s', keyword, matches)

    cr.execute('SELECT id FROM runbot_repo WHERE nginx = true')
    if cr.fetchone():
        cr.execute("""INSERT INTO ir_config_parameter (KEY, value) VALUES ('runbot_nginx', 'True')""")

    ########################
    # Repo, remotes, triggers and projects
    ########################

    visited = set()
    owner_to_remote = {}
    repos_infos = {}
    triggers = {}
    triggers_by_project = defaultdict(list)
    deleted_repos_ids = set()

    RD_project = env.ref('runbot.main_project')
    security_project = env['runbot.project'].create({
        'name': 'Security'
    })

    # create a master bundle for security (master was discovered after saas-14)
    env['runbot.bundle'].create({
        'name': 'master',
        'is_base': True,
        'project_id': security_project.id
    })

    project_matching = { # some hardcoded info
        'odoo': RD_project,
        'enterprise': RD_project,
        'upgrade': RD_project,
        'design-themes': RD_project,
        'odoo-security': security_project,
        'enterprise-security': security_project,
    }

    # remove the SET NULL on runbot_build.repo_id when the repo is deleted.
    # Otherwise we will not be able to create commits later
    cr.execute('ALTER TABLE runbot_build DROP CONSTRAINT IF EXISTS runbot_build_repo_id_fkey1;')

    cr.execute("""
        SELECT
        id, name, duplicate_id, modules, modules_auto, server_files, manifest_files, addons_paths, mode, token, repo_config_id
        FROM runbot_repo order by id
    """)
    for id, name, duplicate_id, modules, modules_auto, server_files, manifest_files, addons_paths, mode, token, repo_config_id in cr.fetchall():
        cr.execute(""" SELECT res_groups_id FROM res_groups_runbot_repo_rel WHERE runbot_repo_id = %s""", (id,))
        group_ids = [r[0] for r in cr.fetchall()]
        repo_name = name.split('/')[-1].replace('.git', '')
        owner = name.split(':')[-1].split('/')[0]

        if duplicate_id in visited:
            repo = env['runbot.repo'].browse(duplicate_id)
        else:
            repo = env['runbot.repo'].browse(id)

        cr.execute('ALTER SEQUENCE runbot_remote_id_seq RESTART WITH %s', (id, ))
        remote = env['runbot.remote'].create({
            'name': name,
            'repo_id': repo.id if duplicate_id not in visited else duplicate_id,
            'sequence': repo.sequence,
            'fetch_pull': mode != 'disabled',
            'fetch_heads': mode != 'disabled',
            'token': token,
        })
        assert remote.id == id

        # Move repo_id to remote_id
        # Implies to ensure that remote id's will be the same as the old repo
        # repo_id is set to null to avoid the cascading delete when the repo will be removed
        cr.execute('UPDATE runbot_branch SET remote_id=repo_id, repo_id=NULL WHERE repo_id = %s', (id,))

        owner_to_remote[(owner, repo.id)] = remote.id

        repo_infos = {
            'name': repo_name,
            'modules': modules,
            'group_ids': group_ids,
            'server_files': server_files,
            'manifest_files': manifest_files,
            'addons_paths': addons_paths,
        }

        if duplicate_id in visited:
            cr.execute('DELETE FROM runbot_repo WHERE id = %s', (id, ))
            deleted_repos_ids.add(id)
            if repos_infos[duplicate_id] != repo_infos:
                _logger.warning('deleting duplicate with different values:\nexisting->%s\ndeleted->%s', repos_infos[duplicate_id], repo_infos)
        else:
            visited.add(id)
            repos_infos[id] = repo_infos
            repo.name = repo_name
            repo.main_remote = remote
            # if not, we need to give information on how to group repos: odoo+enterprise+upgarde+design-theme/se/runbot
            # this mean that we will need to group build too. Could be nice but maybe a little difficult.
            if repo_name in project_matching:
                project = project_matching[repo_name]
            else:
                project = env['runbot.project'].create({
                    'name': repo_name,
                })
                # also create a master budle, just in case
                env['runbot.bundle'].create({
                    'name': 'master',
                    'is_base': True,
                    'project_id': project.id
                })
            repo.project_id = project.id

            cr.execute(""" SELECT dependency_id FROM runbot_repo_dep_rel WHERE dependant_id = %s""", (id,))
            dependency_ids = [r[0] for r in cr.fetchall()]

            trigger = env['runbot.trigger'].create({
                'name': repo_name,
                'project_id': project.id,
                'repo_ids': [(4, id)],
                'dependency_ids': [(4, dependency_id) for dependency_id in dependency_ids],
                'config_id': repo_config_id if repo_config_id else env.ref('runbot.runbot_build_config_default').id,
            })
            triggers[id] = trigger
            triggers_by_project[project.id].append(trigger)

    #######################
    # Branches
    #######################
    cr.execute('UPDATE runbot_branch SET name=branch_name')

    # no build, config, ...
    dummy_bundle = env.ref('runbot.bundle_dummy')
    ########################
    # Bundles
    ########################
    _logger.info('Creating bundles')

    branches = env['runbot.branch'].search([], order='id')

    branches._compute_reference_name()

    bundles = {('master', RD_project.id): env.ref('runbot.bundle_master')}
    branch_to_bundle = {}
    branch_to_version = {}
    progress = _bar(len(branches))
    env.cr.execute("""SELECT id FROM runbot_branch WHERE sticky='t'""")
    sticky_ids = [rec[0] for rec in env.cr.fetchall()]

    for i, branch in enumerate(branches):
        progress.update(i)
        repo = branch.remote_id.repo_id
        if branch.target_branch_name and branch.pull_head_name:
            # 1. update source_repo: do not call github and use a naive approach:
            # pull_head_name contains odoo-dev and a repo in group starts with odoo-dev -> this is a known repo.
            owner = branch.pull_head_name.split(':')[0]
            pull_head_remote_id = owner_to_remote.get((owner, repo.id))
            if pull_head_remote_id:
                branch.pull_head_remote_id = pull_head_remote_id
        project_id = repo.project_id.id
        name = branch.reference_name

        key = (name, project_id)
        if key not in bundles:
            bundle = env['runbot.bundle'].create({
                'name': name,
                'project_id': project_id,
                'sticky': branch.id in sticky_ids,
                'is_base': branch.id in sticky_ids,
            })
            bundles[key] = bundle
        bundle = bundles[key]

        if branch.is_pr:
            if bundle.is_base:
                _logger.warning('Trying to add pr %s (%s) to base bundle (%s)', branch.name, branch.id, bundle.name)
                bundle = dummy_bundle
            elif ':' in name:
                #  handle external PR's
                base_name = name.split(':')[1].split('-')[0]
                defined_base_key = (base_name, project_id)
                if defined_base_key in bundles:
                    bundle.defined_base_id = bundles[defined_base_key]

        branch.bundle_id = bundle
        branch_to_bundle[branch.id] = bundle
        branch_to_version[branch.id] = bundle.version_id.id

    branches.flush()
    env['runbot.bundle'].flush()
    progress.finish()

    batch_size = 100000

    sha_commits = {}
    sha_repo_commits = {}
    branch_heads = {}
    commit_link_ids = defaultdict(dict)
    cr.execute("SELECT count(*) FROM runbot_build")
    nb_build = cr.fetchone()[0]

    ########################
    # BUILDS
    ########################
    _logger.info('Creating main commits')
    counter = 0
    progress = _bar(nb_build)
    cross_project_duplicate_ids = []
    for offset in range(0, nb_build, batch_size):
        cr.execute("""
            SELECT id,
            repo_id, name, author, author_email, committer, committer_email, subject, date, duplicate_id, branch_id
            FROM runbot_build ORDER BY id asc LIMIT %s OFFSET %s""", (batch_size, offset))

        for id, repo_id, name, author, author_email, committer, committer_email, subject, date, duplicate_id, branch_id in cr.fetchall():
            progress.update(counter)
            remote_id = env['runbot.remote'].browse(repo_id)
            #assert remote_id.exists()
            if not repo_id:
                _logger.warning('No repo_id for build %s, skipping', id)
                continue
            key = (name, remote_id.repo_id.id)
            if key in sha_repo_commits:
                commit = sha_repo_commits[key]
            else:
                if duplicate_id and remote_id.repo_id.project_id.id != RD_project.id:
                    cross_project_duplicate_ids.append(id)
                elif duplicate_id:
                    _logger.warning('Problem: duplicate: %s,%s', id, duplicate_id)

                commit = env['runbot.commit'].create({
                    'name': name,
                    'repo_id': remote_id.repo_id.id,  # now that the repo_id on the build correspond to a remote_id
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': date
                })
                sha_repo_commits[key] = commit
                sha_commits[name] = commit
            branch_heads[branch_id] = commit.id
            counter += 1

            commit_link_ids[id][commit.repo_id.id] = commit.id


    progress.finish()

    if cross_project_duplicate_ids:
        _logger.info('Cleaning cross project duplicates')
        cr.execute("UPDATE runbot_build SET local_state='done', duplicate_id=NULL WHERE id IN %s", (tuple(cross_project_duplicate_ids), ))

    _logger.info('Creating params')
    counter = 0

    cr.execute("SELECT count(*) FROM runbot_build WHERE duplicate_id IS NULL")
    nb_real_build = cr.fetchone()[0]
    progress = _bar(nb_real_build)

    # monkey patch to avoid search
    original = env['runbot.build.params']._find_existing
    existing = {}

    def _find_existing(fingerprint):
        return existing.get(fingerprint, env['runbot.build.params'])

    param = env['runbot.build.params']
    param._find_existing = _find_existing

    builds_deps = defaultdict(list)
    def get_deps(bid):
        if bid < get_deps.start or bid > get_deps.stop:
            builds_deps.clear()
            get_deps.start = bid
            get_deps.stop = bid+batch_size
            cr.execute('SELECT build_id, dependency_hash, dependecy_repo_id, closest_branch_id, match_type FROM runbot_build_dependency WHERE build_id>=%s and build_id<=%s', (get_deps.start, get_deps.stop))
            for build_id, dependency_hash, dependecy_repo_id, closest_branch_id, match_type in cr.fetchall():
                builds_deps[build_id].append((dependency_hash, dependecy_repo_id, closest_branch_id, match_type))
        return builds_deps[bid]
    get_deps.start = 0
    get_deps.stop = 0

    def update_build_params(params_id, id):
        cr.execute('UPDATE runbot_build SET params_id=%s WHERE id=%s OR duplicate_id = %s', (params_id, id, id))

    build_ids_to_recompute = []
    for offset in range(0, nb_real_build, batch_size):
        cr.execute("""
            SELECT
            id, branch_id, repo_id, extra_params, config_id, config_data
            FROM runbot_build WHERE duplicate_id IS NULL ORDER BY id asc LIMIT %s OFFSET %s""", (batch_size, offset))

        for id, branch_id, repo_id, extra_params, config_id, config_data in cr.fetchall():
            progress.update(counter)
            counter += 1
            build_ids_to_recompute.append(id)

            remote_id = env['runbot.remote'].browse(repo_id)
            commit_link_ids_create_values = [
                {'commit_id': commit_link_ids[id][remote_id.repo_id.id], 'match_type':'base_head'}]

            for dependency_hash, dependecy_repo_id, closest_branch_id, match_type in get_deps(id):
                dependency_remote_id = env['runbot.remote'].browse(dependecy_repo_id)
                key = (dependency_hash, dependency_remote_id.id)
                commit = sha_repo_commits.get(key) or sha_commits.get(dependency_hash)
                if not commit:
                    # -> most of the time, commit in exists but with wrong repo. Info can be found on other commit.
                    _logger.warning('Missing commit %s created', dependency_hash)
                    commit = env['runbot.commit'].create({
                        'name': dependency_hash,
                        'repo_id': dependency_remote_id.repo_id.id,
                    })
                    sha_repo_commits[key] = commit
                    sha_commits[dependency_hash] = commit
                commit_link_ids[id][dependency_remote_id.id] = commit.id
                match_type = 'base_head' if match_type in ('pr_target', 'prefix', 'default') else 'head'
                commit_link_ids_create_values.append({'commit_id': commit.id, 'match_type':match_type, 'branch_id': closest_branch_id})

            params = param.create({
                'version_id':  branch_to_version[branch_id],
                'extra_params': extra_params,
                'config_id': config_id,
                'project_id': env['runbot.repo'].browse(remote_id.repo_id.id).project_id,
                'trigger_id': triggers[remote_id.repo_id.id].id,
                'config_data': config_data,
                'commit_link_ids': [(0, 0, values) for values in commit_link_ids_create_values]
            })
            existing[params.fingerprint] = params
            update_build_params(params.id, id)
        env.cache.invalidate()
    progress.finish()

    env['runbot.build.params']._find_existing = original

    ######################
    # update dest
    ######################
    _logger.info('Updating build dests')
    counter = 0
    progress = _bar(nb_real_build)
    for offset in range(0, len(build_ids_to_recompute), batch_size):
        builds = env['runbot.build'].browse(build_ids_to_recompute[offset:offset+batch_size])
        builds._compute_dest()
        progress.update(batch_size)
    progress.finish()

    for branch, head in branch_heads.items():
        cr.execute('UPDATE runbot_branch SET head=%s WHERE id=%s', (head, branch))
    del branch_heads
    # adapt build commits


    _logger.info('Creating batchs')
    ###################
    # Bundle batch
    ####################
    cr.execute("SELECT count(*) FROM runbot_build WHERE parent_id IS NOT NULL")
    nb_root_build = cr.fetchone()[0]
    counter = 0
    progress = _bar(nb_root_build)
    previous_batch = {}
    for offset in range(0, nb_root_build, batch_size):
        cr.execute("""
            SELECT
            id, duplicate_id, repo_id, branch_id, create_date, build_type, config_id, params_id
            FROM runbot_build WHERE parent_id IS NULL order by id asc
            LIMIT %s OFFSET %s""", (batch_size, offset))
        for id, duplicate_id, repo_id, branch_id, create_date, build_type, config_id, params_id in cr.fetchall():
            progress.update(counter)
            counter += 1
            if repo_id is None:
                _logger.warning('Skipping %s: no repo', id)
                continue
            bundle = branch_to_bundle[branch_id]
            # try to merge build in same batch
            # not temporal notion in this case, only hash consistency
            batch = False
            build_id = duplicate_id or id
            build_commits = commit_link_ids[build_id]
            batch_repos_ids = []

            # check if this build can be added to last_batch
            if bundle.last_batch:
                if create_date - bundle.last_batch.last_update < datetime.timedelta(minutes=5):
                    if duplicate_id and build_id in bundle.last_batch.slot_ids.mapped('build_id').ids:
                        continue

                    # to fix: nightly will be in the same batch of the previous normal one. If config_id is diffrent, create batch?
                    # possible fix: max create_date diff
                    batch = bundle.last_batch
                    batch_commits = batch.commit_ids
                    batch_repos_ids = batch_commits.mapped('repo_id').ids
                    for commit in batch_commits:
                        if commit.repo_id.id in build_commits:
                            if commit.id != build_commits[commit.repo_id.id]:
                                batch = False
                                batch_repos_ids = []
                                break

            missing_commits = [commit_id for repo_id, commit_id in build_commits.items() if repo_id not in batch_repos_ids]

            if not batch:
                batch = env['runbot.batch'].create({
                    'create_date': create_date,
                    'last_update': create_date,
                    'state': 'ready',
                    'bundle_id': bundle.id
                })
                #if bundle.last_batch:
                #    previous = previous_batch.get(bundle.last_batch.id)
                #    if previous:
                #        previous_build_by_trigger = {slot.trigger_id.id: slot.build_id.id for slot in previous.slot_ids}
                #    else:
                #        previous_build_by_trigger = {}
                #    batch_slot_triggers = bundle.last_batch.slot_ids.mapped('trigger_id').ids
                #    missing_trigger_ids = [trigger for trigger in triggers_by_project[bundle.project_id.id] if trigger.id not in batch_slot_triggers]
                #    for trigger in missing_trigger_ids:
                #        env['runbot.batch.slot'].create({
                #            'trigger_id': trigger.id,
                #            'batch_id': bundle.last_batch.id,
                #            'build_id': previous_build_by_trigger.get(trigger.id), # may be None, if we want to create empty slots. Else, iter on slot instead
                #            'link_type': 'matched',
                #            'active': True,
                #        })

                previous_batch[batch.id] = bundle.last_batch
                bundle.last_batch = batch
            else:
                batch.last_update = create_date

            real_repo_id = env['runbot.remote'].browse(repo_id).repo_id.id
            env['runbot.batch.slot'].create({
                'params_id': params_id,
                'trigger_id': triggers[real_repo_id].id,
                'batch_id': batch.id,
                'build_id': build_id,
                'link_type': 'rebuild' if build_type == 'rebuild' else 'matched' if duplicate_id else 'created',
                'active': True,
            })
            commit_links_values = []
            for missing_commit in missing_commits:
                commit_links_values.append({
                    'commit_id': missing_commit,
                    'match_type': 'new',
                })
            batch.commit_link_ids = [(0, 0, values) for values in commit_links_values]
            if batch.state == 'ready' and all(slot.build_id.global_state in (False, 'running', 'done') for slot in batch.slot_ids):
                batch.state = 'done'

        env.cache.invalidate()
    progress.finish()

    #Build of type rebuild may point to same params as rebbuild?

    ###################
    # Cleaning (performances)
    ###################
    # 1. avoid UPDATE "runbot_build" SET "commit_path_mode"=NULL WHERE "commit_path_mode"='soft'

    _logger.info('Pre-cleaning')
    cr.execute('alter table runbot_build alter column commit_path_mode drop not null')
    cr.execute('ANALYZE')
    cr.execute("delete from runbot_build where local_state='duplicate'") # what about duplicate childrens?
    _logger.info('End')
