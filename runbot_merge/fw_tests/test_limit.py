import lxml.html
import pytest

from utils import seen, Commit, make_basic, to_pr


@pytest.mark.parametrize('source,limit,count', [
    pytest.param('a', 'b', 1, id='not-last'),
    pytest.param('b', 'b', 0, id='current'),
    pytest.param('b', 'a', 0, id='earlier'),
])
def test_configure_fp_limit(env, config, make_repo, source, limit, count, page):
    prod, _other = make_basic(env, config, make_repo, statuses="default")
    with prod:
        [c] = prod.make_commits(
            source, Commit('c', tree={'f': 'g'}),
            ref='heads/branch',
        )
        pr = prod.make_pr(target=source, head='branch')
        prod.post_status(c, 'success')
        pr.post_comment(f'hansen r+ up to {limit}', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status(f'staging.{source}', 'success')
    env.run_crons()

    pr_id = to_pr(env, pr)
    descendants = env['runbot_merge.pull_requests'].search([
        ('source_id', '=', pr_id.id)
    ])
    assert len(descendants) == count
    limit_id = env['runbot_merge.branch'].search([('name', '=', limit)])

    assert pr_id.limit_id == limit_id
    assert not descendants.limit_id, "descendant should not inherit the limit explicitly"

    # check that the basic thingie works
    page(f'/{prod.name}/pull/{pr.number}.png')

    if descendants:
        c = env['runbot_merge.branch'].search([('name', '=', 'c')])
        descendants.limit_id = c.id

        page(f'/{prod.name}/pull/{pr.number}.png')

def test_ignore(env, config, make_repo, users):
    """ Provide an "ignore" command which is equivalent to setting the limit
    to target
    """
    prod, _ = make_basic(env, config, make_repo, statuses="default")
    branch_a = env['runbot_merge.branch'].search([('name', '=', 'a')])
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head='mybranch')
        prod.post_status(c, 'success')
    env.run_crons()
    with prod:
        pr.post_comment('hansen ignore', config['role_reviewer']['token'])
        pr.post_comment('hansen r+ fw=no', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = env['runbot_merge.pull_requests'].search([('number', '=', pr.number)])
    assert pr_id.limit_id == branch_a

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search([]) == pr_id,\
        "should not have created a forward port"

    assert pr.comments == [
        seen(env, pr, users),
        (users['reviewer'], "hansen ignore"),
        (users['reviewer'], "hansen r+ fw=no"),
        (users['user'], "'ignore' is deprecated, use 'fw=no' to disable forward porting."),
        (users['user'], "Forward-port disabled (via limit)."),
        (users['user'], "Disabled forward-porting."),
    ]

    with prod:
        pr.post_comment('hansen up to c', config['role_reviewer']['token'])
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search([]) == pr_id,\
        "should still not have created a forward port"
    assert pr.comments[6:] == [
        (users['reviewer'], 'hansen up to c'),
        (users['user'], "@%s can not forward-port, policy is 'no' on %s" % (
            users['reviewer'],
            pr_id.display_name,
        ))
    ]

    assert pr_id.limit_id == env['runbot_merge.branch'].search([('name', '=', 'c')])
    pr_id.limit_id = False

    with prod:
        pr.post_comment("hansen fw=default", config['role_reviewer']['token'])
    env.run_crons()

    assert pr.comments[8:] == [
        (users['reviewer'], "hansen fw=default"),
        (users['user'], "Starting forward-port. Waiting for CI to create followup forward-ports.")
    ]

    assert env['runbot_merge.pull_requests'].search([('source_id', '=', pr_id.id)]),\
        "should finally have created a forward port"

def test_ignore_fw(env, config, make_repo, users):
    """Allow using fw=no to stop forward porting
    """
    prod, _ = make_basic(env, config, make_repo, statuses="default")
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head='mybranch')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'merged'

    [pr_b_id] = env['runbot_merge.pull_requests'].search([('parent_id', '=', pr_id.id)], order='number')
    pr_b = prod.get_pr(pr_b_id.number)
    with prod:
        pr_b.post_comment('hansen fw=no r+', config['role_reviewer']['token'])

    assert pr_b_id.batch_id.genealogy_ids.mapped('fw_policy') == ['no', 'no']

    with prod:
        prod.post_status(pr_b_id.head, 'success')
    env.run_crons()

    assert pr_b_id.batch_id.genealogy_ids.mapped('fw_policy') == ['no', 'no']
    assert pr_b_id.state == 'ready'

    with prod:
        prod.post_status('staging.b', 'success')
    env.run_crons()

    assert pr_b_id.state == 'merged'

    assert not env['runbot_merge.pull_requests'].search([('target.name', '=', 'c')])

def test_unignore(env, config, make_repo, users, page):
    env['res.partner'].create({'name': users['other'], 'github_login': users['other'], 'email': 'other@example.org'})

    prod, _ = make_basic(env, config, make_repo, statuses="default")
    token = config['role_other']['token']
    fork = prod.fork(token=token)
    with prod, fork:
        [c] = fork.make_commits(prod.commit('a').id, Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head=f'{fork.owner}:mybranch', title="title", token=token)
        prod.post_status(c, 'success')
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.batch_id.fw_policy == 'default'

    doc = lxml.html.fromstring(page(f'/{prod.name}/pull/{pr.number}'))
    assert len(doc.cssselect("table > tbody > tr")) == 3, \
        "the overview table should have as many rows as there are tables"

    with prod:
        pr.post_comment('hansen fw=no', token)
    env.run_crons(None)
    assert pr_id.batch_id.fw_policy == 'no'

    doc = lxml.html.fromstring(page(f'/{prod.name}/pull/{pr.number}'))
    assert len(doc.cssselect("table > tbody > tr")) == 1, \
        "if fw=no, there should be just one row"

    with prod:
        pr.post_comment('hansen fw=default', token)
    env.run_crons(None)
    assert pr_id.batch_id.fw_policy == 'default'

    with prod:
        pr.post_comment('hansen fw=no', token)
    env.run_crons(None)
    with prod:
        pr.post_comment('hansen up to c', token)
    env.run_crons(None)
    assert pr_id.batch_id.fw_policy == 'default'

    assert pr.comments == [
        seen(env, pr, users),
        (users['other'], "hansen fw=no"),
        (users['user'], "Disabled forward-porting."),
        (users['other'], "hansen fw=default"),
        (users['user'], "Waiting for CI to create followup forward-ports."),
        (users['other'], "hansen fw=no"),
        (users['user'], "Disabled forward-porting."),
        (users['other'], "hansen up to c"),
        (users['user'], "Forward-porting to 'c'. Re-enabled forward-porting "\
                        "(you should use `fw=default` to re-enable forward "\
                        "porting after disabling)."
        ),
    ]

def test_disable(env, config, make_repo, users):
    """ Checks behaviour if the limit target is disabled:

    * disable target while FP is ongoing -> skip over (and stop there so no FP)
    * forward-port over a disabled branch
    * request a disabled target as limit
    """
    prod, _other = make_basic(env, config, make_repo, statuses='default')
    with prod:
        [c] = prod.make_commits('a', Commit('c 0', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+ up to b', config['role_reviewer']['token'])

        [c] = prod.make_commits('a', Commit('c 1', tree={'1': '1'}), ref='heads/branch1')
        pr = prod.make_pr(target='a', head='branch1')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    # disable branch b
    env['runbot_merge.branch'].search([('name', '=', 'b')]).active = False
    env.run_crons()

    # should have created a single PR (to branch c, for pr 1)
    _0, _1, p = env['runbot_merge.pull_requests'].search([], order='number')
    assert p.parent_id == _1
    assert p.target.name == 'c'

    with prod:
        [c] = prod.make_commits('a', Commit('c 2', tree={'2': '2'}), ref='heads/branch2')
        pr = prod.make_pr(target='a', head='branch2')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+ up to', config['role_reviewer']['token'])
        pr.post_comment('hansen up to b', config['role_reviewer']['token'])
        pr.post_comment('hansen up to foo', config['role_reviewer']['token'])
        pr.post_comment('hansen up to c', config['role_reviewer']['token'])
    env.run_crons()

    # use a set because git webhooks delays might lead to mis-ordered
    # responses and we don't care that much
    assert set(pr.comments) == {
        seen(env, pr, users),
        (users['reviewer'], "hansen r+ up to"),
        (users['user'], """\
@{reviewer} please provide a branch to forward-port to.

For your own safety I've ignored *everything in your entire comment*.

Currently available commands:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)+`|approves the PR, if it&#39;s a forwardport also approves all non-detached parents|
|`r(eview)=<number>`|only approves the specified parents|
|`r(eview)-`|removes approval of a previously approved PR, if the PR is staged the staging will be cancelled|
|`retry`|re-tries staging a PR in the &#34;error&#34; state|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`fw=skipci`|does not wait for a forward-port&#39;s statuses to succeed before creating the next one|
|`fw=skipmerge`|does not wait for the source to be merged before creating forward ports|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`merge`|integrate the PR with a simple merge commit, using the PR description as message|
|`rebase-merge`|rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message|
|`rebase-ff`|rebases the PR on top of the target branch, then fast-forwards|
|`squash`|squashes the PR as a single commit on the target branch, using the PR description as message|
|`delegate+`|grants approval rights to the PR author|
|`delegate=<...>`|grants approval rights on this PR to the specified github users|
|`nice`|only stages the PR if there&#39;s room in the batch after `default` PRs|
|`default`|stages the PR normally|
|`priority`|tries to stage this PR first, then adds `default` PRs if the staging has room|
|`alone`|stages this PR only with other PRs of the same priority|
|`cancel=staging`|automatically cancels the current staging when this PR becomes ready|
|`check`|fetches or refreshes PR metadata, resets mergebot state|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to &lt;branch&gt;, ping you with &lt;message&gt;. &lt;message&gt; can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.
""".format_map(users)),
        (users['reviewer'], "hansen up to b"),
        (users['user'], "@{reviewer} branch 'b' is disabled, it can't be used as a forward port target.".format_map(users)),
        (users['reviewer'], "hansen up to foo"),
        (users['user'], "@{reviewer} there is no branch 'foo', it can't be used as a forward port target.".format_map(users)),
        (users['reviewer'], "hansen up to c"),
        (users['user'], "Forward-porting to 'c'."),
    }


def test_limit_after_merge(env, config, make_repo, users):
    prod, other = make_basic(env, config, make_repo, statuses='default')
    reviewer = config['role_reviewer']['token']
    branch_b = env['runbot_merge.branch'].search([('name', '=', 'b')])
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status(c, 'success')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    p1, p2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p1.limit_id == p2.limit_id == env['runbot_merge.branch'].browse(())
    pr2 = prod.get_pr(p2.number)
    with prod:
        pr1.post_comment('hansen up to b', reviewer)
        pr2.post_comment('hansen up to b', reviewer)
    env.run_crons()

    assert p1.limit_id == p2.limit_id == branch_b
    assert pr1.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr1, users),
        (users['reviewer'], 'hansen up to b'),
        (users['user'], "Forward-porting to 'b'."),
        (users['user'], f"Forward-porting to 'b' (from {p2.display_name})."),
    ]
    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], 'hansen up to b'),
        (users['user'], f"Forward-porting {p1.display_name} to 'b'."),
    ]

    # update pr2 to detach it from pr1
    oldhead = p2.head
    with other:
        other.make_commits(
            prod.commit(p2.target.name).id,
            Commit('updated', tree={'1': '1'}),
            ref=pr2.ref,
            make=False
        )
    env.run_crons()
    assert not p2.parent_id
    assert p2.source_id == p1

    with prod:
        pr2.post_comment('hansen up to c', reviewer)
    env.run_crons()

    assert pr2.comments[4:] == [
        (users['reviewer'], 'hansen up to c'),
        (users['user'], "Forward-porting to 'c'."),
    ]
    with prod:
        prod.post_status(p2.head, 'success')
        pr2.post_comment('hansen r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success')
    env.run_crons()

    _, _, p3 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p3
    pr3 = prod.get_pr(p3.number)
    with prod:
        pr3.post_comment("hansen up to c", reviewer)
    env.run_crons()
    assert pr3.comments == [
        seen(env, pr3, users),
        (users['user'], f"""\
@{users['user']} @{users['reviewer']} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], "hansen up to c"),
        (users['user'], f"Forward-porting {p2.display_name} to 'c'."),
    ]
    # 6 of previous check, plus r+
    assert pr2.comments[7:] == [
        (users['user'], f"Forward-porting to 'c' (from {p3.display_name}).")
    ]


def test_limit_multiple_fw_tasks(env, config, make_repo, users):
    """Bit of a special case here: technically nothing prevents having multiple
    forward port tasks in flight (the second will just fail / be cancelled), but
    `_maybe_update_limit` assumed only one batch was possible.
    """
    # disable cron so we don't risk the entire thing running on us
    env.ref('runbot_merge.port_forward').active = False
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    with prod:
        prod.make_commits('a', Commit('c', tree={'a': '0'}), ref='heads/abranch')
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    fp = env['forwardport.batches'].search([])
    assert len(fp) == 1
    assert fp.source == 'merge'
    fp.copy()
    with prod:
        pr.post_comment("hansen up to a", config['role_reviewer']['token'])
    env.run_crons()

    assert to_pr(env, pr).limit_id.name == 'a'


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
    _project, prod, _ = post_merge
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
        from_.post_comment(f'hansen up to {limit}', reviewer)
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
    _project, prod, _ = post_merge
    reviewer = config['role_reviewer']['token']

    # fetch source PR
    [source] = PRs.search([('source_id', '=', False)])
    with prod:
        prod.get_pr(source.number).post_comment('hansen up to 5', reviewer)
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
                prod.get_pr(number).post_comment('hansen r+', reviewer)
        env.run_crons()
        with prod:
            for target in numbers:
                prod.post_status(f'staging.{target}', 'success')
        env.run_crons()
        for number in numbers:
            assert PRs.search([('number', '=', number)]).state == 'merged'

    from_ = prod.get_pr(source.number)
    with prod:
        from_.post_comment('hansen up to 6', reviewer)
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
    assert pr, f"could not find forward port of {source.display_name} to {target}"
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
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
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
    env['runbot_merge.events_sources'].create({'repository': prod.name})

    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': proj.repo_ids.id, 'review': True})]
    })

    reviewer = config['role_reviewer']['token']
    # merge the source PR
    source_target = str(branches[0])
    with prod:
        [c] = prod.make_commits(source_target, Commit('my pr', tree={'x': ''}), ref='heads/mypr')
        pr1 = prod.make_pr(target=source_target, head=c, title="a title")

        prod.post_status(c, 'success')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status(f'staging.{source_target}', 'success')
    env.run_crons()

    return proj, prod, dev
