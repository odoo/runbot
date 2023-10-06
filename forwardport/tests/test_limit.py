
import pytest

from utils import seen, Commit, make_basic, to_pr


@pytest.mark.parametrize('source,limit,count', [
    pytest.param('a', 'b', 1, id='not-last'),
    pytest.param('b', 'b', 0, id='current'),
    pytest.param('b', 'a', 0, id='earlier'),
])
def test_configure_fp_limit(env, config, make_repo, source, limit, count):
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    with prod:
        [c] = prod.make_commits(
            source, Commit('c', tree={'f': 'g'}),
            ref='heads/branch',
        )
        pr = prod.make_pr(target=source, head='branch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment(f'hansen r+\n{bot_name} up to {limit}', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status(f'staging.{source}', 'success', 'legal/cla')
        prod.post_status(f'staging.{source}', 'success', 'ci/runbot')
    env.run_crons()

    descendants = env['runbot_merge.pull_requests'].search([
        ('source_id', '=', to_pr(env, pr).id)
    ])
    assert len(descendants) == count


def test_ignore(env, config, make_repo):
    """ Provide an "ignore" command which is equivalent to setting the limit
    to target
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    branch_a = env['runbot_merge.branch'].search([('name', '=', 'a')])
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head='mybranch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s ignore' % bot_name, config['role_reviewer']['token'])
    env.run_crons()
    pr_id = env['runbot_merge.pull_requests'].search([('number', '=', pr.number)])
    assert pr_id.limit_id == branch_a

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    assert env['runbot_merge.pull_requests'].search([]) == pr_id,\
        "should not have created a forward port"


def test_disable(env, config, make_repo, users):
    """ Checks behaviour if the limit target is disabled:

    * disable target while FP is ongoing -> skip over (and stop there so no FP)
    * forward-port over a disabled branch
    * request a disabled target as limit
    """
    prod, other = make_basic(env, config, make_repo)
    project = env['runbot_merge.project'].search([])
    bot_name = project.fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c 0', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to b' % bot_name, config['role_reviewer']['token'])

        [c] = prod.make_commits('a', Commit('c 1', tree={'1': '1'}), ref='heads/branch1')
        pr = prod.make_pr(target='a', head='branch1')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    # disable branch b
    env['runbot_merge.branch'].search([('name', '=', 'b')]).active = False
    env.run_crons()

    # should have created a single PR (to branch c, for pr 1)
    _0, _1, p = env['runbot_merge.pull_requests'].search([], order='number')
    assert p.parent_id == _1
    assert p.target.name == 'c'

    project.fp_github_token = config['role_other']['token']
    bot_name = project.fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c 2', tree={'2': '2'}), ref='heads/branch2')
        pr = prod.make_pr(target='a', head='branch2')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to b' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to foo' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to c' % bot_name, config['role_reviewer']['token'])
    env.run_crons()

    # use a set because git webhooks delays might lead to mis-ordered
    # responses and we don't care that much
    assert set(pr.comments) == {
        (users['reviewer'], "hansen r+\n%s up to" % bot_name),
        (users['other'], "@%s please provide a branch to forward-port to." % users['reviewer']),
        (users['reviewer'], "%s up to b" % bot_name),
        (users['other'], "@%s branch 'b' is disabled, it can't be used as a forward port target." % users['reviewer']),
        (users['reviewer'], "%s up to foo" % bot_name),
        (users['other'], "@%s there is no branch 'foo', it can't be used as a forward port target." % users['reviewer']),
        (users['reviewer'], "%s up to c" % bot_name),
        (users['other'], "Forward-porting to 'c'."),
        seen(env, pr, users),
    }


def test_limit_after_merge(env, config, make_repo, users):
    prod, other = make_basic(env, config, make_repo)
    reviewer = config['role_reviewer']['token']
    branch_b = env['runbot_merge.branch'].search([('name', '=', 'b')])
    branch_c = env['runbot_merge.branch'].search([('name', '=', 'c')])
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    p1, p2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p1.limit_id == p2.limit_id == branch_c, "check that limit is correctly set"
    pr2 = prod.get_pr(p2.number)
    with prod:
        pr1.post_comment(bot_name + ' up to b', reviewer)
        pr2.post_comment(bot_name + ' up to b', reviewer)
    env.run_crons()

    assert p1.limit_id == p2.limit_id == branch_b
    assert pr1.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr1, users),
        (users['reviewer'], f'{bot_name} up to b'),
        (users['user'], "Forward-porting to 'b'."),
        (users['user'], f"Forward-porting to 'b' (from {p2.display_name})."),
    ]
    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], f'{bot_name} up to b'),
        (users['user'], f"Forward-porting {p1.display_name} to 'b'."),
    ]

    # update pr2 to detach it from pr1
    with other:
        other.make_commits(
            p2.target.name,
            Commit('updated', tree={'1': '1'}),
            ref=pr2.ref,
            make=False
        )
    env.run_crons()
    assert not p2.parent_id
    assert p2.source_id == p1

    with prod:
        pr2.post_comment(f'{bot_name} up to c', reviewer)
    env.run_crons()

    assert pr2.comments[4:] == [
        (users['user'], "@%s @%s this PR was modified / updated and has become a normal PR. "
                   "It should be merged the normal way (via @%s)" % (
            users['user'], users['reviewer'],
            p2.repository.project_id.github_prefix
        )),
        (users['reviewer'], f'{bot_name} up to c'),
        (users['user'], "Forward-porting to 'c'."),
    ]
    with prod:
        prod.post_status(p2.head, 'success', 'legal/cla')
        prod.post_status(p2.head, 'success', 'ci/runbot')
        pr2.post_comment('hansen r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    _, _, p3 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p3
    pr3 = prod.get_pr(p3.number)
    with prod:
        pr3.post_comment(f"{bot_name} up to c", reviewer)
    env.run_crons()
    assert pr3.comments == [
        seen(env, pr3, users),
        (users['user'], f"""\
@{users['user']} @{users['reviewer']} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @{p1.repository.project_id.fp_github_name} r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], f"{bot_name} up to c"),
        (users['user'], f"Forward-porting {p2.display_name} to 'c'."),
    ]
    # 7 of previous check, plus r+
    assert pr2.comments[8:] == [
        (users['user'], f"Forward-porting to 'c' (from {p3.display_name}).")
    ]



@pytest.mark.parametrize("update_from", [
    pytest.param(lambda source: [('id', '=', source)], id='source'),
    pytest.param(lambda source: [('source_id', '=', source), ('target', '=', '2')], id='child'),
    pytest.param(lambda source: [('source_id', '=', source), ('target', '=', '3')], id='root'),
    pytest.param(lambda source: [('source_id', '=', source), ('target', '=', '4')], id='parent'),
    pytest.param(lambda source: [('source_id', '=', source), ('target', '=', '5')], id='current'),
    # pytest.param(id='tip'), # doesn't exist
])
@pytest.mark.parametrize("limit", range(1, 6+1))
def test_post_merge(
        env, post_merge, users, config, branches,
        update_from: callable,
        limit: int,
):
    PRs = env['runbot_merge.pull_requests']
    project, prod, _ = post_merge
    reviewer = config['role_reviewer']['token']

    # fetch source PR
    [source] = PRs.search([('source_id', '=', False)])

    # validate the forward ports for "child", "root", and "parent" so "current"
    # exists and we have one more target
    for branch in map(str, range(2, 4+1)):
        setci(source=source, repo=prod, target=branch)
        env.run_crons()
    # update 3 to make it into a root
    root = PRs.search([('source_id', '=', source.id), ('target.name', '=', '3')])
    root.write({'parent_id': False, 'detach_reason': 'testing'})
    # send detach messages so they're not part of the limit stuff batch
    env.run_crons()

    # cheat: we know PR numbers are assigned sequentially
    prs = list(map(prod.get_pr, range(1, 6)))
    before = {p.number: len(p.comments) for p in prs}

    from_id = PRs.search(update_from(source.id))
    from_ = prod.get_pr(from_id.number)
    with prod:
        from_.post_comment(f'{project.fp_github_name} up to {limit}', reviewer)
    env.run_crons()

    # there should always be a comment on the source and root indicating how
    # far we port
    # the PR we post on should have a comment indicating the correction
    current_id = PRs.search([('number', '=', '5')])
    actual_limit = max(limit, 5)
    for p in prs:
        # case for the PR on which we posted the comment
        if p.number == from_.number:
            root_opt = '' if p.number == root.number else f' {root.display_name}'
            trailer = '' if actual_limit == limit else f" (instead of the requested '{limit}' because {current_id.display_name} already exists)"
            assert p.comments[before[p.number] + 1:] == [
                (users['user'], f"Forward-porting{root_opt} to '{actual_limit}'{trailer}.")
            ]
        # case for reference PRs source and root (which get their own notifications)
        elif p.number in (source.number, root.number):
            assert p.comments[before[p.number]:] == [
                (users['user'], f"Forward-porting to '{actual_limit}' (from {from_id.display_name}).")
            ]

@pytest.mark.parametrize('mode', [
    None,
    # last forward port should fail ci, and only be validated after target bump
    'failbump',
    # last forward port should fail ci, then be validated, then target bump
    'failsucceed',
    # last forward port should be merged before bump
    'mergetip',
    # every forward port should be merged before bump
    'mergeall',
])
def test_resume_fw(env, post_merge, users, config, branches, mode):
    """Singleton version of test_post_merge: completes the forward porting
    including validation then tries to increase the limit, which should resume
    forward porting
    """

    PRs = env['runbot_merge.pull_requests']
    project, prod, _ = post_merge
    reviewer = config['role_reviewer']['token']

    # fetch source PR
    [source] = PRs.search([('source_id', '=', False)])
    with prod:
        prod.get_pr(source.number).post_comment(f'{project.fp_github_name} up to 5', reviewer)
    # validate the forward ports for "child", "root", and "parent" so "current"
    # exists and we have one more target
    for branch in map(str, range(2, 5+1)):
        setci(
            source=source, repo=prod, target=branch,
            status='failure' if branch == '5' and mode in ('failbump', 'failsucceed') else 'success'
        )
        env.run_crons()
    # cheat: we know PR numbers are assigned sequentially
    prs = list(map(prod.get_pr, range(1, 6)))
    before = {p.number: len(p.comments) for p in prs}

    if mode == 'failsucceed':
        setci(source=source, repo=prod, target=5)
        # sees the success, limit is still 5, considers the porting finished
        env.run_crons()

    if mode and mode.startswith('merge'):
        numbers = range(5 if mode == 'mergetip' else 2, 5 + 1)
        with prod:
            for number in numbers:
                prod.get_pr(number).post_comment(f'{project.github_prefix} r+', reviewer)
        env.run_crons()
        with prod:
            for target in numbers:
                pr = PRs.search([('target.name', '=', str(target))])
                print(pr.display_name, pr.state, pr.staging_id)
                prod.post_status(f'staging.{target}', 'success')
        env.run_crons()
        for number in numbers:
            assert PRs.search([('number', '=', number)]).state == 'merged'

    from_ = prod.get_pr(source.number)
    with prod:
        from_.post_comment(f'{project.fp_github_name} up to 6', reviewer)
    env.run_crons()

    if mode == 'failbump':
        setci(source=source, repo=prod, target=5)
        # setci moved the PR from opened to validated, so *now* it can be
        # forward-ported, but that still needs to actually happen
        env.run_crons()

    # since PR5 CI succeeded and we've increased the limit there should be a
    # new PR
    assert PRs.search([('source_id', '=', source.id), ('target.name', '=', 6)])
    pr5_id = PRs.search([('source_id', '=', source.id), ('target.name', '=', 5)])
    if mode == 'failbump':
        # because the initial forward porting was never finished as the PR CI
        # failed until *after* we bumped the limit, so it's not *resuming* per se.
        assert prs[0].comments[before[1]+1:] == [
            (users['user'], f"Forward-porting to '6'.")
        ]
    else:
        assert prs[0].comments[before[1]+1:] == [
            (users['user'], f"Forward-porting to '6', resuming forward-port stopped at {pr5_id.display_name}.")
        ]

def setci(*, source, repo, target, status='success'):
    """Validates (CI success) the descendant of ``source`` targeting ``target``
    in  ``repo``.
    """
    pr = source.search([('source_id', '=', source.id), ('target.name', '=', str(target))])
    with repo:
        repo.post_status(pr.head, status)


@pytest.fixture(scope='session')
def branches():
    """Need enough branches to make space for:

    - a source
    - an ancestor (before and separated from the root, but not the source)
    - a root (break in the parent chain
    - a parent (between "current" and root)
    - "current"
    - the tip branch
    """
    return range(1, 6 + 1)

@pytest.fixture
def post_merge(env, config, users, make_repo, branches):
    """Create a setup for the post-merge limits test which is both simpler and
    more complicated than the standard test setup(s): it doesn't need more
    variety in code, but it needs a lot more "depth" in terms of number of
    branches it supports. Branches are fixture-ed to make it easier to share
    between this fixture and the actual test.

    All the branches are set to the same commit because that basically
    shouldn't matter.
    """
    prod = make_repo("post-merge-test")
    with prod:
        [c] = prod.make_commits(None, Commit('base', tree={'f': ''}))
        for i in branches:
            prod.make_ref(f'heads/{i}', c)
    dev = prod.fork()

    proj = env['runbot_merge.project'].create({
        'name': prod.name,
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
        'fp_github_email': 'hb@example.com',
        'branch_ids': [
            (0, 0, {'name': str(i), 'sequence': 1000 - (i * 10)})
            for i in branches
        ],
        'repo_ids': [
            (0, 0, {
                'name': prod.name,
                'required_statuses': 'default',
                'fp_remote_target': dev.name,
            })
        ]
    })

    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': proj.repo_ids.id, 'review': True})]
    })

    mbot = proj.github_prefix
    reviewer = config['role_reviewer']['token']
    # merge the source PR
    source_target = str(branches[0])
    with prod:
        [c] = prod.make_commits(source_target, Commit('my pr', tree={'x': ''}), ref='heads/mypr')
        pr1 = prod.make_pr(target=source_target, head=c, title="a title")

        prod.post_status(c, 'success')
        pr1.post_comment(f'{mbot} r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status(f'staging.{source_target}', 'success')
    env.run_crons()

    return proj, prod, dev
