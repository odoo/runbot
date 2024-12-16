import copy
import pprint

import pytest

from utils import Commit, to_pr


def test_basic(make_repo, project, env, setreviewers, config, users, partners, pytestconfig):
    repos = {}
    #region project setup
    project.repo_pythonpath_layout = make_repo('pythonpath', hooks=False).name
    for name, conf in zip('abcd', [
        # flat-style repo (odoo/documentation)
        {},
        # flat repo with symlink into (odoo/odoo)
        {
            'pythonpath_location': '%(name)s',
            'pythonpath_link_path': 'community/odoo/addons',
            'pythonpath_link_target': 'b',
        },
        # modules directory style
        {'pythonpath_location': '%(name)s/odoo/addons'},
        # upgrade style
        {
            'pythonpath_link_path': '%(name)s/odoo/upgrade',
            'pythonpath_link_target': 'd',
        },
    ]):
        r = repos[name] = make_repo(name)
        env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': r.name,
            'required_statuses': 'default',
            'group_id': False,
            **conf,
        })
    setreviewers(*project.repo_ids)
    env['runbot_merge.events_sources'].create([{'repository': r.name} for r in project.repo_ids])
    #endregion
    #region repos setup
    for repo_name, r in repos.items():
        with r:
            r.make_commits(
                None,
                Commit('initial', tree={
                    'x': '1',
                    f'{repo_name}/b': '2',
                    f'{repo_name}/c': '3',
                }),
                ref='heads/master',
            )
    #endregion
    #region setup PR
    with (r := repos['b']):
        [c] = r.make_commits('master', Commit('second', tree={'b/c': '42'}), ref='heads/other')
        pr = r.make_pr(target='master', title='title', head='other')
    env.run_crons()
    with (r := repos['b']):
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
        r.post_status(c, 'success')
    env.run_crons()
    # endregion

    pr_id = to_pr(env, pr)
    assert not pr_id.blocked
    staging = env['runbot_merge.stagings'].search([])
    assert staging
    for r in repos.values():
        with r:
            r.post_status('staging.master', 'success')
    env.run_crons()
    assert staging.state == 'success'
    assert not staging.active
    assert staging.hash_pythonpath_layout

    repo_pythonpath = copy.copy(repos['a'])
    repo_pythonpath.name = project.repo_pythonpath_layout
    c = repo_pythonpath.commit('master')
    t = repo_pythonpath.read_tree(c, recursive=True)
    del t['.gitmodules']
    del t['meta.json']

    def name(repo_name):
        return repos[repo_name].name.split('/')[1]
    def ref(repo_name):
        return '@' + repos[repo_name].commit('master').id
    assert t == {
        name('a'): ref('a'),
        name('b'): ref('b'),
        'community/odoo/addons': f'../../../{name("b")}/b',
        f'{name("c")}/odoo/addons': ref('c'),
        f'.repos/{name("d")}': ref('d'),
        f'{name("d")}/odoo/upgrade': f'../../../.repos/{name("d")}/d',
    }
