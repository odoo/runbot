""" Implements direct (unstaged) patching.

Useful for massive data changes which are a pain to merge normally but very
unlikely to break things (e.g. i18n), fixes so urgent staging is an unacceptable
overhead, or FBI backdoors oh wait forget about that last one.
"""
from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from email import message_from_string, policy
from email.utils import parseaddr
from typing import Union

from odoo import models, fields, api, Command
from odoo.exceptions import ValidationError
from odoo.tools.mail import plaintext2html

from .pull_requests import Branch
from .. import git

_logger = logging.getLogger(__name__)
FILE_PATTERN = re.compile(r"""
# paths with spaces don't work well as the path can be followed by a timestamp
# (in an unspecified format?)
---\x20(?P<prefix_a>a/)?(?P<file_from>\S+)(:?\s.*)?\n
\+\+\+\x20(?P<prefix_b>b/)?(?P<file_to>\S+)(:?\s.*)?\n
@@\x20-(\d+(,\d+)?)\x20\+(\d+(,\d+)?)\x20@@ # trailing garbage
""", re.VERBOSE)


Authorship = Union[None, tuple[str, str], tuple[str, str, str]]
@dataclass
class ParseResult:
    kind: str
    author: Authorship
    committer: Authorship
    message: str
    patch: str


def expect(line: str, starts_with: str, message: str) -> str:
    if not line.startswith(starts_with):
        raise ValidationError(message)
    return line


def parse_show(p: Patch) -> ParseResult:
    # headers are Author, Date or Author, AuthorDate, Commit, CommitDate
    # commit message is indented 4 spaces
    lines = (l + '\n' for l in p.patch.splitlines(keepends=False))
    if not next(lines, '').startswith("commit "):
        raise ValidationError("Invalid patch")
    name, email = parseaddr(
        expect(next(lines, ''), "Author:", "Missing author")
            .split(maxsplit=1)[1])
    date: str = next(lines, '')
    header, date = date.split(maxsplit=1)
    author = (name, email, date)
    if header.startswith("Date:"):
        committer = author
    elif header.startswith("AuthorDate:"):
        commit = expect(next(lines, ''), "Commit:", "Missing committer")
        commit_date = expect(next(lines, ''), "CommitDate:", "Missing commit date")
        name, email = parseaddr(commit.split(maxsplit=1)[1])
        committer = (name, email, commit_date.split(maxsplit=1)[1])
    else:
        raise ValidationError(
            "Invalid patch: expected 'Date:' or 'AuthorDate:' pseudo-header, "
            f"found {header}.\nOnly 'medium' and 'fuller' formats are supported")

    # skip possible extra headers before the message
    while not next(lines, ' ').isspace():
        continue

    body = []
    while (l := next(lines, '')) and l.startswith('    '):
        body.append(l.removeprefix('    '))

    # remainder should be the patch
    patch = "".join(
        line for line in lines
        if not line.startswith("git --diff ")
        if not line.startswith("index ")
    )
    return ParseResult(kind="show", author=author, committer=committer, message="".join(body).rstrip(), patch=patch)


def parse_format_patch(p: Patch) -> ParseResult:
    m = message_from_string(p.patch, policy=policy.default)
    if m.is_multipart():
        raise ValidationError("multipart patches are not supported.")

    name, email = parseaddr(m['from'])
    author = (name, email, m['date'])
    msg = re.sub(r'^\[PATCH( \d+/\d+)?\] ', '', m['subject'])
    body, _, rest = m.get_payload().partition('---\n')
    if body:
        msg += '\n\n' + body.replace('\r\n', '\n')

    # split off the signature, per RFC 3676 § 4.3.
    # leave the diffstat in as it *should* not confuse tooling?
    patch, _, _ = rest.partition("-- \n")
    # git (diff, show, format-patch) adds command and index headers to every
    # file header, which patch(1) chokes on, strip them... but maybe this should
    # extract the udiff sections instead?
    patch = re.sub(
        "^(git --diff .*|index .*)\n",
        "",
        patch,
        flags=re.MULTILINE,
    )
    return ParseResult(kind="format-patch", author=author, committer=author, message=msg, patch=patch)


class PatchFailure(Exception):
    pass


class PatchFile(models.TransientModel):
    _name = "runbot_merge.patch.file"
    _description = "metadata for single file to patch"
    _order = "create_date desc"

    name = fields.Char()


class Patch(models.Model):
    _name = "runbot_merge.patch"
    _inherit = ['mail.thread']
    _description = "Unstaged direct-application patch"

    active = fields.Boolean(default=True, tracking=True)
    repository = fields.Many2one('runbot_merge.repository', required=True, tracking=True)
    target = fields.Many2one('runbot_merge.branch', required=True, tracking=True)
    commit = fields.Char(size=40, string="commit to cherry-pick, must be in-network", tracking=True)

    patch = fields.Text(string="unified diff to apply", tracking=True)
    format = fields.Selection([
        ("format-patch", "format-patch"),
        ("show", "show"),
    ], compute="_compute_patch_meta")
    author = fields.Char(compute="_compute_patch_meta")
    # TODO: should be a datetime, parse date
    authordate = fields.Char(compute="_compute_patch_meta")
    committer = fields.Char(compute="_compute_patch_meta")
    # TODO: should be a datetime, parse date
    commitdate = fields.Char(compute="_compute_patch_meta")
    file_ids = fields.One2many(
        "runbot_merge.patch.file",
        compute="_compute_patch_meta",
    )
    message = fields.Text(compute="_compute_patch_meta")

    _sql_constraints = [
        ('patch_contents_either', 'check ((commit is null) != (patch is null))', 'Either the commit or patch must be set, and not both.'),
    ]

    @api.depends("patch")
    def _compute_patch_meta(self) -> None:
        File = self.env['runbot_merge.patch.file']
        for p in self:
            if r := p._parse_patch():
                p.format = r.kind
                match r.author:
                    case [name, email]:
                        p.author = f"{name} <{email}>"
                    case [name, email, date]:
                        p.author = f"{name} <{email}>"
                        p.authordate = date
                match r.committer:
                    case [name, email]:
                        p.committer = f"{name} <{email}>"
                    case [name, email, date]:
                        p.committer = f"{name} <{email}>"
                        p.commitdate = date
                p.file_ids = File.concat(*(
                    File.new({'name': m['file_to']})
                    for m in FILE_PATTERN.finditer(p.patch)
                ))
                p.message = r.message
            else:
                p.update({
                    'format': False,
                    'author': False,
                    'authordate': False,
                    'committer': False,
                    'commitdate': False,
                    'file_ids': False,
                    'message': False,
                })

    def _parse_patch(self) -> ParseResult | None:
        if not self.patch:
            return None

        if self.patch.startswith("commit "):
            return parse_show(self)
        elif self.patch.startswith("From "):
            return parse_format_patch(self)
        else:
            raise ValidationError("Only `git show` and `git format-patch` formats are supported")

    def _auto_init(self):
        super()._auto_init()
        self.env.cr.execute("""
        CREATE INDEX IF NOT EXISTS runbot_merge_patch_active
            ON runbot_merge_patch (target) WHERE active
        """)

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('active') is not False for vals in vals_list):
            self.env.ref("runbot_merge.staging_cron")._trigger()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("active") is not False:
            self.env.ref("runbot_merge.staging_cron")._trigger()
        return super().write(vals)

    @api.constrains('patch')
    def _validate_patch(self):
        for p in self:
            patch = p._parse_patch()
            if not patch:
                continue

            has_files = False
            for m in FILE_PATTERN.finditer(patch.patch):
                has_files = True
                if m['file_from'] != m['file_to'] and m['file_from'] != '/dev/null':
                    raise ValidationError("Only patches updating a file in place are supported, not creation, removal, or renaming.")
            if not has_files:
                raise ValidationError("Patches should have files they patch, found none.")

    def _apply_patches(self, target: Branch) -> bool:
        patches = self.search([('target', '=', target.id)], order='id asc')
        if not patches:
            return True

        commits = {}
        repos = {}
        for p in patches:
            repos[p.repository] = git.get_local(p.repository).check(True)
            commits.setdefault(p.repository, set())
            if p.commit:
                commits[p.repository].add(p.commit)

        for patch in patches:
            patch.active = False
            r = repos[patch.repository]
            remote = git.source_url(patch.repository)
            if (cs := commits.pop(patch.repository, None)) is not None:
                # first time encountering a repo, fetch the branch and commits
                r.fetch(remote, f"+refs/heads/{target.name}:refs/heads/{target.name}", *cs, no_tags=True)

            _logger.info(
                "Applying %s to %r (in %s)",
                patch,
                patch.target.display_name,
                patch.repository.name,
            )
            try:
                if patch.commit:
                    c = patch._apply_commit(r)
                else:
                    c = patch._apply_patch(r)
            except Exception as e:
                if isinstance(e, PatchFailure):
                    subject = "Unable to apply patch"
                else:
                    subject = "Unknown error while trying to apply patch"
                _logger.error("%s:\n%s", subject, str(e))
                patch.message_post(body=plaintext2html(e), subject=subject)
                continue
            # `.` is the local "remote", so this updates target to c
            r.fetch(".", f"{c}:{target.name}")

            # push patch by patch, avoids sync issues and in most cases we have 0~1 patches
            res = r.check(False).stdout()\
                .with_config(encoding="utf-8")\
                .push(remote, f"{target.name}:{target.name}")
            ## one of the repos is out of consistency, loop around to new staging?
            if res.returncode:
                _logger.warning(
                    "Unable to push result of %s\nout:\n%s\nerr:\n%s",
                    patch.id,
                    res.stdout,
                    res.stderr,
                )
                return False

        return True

    def _apply_commit(self, r: git.Repo) -> str:
        r = r.check(True).stdout().with_config(encoding="utf-8")
        # TODO: maybe use git-rev-list instead?
        sha = r.show('--no-patch', '--pretty=%H', self.target.name).stdout.strip()
        target = r.show('--no-patch', '--pretty=%an%n%ae%n%ai%n%cn%n%ce%n%ci%n%B', self.commit)
        # retrieve metadata of cherrypicked commit
        author_name, author_email, author_date, committer_name, committer_email, committer_date, body =\
            target.stdout.strip().split("\n", 6)

        res = r.check(False).merge_tree(sha, self.commit)
        if res.returncode:
            _conflict_info, _, informational = res.stdout.partition('\n\n')
            raise PatchFailure(informational)

        return r.commit_tree(
            tree=res.stdout.strip(),
            parents=[sha],
            message=body.strip(),
            author=(author_name, author_email, author_date),
            committer=(committer_name, committer_email, committer_date),
        ).stdout.strip()

    def _apply_patch(self, r: git.Repo) -> str:
        p = self._parse_patch()
        def reader(_r, f):
            return pathlib.Path(tmpdir, f).read_text(encoding="utf-8")

        prefix = 0
        read = set()
        patched = {}
        for m in FILE_PATTERN.finditer(p.patch):
            if not prefix and (m['prefix_a'] or m['file_from'] == '/dev/null') and m['prefix_b']:
                prefix = 1

            if m['file_from'] != '/dev/null':
                read.add(m['file_from'])
            patched[m['file_to']] = reader

        archiver = r.stdout(True)
        # if the parent is checked then we can't get rid of the kwarg and popen doesn't support it
        archiver._config.pop('check', None)
        archiver.runner = subprocess.Popen
        with contextlib.ExitStack() as stack,\
             tempfile.TemporaryDirectory() as tmpdir:
            # if there's no file to *update*, `archive` will extract the entire
            # tree which is unnecessary
            if read:
                out = stack.enter_context(archiver.archive(self.target.name, *read))
                tf = stack.enter_context(tarfile.open(fileobj=out.stdout, mode='r|'))
                tf.extractall(tmpdir)
            patch = subprocess.run(
                ['patch', f'-p{prefix}', '--directory', tmpdir, '--verbose'],
                input=p.patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
            )
            if patch.returncode:
                raise PatchFailure("\n---------\n".join(filter(None, [p.patch, patch.stdout.strip(), patch.stderr.strip()])))
            new_tree = r.update_tree(self.target.name, patched)

        sha = r.stdout().with_config(encoding='utf-8')\
            .show('--no-patch', '--pretty=%H', self.target.name)\
            .stdout.strip()
        return r.commit_tree(
            tree=new_tree,
            parents=[sha],
            message=p.message,
            author=p.author,
            committer=p.committer,
        ).stdout.strip()
