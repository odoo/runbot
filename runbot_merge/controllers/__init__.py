import hashlib
import hmac
import logging
import json

import werkzeug.exceptions

from odoo.http import Controller, request, route

from . import dashboard

_logger = logging.getLogger(__name__)

class MergebotController(Controller):
    @route('/runbot_merge/hooks', auth='none', type='json', csrf=False, methods=['POST'])
    def index(self):
        req = request.httprequest
        event = req.headers['X-Github-Event']

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
            signature = 'sha1=' + hmac.new(secret.encode('ascii'), req.get_data(), hashlib.sha1).hexdigest()
            if not hmac.compare_digest(signature, req.headers.get('X-Hub-Signature', '')):
                _logger.warning("Ignored hook with incorrect signature %s",
                             req.headers.get('X-Hub-Signature'))
                return werkzeug.exceptions.Forbidden()

        return c(env, request.jsonrequest)

def handle_pr(env, event):
    if event['action'] in [
        'assigned', 'unassigned', 'review_requested', 'review_request_removed',
        'labeled', 'unlabeled'
    ]:
        _logger.debug(
            'Ignoring pull_request[%s] on %s:%s',
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
    branch = env['runbot_merge.branch'].search([
        ('name', '=', b),
        ('project_id', '=', repo.project_id.id),
    ])

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
        source_branch = env['runbot_merge.branch'].search([
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

        updates = {}
        if source_branch != branch:
            updates['target'] = branch.id
            updates['squash'] = pr['commits'] == 1
        if event['changes'].keys() & {'title', 'body'}:
            updates['message'] = "{}\n\n{}".format(pr['title'].strip(), pr['body'].strip())
        if updates:
            pr_obj = find(source_branch)
            pr_obj.write(updates)
            return 'Updated {}'.format(pr_obj.id)
        return "Nothing to update ({})".format(event['changes'].keys())

    if not branch:
        _logger.info("Ignoring PR for un-managed branch %s:%s", r, b)
        return "Not set up to care about {}:{}".format(r, b)

    author_name = pr['user']['login']
    author = env['res.partner'].search([('github_login', '=', author_name)], limit=1)
    if not author:
        author = env['res.partner'].create({
            'name': author_name,
            'github_login': author_name,
        })

    _logger.info("%s: %s:%s (%s) (%s)", event['action'], repo.name, pr['number'], pr['title'].strip(), author.github_login)
    if event['action'] == 'opened':
        pr_obj = env['runbot_merge.pull_requests']._from_gh(pr)
        return "Tracking PR as {}".format(pr_obj.id)

    pr_obj = env['runbot_merge.pull_requests']._get_or_schedule(r, pr['number'])
    if not pr_obj:
        _logger.warning("webhook %s on unknown PR %s:%s, scheduled fetch", event['action'], repo.name, pr['number'])
        return "Unknown PR {}:{}, scheduling fetch".format(repo.name, pr['number'])
    if event['action'] == 'synchronize':
        if pr_obj.head == pr['head']['sha']:
            return 'No update to pr head'

        if pr_obj.state in ('closed', 'merged'):
            _logger.error("Tentative sync to closed PR %s:%s", repo.name, pr['number'])
            return "It's my understanding that closed/merged PRs don't get sync'd"

        if pr_obj.state == 'ready':
            pr_obj.unstage(
                "PR %s:%s updated by %s",
                pr_obj.repository.name, pr_obj.number,
                event['sender']['login']
            )

        _logger.info(
            "PR %s:%s updated to %s by %s, resetting to 'open' and squash=%s",
            pr_obj.repository.name, pr_obj.number,
            pr['head']['sha'], event['sender']['login'],
            pr['commits'] == 1
        )

        pr_obj.write({
            'state': 'opened',
            'head': pr['head']['sha'],
            'squash': pr['commits'] == 1,
        })
        return 'Updated {} to {}'.format(pr_obj.id, pr_obj.head)

    # don't marked merged PRs as closed (!!!)
    if event['action'] == 'closed' and pr_obj.state != 'merged':
        # ignore if the PR is already being updated in a separate transaction
        # (most likely being merged?)
        env.cr.execute('''
        SELECT id, state FROM runbot_merge_pull_requests
        WHERE id = %s AND state != 'merged'
        FOR UPDATE SKIP LOCKED;
        ''', [pr_obj.id])
        res = env.cr.fetchone()
        # FIXME: store some sort of "try to close it later" if the merge fails?
        if not res:
            return 'Ignored: could not lock rows (probably being merged)'

        env.cr.execute('''
        UPDATE runbot_merge_pull_requests
        SET state = 'closed'
        WHERE id = %s AND state != 'merged'
        ''', [pr_obj.id])
        env.cr.commit()
        pr_obj.invalidate_cache(fnames=['state'], ids=[pr_obj.id])
        if env.cr.rowcount:
            env['runbot_merge.pull_requests.tagging'].create({
                'pull_request': pr_obj.number,
                'repository': repo.id,
                'state_from': res[1] if not pr_obj.staging_id else 'staged',
                'state_to': 'closed',
            })
            pr_obj.unstage(
                "PR %s:%s closed by %s",
                pr_obj.repository.name, pr_obj.number,
                event['sender']['login']
            )

        return 'Closed {}'.format(pr_obj.id)

    if event['action'] == 'reopened' and pr_obj.state == 'closed':
        pr_obj.write({
            'state': 'opened',
            # updating the head triggers a revalidation
            'head': pr['head']['sha'],
        })

        return 'Reopened {}'.format(pr_obj.id)

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return "Not handling {} yet".format(event['action'])

def handle_status(env, event):
    _logger.info(
        'status %(context)s:%(state)s on commit %(sha)s (%(target_url)s)',
        event
    )
    Commits = env['runbot_merge.commit']
    env.cr.execute('SELECT id FROM runbot_merge_commit WHERE sha=%s FOR UPDATE', [event['sha']])
    c = Commits.browse(env.cr.fetchone())
    if c:
        c.statuses = json.dumps({
            **json.loads(c.statuses),
            event['context']: {
                'state': event['state'],
                'target_url': event['target_url'],
                'description': event['description']
            }
        })
    else:
        Commits.create({
            'sha': event['sha'],
            'statuses': json.dumps({event['context']: {
                'state': event['state'],
                'target_url': event['target_url'],
                'description': event['description']
            }})
        })

    return 'ok'

def handle_comment(env, event):
    if 'pull_request' not in event['issue']:
        return "issue comment, ignoring"

    repo = event['repository']['full_name']
    issue = event['issue']['number']
    author = event['comment']['user']['login']
    comment = event['comment']['body']
    _logger.info('comment[%s]: %s %s:%s "%s"', event['action'], author, repo, issue, comment)
    if event['action'] != 'created':
        return "Ignored: action (%r) is not 'created'" % event['action']

    return _handle_comment(env, repo, issue, author, comment)

def handle_review(env, event):
    repo = event['repository']['full_name']
    pr = event['pull_request']['number']
    author = event['review']['user']['login']
    comment = event['review']['body'] or ''

    _logger.info('review[%s]: %s %s:%s "%s"', event['action'], author, repo, pr, comment)
    if event['action'] != 'submitted':
        return "Ignored: action (%r) is not 'submitted'" % event['action']

    return _handle_comment(
        env, repo, pr, author, comment,
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

def _handle_comment(env, repo, issue, author, comment, target=None):
    repository = env['runbot_merge.repository'].search([('name', '=', repo)])
    if not repository.project_id._find_commands(comment):
        return "No commands, ignoring"

    pr = env['runbot_merge.pull_requests']._get_or_schedule(repo, issue, target=target)
    if not pr:
        return "Unknown PR, scheduling fetch"

    partner = env['res.partner'].search([('github_login', '=', author), ])
    return pr._parse_commands(partner, comment, author)
