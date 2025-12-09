from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import RunbotCase, RunbotCaseMinimalSetup


class TestBranch(RunbotCase):

    def test_base_fields(self):
        self.assertEqual(self.branch_odoo.branch_url, 'https://example.com/base/odoo/tree/master')

    def test_pull_request(self):
        mock_github = self.patchers['github_patcher']
        mock_github.return_value = {
            'base': {'ref': 'master'},
            'head': {'label': 'foo-dev:bar_branch', 'repo': {'full_name': 'foo-dev/bar'}},
            'title': '[IMP] Title',
            'body': 'Body',
            'user': {
                'login': 'Pr author'
            },
        }
        pr = self.Branch.create({
            'remote_id': self.remote_odoo.id,
            'name': '12345',
            'is_pr': True,
        })
        self.assertEqual(pr.name, '12345')
        self.assertEqual(pr.branch_url, 'https://example.com/base/odoo/pull/12345')
        self.assertEqual(pr.target_branch_name, 'master')
        self.assertEqual(pr.pull_head_name, 'foo-dev:bar_branch')

    def test_branch_dname_search(self):
        # Basic branch
        self.assertEqual(
            self.branch_odoo,
            self.Branch.search([('dname', '=', self.branch_odoo.dname)]),
        )
        self.assertEqual(
            self.branch_odoo,
            self.Branch.search([('dname', '=', self.branch_odoo.dname.replace(':', '#'))]),
        )
        # Basic pr
        self.assertEqual(
            self.dev_pr,
            self.Branch.search([('dname', '=', self.dev_pr.dname)]),
        )
        # PR from pull request url
        self.assertEqual(
            self.dev_pr,
            self.Branch.search([('dname', '=', self.dev_pr.branch_url)]),
        )
        # With subtree of PR url
        self.assertEqual(
            self.dev_pr,
            self.Branch.search([('dname', '=', self.dev_pr.branch_url + '/files')]),
        )
        # Branch with a . inside of it
        branch = self.Branch.create({
            'name': '18.0-test',
            'remote_id': self.remote_odoo.id,
            'is_pr': False,
        })
        self.assertEqual(
            branch,
            self.Branch.search([('dname', '=', branch.dname)]),
        )

class TestBranchRelations(RunbotCase):

    def setUp(self):
        super(TestBranchRelations, self).setUp()

        def create_base(name):
            branch = self.Branch.create({
                'remote_id': self.remote_odoo.id,
                'name': name,
                'is_pr': False,
            })
            branch.bundle_id.is_base = True
            return branch
        self.master = self.branch_odoo
        create_base('11.0')
        create_base('saas-11.1')
        create_base('12.0')
        create_base('saas-12.3')
        create_base('13.0')
        create_base('saas-13.1')
        self.last = create_base('saas-13.2')
        self.env['runbot.bundle'].flush_model()
        self.env['runbot.version'].flush_model()

    def test_relations_master_dev(self):
        b = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': 'master-test-tri',
                'is_pr': False,
            })
        self.assertEqual(b.bundle_id.base_id.name, 'master')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, '13.0')
        self.assertEqual(b.bundle_id.intermediate_version_base_ids.mapped('name'), ['saas-13.1', 'saas-13.2'])

    def test_relations_master(self):
        b = self.master
        self.assertEqual(b.bundle_id.base_id.name, 'master')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, '13.0')
        self.assertEqual(b.bundle_id.intermediate_version_base_ids.mapped('name'), ['saas-13.1', 'saas-13.2'])

    def test_relations_no_intermediate(self):
        b = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': 'saas-13.1-test-tri',
                'is_pr': False,
            })
        self.assertEqual(b.bundle_id.base_id.name, 'saas-13.1')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, '13.0')
        self.assertEqual(b.bundle_id.intermediate_version_base_ids.mapped('name'), [])

    def test_relations_old_branch(self):
        b = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': '11.0-test-tri',
                'is_pr': False,
            })
        self.assertEqual(b.bundle_id.base_id.name, '11.0')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, False)
        self.assertEqual(sorted(b.bundle_id.intermediate_version_base_ids.mapped('name')), [])

    def test_relations_closest_forced(self):
        b = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': 'master-test-tri',
                'is_pr': False,
            })
        self.assertEqual(b.bundle_id.base_id.name, 'master')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, '13.0')
        self.assertEqual(sorted(b.bundle_id.intermediate_version_base_ids.mapped('name')), ['saas-13.1', 'saas-13.2'])

        b.bundle_id.defined_base_id = self.last.bundle_id

        self.assertEqual(b.bundle_id.base_id.name, 'saas-13.2')
        self.assertEqual(b.bundle_id.previous_major_version_base_id.name, '13.0')
        self.assertEqual(sorted(b.bundle_id.intermediate_version_base_ids.mapped('name')), ['saas-13.1'])

    def test_relations_no_match(self):
        b = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': 'icantnamemybranches',
                'is_pr': False,
            })

        self.assertEqual(b.bundle_id.base_id.name, 'master')

    def test_relations_pr(self):
        dev_branch = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': 'master-test-tri-imp',
                'is_pr': False,
            })

        self.patchers['github_patcher'].return_value = {
            'base': {'ref': 'master-test-tri'},
            'head': {'label': 'dev:master-test-tri-imp', 'repo': {'full_name': 'dev/odoo'}},
            'title': '[IMP] Title',
            'body': 'Body',
            'user': {
                'login': 'Pr author'
            },
        }
        pr_branch = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': '100',
                'is_pr': True,
            })

        bundle = pr_branch.bundle_id

        self.assertEqual(bundle.name, 'master-test-tri-imp')
        self.assertEqual(bundle.base_id.name, 'master')
        self.assertEqual(bundle.previous_major_version_base_id.name, '13.0')
        self.assertEqual(sorted(bundle.intermediate_version_base_ids.mapped('name')), ['saas-13.1', 'saas-13.2'])
        self.assertIn(dev_branch, bundle.branch_ids)

class TestBranchForbidden(RunbotCase):
    """Test that a branch matching the repo forbidden regex, goes to dummy bundle"""

    def test_forbidden(self):
        dummy_bundle = self.remote_odoo_dev.repo_id.project_id.dummy_bundle_id
        self.remote_odoo_dev.repo_id.forbidden_regex = '^bad_name.+'
        with mute_logger("odoo.addons.runbot.models.branch"):
            branch = self.Branch.create({
                    'remote_id': self.remote_odoo_dev.id,
                    'name': 'bad_name-evil',
                    'is_pr': False,
                })
            self.assertEqual(branch.bundle_id.id, dummy_bundle.id, "A forbidden branch should goes in dummy bundle")


class TestBranchIsBase(RunbotCaseMinimalSetup):
    """Test that a branch matching the is_base_regex goes in the right bundle"""

    def setUp(self):
        super(TestBranchIsBase, self).setUp()
        self.additionnal_setup()

    def test_is_base_regex_on_main_remote(self):
        branch = self.Branch.create({
                'remote_id': self.remote_odoo.id,
                'name': 'saas-13.4',
                'is_pr': False,
            })
        self.assertTrue(branch.bundle_id.is_base, "A branch matching the is_base_regex parameter should create is_base bundle")
        self.assertTrue(branch.bundle_id.sticky, "A branch matching the is_base_regex parameter should create sticky bundle")

        staging = self.Branch.create({
            'remote_id': self.remote_odoo.id,
            'name': 'staging.saas-13.4',
            'is_pr': False,
        })
        self.assertEqual(staging.bundle_id.base_id, branch.bundle_id, 'The staging branch should have the correct base bundle')

    def test_host(self):
        r10 = self.env['runbot.host'].create({'name': 'runbot10.odoo.com'})
        r12 = self.env['runbot.host'].create({'name': 'runbot12.odoo.com', 'assigned_only': True})

        branch = self.Branch.create({
                'remote_id': self.remote_odoo.id,
                'name': 'saas-13.4-runbotinexist-test',
                'is_pr': False,
            })
        self.assertFalse(branch.bundle_id.host_id)
        branch = self.Branch.create({
                'remote_id': self.remote_odoo.id,
                'name': 'saas-13.4-runbot10-test',
                'is_pr': False,
        })
        self.assertEqual(branch.bundle_id.host_id, r10)
        branch = self.Branch.create({
                'remote_id': self.remote_odoo.id,
                'name': 'saas-13.4-runbot_x-test',
                'is_pr': False,
        })
        self.assertEqual(branch.bundle_id.host_id, r12)

    @mute_logger("odoo.addons.runbot.models.branch")
    def test_is_base_regex_on_dev_remote(self):
        """Test that a branch matching the is_base regex on a secondary remote goes to the dummy bundles."""
        dummy_bundle = self.repo_enterprise.project_id.dummy_bundle_id

        # master branch on dev remote
        initial_addons_dev_commit = self.Commit.create({
            'name': 'dddddd',
            'tree_hash': '0dddddd',
            'repo_id': self.repo_enterprise.id,
            'date': '2015-09-30',
            'subject': 'Please use the right repo',
            'author': 'oxo',
            'author_email': 'oxo@somewhere.com'
        })

        branch_addons_dev = self.Branch.create({
            'name': 'master',
            'remote_id': self.remote_enterprise_dev.id,
            'is_pr': False,
            'head': initial_addons_dev_commit.id
        })
        self.assertEqual(branch_addons_dev.bundle_id, dummy_bundle, "A branch matching the is_base_regex should on a secondary repo should goes in dummy bundle")

        # saas-12.3 branch on dev remote
        initial_server_dev_commit = self.Commit.create({
            'name': 'bbbbbb',
            'tree_hash': '0bbbbbb',
            'repo_id': self.repo_odoo.id,
            'date': '2014-05-26',
            'subject': 'Please use the right repo',
            'author': 'oxo',
            'author_email': 'oxo@somewhere.com'
        })

        branch_odoo_dev = self.Branch.create({
            'name': 'saas-12.3',
            'remote_id': self.remote_odoo_dev.id,
            'is_pr': False,
            'head': initial_server_dev_commit.id
        })
        self.assertEqual(branch_odoo_dev.bundle_id, dummy_bundle, "A branch matching the is_base_regex should on a secondary repo should goes in dummy bundle")

        # 12.0 branch on dev remote
        mistaken_commit = self.Commit.create({
            'name': 'eeeeee',
            'tree_hash': '0eeeeee',
            'repo_id': self.repo_odoo.id,
            'date': '2015-06-27',
            'subject': 'dummy commit',
            'author': 'brol',
            'author_email': 'brol@somewhere.com'
        })

        branch_mistake_dev = self.Branch.create({
            'name': '12.0',
            'remote_id': self.remote_odoo_dev.id,
            'is_pr': False,
            'head': mistaken_commit.id
        })
        self.assertEqual(branch_mistake_dev.bundle_id, dummy_bundle, "A branch matching the is_base_regex should on a secondary repo should goes in dummy bundle")


class TestBundleTeam(RunbotCase):

    def test_bundle_team_attribution(self):
        self.stop_patcher('isfile')
        self.stop_patcher('isdir')  # needed to create the user avatar
        create_context = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}
        committer_user = new_test_user(self.env, login='testrunbot', name='testrunbot (tru)', email='trut@somewhere.com', context=create_context)
        github_user = new_test_user(self.env, login='github_author', name='github author (gaut)', email='gaut@somewhere.com', context=create_context)
        github_user.github_login = 'gaut_github'

        team = self.env['runbot.team'].create({
            'name': 'Test Team',
            'project_id': self.project.id,
        })

        team.user_ids += committer_user

        branch = self.Branch.create({
            'remote_id': self.remote_odoo_dev.id,
            'name': 'saas-19.1-test-tru',
            'is_pr': False,
        })

        module = self.env['runbot.module'].create({'name': 'test_module'})
        self.env['runbot.module.ownership'].create({
            'module_id': module.id,
            'team_id': team.id,
        })

        bundle = self.env['runbot.bundle'].search([('name', '=', branch.name)])
        self.assertEqual(bundle.team_id, team)
        self.assertEqual(bundle.author_ids, committer_user, 'The only involved author should be the one based on bundle ngram')

        # now test that a team can be manually set on a bundle
        other_team = self.env['runbot.team'].create({
            'name': 'Another Test Team',
            'project_id': self.project.id,
        })

        bundle.team_id = other_team
        self.assertEqual(bundle.team_id, other_team)

        self.patchers['github_patcher'].return_value = {
            'base': {'ref': 'saas-19.1'},
            'head': {'label': 'dev:saas-19.1-test-tru', 'repo': {'full_name': 'dev/odoo'}},
            'title': '[IMP] Title',
            'body': 'Body',
            'user': {
                'login': github_user.github_login,
            },
        }
        pr_branch = self.Branch.create({
                'remote_id': self.remote_odoo_dev.id,
                'name': '100',
                'is_pr': True,
            })

        self.assertIn(pr_branch, bundle.branch_ids)
        self.assertIn(github_user, bundle.author_ids)
