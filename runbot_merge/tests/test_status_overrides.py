import json

import pytest

from utils import seen, Commit


def test_no_duplicates(env):
    """ Should not have two override records for the same (context, repo)
    """
    Overrides = env['res.partner.override']
    Overrides.create({'context': 'a'})
    with pytest.raises(Exception, match=r'already exists'):
        Overrides.create({'context': 'a'})

def name_search(Model, name):
    """ Convenience function to return a recordset instead of a craplist
    """
    return Model.browse(id_ for id_, _ in Model.name_search(name))
def test_finding(env):
    project = env['runbot_merge.project'].create({
        'name': 'test',
        'github_token': 'xxx', 'github_prefix': 'no',
    })
    repo_1 = env['runbot_merge.repository'].create({'project_id': project.id, 'name': 'r1'})
    repo_2 = env['runbot_merge.repository'].create({'project_id': project.id, 'name': 'r2'})

    Overrides = env['res.partner.override']
    a = Overrides.create({'context': 'ctx1'})
    b = Overrides.create({'context': 'ctx1', 'repository_id': repo_1.id})
    c = Overrides.create({'context': 'ctx1', 'repository_id': repo_2.id})
    d = Overrides.create({'context': 'ctx2', 'repository_id': repo_2.id})

    assert name_search(Overrides, 'ctx1') == a|b|c
    assert name_search(Overrides, 'ctx') == a|b|c|d
    assert name_search(Overrides, 'r2') == c|d

def test_basic(env, project, make_repo, users, setreviewers, config):
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
        seen(env, pr, users),
        (users['reviewer'], 'hansen override=l/int'),
        (users['user'], "I'm sorry, @{}: you are not allowed to override this status.".format(users['reviewer'])),
        (users['other'], "hansen override=l/int"),
    ]
    assert pr_id.statuses == '{}'
    assert json.loads(pr_id.overrides) == {'l/int': {
        'state': 'success',
        'target_url': comments[-1]['html_url'],
        'description': 'Overridden by @{}'.format(users['other']),
    }}

def test_multiple(env, project, make_repo, users, setreviewers, config):
    """ Test that overriding multiple statuses in the same comment works
    """

    repo = make_repo('repo')
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'l/int'}), (0, 0, {'context': 'c/i'})]
    })
    setreviewers(*project.repo_ids)
    # "other" can override the lints
    env['res.partner'].create({
        'name': config['role_other'].get('name', 'Other'),
        'github_login': users['other'],
        'override_rights': [(0, 0, {
            'repository_id': repo_id.id,
            'context': 'l/int',
        }), (0, 0, {
            'repository_id': repo_id.id,
            'context': 'c/i',
        })]
    })

    with repo:
        root = repo.make_commits(None, Commit('root', tree={'a': '0'}), ref='heads/master')
    for i, comment in enumerate([
        # style 1: multiple commands inline
        'hansen override=l/int override=c/i',
        # style 2: multiple parameters to command
        'hansen override=l/int,c/i',
        # style 3: multiple commands each on its own line
        'hansen override=l/int\nhansen override=c/i',
    ], start=1):
        with repo:
            repo.make_commits(root, Commit(f'pr{i}', tree={'a': f'{i}'}), ref=f'heads/change{i}')
            pr = repo.make_pr(target='master', title=f'super change {i}', head=f'change{i}')
        env.run_crons()

        pr_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr.number)
        ])
        assert pr_id.state == 'opened'

        with repo:
            pr.post_comment(comment, config['role_other']['token'])
        env.run_crons()
        assert pr_id.state == 'validated'

        comments = pr.comments
        assert pr_id.statuses == '{}'
        assert json.loads(pr_id.overrides) == {
            'l/int': {
                'state': 'success',
                'target_url': comments[-1]['html_url'],
                'description': 'Overridden by @{}'.format(users['other']),
            },
            'c/i': {
                'state': 'success',
                'target_url': comments[-1]['html_url'],
                'description': 'Overridden by @{}'.format(users['other']),
            },
        }

def test_no_repository(env, project, make_repo, users, setreviewers, config):
    """ A repo missing from an override allows overriding the status in every repo
    """
    repo = make_repo('repo')
    env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'l/int'})]
    })
    setreviewers(*project.repo_ids)
    # "other" can override the lint
    env['res.partner'].create({
        'name': config['role_other'].get('name', 'Other'),
        'github_login': users['other'],
        'override_rights': [(0, 0, {'context': 'l/int'})]
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
        pr.post_comment('hansen override=l/int', config['role_other']['token'])
    env.run_crons()
    assert pr_id.state == 'ready'

    comments = pr.comments
    assert comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr, users),
        (users['other'], "hansen override=l/int"),
    ]
    assert pr_id.statuses == '{}'
    assert json.loads(pr_id.overrides) == {'l/int': {
        'state': 'success',
        'target_url': comments[-1]['html_url'],
        'description': 'Overridden by @{}'.format(users['other']),
    }}
