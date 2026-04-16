
import datetime
import subprocess

from ..common import os, RunbotException, make_github_session, transactioncache
import shutil

from odoo import models, fields, api
from odoo.tools import file_open
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class Commit(models.Model):
    _name = 'runbot.commit'
    _description = "Commit"

    _commit_unique = models.Constraint(
        'unique (name, repo_id, rebase_on_id)',
        "Commit must be unique to ensure correct duplicate matching",
    )
    name = fields.Char('SHA')
    tree_hash = fields.Char('Tree hash', readonly=True)
    repo_id = fields.Many2one('runbot.repo', string='Repo group')
    date = fields.Datetime('Commit date')
    author = fields.Char('Author')
    author_email = fields.Char('Author Email')
    committer = fields.Char('Committer')
    committer_email = fields.Char('Committer Email')
    subject = fields.Text('Subject')
    dname = fields.Char('Display name', compute='_compute_dname')
    rebase_on_id = fields.Many2one('runbot.commit', 'Rebase on commit')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'date' not in vals:
                vals['date'] = datetime.datetime.now()
        return super().create(vals_list)

    def _get_commit_infos(self, sha, repo):
        fields = ['date', 'author', 'author_email', 'committer', 'committer_email', 'subject', 'tree_hash']
        pretty_format = '%x00'.join(['%ct', '%an', '%ae', '%cn', '%ce', '%s', '%T'])
        vals = {}
        try:
            vals = dict(zip(fields, repo._git(['show', '-s', f'--pretty=format:{pretty_format}', sha]).split('\x00')))
            vals['date'] = datetime.datetime.fromtimestamp(int(vals['date']))
        except subprocess.CalledProcessError as e:
            _logger.warning('git show failed with message %s', e.output.decode())
        return vals

    def _get(self, name, repo_id, vals=None, rebase_on_id=False):
        commit = self.search([('name', '=', name), ('repo_id', '=', repo_id), ('rebase_on_id', '=', rebase_on_id)])
        if not commit:
            if not vals:
                repo = self.env['runbot.repo'].browse(repo_id)
                vals = self._get_commit_infos(name, repo)
            commit = self.env['runbot.commit'].create({**vals, 'name': name, 'repo_id': repo_id, 'rebase_on_id': rebase_on_id})
        return commit

    def _rebase_on(self, commit):
        if self == commit:
            return self
        return self._get(self.name, self.repo_id.id, self.read()[0], commit.id)

    def _list_files(self, patterns):
        #example: git ls-files --with-tree=abcf390f90dbdd39fd61abc53f8516e7278e0931 ':(glob)addons/*/*.py' ':(glob)odoo/addons/*/*.py'
        # note that glob is needed to avoid the star matching **
        self.ensure_one()
        self._fetch()
        return self.repo_id._git(['ls-files', '--with-tree', self.tree_hash, *patterns]).split('\n')

    def _list_available_modules(self):
        addons_paths = (self.repo_id.addons_paths or '').split(',')
        patterns = []
        for manifest_file_name in self.repo_id.manifest_files.split(','):  # '__manifest__.py' '__openerp__.py'
            for addon_path in addons_paths:
                addon_path = addon_path or '.'
                patterns.append(f':(glob){addon_path}/*/{manifest_file_name}')
        for file_path in self._list_files(patterns):
            if file_path:
                elems = file_path.rsplit('/', 2)
                if len(elems) == 3:
                    addons_path, module, manifest_file_name = elems
                else:
                    addons_path = ''
                    module, manifest_file_name = elems
                yield (addons_path, module, manifest_file_name)

    @transactioncache  # hack to avoid to fetch two time the same commit inside the same transaction
    def _fetch(self):
        try:
            self.repo_id._fetch(self.name)
        except RunbotException:
            self.repo_id._fetch(self.tree_hash)

    def _export(self, build):
        """Export a git repo into a sources"""
        #  TODO add automated tests
        self.ensure_one()
        self._fetch()
        if not self.env['runbot.commit.export'].search([('build_id', '=', build.id), ('commit_id', '=', self.id)]):
            self.env['runbot.commit.export'].create({'commit_id': self.id, 'build_id': build.id})
        export_path = self._source_path()

        if os.path.isdir(export_path):
            _logger.info('git export: exporting to %s (already exists)', export_path)
            return export_path

        _logger.info('git export: exporting to %s (new)', export_path)
        os.makedirs(export_path)

        export_commit = self
        if self.rebase_on_id:
            export_commit = self.rebase_on_id
            self.rebase_on_id.repo_id._fetch(export_commit.name)

        export_sha = export_commit.tree_hash

        p1 = subprocess.Popen(['git', '--git-dir=%s' % self.repo_id.path, 'archive', export_sha, '--mtime', self.date.strftime('%Y-%m-%d %H:%M:%S')], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(['tar', '-xC', export_path], stdin=p1.stdout, stdout=subprocess.PIPE)


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
            p1 = subprocess.Popen(['git', '--git-dir=%s' % self.repo_id.path, 'diff', '%s...%s' % (export_commit.name, self.name)], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
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

        return export_path

    def _read_source(self, file, mode='r'):
        file_path = self._source_path(file)
        try:
            with file_open(file_path, mode) as f:
                return f.read()
        except:
            return False

    @transactioncache
    def _git_show_file(self, file):
        return self._git_show_files([file])[0]

    def _git_show_files(self, files):
        self.ensure_one()
        if not files:
            return []

        self.repo_id._fetch(self.name)

        queries = "\n".join([f"{self.name}:{f}" for f in files]) + "\n"

        try:
            buffer = self.repo_id._git(
                ['cat-file', '--batch'],
                input_data=queries,
                raw=True,
            )
        except subprocess.CalledProcessError:
            return [False] * len(files)

        results = []
        offset = 0
        buffer_len = len(buffer)
        while offset < buffer_len:
            newline_idx = buffer.find(b'\n', offset)
            if newline_idx == -1:
                break
            header = buffer[offset:newline_idx].decode('utf-8')
            offset = newline_idx + 1
            try:
                size_in_bytes = int(header.rsplit(' ', 1)[-1])
            except ValueError:  # most likely missing
                results.append(False)
                continue
            results.append(buffer[offset : offset + size_in_bytes].decode('utf-8', errors='replace'))
            offset += size_in_bytes + 1
        return results

    def _source_path(self, *paths):
        if not self.tree_hash:
            vals = self._get_commit_infos(self.name, self.repo_id)
            if vals.get('tree_hash'):
                self.tree_hash = vals['tree_hash']
            else:
                raise ValidationError("Commit %s has no tree hash, cannot export" % self.name)
        export_name = self.tree_hash
        if self.rebase_on_id:
            export_name = '%s_%s' % (self.name, self.rebase_on_id.name)
        return self.repo_id._source_path(export_name, *paths)

    @api.depends('name', 'repo_id.name')
    def _compute_dname(self):
        for commit in self:
            commit.dname = '%s:%s' % (commit.repo_id.name, commit.name[:8])

    def _github_status(self, build, context, state, target_url, description=None, ci_strategy="all"):
        if state == 'failure':
            state = 'error'  # github does not make a big difference between error and failure, lets simplify
        self.ensure_one()
        build_id = build.id if build else False
        Status = self.env['runbot.commit.status']
        last_status = Status.search([('commit_id', '=', self.id), ('context', '=', context)], order='id desc', limit=1)
        if last_status and last_status.state == state:
            _logger.info('Skipping already sent status %s:%s for %s', context, state, self.name)
            return

        if ci_strategy != 'all' and state == 'pending':
            _logger.debug("skipping github pending status for build %s and ci %s", build_id, context)
            return

        if ci_strategy == 'errors' and state != 'error' and last_status.state not in ('error', 'failure'):
            _logger.info("skipping github status for build %s, ci_strategy is failures", build_id)
            return

        last_status = Status.create({
            'build_id': build_id,
            'commit_id': self.id,
            'context': context,
            'state': state,
            'target_url': target_url,
            'description': description or context,
            'to_process': True,
        })
        return last_status

    def _get_last_statuses(self):
        status_list = self.env['runbot.commit.status'].search([('commit_id', '=', self.id)], order='id desc')
        last_status_by_context = {}
        for status in status_list:
            if status.context in last_status_by_context:
                continue
            last_status_by_context[status.context] = status
        return status_list, last_status_by_context


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
        commits_status = self.search([('to_process', '=', True), ('build_id.create_date', '<', datetime.datetime.now() - datetime.timedelta(minutes=2))], order='create_date DESC, id DESC')
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
                            session_cache[remote.token] = make_github_session(remote.token)
                        session = session_cache[remote.token]
                        _logger.info(
                            "github updating %s status %s to %s in repo %s",
                            status['context'], commit_status.commit_id.name, status['state'], remote.name)
                        remote._github('/repos/:owner/:repo/statuses/%s' % commit_status.commit_id.name,
                            status,
                            ignore_errors=True,
                            session=session
                        )
                commit_status.sent_date = datetime.datetime.now()
            else:
                _logger.info('Skipping outdated status for %s %s', commit_status.context, commit_status.commit_id.name)



class CommitExport(models.Model):
    _name = 'runbot.commit.export'
    _description = 'Commit export'

    build_id = fields.Many2one('runbot.build', index=True)
    commit_id = fields.Many2one('runbot.commit')

    host = fields.Char(related='build_id.host', store=True)
