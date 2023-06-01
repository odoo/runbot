
import subprocess

from ..common import os, RunbotException, _make_github_session
import glob
import shutil

from odoo import models, fields, api, registry
import logging

_logger = logging.getLogger(__name__)


class Commit(models.Model):
    _name = 'runbot.commit'
    _description = "Commit"

    _sql_constraints = [
        (
            "commit_unique",
            "unique (name, repo_id, rebase_on_id)",
            "Commit must be unique to ensure correct duplicate matching",
        )
    ]
    name = fields.Char('SHA')
    repo_id = fields.Many2one('runbot.repo', string='Repo group')
    date = fields.Datetime('Commit date')
    author = fields.Char('Author')
    author_email = fields.Char('Author Email')
    committer = fields.Char('Committer')
    committer_email = fields.Char('Committer Email')
    subject = fields.Text('Subject')
    dname = fields.Char('Display name', compute='_compute_dname')
    rebase_on_id = fields.Many2one('runbot.commit', 'Rebase on commit')

    def _get(self, name, repo_id, vals=None, rebase_on_id=False):
        commit = self.search([('name', '=', name), ('repo_id', '=', repo_id), ('rebase_on_id', '=', rebase_on_id)])
        if not commit:
            commit = self.env['runbot.commit'].create({**(vals or {}), 'name': name, 'repo_id': repo_id, 'rebase_on_id': rebase_on_id})
        return commit

    def _rebase_on(self, commit):
        if self == commit:
            return self
        return self._get(self.name, self.repo_id.id, self.read()[0], commit.id)

    def _get_available_modules(self):
        for manifest_file_name in self.repo_id.manifest_files.split(','):  # '__manifest__.py' '__openerp__.py'
            for addons_path in (self.repo_id.addons_paths or '').split(','):  # '' 'addons' 'odoo/addons'
                sep = os.path.join(addons_path, '*')
                for manifest_path in glob.glob(self._source_path(sep, manifest_file_name)):
                    module = os.path.basename(os.path.dirname(manifest_path))
                    yield (addons_path, module, manifest_file_name)

    def export(self, build):
        """Export a git repo into a sources"""
        #  TODO add automated tests
        self.ensure_one()
        if not self.env['runbot.commit.export'].search([('build_id', '=', build.id), ('commit_id', '=', self.id)]):
            self.env['runbot.commit.export'].create({'commit_id': self.id, 'build_id': build.id})
        export_path = self._source_path()

        if os.path.isdir(export_path):
            _logger.info('git export: exporting to %s (already exists)', export_path)
            return export_path


        _logger.info('git export: exporting to %s (new)', export_path)
        os.makedirs(export_path)

        self.repo_id._fetch(self.name)
        export_sha = self.name
        if self.rebase_on_id:
            export_sha = self.rebase_on_id.name
            self.rebase_on_id.repo_id._fetch(export_sha)

        p1 = subprocess.Popen(['git', '--git-dir=%s' % self.repo_id.path, 'archive', export_sha], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '--mtime', self.date.strftime('%Y-%m-%d %H:%M:%S'), '-xC', export_path], stdin=p1.stdout, stdout=subprocess.PIPE)
        p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
        (_, err) = p2.communicate()
        p1.poll()  # fill the returncode
        if p1.returncode:
            _logger.info("git export: removing corrupted export %r", export_path)
            shutil.rmtree(export_path)
            raise RunbotException("Git archive failed for %s with error code %s. (%s)" % (self.name, p1.returncode, p1.stderr.read().decode()))
        if err:
            _logger.info("git export: removing corrupted export %r", export_path)
            shutil.rmtree(export_path)
            raise RunbotException("Export for %s failed. (%s)" % (self.name, err))

        if self.rebase_on_id:
            # we could be smart here and detect if merge_base == commit, in witch case checkouting base_commit is enough. Since we don't have this info
            # and we are exporting in a custom folder anyway, lets
            _logger.info('Applying patch for %s', self.name)
            p1 = subprocess.Popen(['git', '--git-dir=%s' % self.repo_id.path, 'diff', '%s...%s' % (export_sha, self.name)], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            p2 = subprocess.Popen(['patch', '-p0', '-d', export_path], stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()
            (message, err) = p2.communicate()
            p1.poll()
            if err:
                shutil.rmtree(export_path)
                raise RunbotException("Apply patch failed for %s...%s. (%s)" % (export_sha, self.name, err))
            if p1.returncode or p2.returncode:
                shutil.rmtree(export_path)
                raise RunbotException("Apply patch failed for %s...%s with error code %s+%s. (%s)" % (export_sha, self.name, p1.returncode, p2.returncode, message))

        # migration scripts link if necessary
        icp = self.env['ir.config_parameter']
        ln_param = icp.get_param('runbot_migration_ln', default='')
        migration_repo_id = int(icp.get_param('runbot_migration_repo_id', default=0))
        if ln_param and migration_repo_id and self.repo_id.server_files:
            scripts_dir = self.env['runbot.repo'].browse(migration_repo_id).name
            try:
                os.symlink('/data/build/%s' % scripts_dir,  self._source_path(ln_param))
            except FileNotFoundError:
                _logger.warning('Impossible to create migration symlink')

        return export_path

    def read_source(self, file, mode='r'):
        file_path = self._source_path(file)
        try:
            with open(file_path, mode) as f:
                return f.read()
        except:
            return False

    def _source_path(self, *path):
        export_name = self.name
        if self.rebase_on_id:
            export_name = '%s_%s' % (self.name, self.rebase_on_id.name)
        return os.path.join(self.env['runbot.runbot']._root(), 'sources', self.repo_id.name, export_name, *path)

    @api.depends('name', 'repo_id.name')
    def _compute_dname(self):
        for commit in self:
            commit.dname = '%s:%s' % (commit.repo_id.name, commit.name[:8])

    def _github_status(self, build, context, state, target_url, description=None):
        self.ensure_one()
        Status = self.env['runbot.commit.status']
        last_status = Status.search([('commit_id', '=', self.id), ('context', '=', context)], order='id desc', limit=1)
        if last_status and last_status.state == state:
            _logger.info('Skipping already sent status %s:%s for %s', context, state, self.name)
            return
        last_status = Status.create({
            'build_id': build.id if build else False,
            'commit_id': self.id,
            'context': context,
            'state': state,
            'target_url': target_url,
            'description': description or context,
            'to_process': True,
        })


class CommitLink(models.Model):
    _name = 'runbot.commit.link'
    _description = "Build commit"

    commit_id = fields.Many2one('runbot.commit', 'Commit', required=True, index=True)
    # Link info
    match_type = fields.Selection([('new', 'New head of branch'), ('head', 'Head of branch'), ('base_head', 'Found on base branch'), ('base_match', 'Found on base branch')])  # HEAD, DEFAULT
    branch_id = fields.Many2one('runbot.branch', string='Found in branch')  # Shouldn't be use for anything else than display

    base_commit_id = fields.Many2one('runbot.commit', 'Base head commit', index=True)
    merge_base_commit_id = fields.Many2one('runbot.commit', 'Merge Base commit', index=True)
    base_behind = fields.Integer('# commits behind base')
    base_ahead = fields.Integer('# commits ahead base')
    file_changed = fields.Integer('# file changed')
    diff_add = fields.Integer('# line added')
    diff_remove = fields.Integer('# line removed')


class CommitStatus(models.Model):
    _name = 'runbot.commit.status'
    _description = 'Commit status'
    _order = 'id desc'

    commit_id = fields.Many2one('runbot.commit', string='Commit', required=True, index=True)
    context = fields.Char('Context', required=True)
    state = fields.Char('State', required=True, copy=True)
    build_id = fields.Many2one('runbot.build', string='Build', index=True)
    target_url = fields.Char('Url')
    description = fields.Char('Description')
    sent_date = fields.Datetime('Sent Date')
    to_process = fields.Boolean('Status was not processed yet', index=True)

    def _send_to_process(self):
        commits_status = self.search([('to_process', '=', True)], order='create_date DESC, id DESC')
        if commits_status:
            _logger.info('Sending %s commit status', len(commits_status))
            commits_status._send()

    def _send(self):
        session_cache = {}
        processed = set()
        for commit_status in self.sorted(lambda cs: (cs.create_date, cs.id), reverse=True): # ensure most recent are processed first
            commit_status.to_process = False
            # only send the last status for each commit+context
            key = (commit_status.context, commit_status.commit_id.name)
            if key not in processed:
                processed.add(key)
                status = {
                    'context': commit_status.context,
                    'state': commit_status.state,
                    'target_url': commit_status.target_url,
                    'description': commit_status.description,
                }
                for remote in commit_status.commit_id.repo_id.remote_ids.filtered('send_status'):
                    if not remote.token:
                        _logger.warning('No token on remote %s, skipping status', remote.mapped("name"))
                    else:
                        if remote.token not in session_cache:
                            session_cache[remote.token] = _make_github_session(remote.token)
                        session = session_cache[remote.token]
                        _logger.info(
                            "github updating %s status %s to %s in repo %s",
                            status['context'], commit_status.commit_id.name, status['state'], remote.name)
                        remote._github('/repos/:owner/:repo/statuses/%s' % commit_status.commit_id.name,
                            status,
                            ignore_errors=True,
                            session=session
                        )
                commit_status.sent_date = fields.Datetime.now()
            else:
                _logger.info('Skipping outdated status for %s %s', commit_status.context, commit_status.commit_id.name)



class CommitExport(models.Model):
    _name = 'runbot.commit.export'
    _description = 'Commit export'

    build_id = fields.Many2one('runbot.build', index=True)
    commit_id = fields.Many2one('runbot.commit')

    host = fields.Char(related='build_id.host', store=True)
