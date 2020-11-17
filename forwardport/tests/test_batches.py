from utils import Commit, make_basic


def test_single_updated(env, config, make_repo):
    """ Given co-dependent PRs getting merged, one of them being modified should
    lead to a restart of the merge & forward port process.

    See test_update_pr for a simpler (single-PR) version
    """
    r1, _ = make_basic(env, config, make_repo, reponame='repo-1')
    r2, _ = make_basic(env, config, make_repo, reponame='repo-2')

    with r1:
        r1.make_commits('a', Commit('1', tree={'1': '0'}), ref='heads/aref')
        pr1 = r1.make_pr(target='a', head='aref')
        r1.post_status('aref', 'success', 'legal/cla')
        r1.post_status('aref', 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with r2:
        r2.make_commits('a', Commit('2', tree={'2': '0'}), ref='heads/aref')
        pr2 = r2.make_pr(target='a', head='aref')
        r2.post_status('aref', 'success', 'legal/cla')
        r2.post_status('aref', 'success', 'ci/runbot')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.a', 'success', 'legal/cla')
        r1.post_status('staging.a', 'success', 'ci/runbot')
        r2.post_status('staging.a', 'success', 'legal/cla')
        r2.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr1_id, pr11_id, pr2_id, pr21_id = pr_ids = env['runbot_merge.pull_requests'].search([]).sorted('display_name')
    assert pr1_id.number == pr1.number
    assert pr2_id.number == pr2.number
    assert pr1_id.state == pr2_id.state == 'merged'

    assert pr11_id.parent_id == pr1_id
    assert pr11_id.repository.name == pr1_id.repository.name == r1.name

    assert pr21_id.parent_id == pr2_id
    assert pr21_id.repository.name == pr2_id.repository.name == r2.name

    assert pr11_id.target.name == pr21_id.target.name == 'b'

    # don't even bother faking CI failure, straight update pr21_id
    repo, ref = r2.get_pr(pr21_id.number).branch
    with repo:
        repo.make_commits(
            pr21_id.target.name,
            Commit('Whops', tree={'2': '1'}),
            ref='heads/' + ref,
            make=False
        )
    env.run_crons()

    assert not pr21_id.parent_id

    with r1, r2:
        r1.post_status(pr11_id.head, 'success', 'legal/cla')
        r1.post_status(pr11_id.head, 'success', 'ci/runbot')
        r1.get_pr(pr11_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
        r2.post_status(pr21_id.head, 'success', 'legal/cla')
        r2.post_status(pr21_id.head, 'success', 'ci/runbot')
        r2.get_pr(pr21_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    prs_again = env['runbot_merge.pull_requests'].search([])
    assert prs_again == pr_ids,\
        "should not have created FP PRs as we're now in a detached (iso new PR) state " \
        "(%s)" % prs_again.mapped('display_name')

    with r1, r2:
        r1.post_status('staging.b', 'success', 'legal/cla')
        r1.post_status('staging.b', 'success', 'ci/runbot')
        r2.post_status('staging.b', 'success', 'legal/cla')
        r2.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    new_prs = env['runbot_merge.pull_requests'].search([]).sorted('display_name') - pr_ids
    assert len(new_prs) == 2, "should have created the new FP PRs"
    pr12_id, pr22_id = new_prs

    assert pr12_id.source_id == pr1_id
    assert pr12_id.parent_id == pr11_id

    assert pr22_id.source_id == pr2_id
    assert pr22_id.parent_id == pr21_id
