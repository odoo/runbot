import logging
import json

from odoo.http import Controller, request, route

_logger = logging.getLogger(__name__)

class MergebotController(Controller):
    @route('/runbot_merge/hooks', auth='none', type='json', csrf=False, methods=['POST'])
    def index(self):
        event = request.httprequest.headers['X-Github-Event']

        return EVENTS.get(event, lambda _: "Unknown event {}".format(event))(request.jsonrequest)

def handle_pr(event):
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

    env = request.env(user=1)
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
        source = event['changes'].get('base', {'from': pr['base']})['from']['ref']
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
            return handle_pr(dict(event, action='opened'))

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

    _logger.info("%s: %s:%s (%s)", event['action'], repo.name, pr['number'], author.github_login)
    if event['action'] == 'opened':
        # some PRs have leading/trailing newlines in body/title (resp)
        title = pr['title'].strip()
        body = pr['body'].strip()
        pr_obj = env['runbot_merge.pull_requests'].create({
            'number': pr['number'],
            'label': pr['head']['label'],
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': pr['head']['sha'],
            'squash': pr['commits'] == 1,
            'message': '{}\n\n{}'.format(title, body),
        })
        return "Tracking PR as {}".format(pr_obj.id)

    pr_obj = find(branch)
    if not pr_obj:
        _logger.warn("webhook %s on unknown PR %s:%s", event['action'], repo.name, pr['number'])
        return "Unknown PR {}:{}".format(repo.name, pr['number'])
    if event['action'] == 'synchronize':
        if pr_obj.head == pr['head']['sha']:
            return 'No update to pr head'

        if pr_obj.state in ('closed', 'merged'):
            pr_obj.repository.github().comment(
                pr_obj.number, "This pull request is closed, ignoring the update to {}".format(pr['head']['sha']))
            # actually still update the head of closed (but not merged) PRs
            if pr_obj.state == 'merged':
                return 'Ignoring update to {}'.format(pr_obj.id)

        if pr_obj.state == 'validated':
            pr_obj.state = 'opened'
        elif pr_obj.state == 'ready':
            pr_obj.state = 'approved'
            pr_obj.staging_id.cancel(
                "Updated PR %s:%s, removing staging %s",
                pr_obj.repository.name, pr_obj.number,
                pr_obj.staging_id,
            )

        # TODO: should we update squash as well? What of explicit squash commands?
        pr_obj.head = pr['head']['sha']
        return 'Updated {} to {}'.format(pr_obj.id, pr_obj.head)

    # don't marked merged PRs as closed (!!!)
    if event['action'] == 'closed' and pr_obj.state != 'merged':
        pr_obj.state = 'closed'
        return 'Closed {}'.format(pr_obj.id)

    if event['action'] == 'reopened' and pr_obj.state == 'closed':
        pr_obj.state = 'opened'
        return 'Reopened {}'.format(pr_obj.id)

    _logger.info("Ignoring event %s on PR %s", event['action'], pr['number'])
    return "Not handling {} yet".format(event['action'])

def handle_status(event):
    _logger.info(
        'status %s:%s on commit %s',
        event['context'], event['state'],
        event['sha'],
    )
    Commits = request.env(user=1)['runbot_merge.commit']
    c = Commits.search([('sha', '=', event['sha'])])
    if c:
        c.statuses = json.dumps({
            **json.loads(c.statuses),
            event['context']: event['state']
        })
    else:
        Commits.create({
            'sha': event['sha'],
            'statuses': json.dumps({event['context']: event['state']})
        })

    return 'ok'

def handle_comment(event):
    if 'pull_request' not in event['issue']:
        return "issue comment, ignoring"

    env = request.env(user=1)
    partner = env['res.partner'].search([('github_login', '=', event['sender']['login']),])
    pr = env['runbot_merge.pull_requests'].search([
        ('number', '=', event['issue']['number']),
        ('repository.name', '=', event['repository']['full_name']),
    ])
    if not partner:
        _logger.info("ignoring comment from %s: not in system", event['sender']['login'])
        return 'ignored'

    return pr._parse_commands(partner, event['comment']['body'])

def handle_ping(event):
    print("Got ping! {}".format(event['zen']))
    return "pong"

EVENTS = {
    # TODO: add review?
    'pull_request': handle_pr,
    'status': handle_status,
    'issue_comment': handle_comment,
    'ping': handle_ping,
}
