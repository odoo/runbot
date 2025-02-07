from __future__ import annotations

import hashlib
import hmac
import logging
import json
from datetime import datetime, timedelta
from typing import Callable

import sentry_sdk
from werkzeug.exceptions import NotFound, UnprocessableEntity

from odoo.api import Environment
from odoo.http import Controller, request, route, Response

from . import dashboard
from . import misc
from . import reviewer_provisioning
from .. import utils, github

_logger = logging.getLogger(__name__)

def staging_dict(staging):
    return {
        'id': staging.id,
        'pull_requests': staging.batch_ids.prs.mapped(lambda p: {
            'name': p.display_name,
            'repository': p.repository.name,
            'number': p.number,
        }),
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
        if not staging.exists:
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
    def index(self) -> Response:
        req = request.httprequest
        event = req.headers['X-Github-Event']
        with sentry_sdk.configure_scope() as scope:
            if scope.transaction:
                # only in 1.8.0 (or at least 1.7.2
                if hasattr(scope, 'set_transaction_name'):
                    scope.set_transaction_name(f"webhook {event}")
                else: # but our servers use 1.4.3
                    scope.transaction = f"webhook {event}"

        github._gh.info(self._format(req))

        env = request.env(user=1)
        data = request.get_json_data()
        if event == 'ping':
            return handle_ping(env, request.get_json_data())

        repo = data.get('repository', {}).get('full_name')
        source = repo and env['runbot_merge.events_sources'].search([('repository', '=', repo)])
        if not source:
            _logger.warning(
                "Ignored hook %s to unknown source repository %s",
                req.headers.get("X-Github-Delivery"),
                repo,
            )
            return Response(status=403, mimetype="text/plain")
        elif secret := source.secret:
            signature = 'sha256=' + hmac.new(secret.strip().encode(), req.get_data(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, req.headers.get('X-Hub-Signature-256', '')):
                _logger.warning(
                    "Ignored hook %s with incorrect signature on %s: got %s expected %s, in:\n%s",
                    req.headers.get('X-Github-Delivery'),
                    repo,
                    req.headers.get('X-Hub-Signature-256'),
                    signature,
                    req.headers,
                )
                return Response(status=403, mimetype="text/plain")
        elif req.headers.get('X-Hub-Signature-256'):
            _logger.info("No secret for %s but received a signature in:\n%s", repo, req.headers)
        else:
            _logger.info("No secret or signature for %s", repo)

        c = EVENTS.get(event)
        if not c:
            _logger.warning('Unknown event %s', event)
            return Response(
                status=422,
                mimetype="text/plain",
                response="Not setup to receive event.",
            )

        sentry_sdk.set_context('webhook', data)
        return c(env, data)

    def _format(self, request):
        return """{r.method} {r.full_path}
{headers}

{body}
vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv\
""".format(
            r=request,
            headers='\n'.join(
                '\t%s: %s' % entry for entry in request.headers.items()
            ),
            body=request.get_data(as_text=True),
        )

def handle_pr(env, event):
    pr = event['pull_request']
    squash = pr['commits'] == 1
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
    env['runbot_merge.pull_requests']._track_set_author(sender, fallback=True)

    if event['action'] == 'opened':
        author_name = pr['user']['login']
        author = env['res.partner'].search([('github_login', '=', author_name)], limit=1)
        if not author:
            env['res.partner'].create({'name': author_name, 'github_login': author_name})
        pr_obj = env['runbot_merge.pull_requests']._from_gh(pr)
        return Response(status=201, mimetype="text/plain", response=f"Tracking PR as {pr_obj.display_name}")

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
        return Response(mimetype="text/plain", response=f'Updated to {pr_obj.head}')

    if event['action'] not in ('closed', 'reopened'):
        if pr_obj.squash != squash:
            pr_obj.squash = squash

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

            return Response(mimetype="text/plain", response=f'Reopened {pr_obj.display_name}')

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return Response(status=200, mimetype="text/plain", response=f"Not handling {event['action']} yet")

def handle_status(env: Environment, event: dict) -> Response:
    _logger.info(
        'status on %(sha)s %(context)s:%(state)s (%(target_url)s) [%(description)r]',
        event
    )
    status_value = json.dumps({
        event['context']: {
            'state': event['state'],
            'target_url': event['target_url'],
            'description': event['description'],
            'updated_at': datetime.now().isoformat(timespec='seconds'),
        }
    })
    # create status, or merge update into commit *unless* the update is already
    # part of the status (dupe status)
    env.cr.execute("""
        INSERT INTO runbot_merge_commit AS c (sha, to_check, statuses)
        VALUES (%s, true, %s)
        ON CONFLICT (sha) DO UPDATE
            SET to_check = true,
                statuses = c.statuses::jsonb || EXCLUDED.statuses::jsonb
            WHERE NOT c.statuses::jsonb @> EXCLUDED.statuses::jsonb
    """, [event['sha'], status_value], log_exceptions=False)
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

EVENTS: dict[str, Callable[[Environment, dict], Response]] = {
    'pull_request': handle_pr,
    'status': handle_status,
    'issue_comment': handle_comment,
    'pull_request_review': handle_review,
    'ping': handle_ping,
}

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
