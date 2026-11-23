import datetime
from collections import defaultdict

from werkzeug.exceptions import BadRequest

from odoo.fields import Domain
from odoo.http import Controller, Response, request, route
from odoo.tools import file_open


def _check_max_records(count):
    max_records = int(request.env['ir.config_parameter'].sudo().get_param('runbot.api_max_records', 50))
    if count > max_records:
        raise BadRequest(f'Too many records requested ({count}), maximum is {max_records}')


def _datetime_ms(value):
    return value.isoformat(sep=' ', timespec='milliseconds') if value else False


def _serialize_trigger(trigger):
    if not trigger:
        return False
    data = {'id': trigger.id, 'name': trigger.name}
    if trigger.purpose:
        data['purpose'] = trigger.purpose
    return data


def _serialize_log(log):
    error = log.error_content_id.error_id
    return {
        'id': log.id,
        'create_date': _datetime_ms(log.create_date),
        'level': log.level,
        'type': log.type,
        'message': log.message,
        'func': log.func,
        'path': log.path,
        'line': log.line,
        'canonical_tag': log.metadata.get('test', {}).get('canonical_tag', False),
        'known_error': {
            'id': error.id,
            'first_seen_date': error.first_seen_date,
            'last_seen_date': error.last_seen_date,
            'active': error.active,
            'random': error.random,
        } if error else False,
    }


def _serialize_build(build, depth=1, logs='errors', parent_values=None):
    assert logs in ('errors', 'all', 'none')
    params = build.params_id
    values = {
        'trigger': _serialize_trigger(build.trigger_id),
        'config': build.config_id.name,
        'version': build.version_id.name,
        'dockerfile': params.dockerfile_id.sudo().name or False,
        'create_batch_id': build.create_batch_id.id or False,
        'reference_build_id': (params.dump_db.build_id or params.reference_build_id).id or False,
        'commits': [{'repo': link.commit_id.repo_id.name, 'sha': link.commit_id.name} for link in params.commit_link_ids],
    }
    build_logs = build.sudo().log_ids.filtered(lambda log: logs == 'all' or log.level in ('WARNING', 'ERROR', 'CRITICAL')) if logs != 'none' else []
    children = [(child, {'relation': 'child'}) for child in build.children_ids]
    children += [(link.child_id, {'relation': link.link_type, 'link_orphan_result': link.orphan_result}) for link in build.child_link_ids]
    data = {
        'id': build.id,
        'description': build.description,
        'display_name': build.display_name,
        'host': build.host,
        'parent_id': build.parent_id.id or False,
        'orphan_result': build.orphan_result,
        'local_state': build.local_state,
        'global_state': build.global_state,
        'local_result': build.local_result,
        'global_result': build.global_result,
        'build_start': build.build_start,
        'build_end': build.build_end,
        'build_time': build.build_time,
        'docker_time': build.docker_time,
        'logs': [_serialize_log(log) for log in build_logs],
        'jobs': [{
            'name': job.name,
            'start_date': _datetime_ms(job.start_date),
            'duration': job.duration,
            'python_time': job.python_time,
            'git_time': job.git_time,
            'docker_pull_time': job.docker_pull_time,
            'docker_time': job.docker_time,
            'result_time': job.result_time,
            'docker_image': job._image_tag(),
            'log_url': f'{build._http_log_url()}{job.log_name}.txt' if job.log_name else False,
        } for job in build.sudo().job_ids],
        'log_files': [
            {'name': name, 'url': f'{build._http_log_url()}{name}.txt'}
            for name in (build.log_list or '').split(',') if name
        ],
        'databases': [
            {'name': database.db_suffix, 'db_name': database.name, 'url': f'{build._http_log_url()}{database.name}.zip'}
            for database in build.database_ids if database.exported
        ],
        'children': [
            {**_serialize_build(child, depth=depth - 1, logs=logs, parent_values=values), **extra}
            for child, extra in children
        ] if depth > 0 else [],
        # keys left out of a child have the same value as on its parent
        **{key: value for key, value in values.items() if not parent_values or parent_values[key] != value},
    }
    return data


def _serialize_batch(batch, depth=1, logs='errors'):
    return {
        'id': batch.id,
        'bundle_id': batch.bundle_id.id,
        'bundle_name': batch.bundle_id.name,
        'base_reference_batch_id': batch.base_reference_batch_id.id or False,
        'category': batch.category_id.name,
        'create_date': batch.create_date,
        'state': batch.state,
        'result': batch._get_global_result(),
        'commits': [{'repo': link.commit_id.repo_id.name, 'sha': link.commit_id.name} for link in batch.commit_link_ids],
        'logs': [
            {'id': log.id, 'create_date': log.create_date, 'level': log.level, 'message': log.message}
            for log in batch.log_ids if log.level != 'INFO'
        ],
        'slots': [_serialize_slot(slot, depth, logs) for slot in batch.slot_ids if slot.build_id or not slot.trigger_id.manual],
    }


def _serialize_slot(slot, depth, logs):
    data = {
        'trigger': _serialize_trigger(slot.trigger_id),
        'manual': slot.trigger_id.manual,
        'link_type': slot.link_type,
        'skipped': slot.skipped,
        'build': False,
    }
    if slot.build_id:
        data['build'] = _serialize_build(slot.build_id, depth=depth, logs=logs)
        # the trigger is given by the slot
        if data['build'].get('trigger') == data['trigger']:
            data['build'].pop('trigger')
    return data


def _serialize_batch_summary(batch):
    if not batch:
        return False
    build_states = batch.slot_ids.build_id.mapped('global_state')
    return {
        'id': batch.id,
        'state': batch.state,
        'result': batch._get_global_result(),
        'category': batch.category_id.name,
        'create_date': batch.create_date,
        # a done batch can still have builds in progress, e.g. after a rebuild
        'global_state': batch.env['runbot.build']._get_youngest_state(build_states) if build_states else False,
        'commits': [{'repo': link.commit_id.repo_id.name, 'sha': link.commit_id.name} for link in batch.commit_link_ids],
    }


def _serialize_bundle(bundle, batch_history=0):
    data = {
        'id': bundle.id,
        'name': bundle.name,
        'project_id': bundle.project_id.id,
        'project_name': bundle.project_id.name,
        'version': bundle.version_id.name,
        'sticky': bundle.sticky,
        'is_base': bundle.is_base,
        'last_batch': _serialize_batch_summary(bundle.last_batch),
    }
    if batch_history:
        batches = bundle.env['runbot.batch'].search([('bundle_id', '=', bundle.id), ('hidden', '=', False)], order='id desc', limit=batch_history)
        data['batches'] = [_serialize_batch_summary(batch) for batch in batches]
    return data


def _serialize_commit(commit):
    _statuses, last_status_by_context = commit._get_last_statuses()
    return {
        'id': commit.id,
        'repo': commit.repo_id.name,
        'sha': commit.name,
        'subject': commit.subject,
        'author': commit.author,
        'date': commit.date,
        'statuses': [{
            'context': status.context,
            'state': status.state,
            'description': status.description,
            'target_url': status.target_url,
            'build_id': status.build_id.id or False,
            'create_date': status.create_date,
            'sent_date': status.sent_date,
        } for status in last_status_by_context.values()],
    }



ERROR_SORTS = ('last_seen_date desc', 'last_seen_date asc', 'first_seen_date desc', 'first_seen_date asc', 'build_count desc', 'build_count asc')


def _serialize_error_summary(error):
    return {
        'id': error.id,
        'name': error.name,
        'active': error.active,
        'random': error.random,
        'assigned': bool(error.responsible),
        'responsible_github_login': error.responsible.sudo().github_login or False,
        'team': error.team_id.sudo().name or False,
        'triggers': error.trigger_ids.mapped('name'),
        'versions': error.version_ids.mapped('name'),
        'test_tags': error.test_tags or False,
        'first_seen_date': error.first_seen_date,
        'first_seen_build_id': error.first_seen_build_id.id or False,
        'last_seen_date': error.last_seen_date,
        'last_seen_build_id': error.last_seen_build_id.id or False,
        'build_count': error.build_count,
    }


def _serialize_error_versions(error):
    today = datetime.date.today()
    daily_counts = defaultdict(dict)
    for (batch_date, version_id), count in error._get_log_dates(today - datetime.timedelta(days=30), today + datetime.timedelta(days=1))[error].items():
        daily_counts[version_id][batch_date.strftime('%Y-%m-%d')] = count

    def done_batch_id(batch, operator, order):
        domain = [('bundle_id', '=', batch.bundle_id.id), ('category_id', '=', batch.category_id.id), ('state', '=', 'done'), ('id', operator, batch.id)]
        return error.env['runbot.batch'].search(domain, order=order, limit=1).id or False

    last_seen_batches = {batch.bundle_id.version_id: batch for batch in error.last_seen_batch_ids}
    seen_batches = [(first, last_seen_batches.get(first.bundle_id.version_id, first)) for first in error.first_seen_batch_ids]
    return [{
        'version': first.bundle_id.version_id.name,
        'previous_batch_id': done_batch_id(first, '<', 'id desc'),
        'first_seen_batch_id': first.id,
        'last_seen_batch_id': last.id,
        'next_batch_id': done_batch_id(last, '>', 'id'),
        'daily_counts': daily_counts[first.bundle_id.version_id.id],
    } for first, last in seen_batches]


def _serialize_error(error):
    return {
        **_serialize_error_summary(error),
        'project_id': error.project_id.id or False,
        'triggers': [_serialize_trigger(trigger) for trigger in error.trigger_ids],
        'tags': error.tag_ids.mapped('name'),
        'tags_min_version': error.tags_min_version_id.name or False,
        'tags_max_version': error.tags_max_version_id.name or False,
        'breaking_pr': error.breaking_pr_id.name or False,
        'breaking_pr_url': error.breaking_pr_url or False,
        'fixing_pr': error.fixing_pr_id.name or False,
        'fixing_pr_url': error.fixing_pr_url or False,
        'fixing_bundle_id': error.fixing_bundle_id.id or False,
        'fixing_commit': error.fixing_commit or False,
        'previous_error_id': error.previous_error_id.id or False,
        'versions': _serialize_error_versions(error),
        'contents': [{
            'id': content.id,
            'module_name': content.module_name,
            'function': content.function,
            'file_path': content.file_path,
            'canonical_tag': content.canonical_tag or False,
            'content': content.content,
            'first_seen_date': content.first_seen_date,
            'last_seen_date': content.last_seen_date,
            'build_count': content.build_count,
        } for content in error.error_content_ids.sorted('last_seen_date DESC')],
    }


class RunbotApi(Controller):

    @route(['/runbot/api'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_documentation(self, **kwargs):
        with file_open('runbot/documentation/api.md') as f:
            return Response(f.read(), status=200, mimetype='text/plain')

    @route(['/runbot/api/build/<string:ids>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_build(self, ids, depth=1, logs='errors', **kwargs):
        build_ids = [int(build_id) for build_id in ids.split(',')]
        _check_max_records(len(build_ids))
        builds = request.env['runbot.build'].browse(build_ids).exists()
        return request.make_json_response([{
            **_serialize_build(build, depth=int(depth), logs=logs),
            'bundle_id': build.create_batch_id.bundle_id.id or False,
            'bundle_name': build.create_batch_id.bundle_id.name or False,
        } for build in builds])

    @route(['/runbot/api/batch/<string:ids>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_batch(self, ids, depth=1, logs='errors', **kwargs):
        batch_ids = [int(batch_id) for batch_id in ids.split(',')]
        _check_max_records(len(batch_ids))
        batches = request.env['runbot.batch'].browse(batch_ids).exists()
        return request.make_json_response([_serialize_batch(batch, depth=int(depth), logs=logs) for batch in batches])

    @route(['/runbot/api/commit/<string:ids_or_shas>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_commit(self, ids_or_shas, **kwargs):
        values = ids_or_shas.split(',')
        _check_max_records(len(values))
        ids, shas = [], []
        for value in values:
            _slug, record_id = request.env['ir.http']._unslug(value)
            if record_id is None:
                shas.append(value)
            else:
                ids.append(record_id)
        commits = request.env['runbot.commit'].search(Domain('id', 'in', ids) | Domain('name', 'in', shas), order='id')
        return request.make_json_response([_serialize_commit(commit) for commit in commits])

    @route(['/runbot/api/bundle', '/runbot/api/bundle/<path:ids_or_names>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_bundle(self, ids_or_names='', project_id=None, pr='', batch_history=0, **kwargs):
        values = [value for value in (pr or ids_or_names).split(',') if value]
        _check_max_records(len(values))
        if pr:
            branch_domains = []
            for value in values:
                repo_name, _sep, number = value.rpartition(':')
                branch_domain = Domain('is_pr', '=', True) & Domain('name', '=', number)
                if repo_name:
                    branch_domain &= Domain('remote_id.repo_id.name', '=', repo_name)
                branch_domains.append(branch_domain)
            domain = Domain('id', 'in', request.env['runbot.branch'].search(Domain.OR(branch_domains)).bundle_id.ids)
        else:
            ids, names = [], []
            for value in values:
                _slug, record_id = request.env['ir.http']._unslug(value)
                if record_id is None:
                    names.append(value)
                else:
                    ids.append(record_id)
            domain = Domain('id', 'in', ids) | Domain('name', 'in', names)
        projects = request.env['runbot.project'].search([('id', '=', int(project_id))] if project_id else [])
        bundles = request.env['runbot.bundle'].search(Domain('project_id', 'in', projects.ids) & domain, order='id')
        batch_history = min(max(int(batch_history or 0), 0), 40)
        return request.make_json_response([_serialize_bundle(bundle, batch_history=batch_history) for bundle in bundles])

    @route(['/runbot/api/error/<string:ids>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_error(self, ids, **kwargs):
        error_ids = [int(error_id) for error_id in ids.split(',')]
        _check_max_records(len(error_ids))
        errors = request.env['runbot.build.error'].with_context(active_test=False).search([('id', 'in', error_ids)], order='id')
        return request.make_json_response([_serialize_error(error) for error in errors])

    @route(['/runbot/api/errors'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_errors(self, sort='last_seen_date desc', limit=20, offset=0, **kwargs):
        assert sort in ERROR_SORTS
        limit = int(limit)
        assert limit > 0  # limit=0 means no limit for the orm
        _check_max_records(limit)
        errors = request.env['runbot.build.error'].search([('build_count', '>', 1)], order=f'{sort}, id desc', limit=limit, offset=int(offset))
        return request.make_json_response([_serialize_error_summary(error) for error in errors])
