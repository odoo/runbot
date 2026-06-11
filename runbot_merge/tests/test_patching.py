import threading
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from utils import Commit, read_tracking_value, matches

# basic udiff / show style patch, updates `b` from `1` to `2`
BASIC_UDIFF = """\
commit 0000000000000000000000000000000000000000
Author: 3 Discos Down <bar@example.org>
Date:   2021-04-24T17:09:14Z

    whop
    
    whop whop

diff --git a/b b/b
index d00491fd7e5b..0cfbf08886fc 100644
--- a/b
+++ b/b
@@ -1,1 +1,1 @@
-1
+2
"""

FORMAT_PATCH_XMO = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: 3 Discos Down <bar@example.org>
Date: Sat, 24 Apr 2021 17:09:14 +0000
Subject: [PATCH] [I18N] whop

whop whop
---
 b | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
 
diff --git a/b b/b
index d00491fd7e5b..0cfbf08886fc 100644
--- a/b
+++ b/b
@@ -1,1 +1,1 @@
-1
+2
-- 
2.46.2
"""

# slightly different format than the one I got, possibly because older?
FORMAT_PATCH_MAT = """\
From 3000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: 3 Discos Down <bar@example.org>
Date: Sat, 24 Apr 2021 17:09:14 +0000
Subject: [PATCH 1/1] [I18N] whop

whop whop
---
 b | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
 
diff --git b b
index d00491fd7e5b..0cfbf08886fc 100644
--- b
+++ b
@@ -1,1 +1,1 @@
-1
+2
-- 
2.34.1
"""


@pytest.fixture(autouse=True)
def _setup(repo):
    with repo:
        [c, _] = repo.make_commits(
            None,
            Commit("a", tree={"a": "1", "b": "1\n"}),
            Commit("b", tree={"a": "2"}),
            ref="heads/master",
        )
        repo.make_ref("heads/x", c)

@pytest.mark.parametrize("group,access", [
    ('base.group_portal', False),
    ('base.group_user', False),
    ('runbot_merge.group_patcher', True),
    ('runbot_merge.group_admin', False),
    ('base.group_system', True),
])
def test_patch_acl(env, project, group, access):
    g = env.ref(group)
    assert g._name == 'res.groups'
    env['res.users'].create({
        'name': 'xxx',
        'login': 'xxx',
        'password': 'xxx',
        'groups_id': [(6, 0, [g.id])],
    })
    env2 = env.with_user('xxx', 'xxx')
    def create():
        return env2['runbot_merge.patch'].create({
            'target': project.branch_ids.id,
            'repository': project.repo_ids.id,
            'patch': BASIC_UDIFF,
        })
    if access:
        create()
    else:
        pytest.raises(xmlrpc.client.Fault, create)\
            .match("You are not allowed to create")

def test_apply_commit(env, project, repo, users):
    with repo:
        [c] = repo.make_commits("x", Commit("c", tree={"b": "2"}, author={
            'name': "Henry Hoover",
            "email": "dustsuckinghose@example.org",
        }), ref="heads/abranch")
        repo.delete_ref('heads/abranch')

    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'commit': c,
    })

    env.run_crons()

    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '2',
    }
    assert HEAD.message == "c"
    assert HEAD.author['name'] == "Henry Hoover"
    assert HEAD.author['email'] == "dustsuckinghose@example.org"
    assert not p.active

    # try to apply a dupe version
    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'commit': c,
    })

    env.run_crons()

    # the patch should have been rejected since it leads to an empty commit
    NEW_HEAD = repo.commit('master')
    assert NEW_HEAD.id == HEAD.id
    assert not p.active
    assert p.message_ids.mapped('body')[::-1] == [
        '<p>Unstaged direct-application patch created</p>',
        "<p>Patch results in an empty commit when applied, "
        "it is likely a duplicate of a merged commit.</p>",
        "",  # empty message alongside active tracking value
    ]

def test_commit_conflict(env, project, repo, users):
    with repo:
        [c] = repo.make_commits("x", Commit("x", tree={"b": "3"}))
        repo.make_commits("master", Commit("c", tree={"b": "2"}), ref="heads/master", make=False)

    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'commit': c,
    })

    env.run_crons()

    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '2',
    }
    assert not p.active
    assert [(
        m.subject,
        m.body,
        list(map(read_tracking_value, m.tracking_value_ids)),
    )
        for m in reversed(p.message_ids)
    ] == [
        (False, '<p>Unstaged direct-application patch created</p>', []),
        (
            "Unable to apply patch",
            "<pre>Auto-merging b\nCONFLICT (content): Merge conflict in b\n</pre>",
            [],
        ),
        (False, '', [('active', 1, 0)]),
    ]

def test_apply_not_found(env, project, repo, users):
    """ Github can take some time to propagate commits through the network,
    resulting in patches getting not found and killing the application.

    Commits which are not found should just be skipped (and trigger a new
    staging?).
    """
    with repo:
        [c] = repo.make_commits("x", Commit("c", tree={"b": "2"}), ref="heads/abranch")
        repo.delete_ref('heads/abranch')

    p1 = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'commit': c,
    })
    # simulate commit which hasn't propagated yet
    p2 = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'commit': "0123456789012345678901234567890123456789",
    })

    env.run_crons()

    assert not p1.active
    assert p2.active
    assert p2.message_ids.mapped('body')[::-1] == [
        "<p>Unstaged direct-application patch created</p>",
        matches('''\
<p>Commit 0123456789012345678901234567890123456789 not found</p>
<p>stderr:</p>
<pre>
$$
</pre>\
'''),
    ]

def test_apply_udiff(env, project, repo, users):
    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF,
    })

    env.run_crons()

    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '2\n',
    }
    assert HEAD.message == "whop\n\nwhop whop"
    assert HEAD.author['name'] == "3 Discos Down"
    assert HEAD.author['email'] == "bar@example.org"
    assert not p.active


@pytest.mark.parametrize('patch', [
    pytest.param(FORMAT_PATCH_XMO, id='xmo'),
    pytest.param(FORMAT_PATCH_MAT, id='mat'),
    pytest.param(
        FORMAT_PATCH_XMO.replace('\n', '\r\n'),
        id='windows',
    ),
    pytest.param(
        FORMAT_PATCH_XMO.rsplit('-- \n')[0],
        id='no-signature',
    )
])
def test_apply_format_patch(env, project, repo, users, patch):
    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': patch,
    })

    env.run_crons()

    bot = env['res.users'].browse((1,))
    assert p.message_ids[::-1].mapped(lambda m: (
        m.author_id.display_name,
        m.body,
        list(map(read_tracking_value, m.tracking_value_ids)),
    )) == [
        (p.create_uid.partner_id.display_name, '<p>Unstaged direct-application patch created</p>', []),
        (bot.partner_id.display_name, "", [('active', 1, 0)]),
    ]
    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '2\n',
    }
    assert HEAD.message == "[I18N] whop\n\nwhop whop"
    assert HEAD.author['name'] == "3 Discos Down"
    assert HEAD.author['email'] == "bar@example.org"
    assert not p.active

def test_patch_conflict(env, project, repo, users):
    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF,
    })
    with repo:
        repo.make_commits('master', Commit('cccombo breaker', tree={'b': '3'}), ref='heads/master', make=False)

    env.run_crons()

    HEAD = repo.commit('master')
    assert HEAD.message == 'cccombo breaker'
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '3',
    }
    assert not p.active
    assert [(
        m.subject,
        m.body,
        list(map(read_tracking_value, m.tracking_value_ids)),
    )
        for m in reversed(p.message_ids)
    ] == [(
        False,
        '<p>Unstaged direct-application patch created</p>',
        [],
    ), (
        "Unable to apply patch",
        matches("$$"),  # feedback from patch can vary
        [],
    ), (
        False, '', [('active', 1, 0)]
    )]

CREATE_FILE_FORMAT_PATCH = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: 3 Discos Down <bar@example.org>
Date: Sat, 24 Apr 2021 17:09:14 +0000
Subject: [PATCH] [I18N] whop

whop whop
---
 x | 1 +
 1 file changed, 1 insertion(+)
 create mode 100644 b

diff --git a/x b/x
new file mode 100644
index 000000000000..d00491fd7e5b
--- /dev/null
+++ b/x
@@ -0,0 +1 @@
+1
-- 
2.48.1
"""

CREATE_FILE_SHOW = """\
commit 0000000000000000000000000000000000000000
Author: 3 Discos Down <bar@example.org>
Date:   2021-04-24T17:09:14Z

    [I18N] whop
    
    whop whop

diff --git a/x b/x
new file mode 100644
index 000000000000..d00491fd7e5b
--- /dev/null
+++ b/x
@@ -0,0 +1 @@
+1
"""

@pytest.mark.parametrize('patch', [
    pytest.param(CREATE_FILE_SHOW, id='show'),
    pytest.param(CREATE_FILE_FORMAT_PATCH, id='format-patch'),
])
def test_apply_creation(env, project, repo, users, patch):
    assert repo.read_tree(repo.commit('master')) == {
        'a': '2',
        'b': '1\n',
    }

    env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': patch,
    })
    # trying to check the list of files doesn't work, even using web_read

    env.run_crons()

    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {
        'a': '2',
        'b': '1\n',
        'x': '1\n',
    }
    assert HEAD.message == "[I18N] whop\n\nwhop whop"
    assert HEAD.author['name'] == "3 Discos Down"
    assert HEAD.author['email'] == "bar@example.org"

def test_apply_empty(env, project, repo, users):
    with repo:
        [c] = repo.make_commits(None, Commit("x", tree={"a": "1"}), ref="heads/master", make=False)

    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF,
    })

    env.run_crons()

    assert repo.read_tree(repo.commit('master')) == {'a': '1', }
    assert not p.active
    assert p.message_ids.mapped('body')[::-1] == [
        '<p>Unstaged direct-application patch created</p>',
        f"""\
<p>Files to patch not found in {repo.name}:master (at {c}):</p>
<ul>
<li>b</li>
</ul>\
""",
        '',
    ]

def test_apply_invalid_path(env, project, repo, users):
    with repo:
        [c] = repo.make_commits(None, Commit("x", tree={"a": "1\n"}), ref="heads/master", make=False)

    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF + """\
diff --git a/a b/a
index d00491fd7e5b..0cfbf08886fc 100644
--- a/a
+++ b/a
@@ -1,1 +1,1 @@
-1
+2
""",
    })

    env.run_crons()

    assert repo.read_tree(repo.commit('master')) == {'a': '1\n', }
    assert not p.active
    assert p.message_ids.mapped('body')[::-1] == [
        '<p>Unstaged direct-application patch created</p>',
        f"""\
<p>Files to patch not found in {repo.name}:master (at {c}):</p>
<ul>
<li>b</li>
</ul>\
""",
        '',
    ]


@pytest.fixture
def callback_server():
    """Spins up a local HTTP server the patch callback can POST to, records the
    requests it receives and allows configuring the status code it answers with.
    """
    state = SimpleNamespace(requests=[], status=200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length:
                self.rfile.read(length)
            state.requests.append(self.path)
            self.send_response(state.status)
            self.end_headers()

        def log_message(self, *args):  # silence the default stderr logging
            pass

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        state.url = f"http://127.0.0.1:{server.server_address[1]}/hook?state=42"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()

def test_apply_callback_success(env, project, repo, users, callback_server):
    """When a patch with a callback URL applies successfully, the target is
    notified (once) with ``success=True`` and the callback is dropped from the
    queue.
    """
    env['ir.model.access'].create({
        "name": "xxx",
        "model_id": env.ref("runbot_merge.model_runbot_merge_patch_callback").id,
        "group_id": env.ref("runbot_merge.group_admin").id,
        "perm_read": True,
    })
    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF,
        'callback_url': callback_server.url,
    })

    env.run_crons()

    HEAD = repo.commit('master')
    assert repo.read_tree(HEAD) == {'a': '2', 'b': '2\n'}, "the branch was correctly patched"
    assert not p.active

    assert len(callback_server.requests) == 1, "the callback_url was hit once"
    assert callback_server.requests == ["/hook?state=42&success=1"]

    assert env['runbot_merge.patch.callback'].search_count([]) == 0


@pytest.mark.expect_log_errors(
    reason="a callback target answering with an error status is logged on"
           " every failed attempt until the hook is cancelled",
)
def test_apply_callback_failure(env, project, repo, users, callback_server):
    """If the callback target does not respond appropriately, the branch is
    still patched but the hook is retried and eventually cancelled (disabled).
    """
    callback_server.status = 404
    env['ir.model.access'].create({
        "name": "xxx",
        "model_id": env.ref("runbot_merge.model_runbot_merge_patch_callback").id,
        "group_id": env.ref("runbot_merge.group_admin").id,
        "perm_read": True,
        "perm_write": True,
    })

    p = env['runbot_merge.patch'].create({
        'target': project.branch_ids.id,
        'repository': project.repo_ids.id,
        'patch': BASIC_UDIFF,
        'callback_url': callback_server.url,
    })

    env.run_crons()

    assert repo.read_tree(repo.commit('master')) == {'a': '2', 'b': '2\n'},\
        "the branch was correctly patched"
    assert not p.active

    cb = env['runbot_merge.patch.callback'].search([('patch_id', '=', p.id)])
    assert cb, "the callback should still be queued for retry"
    assert cb.sequence == 1, "the job should have failed once"
    assert not cb.disabled, "a single failure should not cancel the hook yet"

    for _ in range(5):  # PatchCallback.RETRY_LIMIT
        cb.retry_after = '0001-01-01 00:00:00'
        env.run_crons('runbot_merge.patch_callback_cron')

    assert cb.disabled
    assert callback_server.requests == ["/hook?state=42&success=1"]*5

    assert repo.read_tree(repo.commit('master')) == {'a': '2', 'b': '2\n'},\
        "the branch is still patched"