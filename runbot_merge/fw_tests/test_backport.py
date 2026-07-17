from xmlrpc.client import Fault

import pytest

from utils import make_basic, Commit, to_pr, seen


@pytest.fixture
def repo(env, config, make_repo):
    repo, _ = make_basic(env, config, make_repo, statuses="default")
    return repo

@pytest.fixture
def pr_id(env, repo, config):
    with repo:
        repo.make_commits('c', Commit("c", tree={'x': '1'}), ref='heads/aref')
        pr = repo.make_pr(target='c', head='aref')
        repo.post_status('aref', 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with repo:
        repo.post_status('staging.c', 'success')
    env.run_crons()
    pr_id = to_pr(env, pr)
    assert pr_id.merge_date
    return pr_id

@pytest.fixture
def backport_id(env, pr_id):
    action = pr_id.backport()
    backport_id = env[action['res_model']].browse([action['res_id']])
    assert backport_id._name == 'runbot_merge.pull_requests.backport'
    assert backport_id
    return backport_id

def test_golden_path(env, repo, config, pr_id, backport_id, users, botname):
    branch_a, branch_b, _branch_c = env['runbot_merge.branch'].search([], order='name')
    backport_id.target = branch_a.id
    act2 = backport_id.action_apply()
    env.run_crons()  # run cron to update labels

    _, bp_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert bp_id.limit_id == branch_b
    assert bp_id._name == act2['res_model']
    assert bp_id.id == act2['res_id']
    bp_head = repo.commit(bp_id.head)
    assert repo.read_tree(bp_head) == {
        'f': 'e',
        'x': '1',
    }
    assert bp_head.message == f"""c

X-original-commit: {pr_id.head}\
"""
    assert bp_id.message == f"c\n\nBackport of {pr_id.display_name}"
    assert repo.get_pr(bp_id.number).labels == {"backport"}

    # check that the backport can actually be merged and forward-ports successfully...
    with repo:
        repo.post_status(bp_id.head, 'success')
        repo.get_pr(bp_id.number).post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()
    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()
    _pr, _backport, fw_id = env['runbot_merge.pull_requests'].search([], order='number')
    fw_pr = repo.get_pr(fw_id.number)
    assert fw_pr.comments == [
        (botname, seen(env, fw_pr)),
        (botname, '''\
@{fp} @{reviewer} this PR targets b and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''.format_map(users)),
    ]

def test_conflict(env, repo, config, backport_id):
    with repo:
        repo.make_commits('a', Commit('conflict', tree={'x': '-1'}), ref='heads/a', make=False)

    branch_a, _branch_b, _branch_c = env['runbot_merge.branch'].search([], order='name')
    backport_id.target = branch_a.id
    with pytest.raises(Fault) as exc:
        backport_id.action_apply()
    assert exc.value.faultString == """\
backport conflict:

Auto-merging x
CONFLICT (add/add): Merge conflict in x
"""

def test_target_error(env, config, backport_id):
    branch_a, _branch_b, branch_c = env['runbot_merge.branch'].search([], order='name')
    with pytest.raises(Fault) as exc:
        backport_id.action_apply()
    assert exc.value.faultString == "A backport needs a backport target"

    backport_id.target = branch_c.id
    with pytest.raises(Fault) as exc:
        backport_id.action_apply()
    assert exc.value.faultString == "The backport branch needs to be before the source's branch (got 'c' and 'c')"

    backport_id.target = branch_a.id
    backport_id.action_apply()

@pytest.mark.skip(
    reason="Currently no way to make just the PR creation fail, swapping the "
           "fp_github_token for an invalid one breaks git itself"
)
def test_pr_fail(env, config, repo, pr_id, backport_id):
    backport_id.target = env['runbot_merge.branch'].search([], order='name', limit=1).id
    with pytest.raises(Fault) as exc:
        backport_id.action_apply()
    assert exc.value.faultString == 'Backport PR creation failure: '
