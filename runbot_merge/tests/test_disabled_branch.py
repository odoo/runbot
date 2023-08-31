import pytest

from utils import seen, Commit, pr_page

def test_existing_pr_disabled_branch(env, project, make_repo, setreviewers, config, users, page):
    """ PRs to disabled branches are ignored, but what if the PR exists *before*
    the branch is disabled?
    """
    # run crons from template to clean up the queue before possibly creating
    # new work
    assert env['base'].run_crons()

    repo = make_repo('repo')
    project.branch_ids.sequence = 0
    project.write({'branch_ids': [
        (0, 0, {'name': 'other', 'sequence': 1}),
        (0, 0, {'name': 'other2', 'sequence': 2}),
    ]})
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'status'})],
        'group_id': False,
    })
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')
        repo.make_commits(m, Commit('other2', tree={'c': '1'}), ref='heads/other2')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
        repo.post_status(c, 'success', 'status')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository', '=', repo_id.id),
        ('number', '=', pr.number),
    ])
    branch_id = pr_id.target
    assert pr_id.staging_id
    staging_id = branch_id.active_staging_id
    assert staging_id == pr_id.staging_id

    # disable branch "other"
    branch_id.active = False
    env.run_crons()

    # the PR should not have been closed implicitly
    assert pr_id.state == 'ready'
    # but it should be unstaged
    assert not pr_id.staging_id

    assert not branch_id.active_staging_id
    assert staging_id.state == 'cancelled', \
        "closing the PRs should have canceled the staging"
    assert staging_id.reason == "Target branch deactivated by 'admin'."

    p = pr_page(page, pr)
    target = dict(zip(
        (e.text for e in p.cssselect('dl.runbot-merge-fields dt')),
        (p.cssselect('dl.runbot-merge-fields dd'))
    ))['target']
    assert target.text_content() == 'other (inactive)'
    assert target.get('class') == 'text-muted bg-warning'

    assert pr.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr, users),
        (users['user'], "@%(user)s @%(reviewer)s the target branch 'other' has been disabled, you may want to close this PR." % users),
    ]

    with repo:
        [c2] = repo.make_commits(ot, Commit('wheee', tree={'b': '3'}))
        repo.update_ref(pr.ref, c2, force=True)
    assert pr_id.head == c2, "pr should be aware of its update"

    with repo:
        pr.base = 'other2'
        repo.post_status(c2, 'success', 'status')
        pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr_id.state == 'ready'
    assert pr_id.target == env['runbot_merge.branch'].search([('name', '=', 'other2')])
    assert pr_id.staging_id

    # staging of `pr` should have generated a staging branch
    _ = repo.get_ref('heads/staging.other')
    # stagings should not need a tmp branch anymore, so this should not exist
    with pytest.raises(AssertionError, match=r'Not Found'):
        repo.get_ref('heads/tmp.other')

    assert env['base'].run_crons()

    # triggered cleanup should have deleted the staging for the disabled `other`
    # target branch
    with pytest.raises(AssertionError, match=r'Not Found'):
        repo.get_ref('heads/staging.other')

def test_new_pr_no_branch(env, project, make_repo, setreviewers, users):
    """ A new PR to an *unknown* branch should be ignored and warn
    """
    repo = make_repo('repo')
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
    env.run_crons()

    assert not env['runbot_merge.pull_requests'].search([
        ('repository', '=', repo_id.id),
        ('number', '=', pr.number),
    ]), "the PR should not have been created in the backend"
    assert pr.comments == [
        (users['user'], "This PR targets the un-managed branch %s:other, it needs to be retargeted before it can be merged." % repo.name),
    ]

def test_new_pr_disabled_branch(env, project, make_repo, setreviewers, users):
    """ A new PR to a *disabled* branch should be accepted (rather than ignored)
    but should warn
    """
    repo = make_repo('repo')
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    env['runbot_merge.branch'].create({
        'project_id': project.id,
        'name': 'other',
        'active': False,
    })
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
    env.run_crons()

    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository', '=', repo_id.id),
        ('number', '=', pr.number),
    ])
    assert pr_id, "the PR should have been created in the backend"
    assert pr_id.state == 'opened'
    assert pr.comments == [
        (users['user'], "This PR targets the disabled branch %s:other, it needs to be retargeted before it can be merged." % repo.name),
        seen(env, pr, users),
    ]
