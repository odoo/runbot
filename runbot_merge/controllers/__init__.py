import hashlib
import hmac
import logging
import json

import sentry_sdk
import werkzeug.exceptions

from odoo.http import Controller, request, route

from . import dashboard
from . import reviewer_provisioning
from .. import utils, github

_logger = logging.getLogger(__name__)

class MergebotController(Controller):
    @route('/runbot_merge/stagings', auth='none', type='json')
    def stagings_for_commits(self, commits=None, heads=None):
        Stagings = request.env(user=1)['runbot_merge.stagings'].sudo()
        if commits:
            stagings = Stagings.for_commits(*commits)
        elif heads:
            stagings = Stagings.for_heads(*heads)
        else:
            raise ValueError('Must receive one of "commits" or "heads" kwarg')

        return stagings.ids

    @route('/runbot_merge/stagings/<int:staging>', auth='none', type='json')
    def prs_for_staging(self, staging):
        staging = request.env(user=1)['runbot_merge.stagings'].browse(staging)
        return [
            batch.prs.mapped(lambda p: {
                'name': p.display_name,
                'repository': p.repository.name,
                'number': p.number,
            })
            for batch in staging.sudo().batch_ids
        ]

    @route('/runbot_merge/stagings/<int:from_staging>/<int:to_staging>', auth='none', type='json')
    def prs_for_staging(self, from_staging, to_staging, include_from=True, include_to=True):
        Stagings = request.env(user=1, context={"active_test": False})['runbot_merge.stagings']
        from_staging = Stagings.browse(from_staging)
        to_staging = Stagings.browse(to_staging)
        if from_staging.target != to_staging.target:
            raise ValueError(f"Stagings must have the same target branch, found {from_staging.target.name} and {to_staging.target.name}")
        if from_staging.id >= to_staging.id:
            raise ValueError("first staging must be older than second staging")

        stagings = Stagings.search([
            ('target', '=', to_staging.target.id),
            ('state', '=', 'success'),
            ('id', '>=' if include_from else '>', from_staging.id),
            ('id', '<=' if include_to else '<', to_staging.id),
        ], order="id asc")

        return [
            {
                'staging': staging.id,
                'prs': [
                    batch.prs.mapped(lambda p: {
                        'name': p.display_name,
                        'repository': p.repository.name,
                        'number': p.number,
                    })
                    for batch in staging.batch_ids
                ]
            }
            for staging in stagings
        ]


    @route('/runbot_merge/hooks', auth='none', type='json', csrf=False, methods=['POST'])
    def index(self):
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

        c = EVENTS.get(event)
        if not c:
            _logger.warning('Unknown event %s', event)
            return 'Unknown event {}'.format(event)

        repo = request.jsonrequest['repository']['full_name']
        env = request.env(user=1)

        secret = env['runbot_merge.repository'].search([
            ('name', '=', repo),
        ]).project_id.secret
        if secret:
            signature = 'sha256=' + hmac.new(secret.encode(), req.get_data(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, req.headers.get('X-Hub-Signature-256', '')):
                _logger.warning("Ignored hook %s with incorrect signature",
                                req.headers.get('X-Github-Delivery'))
                return werkzeug.exceptions.Forbidden()

        sentry_sdk.set_context('webhook', request.jsonrequest)
        return c(env, request.jsonrequest)

    def _format(self, request):
        return """<= {r.method} {r.full_path}
{headers}
{body}
vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv
""".format(
            r=request,
            headers='\n'.join(
                '\t%s: %s' % entry for entry in request.headers.items()
            ),
            body=utils.shorten(request.get_data(as_text=True).strip(), 400)
        )

def handle_pr(env, event):
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
        return 'Ignoring'

    pr = event['pull_request']
    r = pr['base']['repo']['full_name']
    b = pr['base']['ref']

    repo = env['runbot_merge.repository'].search([('name', '=', r)])
    if not repo:
        _logger.warning("Received a PR for %s but not configured to handle that repo", r)
        # sadly shit's retarded so odoo json endpoints really mean
        # jsonrpc and it's LITERALLY NOT POSSIBLE TO REPLY WITH
        # ACTUAL RAW HTTP RESPONSES and thus not possible to
        # report actual errors to the webhooks listing thing on
        # github (not that we'd be looking at them but it'd be
        # useful for tests)
        return "Not configured to handle {}".format(r)

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
            ('target', '=', target.id),
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
            pr.unlink()
            return 'Retargeted {} to un-managed branch {}, deleted'.format(pr.id, b)

        # retargeting from un-managed => create
        if not source_branch:
            return handle_pr(env, dict(event, action='opened'))

        pr_obj = find(source_branch)
        updates = {}
        if source_branch != branch:
            if branch != pr_obj.target:
                updates['target'] = branch.id
                updates['squash'] = pr['commits'] == 1

        # turns out github doesn't bother sending a change key if the body is
        # changing from empty (None), therefore ignore that entirely, just
        # generate the message and check if it changed
        message = utils.make_message(pr)
        if message != pr_obj.message:
            updates['message'] = message

        _logger.info("update: %s = %s (by %s)", pr_obj.display_name, updates, event['sender']['login'])
        if updates:
            # copy because it updates the `updates` dict internally
            pr_obj.write(dict(updates))
            return 'Updated {}'.format(', '.join(updates))
        return "Nothing to update ({})".format(', '.join(event['changes'].keys()))

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
        return "Not set up to care about {}:{}".format(r, b)

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
    if event['action'] == 'opened':
        author_name = pr['user']['login']
        author = env['res.partner'].search([('github_login', '=', author_name)], limit=1)
        if not author:
            env['res.partner'].create({'name': author_name, 'github_login': author_name})
        pr_obj = env['runbot_merge.pull_requests']._from_gh(pr)
        return "Tracking PR as {}".format(pr_obj.id)

    pr_obj = env['runbot_merge.pull_requests']._get_or_schedule(r, pr['number'])
    if not pr_obj:
        _logger.info("webhook %s on unknown PR %s#%s, scheduled fetch", event['action'], repo.name, pr['number'])
        return "Unknown PR {}:{}, scheduling fetch".format(repo.name, pr['number'])
    if event['action'] == 'synchronize':
        if pr_obj.head == pr['head']['sha']:
            _logger.warning(
                "PR %s sync %s -> %s => same head",
                pr_obj.display_name,
                pr_obj.head,
                pr['head']['sha'],
            )
            return 'No update to pr head'

        if pr_obj.state in ('closed', 'merged'):
            _logger.error("PR %s sync %s -> %s => failure (closed)", pr_obj.display_name, pr_obj.head, pr['head']['sha'])
            return "It's my understanding that closed/merged PRs don't get sync'd"

        if pr_obj.state == 'ready':
            pr_obj.unstage("updated by %s", event['sender']['login'])

        _logger.info(
            "PR %s sync %s -> %s by %s => reset to 'open' and squash=%s",
            pr_obj.display_name,
            pr_obj.head,
            pr['head']['sha'],
            event['sender']['login'],
            pr['commits'] == 1
        )

        pr_obj.write({
            'state': 'opened',
            'head': pr['head']['sha'],
            'squash': pr['commits'] == 1,
        })
        return f'Updated to {pr_obj.head}'

    if event['action'] == 'ready_for_review':
        pr_obj.draft = False
        return f'Updated {pr_obj.display_name} to ready'
    if event['action'] == 'converted_to_draft':
        pr_obj.draft = True
        return f'Updated {pr_obj.display_name} to draft'

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
            return 'Closed {}'.format(pr_obj.display_name)

        _logger.info(
            '%s tried to close %s (state=%s) but locking failed',
            event['sender']['login'],
            pr_obj.display_name,
            oldstate,
        )
        return 'Ignored: could not lock rows (probably being merged)'

    if event['action'] == 'reopened' :
        if pr_obj.state == 'merged':
            feedback(
                close=True,
                message=env.ref('runbot_merge.handle.pr.merged')._format(event=event),
            )

        if pr_obj.state == 'closed':
            _logger.info('%s reopening %s', event['sender']['login'], pr_obj.display_name)
            pr_obj.write({
                'state': 'opened',
                # updating the head triggers a revalidation
                'head': pr['head']['sha'],
                'squash': pr['commits'] == 1,
            })

            return 'Reopened {}'.format(pr_obj.display_name)

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return "Not handling {} yet".format(event['action'])

def handle_status(env, event):
    _logger.info(
        'status on %(sha)s %(context)s:%(state)s (%(target_url)s) [%(description)r]',
        event
    )
    status_value = json.dumps({
        event['context']: {
            'state': event['state'],
            'target_url': event['target_url'],
            'description': event['description']
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
    """, [event['sha'], status_value])

    return 'ok'

def handle_comment(env, event):
    if 'pull_request' not in event['issue']:
        return "issue comment, ignoring"

    repo = event['repository']['full_name']
    issue = event['issue']['number']
    author = event['comment']['user']['login']
    comment = event['comment']['body']
    _logger.info('comment[%s]: %s %s#%s %r', event['action'], author, repo, issue, comment)
    if event['action'] != 'created':
        return "Ignored: action (%r) is not 'created'" % event['action']

    return _handle_comment(env, repo, issue, event['comment'])

def handle_review(env, event):
    repo = event['repository']['full_name']
    pr = event['pull_request']['number']
    author = event['review']['user']['login']
    comment = event['review']['body'] or ''

    _logger.info('review[%s]: %s %s#%s %r', event['action'], author, repo, pr, comment)
    if event['action'] != 'submitted':
        return "Ignored: action (%r) is not 'submitted'" % event['action']

    return _handle_comment(
        env, repo, pr, event['review'],
        target=event['pull_request']['base']['ref'])

def handle_ping(env, event):
    print("Got ping! {}".format(event['zen']))
    return "pong"

EVENTS = {
    'pull_request': handle_pr,
    'status': handle_status,
    'issue_comment': handle_comment,
    'pull_request_review': handle_review,
    'ping': handle_ping,
}

def _handle_comment(env, repo, issue, comment, target=None):
    repository = env['runbot_merge.repository'].search([('name', '=', repo)])
    if not repository.project_id._find_commands(comment['body'] or ''):
        return "No commands, ignoring"

    pr = env['runbot_merge.pull_requests']._get_or_schedule(repo, issue, target=target)
    if not pr:
        return "Unknown PR, scheduling fetch"

    partner = env['res.partner'].search([('github_login', '=', comment['user']['login'])])
    return pr._parse_commands(partner, comment, comment['user']['login'])
