from utils import seen, Commit

def test_existing_pr_disabled_branch(env, project, make_repo, setreviewers, config, users):
    """ PRs to disabled branches are ignored, but what if the PR exists *before*
    the branch is disabled?
    """
    repo = make_repo('repo')
    project.branch_ids.sequence = 0
    project.write({'branch_ids': [
        (0, 0, {'name': 'other', 'sequence': 1}),
        (0, 0, {'name': 'other2', 'sequence': 2}),
    ]})
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')
        repo.make_commits(m, Commit('other2', tree={'c': '1'}), ref='heads/other2')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
        repo.post_status(c, 'success', 'status')

    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository', '=', repo_id.id),
        ('number', '=', pr.number),
    ])

    # disable branch "other"
    project.branch_ids.filtered(lambda b: b.name == 'other').active = False

    # r+ the PR
    with repo:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # nothing should happen, the PR should be unstaged forever, maybe?
    assert pr_id.state == 'ready'
    assert not pr_id.staging_id

    with repo:
        [c2] = repo.make_commits(ot, Commit('wheee', tree={'b': '3'}))
        repo.update_ref(pr.ref, c2, force=True)
    assert pr_id.head == c2, "pr should be aware of its update"

    with repo:
        pr.close()
    assert pr_id.state == 'closed', "pr should be closeable"
    with repo:
        pr.open()
    assert pr_id.state == 'opened', "pr should be reopenable (state reset due to force push"
    env.run_crons()
    assert pr.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr, users),
        (users['user'], "This PR targets the disabled branch %s:other, it can not be merged." % repo.name),
    ], "reopening a PR to an inactive branch should send feedback, but not closing it"

    with repo:
        pr.base = 'other2'
        repo.post_status(c2, 'success', 'status')
        pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr_id.state == 'ready'
    assert pr_id.target == env['runbot_merge.branch'].search([('name', '=', 'other2')])
    assert pr_id.staging_id


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
        (users['user'], "This PR targets the un-managed branch %s:other, it can not be merged." % repo.name),
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
        (users['user'], "This PR targets the disabled branch %s:other, it can not be merged." % repo.name),
        seen(env, pr, users),
    ]

def test_retarget_from_disabled(env, make_repo, project, setreviewers):
    """ Retargeting a PR from a disabled branch should not duplicate the PR
    """
    repo = make_repo('repo')
    project.write({'branch_ids': [(0, 0, {'name': '1.0'}), (0, 0, {'name': '2.0'})]})
    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'legal/cla,ci/runbot',
    })
    setreviewers(repo_id)

    with repo:
        [c0] = repo.make_commits(None, Commit('0', tree={'a': '0'}), ref='heads/1.0')
        [c1] = repo.make_commits(c0, Commit('1', tree={'a': '1'}), ref='heads/2.0')
        repo.make_commits(c1, Commit('2', tree={'a': '2'}), ref='heads/master')

        # create PR on 1.0
        repo.make_commits(c0, Commit('c', tree={'a': '0', 'b': '0'}), ref='heads/changes')
        prx = repo.make_pr(head='changes', target='1.0')
    branch_1 = project.branch_ids.filtered(lambda b: b.name == '1.0')
    # there should only be a single PR in the system at this point
    [pr] = env['runbot_merge.pull_requests'].search([])
    assert pr.target == branch_1

    # branch 1 is EOL, disable it
    branch_1.active = False

    with repo:
        # we forgot we had active PRs for it, and we may want to merge them
        # still, retarget them!
        prx.base = '2.0'

    # check that we still only have one PR in the system
    [pr_] = env['runbot_merge.pull_requests'].search([])
    # and that it's the same as the old one, just edited with a new target
    assert pr_ == pr
    assert pr.target == project.branch_ids.filtered(lambda b: b.name == '2.0')
