from utils import Commit, to_pr


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

def test_name_search(env):
    """ PRs should be findable by:

    * number
    * display_name (`repository#number`)
    * label

    This way we can find parents or sources by these informations.
    """
    p = env['runbot_merge.project'].create({
        'name': 'proj',
        'github_token': 'no',
    })
    b = env['runbot_merge.branch'].create({
        'name': 'target',
        'project_id': p.id
    })
    r = env['runbot_merge.repository'].create({
        'name': 'repo',
        'project_id': p.id,
    })

    baseline = {'target': b.id, 'repository': r.id}
    PRs = env['runbot_merge.pull_requests']
    prs = PRs.create({**baseline, 'number': 1964, 'label': 'victor:thump', 'head': 'a', 'message': 'x'})\
        | PRs.create({**baseline, 'number': 1959, 'label': 'marcus:frankenstein', 'head': 'b', 'message': 'y'})\
        | PRs.create({**baseline, 'number': 1969, 'label': 'victor:patch-1', 'head': 'c', 'message': 'z'})
    pr0, pr1, pr2 = prs.name_get()

    assert PRs.name_search('1964') == [pr0]
    assert PRs.name_search('1969') == [pr2]

    assert PRs.name_search('frank') == [pr1]
    assert PRs.name_search('victor') == [pr2, pr0]

    assert PRs.name_search('thump') == [pr0]

    assert PRs.name_search('repo') == [pr2, pr0, pr1]
    assert PRs.name_search('repo#1959') == [pr1]

def test_message_desync(env, project, make_repo, users, setreviewers, config):
    """If the PR message gets desync'd (github misses sending an update), the
    merge message should still match what's on github rather than what's in the
    db
    """
    repo = make_repo('repo')
    env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        [c] = repo.make_commits('master', Commit('whee', tree={'b': '2'}))
        pr = repo.make_pr(title='title', body='body', target='master', head=c)
        repo.post_status(c, 'success', 'status')
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.message == 'title\n\nbody'
    pr_id.message = "xxx"

    with repo:
        pr.post_comment('hansen merge r+', config['role_reviewer']['token'])
    env.run_crons()

    st = repo.commit('staging.master')
    assert st.message.startswith('title\n\nbody'),\
        "the stored PR message should have been ignored when staging"
    assert st.parents == [m, c], "check the staging's ancestry is the right one"
