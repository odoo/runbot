from odoo import models, fields, api
from unittest.mock import patch
from odoo.tools import mute_logger

import logging
_logger = logging.getLogger(__name__)

# after this point, not realy a repo buisness
class Runbot(models.AbstractModel):
    _inherit = 'runbot.runbot'

    @api.model
    @patch('odoo.addons.runbot.models.repo.Remote._github')
    @patch('odoo.addons.runbot.models.repo.Repo._git')
    def _create_demo_data(self, mock_git, mock_github):
        mock_github.return_value = False
        project = self.env.ref('runbot.main_project')
        bundles = self.env['runbot.bundle'].browse(
            self.env['ir.model.data'].search([
                ('module', '=', 'runbot_populate'), ('model', '=', 'runbot.bundle')
            ]).mapped('res_id')
        ).filtered(lambda bundle: bundle.project_id == project)
        bundles |= project.master_bundle_id
        bundles = bundles.sorted('is_base', reverse=True)

        existing_bundle = bundles.search([('project_id', '=', project.id)])
        expected_bundle = bundles | project.dummy_bundle_id

        assert expected_bundle == existing_bundle

        if bundles.branch_ids:
            # only populate data if no branch are found
            return

        if not bundles.branch_ids:
            pr = True
            count = 1000
            for bundle in bundles:
                _logger.info(bundle.name)
                for repo in bundle.project_id.repo_ids:
                    main_remote = repo.main_remote_id
                    dev_remote = next((remote for remote in repo.remote_ids if remote != main_remote), main_remote)
                    if bundle.is_base:
                        dev_remote = main_remote
                    self.env['runbot.branch'].create({'remote_id': dev_remote.id, 'name': bundle.name, 'is_pr': False})
                    if not bundle.is_base:
                        mock_github.return_value = {
                            'base': {
                                'ref': bundle.base_id.name
                            },
                            'head': {
                                'label': '%s:%s' % (dev_remote.owner, bundle.name),
                                'repo': {'full_name': '%s/%s' % (dev_remote.owner, dev_remote.repo_name)}
                            },
                            'title': '[IMP] Title',
                            'body': 'Body',
                            'user': {
                                'login': 'Pr author'
                            },
                        }
                        branch = self.env['runbot.branch'].create({
                            'remote_id': main_remote.id,
                            'name': str(count),
                            'is_pr': True,
                        })
                        count += 1
                        branch.flush()

                    if 'partial' in bundle.name:
                        break

                if not bundle.is_base:
                    pr = not pr

        security_config = self.env.ref('runbot_populate.runbot_build_config_security')
        linting_config = self.env.ref('runbot_populate.runbot_build_config_linting')

        for bundle in bundles:
            nb_batch = 4 if bundle.sticky else 2
            for i in range(nb_batch):
                values = {
                    'last_update': fields.Datetime.now(),
                    'bundle_id': bundle.id,
                    'state': 'preparing',
                }
                batch = self.env['runbot.batch'].create(values)
                bundle.last_batch = batch
                for repo in bundle.project_id.repo_ids:
                    commit = self.env['runbot.commit']._get('%s00b%s0000ba%s000' % (repo.id, bundle.id, batch.id), repo.id, {
                        'author': 'Author',
                        'author_email': 'author@example.com',
                        'committer': 'Committer',
                        'committer_email': 'committer@example.com',
                        'subject': '[IMP] core: come imp',
                        'date': fields.Datetime.now(),
                    })
                    branches = bundle.branch_ids.filtered(lambda b: b.remote_id.repo_id == repo)
                    for branch in branches:
                        branch.head = commit
                        batch._new_commit(branch)

                def git(command):
                    if command[0] == 'merge-base':
                        _, sha1, sha2 = command
                        return sha1 if sha1 == sha2 else sha2 #if bundle.is_base else '%s_%s' % (sha1, sha2)
                    elif command[0] == 'rev-list':
                        _, _, _, shas = command
                        sha1, sha2 = shas.split('...')
                        return '0\t0' if command[1] == command[2] else '3\t5'
                    elif command[0] == 'diff':
                        _, _, sha1, sha2 = command
                        return '' if sha1 == sha2 else '0 5 _\n1 8 _'
                    else:
                        _logger.info(command)

                mock_git.side_effect = git
                with mute_logger('odoo.addons.runbot.models.batch'):
                    batch._prepare()
                
                if i != nb_batch - 1:
                    for slot in batch.slot_ids:
                        if slot.build_id:
                            build = slot.build_id
                            with mute_logger('odoo.addons.runbot.models.build'):
                                build._log('******','Starting step X', level='SEPARATOR')
                                build._log('******','Some log')
                                for config in (linting_config, security_config):
                                    child = build._add_child({'config_id': config.id})
                                    build._log('create_build', 'created with config %s' % config.name, log_type='subbuild', path=str(child.id))
                                    child.local_state = 'done'
                                    child.local_result = 'ok'
                                child.description = "Description for security"
                                build._log('******','Step x finished')
                                build._log('******','Starting step Y', level='SEPARATOR')
                                build._log('******','Some log', level='ERROR')
                                build._log('******','Some log\n with multiple lines', level='ERROR')
                                build._log('******','**Some** *markdown* [log](http://example.com)', log_type='markdown')
                                build._log('******','Step x finished', level='SEPARATOR')
                            
                            build.local_state = 'done'
                            build.local_result = 'ok' if bundle.sticky else 'ko'


                batch._process()
