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
            _logger.warn('Unknown event %s', event)
            return 'Unknown event {}'.format(event)

        repo = request.jsonrequest['repository']['full_name']
        env = request.env(user=1)

        secret = env['runbot_merge.repository'].search([
            ('name', '=', repo),
        ]).project_id.secret
        if secret:
            signature = 'sha1=' + hmac.new(secret.encode('ascii'), req.get_data(), hashlib.sha1).hexdigest()
            if not hmac.compare_digest(signature, req.headers.get('X-Hub-Signature', '')):
                _logger.warn("Ignored hook with incorrect signature %s",
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
        # some PRs have leading/trailing newlines in body/title (resp)
        message = pr['title'].strip()
        body = pr['body'] and pr['body'].strip()
        if body:
            message += '\n\n' + body
        pr_obj = env['runbot_merge.pull_requests'].create({
            'number': pr['number'],
            'label': pr['head']['label'],
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': pr['head']['sha'],
            'squash': pr['commits'] == 1,
            'message': message,
        })
        return "Tracking PR as {}".format(pr_obj.id)

    pr_obj = env['runbot_merge.pull_requests']._get_or_schedule(r, pr['number'])
    if not pr_obj:
        _logger.warn("webhook %s on unknown PR %s:%s, scheduled fetch", event['action'], repo.name, pr['number'])
        return "Unknown PR {}:{}, scheduling fetch".format(repo.name, pr['number'])
    if event['action'] == 'synchronize':
        if pr_obj.head == pr['head']['sha']:
            return 'No update to pr head'

        if pr_obj.state in ('closed', 'merged'):
            _logger.error("Tentative sync to closed PR %s:%s", repo.name, pr['number'])
            return "It's my understanding that closed/merged PRs don't get sync'd"

        if pr_obj.state == 'ready':
            pr_obj.staging_id.cancel(
                "PR %s:%s updated by %s",
                pr_obj.repository.name, pr_obj.number,
                event['sender']['login']
            )
        if pr_obj.state != 'error':
            pr_obj.state = 'opened'

        pr_obj.head = pr['head']['sha']
        pr_obj.squash = pr['commits'] == 1
        return 'Updated {} to {}'.format(pr_obj.id, pr_obj.head)

    # don't marked merged PRs as closed (!!!)
    if event['action'] == 'closed' and pr_obj.state != 'merged':
        pr_obj.state = 'closed'
        pr_obj.staging_id.cancel(
            "PR %s:%s closed by %s",
            pr_obj.repository.name, pr_obj.number,
            event['sender']['login']
        )
        return 'Closed {}'.format(pr_obj.id)

    if event['action'] == 'reopened' and pr_obj.state == 'closed':
        pr_obj.state = 'opened'
        return 'Reopened {}'.format(pr_obj.id)

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return "Not handling {} yet".format(event['action'])

def handle_status(env, event):
    _logger.info(
        'status %(context)s:%(state)s on commit %(sha)s (%(target_url)s)',
        event
    )
    Commits = env['runbot_merge.commit']
    c = Commits.search([('sha', '=', event['sha'])])
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
    author = event['sender']['login']
    comment = event['comment']['body']
    _logger.info('comment: %s %s:%s "%s"', author, repo, issue, comment)

    partner = env['res.partner'].search([('github_login', '=', author), ])
    if not partner:
        _logger.info("ignoring comment from %s: not in system", author)
        return 'ignored'

    repository = env['runbot_merge.repository'].search([('name', '=', repo)])
    if not repository.project_id._find_commands(comment):
        return "No commands, ignoring"

    pr = env['runbot_merge.pull_requests']._get_or_schedule(repo, issue)
    if not pr:
        return "Unknown PR, scheduling fetch"

    return pr._parse_commands(partner, comment)

def handle_review(env, event):
    partner = env['res.partner'].search([('github_login', '=', event['review']['user']['login'])])
    if not partner:
        _logger.info('ignoring comment from %s: not in system', event['review']['user']['login'])
        return 'ignored'

    pr = env['runbot_merge.pull_requests']._get_or_schedule(
        event['repository']['full_name'],
        event['pull_request']['number'],
        event['pull_request']['base']['ref']
    )
    if not pr:
        return "Unknown PR, scheduling fetch"

    firstline = ''
    state = event['review']['state'].lower()
    if state == 'approved':
        firstline = pr.repository.project_id.github_prefix + ' r+'
    elif state == 'request_changes':
        firstline = pr.repository.project_id.github_prefix + ' r-'

    body = event['review']['body']
    return pr._parse_commands(partner, firstline + (('\n' + body) if body else ''))

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
