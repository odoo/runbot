import pytest

from utils import seen, Commit, pr_page, to_pr


def test_existing_pr_disabled_branch(env, project, repo, config, users, page, botname):
    """ PRs to disabled branches are ignored, but what if the PR exists *before*
    the branch is disabled?
    """
    # run crons from template to clean up the queue before possibly creating
    # new work
    assert env['base'].run_crons()

    project.write({'branch_ids': [
        (1, project.branch_ids.id, {'sequence': 0}),
        (0, 0, {'name': 'other', 'sequence': 1}),
        (0, 0, {'name': 'other2', 'sequence': 2}),
    ]})

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')
        repo.make_commits(m, Commit('other2', tree={'c': '1'}), ref='heads/other2')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
        repo.post_status(c, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    branch_id = pr_id.target
    assert pr_id.staging_id
    staging_id = branch_id.active_staging_id
    assert staging_id == pr_id.staging_id

    # staging of `pr` should have generated a staging branch
    _ = repo.get_ref('staging.other')

    # disable branch "other"
    branch_id.active = False
    env.run_crons()

    # triggered cleanup should have deleted the staging for the disabled `other`
    # target branch
    with pytest.raises(AssertionError, match=r'Not Found'):
        repo.get_ref('staging.other')

    # the PR should not have been closed implicitly
    assert pr_id.state == 'ready'
    # but it should be unstaged
    assert not pr_id.staging_id

    assert not branch_id.active_staging_id
    assert staging_id.state == 'cancelled', \
        "closing the PRs should have canceled the staging"
    assert staging_id.reason == "Target branch deactivated by 'admin'."

    p = pr_page(page, pr)
    [target] = p.cssselect('table tr.bg-info')
    assert 'inactive' in target.classes
    assert target[0].text_content() == "other"
    env.run_crons()
    assert pr.comments == [
        (users['reviewer'], "hansen r+"),
        (botname, seen(env, pr)),
        (botname, "@%(user)s @%(reviewer)s the target branch 'other' has been disabled, you may want to close this PR." % users),
    ]

    with repo:
        [c2] = repo.make_commits(ot, Commit('wheee', tree={'b': '3'}), ref=pr.ref, make=False)
    env.run_crons()
    assert pr.comments[3] == (
        botname,
        "This PR targets the disabled branch {repository}:{target}, it needs to be retargeted before it can be merged.".format(
            repository=repo.name,
            target="other",
        )
    )
    assert pr_id.head == c2, "pr should be aware of its update"

    with repo:
        pr.base = 'other2'
        repo.post_status(c2, 'success')
        pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr.comments[4:] == [
        (users['reviewer'], 'hansen rebase-ff r+'),
        (botname, "Merge method set to rebase and fast-forward."),
    ]

    assert pr_id.state == 'ready'
    assert pr_id.target == env['runbot_merge.branch'].search([('name', '=', 'other2')])
    assert pr_id.staging_id

    # staging of `pr` should have generated a staging branch
    _ = repo.get_ref('heads/staging.other2')

def test_new_pr_no_branch(env, project, repo, users, botname):
    """ A new PR to an *unknown* branch should be ignored and warn
    """

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
    env.run_crons()

    # the PR should not have been created in the backend
    with pytest.raises(TimeoutError):
        to_pr(env, pr, attempts=1)
    assert pr.comments == [
        (botname, "This PR targets the un-managed branch %s:other, it needs to be retargeted before it can be merged." % repo.name),
    ]

def test_new_pr_disabled_branch(env, project, repo, users, botname):
    """ A new PR to a *disabled* branch should be accepted (rather than ignored)
    but should warn
    """
    project.write({'branch_ids': [(0, 0, {'name': 'other', 'active': False})]})

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')
        [ot] = repo.make_commits(m, Commit('other', tree={'b': '1'}), ref='heads/other')

        [c] = repo.make_commits(ot, Commit('wheee', tree={'b': '2'}))
        pr = repo.make_pr(title="title", body='body', target='other', head=c)
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id, "the PR should have been created in the backend"
    assert pr_id.state == 'opened'
    assert pr.comments == [
        (botname, "This PR targets the disabled branch %s:other, it needs to be retargeted before it can be merged." % repo.name),
        (botname, seen(env, pr)),
    ]

def test_review_disabled_branch(env, project, repo, users, config, botname):
    with repo:
        [m] = repo.make_commits(None, Commit("init", tree={'m': 'm'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('pr', tree={'m': 'n'}))
        pr = repo.make_pr(target="master", head=c)
    env.run_crons()
    target = project.branch_ids
    target.active = False
    env.run_crons()
    with repo:
        pr.post_comment("A normal comment", config['role_other']['token'])
    with repo:
        pr.post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()

    assert pr.comments == [
        (botname, seen(env, pr)),
        (botname, "@{user} the target branch {target!r} has been disabled, you may want to close this PR.".format(
            **users,
            target=target.name,
        )),
        (users['other'], "A normal comment"),
        (users['reviewer'], "hansen r+"),
        (botname, "This PR targets the disabled branch {repository}:{target}, it needs to be retargeted before it can be merged.".format(
            repository=repo.name,
            target=target.name,
        )),
    ]
