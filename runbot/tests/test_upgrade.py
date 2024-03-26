import getpass
import logging
from unittest.mock import patch, mock_open
from odoo.exceptions import UserError
from odoo.tools import mute_logger
from .common import RunbotCase

_logger = logging.getLogger(__name__)


class TestUpgradeFlow(RunbotCase):

    def setUp(self):
        super().setUp()
        self.upgrade_flow_setup()

    def upgrade_flow_setup(self):
        self.start_patcher('find_patcher', 'odoo.addons.runbot.common.find', 0)
        self.additionnal_setup()

        self.master_bundle = self.branch_server.bundle_id
        self.config_test = self.env['runbot.build.config'].create({'name': 'Test'})
        #################
        # upgrade branch
        #################
        self.repo_upgrade = self.env['runbot.repo'].create({
            'name': 'upgrade',
            'project_id': self.project.id,
            'manifest_files': False,
        })
        self.remote_upgrade = self.env['runbot.remote'].create({
            'name': 'bla@example.com:base/upgrade',
            'repo_id': self.repo_upgrade.id,
            'token': '123',
        })
        self.branch_upgrade = self.Branch.create({
            'name': 'master',
            'remote_id': self.remote_upgrade.id,
            'is_pr': False,
            'head': self.Commit.create({
                'name': '123abc789',
                'repo_id': self.repo_upgrade.id,
            }).id,
        })

        self.default_category = self.env.ref('runbot.default_category')
        self.nightly_category = self.env.ref('runbot.nightly_category')


        #######################
        # Basic test config
        #######################

        # 0. Basic configs
        # 0.1. Install
        self.config_step_install = self.env['runbot.build.config.step'].create({
            'name': 'all',
            'job_type': 'install_odoo',
        })
        self.config_install = self.env['runbot.build.config'].create({
            'name': 'Install config',
            'step_order_ids': [
                (0, 0, {'step_id': self.config_step_install.id}),
            ],
        })

        # 0.2. Restore and upgrade
        self.step_restore = self.env['runbot.build.config.step'].create({
            'name': 'restore',
            'job_type': 'restore',
            'restore_rename_db_suffix': False,
        })
        self.step_test_upgrade = self.env['runbot.build.config.step'].create({
            'name': 'test_upgrade',
            'job_type': 'test_upgrade',
        })
        self.test_upgrade_config = self.env['runbot.build.config'].create({
            'name': 'Upgrade server',
            'step_order_ids': [
                (0, 0, {'step_id': self.step_restore.id}),
                (0, 0, {'step_id': self.step_test_upgrade.id}),
            ],
        })


        # 1. Template demo + nodemo
        self.config_step_install_no_demo = self.env['runbot.build.config.step'].create({
            'name': 'no-demo-all',
            'job_type': 'install_odoo',
        })
        self.config_step_upgrade_complement = self.env['runbot.build.config.step'].create({
            'name': 'upgrade_complement',
            'job_type': 'configure_upgrade_complement',
            'upgrade_config_id': self.test_upgrade_config.id,
        })

        self.config_template = self.env['runbot.build.config'].create({
            'name': 'Template config',
            'step_order_ids': [
                (0, 0, {'step_id': self.config_step_install.id}),
                (0, 0, {'step_id': self.config_step_install_no_demo.id}),
                (0, 0, {'step_id': self.config_step_upgrade_complement.id}),
            ],
        })
        self.trigger_template = self.env['runbot.trigger'].create({
            'name': 'Template',
            'dependency_ids': [(4, self.repo_server.id), (4, self.repo_addons.id)],
            'config_id': self.config_template.id,
            'project_id': self.project.id,
            'category_id': self.default_category.id,
        })


        # 2. upgrade to current
        self.step_upgrade_to_current = self.env['runbot.build.config.step'].create({
            'name': 'upgrade',
            'job_type': 'configure_upgrade',
            'upgrade_to_current': True,
            'upgrade_from_previous_major_version': True,
            'upgrade_from_last_intermediate_version': True,
            'upgrade_flat': True,
            'upgrade_config_id': self.test_upgrade_config.id,
            'upgrade_dbs': [
                (0, 0, {'config_id': self.config_template.id, 'db_pattern': 'all', 'min_target_version_id': self.master_bundle.version_id.id}),
                (0, 0, {'config_id': self.config_template.id, 'db_pattern': 'no-demo-all'}),
            ],
        })
        self.config_upgrade_to_current = self.env['runbot.build.config'].create({
            'name': 'Upgrade server',
            'step_order_ids': [(0, 0, {'step_id': self.step_upgrade_to_current.id})],
        })
        self.trigger_upgrade_to_current = self.env['runbot.trigger'].create({
            'name': 'Server upgrade',
            'repo_ids': [(4, self.repo_upgrade.id), (4, self.repo_server.id), (4, self.repo_addons.id)],
            'config_id': self.config_upgrade_to_current.id,
            'project_id': self.project.id,
            'upgrade_dumps_trigger_id': self.trigger_template.id,
        })

        # update the template trigger to cross reference the upgrade trigger
        self.trigger_template.upgrade_dumps_trigger_id = self.trigger_upgrade_to_current

        # 3. upgrade between stable (master upgrade)
        self.step_upgrade_stable = self.env['runbot.build.config.step'].create({
            'name': 'upgrade',
            'job_type': 'configure_upgrade',
            'upgrade_to_major_versions': True,
            'upgrade_from_previous_major_version': True,
            'upgrade_flat': True,
            'upgrade_config_id': self.test_upgrade_config.id,
            'upgrade_dbs': [
                (0, 0, {'config_id': self.config_template.id, 'db_pattern': 'all', 'min_target_version_id': self.master_bundle.version_id.id}),
                (0, 0, {'config_id': self.config_template.id, 'db_pattern': 'no-demo-all'}),
            ],
        })
        self.config_upgrade_stable = self.env['runbot.build.config'].create({
            'name': 'Upgrade server',
            'step_order_ids': [(0, 0, {'step_id': self.step_upgrade_stable.id})],
        })
        self.trigger_upgrade_stable = self.env['runbot.trigger'].create({
            'name': 'Server upgrade',
            'repo_ids': [(4, self.repo_upgrade.id)],
            'dependency_ids': [(4, self.repo_server.id), (4, self.repo_addons.id)],
            'config_id': self.config_upgrade_stable.id,
            'project_id': self.project.id,
            'upgrade_dumps_trigger_id': self.trigger_template.id,
        })

        self.config_all = self.env['runbot.build.config'].create({'name': 'Demo'})
        self.config_all_no_demo = self.env['runbot.build.config'].create({'name': 'No demo'})

        ##########
        # Nightly
        ##########
        self.config_single_module = self.env['runbot.build.config'].create({'name': 'Single'})
        self.trigger_single_nightly = self.env['runbot.trigger'].create({
            'name': 'Nighly server',
            'dependency_ids': [(4, self.repo_server.id), (4, self.repo_addons.id)],
            'config_id': self.config_single_module.id,
            'project_id': self.project.id,
            'category_id': self.nightly_category.id,
        })


        self.step_upgrade_all = self.env['runbot.build.config.step'].create({
            'name': 'upgrade',
            'job_type': 'configure_upgrade',
            'upgrade_to_master': True,
            'upgrade_to_major_versions': True,
            'upgrade_from_previous_major_version': True,
            'upgrade_from_all_intermediate_version': True,
            'upgrade_flat': False,
            'upgrade_config_id': self.test_upgrade_config.id,
            'upgrade_dbs': [
                (0, 0, {'config_id': self.config_install.id, 'db_pattern': '*'}),
            ],
        })
        self.upgrade_config_nighly = self.env['runbot.build.config'].create({
            'name': 'Upgrade',
            'step_order_ids': [(0, 0, {'step_id': self.step_upgrade_all.id})],
        })
        self.trigger_upgrade_nightly = self.env['runbot.trigger'].create({
            'name': 'Upgrade',
            'dependency_ids': [(4, self.repo_upgrade.id)],
            'config_id': self.upgrade_config_nighly.id,
            'project_id': self.project.id,
            'upgrade_dumps_trigger_id': self.trigger_single_nightly.id,
            'category_id': self.nightly_category.id,
        })

        self.batches_per_version = {}
        self.nightly_batches_per_version = {}
        self.template_per_version = {}
        self.nightly_single_per_version = {}
        with mute_logger('odoo.addons.runbot.models.commit'):
            self.create_version('master')
            self.create_version('15.0')
            self.create_version('16.0')
            self.create_version('saas-16.3')
            self.create_version('17.0')
            self.create_version('saas-17.1')
            self.create_version('saas-17.2')
            self.create_version('saas-17.3')

    def create_version(self, name):
        intname = int(''.join(c for c in name if c.isdigit())) if name != 'master' else 0
        if name != 'master':
            branch_server = self.Branch.create({
                'name': name,
                'remote_id': self.remote_server.id,
                'is_pr': False,
                'head': self.Commit.create({
                    'name': 'server%s' % intname,
                    'repo_id': self.repo_server.id,
                }).id,
            })
            branch_addons = self.Branch.create({
                'name': name,
                'remote_id': self.remote_addons.id,
                'is_pr': False,
                'head': self.Commit.create({
                    'name': 'addons%s' % intname,
                    'repo_id': self.repo_addons.id,
                }).id,
            })
        else:
            branch_server = self.branch_server
            branch_addons = self.branch_addons

        host = self.env['runbot.host']._get_current()

        self.assertEqual(branch_server.bundle_id, branch_addons.bundle_id)
        bundle = branch_server.bundle_id
        self.assertEqual(bundle.name, name)
        bundle.is_base = True
        batch = bundle._force()
        batch._prepare()
        build_per_config = {build.params_id.config_id: build for build in batch.slot_ids.mapped('build_id')}
        template_build = build_per_config[self.config_template]
        template_build.database_ids = [
            (0, 0, {'name': '%s-%s' % (template_build.dest, 'all')}),
            (0, 0, {'name': '%s-%s' % (template_build.dest, 'no-demo-all')}),
        ]
        template_build.host = host.name
        template_build.local_state = 'done'
        batch.state = 'done'

        self.batches_per_version[name] = batch
        self.template_per_version[name] = template_build

        batch_nigthly = bundle._force(self.nightly_category.id)
        batch_nigthly._prepare()
        self.assertEqual(batch_nigthly.category_id, self.nightly_category)
        build_per_config = {build.params_id.config_id: build for build in batch_nigthly.slot_ids.mapped('build_id')}
        single_module_build = build_per_config[self.config_single_module]
        for module in ['web', 'base', 'website']:
            module_child = single_module_build._add_child({'config_id': self.config_install.id}, description=f"Installing module {module}")
            module_child.database_ids = [
                (0, 0, {'name': '%s-%s' % (module_child.dest, module)}),
            ]
            module_child.host = host.name
        single_module_build.local_state = 'done'
        batch_nigthly.state = 'done'
        self.nightly_batches_per_version[name] = batch_nigthly
        self.nightly_single_per_version[name] = single_module_build


    def test_all(self):
        # Test setup
        self.assertEqual(self.branch_server.bundle_id, self.branch_upgrade.bundle_id)
        self.assertTrue(self.branch_upgrade.bundle_id.is_base)
        self.assertTrue(self.branch_upgrade.bundle_id.version_id)
        self.assertEqual(self.trigger_upgrade_to_current.upgrade_step_id, self.step_upgrade_to_current)

        with self.assertRaises(UserError):
            self.step_upgrade_to_current.job_type = 'install_odoo'
            self.trigger_upgrade_to_current.flush_recordset(['upgrade_step_id'])

        master_batch = self.master_bundle._force()
        master_batch._prepare()
        self.assertEqual(master_batch.reference_batch_ids, self.env['runbot.batch'].browse([b.id for b in self.batches_per_version.values()]))
        master_upgrade_build = master_batch.slot_ids.filtered(lambda slot: slot.trigger_id == self.trigger_upgrade_to_current).build_id
        self.assertEqual(master_upgrade_build.params_id.builds_reference_ids, (self.template_per_version['17.0'] | self.template_per_version['saas-17.3']))
        master_upgrade_build._schedule()
        self.start_patcher('fetch_local_logs', 'odoo.addons.runbot.models.host.Host._fetch_local_logs', [])  # the local logs have to be empty
        master_upgrade_build._schedule()
        self.assertEqual(master_upgrade_build.local_state, 'done')
        self.assertEqual(len(master_upgrade_build.children_ids), 4)

        [b_17_master_demo, b_17_master_no_demo, b_173_master_demo, b_173_master_no_demo] = master_upgrade_build.children_ids

        def assertOk(build, from_build, target_build, db_suffix):
            self.assertEqual(build.params_id.upgrade_to_build_id, target_build)
            self.assertEqual(build.params_id.upgrade_from_build_id, from_build)
            self.assertEqual(build.params_id.dump_db.build_id, from_build)
            self.assertEqual(build.params_id.dump_db.db_suffix, db_suffix)
            self.assertEqual(build.params_id.config_id, self.test_upgrade_config)

        assertOk(b_17_master_demo, self.template_per_version['17.0'], master_upgrade_build, 'all')
        assertOk(b_17_master_no_demo, self.template_per_version['17.0'], master_upgrade_build, 'no-demo-all')
        assertOk(b_173_master_demo, self.template_per_version['saas-17.3'], master_upgrade_build, 'all')
        assertOk(b_173_master_no_demo, self.template_per_version['saas-17.3'], master_upgrade_build, 'no-demo-all')

        self.assertEqual(b_17_master_demo.params_id.commit_ids.repo_id, self.repo_server | self.repo_upgrade | self.repo_addons)

        # upgrade repos tests
        upgrade_stable_build = master_batch.slot_ids.filtered(lambda slot: slot.trigger_id == self.trigger_upgrade_stable).build_id
        upgrade_stable_build._schedule()
        upgrade_stable_build._schedule()
        self.assertEqual(upgrade_stable_build.local_state, 'done')
        self.assertEqual(len(upgrade_stable_build.children_ids), 2)

        [b_15_16, b_16_17] = upgrade_stable_build.children_ids
        assertOk(b_15_16, self.template_per_version['15.0'], self.template_per_version['16.0'], 'no-demo-all')
        assertOk(b_16_17, self.template_per_version['16.0'], self.template_per_version['17.0'], 'no-demo-all')
        # nightly
        nightly_batch = self.master_bundle._force(self.nightly_category.id)
        nightly_batch._prepare()
        nightly_batches = self.env['runbot.batch'].browse([b.id for b in self.nightly_batches_per_version.values()])

        self.assertEqual(self.trigger_upgrade_nightly.upgrade_step_id, self.step_upgrade_all)
        self.assertEqual(nightly_batch.reference_batch_ids, nightly_batches)
        upgrade_nightly = nightly_batch.slot_ids.filtered(lambda slot: slot.trigger_id == self.trigger_upgrade_nightly).build_id


        nightly_single_builds = self.env['runbot.build'].browse([b.id for b in self.nightly_single_per_version.values()])
        self.assertEqual(upgrade_nightly.params_id.builds_reference_ids, nightly_single_builds)
        upgrade_nightly._schedule()
        upgrade_nightly._schedule()
        to_version_builds = upgrade_nightly.children_ids
        self.assertEqual(upgrade_nightly.local_state, 'done')
        self.assertEqual(len(to_version_builds), 4)
        self.assertEqual(
            to_version_builds.mapped('params_id.upgrade_to_build_id.params_id.version_id.name'),
            ['15.0', '16.0', '17.0', 'master'],
        )
        self.assertEqual(
            to_version_builds.mapped('params_id.upgrade_from_build_id.params_id.version_id.name'),
            [],
        )
        self.assertEqual(
            to_version_builds.mapped('description'),
            [
                'Testing migration to 15.0',
                'Testing migration to 16.0',
                'Testing migration to 17.0',
                'Testing migration to master',
            ],
        )


        for build in to_version_builds:
            build._schedule()  # starts builds
            self.assertEqual(build.local_state, 'testing')
            build._schedule()  # makes result and end build
            self.assertEqual(build.local_state, 'done')

        self.assertEqual(to_version_builds.mapped('global_state'), ['done', 'waiting', 'waiting', 'waiting'], 'One build have no child, other should wait for children')

        from_version_builds = to_version_builds.children_ids

        self.assertEqual(
            from_version_builds.mapped('description'),
            [
                'Testing migration from 15.0 to 16.0',
                'Testing migration from 16.0 to 17.0',
                'Testing migration from saas-16.3 to 17.0',
                'Testing migration from 17.0 to master',
                'Testing migration from saas-17.1 to master',
                'Testing migration from saas-17.2 to master',
                'Testing migration from saas-17.3 to master',
            ],
        )

        for build in from_version_builds:
            build._schedule()
            self.assertEqual(build.local_state, 'testing')
            build._schedule()
            self.assertEqual(build.local_state, 'done')
            self.assertEqual(len(build.children_ids), 3)

        self.assertEqual(from_version_builds.mapped('global_state'), ['waiting'] * 7)

        db_builds = from_version_builds.children_ids
        self.assertEqual(len(db_builds), 3 * 7)

        self.assertEqual(
            db_builds.mapped('params_id.config_id'), self.test_upgrade_config
        )

        self.assertEqual(
            db_builds.mapped('params_id.commit_ids.repo_id'),
            self.repo_upgrade,
            "Build should only have the upgrade commit"
        )

        b15_16 = to_version_builds[1].children_ids[0].children_ids
        self.assertEqual(
            b15_16.mapped('params_id.upgrade_to_build_id.params_id.version_id.name'),
            ['16.0']
        )
        self.assertEqual(
            b15_16.mapped('params_id.upgrade_from_build_id.params_id.version_id.name'),
            ['15.0']
        )
        b173_master = to_version_builds[-1].children_ids[-1].children_ids
        self.assertEqual(
            b173_master.mapped('params_id.upgrade_to_build_id.params_id.version_id.name'),
            ['master'],
        )
        self.assertEqual(
            b173_master.mapped('params_id.upgrade_from_build_id.params_id.version_id.name'),
            ['saas-17.3'],
        )
        self.assertEqual(
            [b.params_id.dump_db.db_suffix for b in b173_master],
            ['base', 'web', 'website'],
        )
        current_build = db_builds[0]

        self.start_patcher('docker_state', 'odoo.addons.runbot.models.build.docker_state', 'END')
        for current_build in db_builds:
            suffix = current_build.params_id.dump_db.db_suffix
            source_dest = current_build.params_id.dump_db.build_id.dest

            def docker_run_restore(cmd, *args, **kwargs):
                dump_url = f'https://host.runbot.com/runbot/static/build/{source_dest}/logs/{source_dest}-{suffix}.zip'
                zip_name = f'{source_dest}-{suffix}.zip'
                db_name = f'{current_build.dest}-{suffix}'
                self.assertEqual(
                    str(cmd).split(' && '),
                    [
                        'mkdir /data/build/restore',
                        'cd /data/build/restore',
                        f'wget {dump_url}',
                        f'unzip -q {zip_name}',
                        'echo "### restoring filestore"',
                        f'mkdir -p /data/build/datadir/filestore/{db_name}',
                        f'mv filestore/* /data/build/datadir/filestore/{db_name}',
                        'echo "### restoring db"',
                        f'psql -q {db_name} < dump.sql',
                        'cd /data/build',
                        'echo "### cleaning"',
                        'rm -r restore',
                        'echo "### listing modules"',
                        f'psql {db_name} -c "select name from ir_module_module where state = \'installed\'" -t -A > /data/build/logs/restore_modules_installed.txt',
                        'echo "### restore" "successful"'
                    ]
                )
            self.patchers['docker_run'].side_effect = docker_run_restore
            #current_build.host = host.name
            current_build._schedule()()
            self.patchers['docker_run'].assert_called()

            def docker_run_upgrade(cmd, *args, ro_volumes=False, **kwargs):
                user = getpass.getuser()
                self.assertTrue(ro_volumes.pop(f'/home/{user}/.odoorc').startswith(self.env['runbot.runbot']._path('build')))
                self.assertEqual(
                    list(ro_volumes.keys()), [
                        '/data/build/addons',
                        '/data/build/server',
                        '/data/build/upgrade',
                    ],
                    "other commit should have been added automaticaly"
                )
                self.assertEqual(
                    str(cmd),
                    'python3 server/server.py {addons_path} --no-xmlrpcs --no-netrpc -u all -d {db_name} --stop-after-init --max-cron-threads=0'.format(
                        addons_path='--addons-path addons,server/addons,server/core/addons',
                        db_name=f'{current_build.dest}-{suffix}')
                )
            self.patchers['docker_run'].side_effect = docker_run_upgrade
            current_build._schedule()()

            with patch('builtins.open', mock_open(read_data='')):
                current_build._schedule()
            self.assertEqual(current_build.local_state, 'done')

            self.assertEqual(current_build.global_state, 'done')
            # self.assertEqual(current_build.global_result, 'ok')

        self.assertEqual(self.patchers['docker_run'].call_count, 3 * 7 * 2)

        self.assertEqual(from_version_builds.mapped('global_state'), ['done'] * 7)

        self.assertEqual(to_version_builds.mapped('global_state'), ['done'] * 4)

        # Test complement upgrades

        bundle_17 = self.master_bundle.previous_major_version_base_id
        bundle_173 = self.master_bundle.intermediate_version_base_ids[-1]
        self.assertEqual(bundle_17.name, '17.0')
        self.assertEqual(bundle_173.name, 'saas-17.3')

        batch13 = bundle_17._force()
        batch13._prepare()
        upgrade_complement_build_17 = batch13.slot_ids.filtered(lambda slot: slot.trigger_id == self.trigger_template).build_id
        self.assertEqual(upgrade_complement_build_17.params_id.config_id, self.config_template)

        def _run_install_odoo(build):
            self.env['runbot.database'].create({
                'name': f'{build.dest}-{build.active_step.name}',
                'build_id': build.id,
            })

        with (
            patch('odoo.addons.runbot.models.build_config.ConfigStep._run_install_odoo', side_effect=_run_install_odoo),
            patch('odoo.addons.runbot.models.build_config.ConfigStep._make_results', return_value=None),
        ):
            self.assertEqual(len(upgrade_complement_build_17.database_ids), 0)
            # install db 1
            upgrade_complement_build_17._schedule()
            self.assertEqual(len(upgrade_complement_build_17.database_ids), 1)
            # install db 2
            upgrade_complement_build_17._schedule()
            self.assertEqual(len(upgrade_complement_build_17.database_ids), 2)

            # create upgrade builds
            upgrade_complement_build_17._schedule()

        self.assertEqual(len(upgrade_complement_build_17.children_ids), 5)
        master_child = upgrade_complement_build_17.children_ids[0]
        self.assertEqual(master_child.params_id.upgrade_from_build_id, upgrade_complement_build_17)
        self.assertEqual(master_child.params_id.dump_db.db_suffix, 'all')
        self.assertEqual(master_child.params_id.config_id, self.test_upgrade_config)
        self.assertEqual(master_child.params_id.upgrade_to_build_id.params_id.version_id.name, 'master')


class TestUpgrade(RunbotCase):

    def test_exceptions_in_env(self):
        env_var = self.env['runbot.upgrade.exception']._generate()
        self.assertEqual(env_var, False)
        self.env['runbot.upgrade.exception'].create({'elements': 'field:module.some_field \nview:some_view_xmlid'})
        self.env['runbot.upgrade.exception'].create({'elements': 'field:module.some_field2'})
        env_var = self.env['runbot.upgrade.exception']._generate()
        self.assertEqual(env_var, 'suppress_upgrade_warnings=field:module.some_field,view:some_view_xmlid,field:module.some_field2')
