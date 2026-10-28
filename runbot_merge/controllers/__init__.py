from __future__ import annotations

import hashlib
import hmac
import logging
import json
import random
import time
import typing
from datetime import datetime, timedelta

import psycopg2.errors
from werkzeug.exceptions import NotFound, UnprocessableEntity

from odoo.api import Environment
from odoo.http import Controller, request, route, Response
from odoo.service.model import MAX_TRIES_ON_CONCURRENCY_FAILURE

from . import apps
from . import dashboard
from . import misc
from . import reviewer_provisioning
from . import statuses
from .. import utils, github

_logger = logging.getLogger(__name__)

def staging_dict(staging):
    PRs = staging.env['runbot_merge.pull_requests']
    return {
        'id': staging.id,
        'pull_requests': staging.batch_ids.prs.mapped(lambda p: {
            'name': p.display_name,
            'repository': p.repository.name,
            'number': p.number,
        }),
        'batches': [
            [
                {
                    'repository': pr.repository_id.name,
                    'number': pr.number,
                    'head': pr.head,
                    'merged_head': json.loads(pr.commits_map)[''],
                }
                for pr in PRs.browse(batch_info['prs'])
            ]
            for batch_info in staging.snapshot
        ],
        'merged': {
            c.repository_id.name: c.commit_id.sha
            for c in staging.commits
        },
        'staged': {
            h.repository_id.name: h.commit_id.sha
            for h in staging.heads
        },
    }

class MergebotController(Controller):
    @route('/runbot_merge/stagings', auth='none', type='http', methods=['GET'])
    def stagings_for_commits(self, **kw):
        commits = request.httprequest.args.getlist('merged')
        heads = request.httprequest.args.getlist('staged')
        if bool(commits) ^ bool(heads):
            Stagings = request.env(user=1)['runbot_merge.stagings'].sudo()
            if commits:
                stagings = Stagings.for_commits(*commits)
            else:
                stagings = Stagings.for_heads(*heads)

            # can pass `?state=` to get stagings in any state
            if state := request.httprequest.args.get('state', 'success'):
                stagings = stagings.filtered(lambda s: s.state == state)
            return request.make_json_response(stagings.mapped(staging_dict))
        else:
            raise UnprocessableEntity("Must receive either `merged` or `staged` query parameters (can receive multiple of either)")

    @route('/runbot_merge/stagings/<int:staging>', auth='none', type='http', methods=['GET'])
    def prs_for_staging(self, staging):
        staging = request.env(user=1)['runbot_merge.stagings'].browse(staging).sudo()
        if not staging.exists():
            raise NotFound()

        return request.make_json_response(staging_dict(staging))

    @route('/runbot_merge/stagings/<int:from_staging>/<int:to_staging>', auth='none', type='http', methods=['GET'])
    def prs_for_stagings(self, from_staging, to_staging, include_from='1', include_to='1'):
        Stagings = request.env(user=1, context={"active_test": False})['runbot_merge.stagings']
        from_staging = Stagings.browse(from_staging)
        to_staging = Stagings.browse(to_staging)
        if not (from_staging.exists() and to_staging.exists()):
            raise NotFound()
        if from_staging.target != to_staging.target:
            raise UnprocessableEntity(f"Stagings must have the same target branch, found {from_staging.target.name} and {to_staging.target.name}")
        if from_staging.id >= to_staging.id:
            raise UnprocessableEntity("first staging must be older than second staging")

        stagings = Stagings.search([
            ('target', '=', to_staging.target.id),
            ('state', '=', 'success'),
            ('id', '>=' if include_from else '>', from_staging.id),
            ('id', '<=' if include_to else '<', to_staging.id),
        ], order="id asc")

        return request.make_json_response([staging_dict(staging) for staging in stagings])

    @route('/runbot_merge/hooks', auth='none', type='http', csrf=False, methods=['POST'])
    def webhooks(self) -> Response:
        req = request.httprequest
        github._gh.getChild('hook').info(self._format(req))

        env = request.env(user=1)
        data = request.get_json_data()

        target = req.headers.get('X-Github-Hook-Installation-Target-Type')
        target_id = req.headers.get('X-Github-Hook-Installation-Target-Id')
        match target:
            case 'repository':
                # TODO: store repo ids and use that instead of name-based?
                sourcename = data.get('repository', {}).get('full_name')
                source = sourcename and env['runbot_merge.events_sources'].search([('repository', '=', sourcename)])
                secret = source and source.secret
            case 'integration':
                connector_ = env['runbot_merge.github_connector']
                source = connector_.search([('app_id', '=', int(target_id))])
                sourcename = source.name
                # TODO: also filter on repository?
                secret = source.webhook_secret
            case it:
                _logger.warning(
                    "Ignored hook %s with unknown installation type %s",
                    req.headers.get('X-Github-Delivery'),
                    it,
                )
                return Response(status=400, mimetype="text/plain")

        if not source:
            _logger.warning(
                "Ignored hook %s (%s) to unknown source repository %s (%s:%s)",
                req.headers.get("X-Github-Delivery"),
                req.headers.get("X-Github-Event"),
                sourcename,
                target,
                target_id,
            )
            return Response(status=403, mimetype="text/plain")
        elif secret:
            signature = 'sha256=' + hmac.new(secret.strip().encode(), req.get_data(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, sig := req.headers.get('X-Hub-Signature-256', '')):
                _logger.warning(
                    "Ignored hook %s with incorrect signature on %s: got %s expected %s, in:\n%s",
                    req.headers.get('X-Github-Delivery'),
                    sourcename,
                    sig,
                    signature,
                    req.headers,
                )
                return Response(status=403, mimetype="text/plain")
        elif req.headers.get('X-Hub-Signature-256'):
            _logger.warning(
                "Ignored hook %s with signature but no secret on %s",
                req.headers.get('X-Github-Delivery'),
                sourcename,
            )
            return Response(status=501, mimetype="text/plain")
        else:
            _logger.info(
                "Hook %s: no secret or signature for %s",
                req.headers.get('X-Github-Delivery'),
                sourcename,
            )

        match req.headers['X-Github-Event']:
            case 'ping':
                return handle_ping(env, data)
            case 'installation':
                installation = data['installation']
                app = env['runbot_merge.github_connector'].search([
                    ('client_id', '=', installation['client_id']),
                ])
                match data['action']:
                    case 'created':
                        _logger.info("created installation %s for %s", installation['id'], app)
                        env['runbot_merge.github_connector.installation'].create({
                            'connector_id': app.id,
                            'installation_id': installation['id'],
                            'owner_id': installation['account']['id'],
                            'owner_login': installation['account']['login'],
                            'repository_selection': installation['repository_selection'],
                            # TODO: repositories
                        })
                    case 'deleted':
                        _logger.info("deleted installation %s for %s", installation['id'], app)
                        env['runbot_merge.github_connector.installation'].search([
                            ('connector_id', '=', app.id),
                            ('installation_id', '=', installation['id']),
                        ]).unlink()
                    case ('suspend' | 'unsuspend' | 'new_permissions_accepted') as act:
                        _logger.info("ignored installation action %r", act)
                    case act:
                        _logger.warning("unknown installation action %r", act)
                return Response(mimetype="text/plain", response="ok")
            case 'installation_repositories':
                # TODO note that this is kinda shit, needs to characterise but
                #      - despite both `added` and `removed` having
                #        `repositories_added` and `repositories_removed` keys,
                #        only the corresponding key is non-empty
                #      - when switching between repository selection modes, you
                #        always get ADD, no indication of the actual change,
                #        and the repos to which access is now granted
                #      - in `selected` mode, both adding and removing repos
                #        triggers both events one after the other
                #      - if selection_mode=all, I think repos being created or
                #        deleted triggers added/removed events, to check
                return Response(mimetype="text/plain", response="ok")
            case 'pull_request':
                return handle_pr(env, data)
            case 'status':
                return handle_status(env, data)
            case 'issue_comment':
                return handle_comment(env, data)
            case 'pull_request_review':
                return handle_review(env, data)
            case event:
                _logger.warning('Unknown event %s', event)
                return Response(
                    status=422,
                    mimetype="text/plain",
                    response="Not setup to receive event.",
                )

    def _format(self, request):
        headers = '\n'.join('\t%s: %s' % entry for entry in request.headers.items())
        action = f".{a}" if (a := request.json.get('action')) else ""
        return f"""{request.headers['x-github-event']}{action}
{headers}
{request.get_data(as_text=True)}
"""

def handle_pr(env, event):
    pr = event['pull_request']
    squash = False
    check_commits = lambda _: None
    match pr['commits']:
        case 0:
            check_commits = lambda pr_obj: env['runbot_merge.pull_requests.check_commits'].create({
                'pull_request_id': pr_obj.id,
            })
        case 1:
            squash = True
    r = pr['base']['repo']['full_name']

    if event['action'] in [
        'assigned', 'unassigned', 'review_requested', 'review_request_removed',
        'labeled', 'unlabeled'
    ]:
        _logger.debug(
            'Ignoring pull_request[%s] on %s#%s',
            event['action'],
            event['pull_request']['base']['repo']['full_name'],
            event['pull_request']['number'],
        )
        if pr := env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', r),
            ('number', '=', pr['number']),
            ('squash', '!=', squash),
        ]):
            pr.squash = squash
            check_commits(pr)

        return Response(
            status=200,
            mimetype="text/plain",
            response="Not setup to receive action.",
        )

    b = pr['base']['ref']

    repo = env['runbot_merge.repository'].search([('name', '=', r)])
    if not repo:
        _logger.warning("Received a PR for %s but not configured to handle that repo", r)
        return Response(
            status=422,
            mimetype="text/plain",
            response="Not configured for repository.",
        )

    # PRs to unmanaged branches are not necessarily abnormal and
    # we don't care
    branch = env['runbot_merge.branch'].with_context(active_test=False).search([
        ('name', '=', b),
        ('project_id', '=', repo.project_id.id),
    ])

    def feedback(**info):
        return env['runbot_merge.pull_requests.feedback'].create({
            'repository': repo.id,
            'pull_request': pr['number'],
            **info,
        })
    def find(target):
        return env['runbot_merge.pull_requests'].search([
            ('repository', '=', repo.id),
            ('number', '=', pr['number']),
            # ('target', '=', target.id),
        ])
    # edition difficulty: pr['base']['ref] is the *new* target, the old one
    # is at event['change']['base']['ref'] (if the target changed), so edition
    # handling must occur before the rest of the steps
    if event['action'] == 'edited':
        source = event['changes'].get('base', {'ref': {'from': b}})['ref']['from']
        source_branch = env['runbot_merge.branch'].with_context(active_test=False).search([
            ('name', '=', source),
            ('project_id', '=', repo.project_id.id),
        ])
        # retargeting to un-managed => delete
        if not branch:
            pr = find(source_branch)
            number = pr.number
            pr.unlink()
            return Response(
                status=200,
                mimetype="text/plain",
                response=f'Retargeted {number} to un-managed branch {b}, deleted',
            )

        # retargeting from un-managed => create
        if not source_branch:
            return handle_pr(env, dict(event, action='opened'))

        pr_obj = find(source_branch)
        updates = {}
        if source_branch != branch:
            if branch != pr_obj.target:
                updates['target'] = branch.id
                updates['squash'] = squash

        if 'title' in event['changes'] or 'body' in event['changes']:
            updates['message'] = utils.make_message(pr)

        _logger.info("update: %s = %s (by %s)", pr_obj.display_name, updates, event['sender']['login'])
        if updates:
            # copy because it updates the `updates` dict internally
            pr_obj.write(dict(updates))
            check_commits(pr_obj)
            return Response(
                status=200,
                mimetype="text/plain",
                response=f'Updated {", ".join(updates)}',
            )
        return Response(
            status=200,
            mimetype="text/plain",
            response=f"Nothing to update ({', '.join(event['changes'])})",
        )

    message = None
    if not branch:
        message = env.ref('runbot_merge.handle.branch.unmanaged')._format(
            repository=r,
            branch=b,
            event=event,
        )
        _logger.info("Ignoring event %s on PR %s#%d for un-managed branch %s",
                     event['action'], r, pr['number'], b)
    elif not branch.active:
        message = env.ref('runbot_merge.handle.branch.inactive')._format(
            repository=r,
            branch=b,
            event=event,
        )
    if message and event['action'] not in ('synchronize', 'closed'):
        feedback(message=message)

    if not branch:
        return Response(status=422, mimetype="text/plain", response=f"Not set up to care about {r}:{b}")

    headers = request.httprequest.headers if request else {}
    _logger.info(
        "%s: %s#%s (%s) (by %s, delivery %s by %s)",
        event['action'],
        repo.name, pr['number'],
        pr['title'].strip(),
        event['sender']['login'],
        headers.get('X-Github-Delivery'),
        headers.get('User-Agent'),
     )
    sender = env['res.partner'].search([('github_login', '=', event['sender']['login'])], limit=1)
    if not sender:
        sender = env['res.partner'].create({'name': event['sender']['login'], 'github_login': event['sender']['login']})
    env['runbot_merge.batch']._track_set_author(sender, fallback=True)
    env['runbot_merge.pull_requests']._track_set_author(sender, fallback=True)

    if event['action'] == 'opened':
        author_name = pr['user']['login']
        author = env['res.partner'].search([('github_login', '=', author_name)], limit=1)
        if not author:
            env['res.partner'].create({'name': author_name, 'github_login': author_name})
        try:
            pr_obj = env['runbot_merge.pull_requests']._from_gh(pr)
            check_commits(pr_obj)
            return Response(status=201, mimetype="text/plain", response=f"Tracking PR as {pr_obj.display_name}")
        except psycopg2.errors.UniqueViolation:
            env.cr.rollback()
            return Response(status=200, mimetype="text/plain", response="Already known")

    pr_obj = env['runbot_merge.pull_requests']._get_or_schedule(r, pr['number'], closing=event['action'] == 'closed')
    if not pr_obj:
        _logger.info("webhook %s on unknown PR %s#%s, scheduled fetch", event['action'], repo.name, pr['number'])
        return Response(
            status=202,  # actually we're ignoring the event so 202 might be wrong?
            mimetype="text/plain",
            response=f"Unknown PR {repo.name}:{pr['number']}, scheduling fetch",
        )
    if event['action'] == 'synchronize':
        if pr_obj.head == pr['head']['sha']:
            _logger.warning(
                "PR %s sync %s -> %s => same head",
                pr_obj.display_name,
                pr_obj.head,
                pr['head']['sha'],
            )
            return Response(mimetype="text/plain", response='No update to pr head')

        if pr_obj.state in ('closed', 'merged'):
            _logger.error("PR %s sync %s -> %s => failure (closed)", pr_obj.display_name, pr_obj.head, pr['head']['sha'])
            return Response(
                status=422,
                mimetype="text/plain",
                response="It's my understanding that closed/merged PRs don't get sync'd",
            )

        _logger.info(
            "PR %s sync %s -> %s by %s => reset to 'open' and squash=%s",
            pr_obj.display_name,
            pr_obj.head,
            pr['head']['sha'],
            event['sender']['login'],
            squash,
        )
        if pr['base']['ref'] != pr_obj.target.name:
            env['runbot_merge.fetch_job'].create({
                'repository': pr_obj.repository.id,
                'number': pr_obj.number,
                'commits_at': datetime.now() + timedelta(minutes=5),
            })

        pr_obj.write({
            'reviewed_by': False,
            'error': False,
            'head': pr['head']['sha'],
            'squash': squash,
        })
        check_commits(pr_obj)
        return Response(mimetype="text/plain", response=f'Updated to {pr_obj.head}')

    if event['action'] not in ('closed', 'reopened'):
        if pr_obj.squash != squash:
            pr_obj.squash = squash
            check_commits(pr_obj)

    if event['action'] == 'ready_for_review':
        pr_obj.draft = False
        return Response(mimetype="text/plain", response=f'Updated {pr_obj.display_name} to ready')
    if event['action'] == 'converted_to_draft':
        pr_obj.draft = True
        return Response(mimetype="text/plain", response=f'Updated {pr_obj.display_name} to draft')

    # don't marked merged PRs as closed (!!!)
    if event['action'] == 'closed' and pr_obj.state != 'merged':
        oldstate = pr_obj.state
        if pr_obj._try_closing(event['sender']['login']):
            _logger.info(
                '%s closed %s (state=%s)',
                event['sender']['login'],
                pr_obj.display_name,
                oldstate,
            )
            return Response(mimetype="text/plain", response=f'Closed {pr_obj.display_name}')

        _logger.info(
            '%s tried to close %s (state=%s) but locking failed',
            event['sender']['login'],
            pr_obj.display_name,
            oldstate,
        )
        return Response(
            status=503,
            mimetype="text/plain",
            response='could not lock rows (probably being merged)',
        )

    if event['action'] == 'reopened' :
        if pr_obj.merge_date:
            if pr_obj.state == 'merged':
                message = env.ref('runbot_merge.handle.pr.merged')._format(event=event)
            else:
                message = env.ref('runbot_merge.handle.pr.mergedbatch')._format(event=event)
            feedback(close=True, message=message)
        elif pr_obj.closed:
            _logger.info('%s reopening %s', event['sender']['login'], pr_obj.display_name)
            pr_obj.write({
                'closed': False,
                # updating the head triggers a revalidation, and unstages the batch
                'head': pr['head']['sha'],
                'squash': pr['commits'] == 1,
            })
            check_commits(pr_obj)

            return Response(mimetype="text/plain", response=f'Reopened {pr_obj.display_name}')

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return Response(status=200, mimetype="text/plain", response=f"Not handling {event['action']} yet")

def handle_status(env: Environment, event: dict) -> Response:
    _logger.info(
        '%(sha)s:%(context)s:%(state)s (%(target_url)s) [%(description)r]',
        event
    )
    updated_at = event.get('created_at', datetime.now().isoformat(timespec='seconds')).rstrip('Z')
    status_value = {
        event['context']: {
            'state': event['state'],
            'target_url': event['target_url'],
            'description': event['description'],
            'updated_at': updated_at,
        }
    }
    for n in range(1, MAX_TRIES_ON_CONCURRENCY_FAILURE+1):
        try:
            commit = env['runbot_merge.commit'].search([('sha', '=', event['sha'])])
            if commit:
                st = json.loads(commit.statuses)
                # check if we already have a more recent status for this context
                if (ctx := st.get(event['context'])) and ctx.get('updated_at', '') >= updated_at:
                    _logger.info(
                        "%(sha)s:%(context)s ignored: outdated",
                        event
                    )
                    return Response("outdated status", status=200)  # or 409 conflict?

                updated = st | status_value
                if updated != st:
                    commit.statuses = json.dumps(updated)
            else:
                env['runbot_merge.commit'].create({
                    'sha': event['sha'],
                    'statuses': json.dumps(status_value),
                })
            env.cr.flush()
            break
        except (
            psycopg2.errors.UniqueViolation,
            psycopg2.errors.SerializationFailure,
            # shouldn't come into play but might as well also handle these
            psycopg2.errors.DeadlockDetected,
            psycopg2.errors.LockNotAvailable,
        ):
            env.cr.rollback()
            env.reset()
            env.registry.reset_changes()
            if n == MAX_TRIES_ON_CONCURRENCY_FAILURE:
                _logger.warning("Serialization failure on statuses", exc_info=True)
                return Response(status=500)
            # fuzz sleep as in `retrying`
            time.sleep(random.uniform(0.0, 2 ** n))
    env.ref("runbot_merge.process_updated_commits")._trigger()

    return Response(status=204)

def handle_comment(env: Environment, event: dict) -> Response:
    if 'pull_request' not in event['issue']:
        return Response(
            status=200,
            mimetype="text/plain",
            response="issue comment, ignoring",
        )
    if event['action'] != 'created':
        return Response(
            status=200,
            mimetype="text/plain",
            response=f"Ignored: action ({event['action']!r}) is not 'created'",
        )

    repo = event['repository']['full_name']
    issue = event['issue']['number']
    author = event['comment']['user']['login']
    comment = event['comment']['body']
    if len(comment) > 5000:
        _logger.warning('comment(%s): %s %s#%s => ignored (%d characters)', event['comment']['html_url'], author, repo, issue, len(comment))
        return Response(status=413, mimetype="text/plain", response="comment too large")

    _logger.info('comment[%s]: %s %s#%s %r', event['action'], author, repo, issue, comment)
    return _handle_comment(env, repo, issue, event['comment'])

def handle_review(env: Environment, event: dict) -> Response:
    if event['action'] != 'submitted':
        return Response(
            status=200,
            mimetype="text/plain",
            response=f"Ignored: action ({event['action']!r}) is not 'submitted'",
        )

    repo = event['repository']['full_name']
    pr = event['pull_request']['number']
    author = event['review']['user']['login']
    comment = event['review']['body'] or ''
    if len(comment) > 5000:
        _logger.warning('comment(%s): %s %s#%s => ignored (%d characters)', event['review']['html_url'], author, repo, pr, len(comment))
        return Response(status=413, mimetype="text/plain", response="review too large")

    _logger.info('review[%s]: %s %s#%s %r', event['action'], author, repo, pr, comment)
    return _handle_comment(env, repo, pr, event['review'], target=event['pull_request']['base']['ref'])

def handle_ping(env: Environment, event: dict) -> Response:
    _logger.info("Got ping! %s", event['zen'])
    return Response(mimetype="text/plain", response="pong")

def _handle_comment(
        env: Environment,
        repo: str,
        issue: int,
        comment: dict,
        target: str | None = None,
) -> Response:
    repository = env['runbot_merge.repository'].search([('name', '=', repo)])
    if not repository.project_id._find_commands(comment['body'] or ''):
        return Response(mimetype="text/plain", response="No commands, ignoring")

    pr = env['runbot_merge.pull_requests']._get_or_schedule(
        repo, issue, target=target, commenter=comment['user']['login'],
    )
    if not pr:
        return Response(status=202, mimetype="text/plain", response="Unknown PR, scheduling fetch")

    partner = env['res.partner'].search([('github_login', '=', comment['user']['login'])])
    return Response(
        mimetype="text/plain",
        response=pr._parse_commands(partner, comment, comment['user']['login']),
    )
