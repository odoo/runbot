from werkzeug.exceptions import BadRequest

from odoo.fields import Domain
from odoo.http import Controller, Response, request, route
from odoo.tools import file_open


def _check_max_records(count):
    max_records = int(request.env['ir.config_parameter'].sudo().get_param('runbot.api_max_records', 50))
    if count > max_records:
        raise BadRequest(f'Too many records requested ({count}), maximum is {max_records}')


def _serialize_log(log):
    error = log.error_content_id.error_id
    return {
        'id': log.id,
        'create_date': log.create_date,
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
        'trigger': build.trigger_id.name,
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
    if 'trigger' in data and build.trigger_id.purpose:
        data['trigger_purpose'] = build.trigger_id.purpose
    return data


def _serialize_batch(batch, depth=1, logs='errors'):
    statuses = {}
    for st in batch.env['runbot.commit.status'].search([('commit_id', 'in', batch.commit_link_ids.commit_id.ids)]):
        sent_at = st.sent_date.isoformat(timespec='seconds') + 'Z'
        statuses.setdefault(st.commit_id, []).append({
            'state': st.state,
            'description': st.description,
            'target_url': st.target_url,
            'context': st.context,
            'created_at': sent_at,
            'updated_at': sent_at,
        })
    return {
        'id': batch.id,
        'bundle_id': batch.bundle_id.id,
        'bundle_name': batch.bundle_id.name,
        'base_reference_batch_id': batch.base_reference_batch_id.id or False,
        'category': batch.category_id.name,
        'create_date': batch.create_date,
        'state': batch.state,
        'result': batch._get_global_result(),
        'commits': [{
            'repo': link.commit_id.repo_id.name,
            'sha': link.commit_id.name,
            'statuses': statuses.get(link.commit_id),
        } for link in batch.commit_link_ids],
        'logs': [
            {'id': log.id, 'create_date': log.create_date, 'level': log.level, 'message': log.message}
            for log in batch.log_ids if log.level != 'INFO'
        ],
        'slots': [_serialize_slot(slot, depth, logs) for slot in batch.slot_ids if slot.build_id or not slot.trigger_id.manual],
    }


def _serialize_slot(slot, depth, logs):
    data = {
        'trigger': slot.trigger_id.name,
        'manual': slot.trigger_id.manual,
        'link_type': slot.link_type,
        'skipped': slot.skipped,
        'build': False,
    }
    if slot.trigger_id.purpose:
        data['trigger_purpose'] = slot.trigger_id.purpose
    if slot.build_id:
        data['build'] = _serialize_build(slot.build_id, depth=depth, logs=logs)
        # the trigger is given by the slot
        if data['build'].get('trigger') == data['trigger']:
            data['build'].pop('trigger')
            data['build'].pop('trigger_purpose', None)
    return data


def _serialize_bundle(bundle):
    return {
        'id': bundle.id,
        'name': bundle.name,
        'project_id': bundle.project_id.id,
        'project_name': bundle.project_id.name,
        'version': bundle.version_id.name,
        'sticky': bundle.sticky,
        'is_base': bundle.is_base,
        'last_batch_id': bundle.last_batch.id or False,
        'batches': [{
            'id': batch.id,
            'state': batch.state,
            'result': batch._get_global_result(),
            'category': batch.category_id.name,
            'create_date': batch.create_date,
        } for batch in bundle.env['runbot.batch'].search([('bundle_id', '=', bundle.id), ('hidden', '=', False)], order='id desc', limit=40)],
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

    @route(['/runbot/api/bundle', '/runbot/api/bundle/<path:ids_or_names>'], type='http', auth='public', methods=['GET'], sitemap=False)
    def api_bundle(self, ids_or_names='', project_id=None, pr='', **kwargs):
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
        return request.make_json_response([_serialize_bundle(bundle) for bundle in bundles])
