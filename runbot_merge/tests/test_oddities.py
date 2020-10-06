import json

from utils import Commit


def test_partner_merge(env):
    p_src = env['res.partner'].create({
        'name': 'kfhsf',
        'github_login': 'tyu'
    }) |  env['res.partner'].create({
        'name': "xxx",
        'github_login': 'xxx'
    })
    # proper login with useful info
    p_dest = env['res.partner'].create({
        'name': 'Partner P. Partnersson',
        'github_login': ''
    })

    env['base.partner.merge.automatic.wizard'].create({
        'state': 'selection',
        'partner_ids': (p_src + p_dest).ids,
        'dst_partner_id': p_dest.id,
    })._call('action_merge')
    assert not p_src.exists()
    assert p_dest.name == 'Partner P. Partnersson'
    assert p_dest.github_login == 'xxx'

def test_override(env, project, make_repo, users, setreviewers, config):
    """
    Test that we can override a status on a PR:

    * @mergebot override context=status
    * target url should be the comment (?)
    * description should be overridden by <user>
    """
    repo = make_repo('repo')
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'l/int'})]
    })
    setreviewers(*project.repo_ids)
    # "other" can override the lint
    env['res.partner'].create({
        'name': config['role_other'].get('name', 'Other'),
        'github_login': users['other'],
        'override_rights': [(0, 0, {
            'repository_id': repo_id.id,
            'context': 'l/int',
        })]
    })

    with repo:
        m = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        repo.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repo.make_pr(target='master', title='super change', head='change')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr.number)
    ])
    assert pr_id.state == 'approved'

    with repo:
        pr.post_comment('hansen override=l/int', config['role_reviewer']['token'])
    env.run_crons()
    assert pr_id.state == 'approved'

    with repo:
        pr.post_comment('hansen override=l/int', config['role_other']['token'])
    env.run_crons()
    assert pr_id.state == 'ready'

    comments = pr.comments
    assert comments == [
        (users['reviewer'], 'hansen r+'),
        (users['reviewer'], 'hansen override=l/int'),
        (users['user'], "I'm sorry, @{}. You are not allowed to override this status.".format(users['reviewer'])),
        (users['other'], "hansen override=l/int"),
    ]
    assert pr_id.statuses == '{}'
    assert json.loads(pr_id.overrides) == {'l/int': {
        'state': 'success',
        'target_url': comments[-1]['html_url'],
        'description': 'Overridden by @{}'.format(users['other']),
    }}

