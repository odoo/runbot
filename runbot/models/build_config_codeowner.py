import re
from collections import defaultdict

from odoo import models, fields
from ..common import markdown_escape


class ConfigStep(models.Model):
    _inherit = 'runbot.build.config.step'

    job_type = fields.Selection(selection_add=[('codeowner', 'Codeowner')], ondelete={'codeowner': 'cascade'})
    fallback_reviewer = fields.Char('Fallback reviewer')

    def _pr_by_commit(self, build, prs):
        pr_by_commit = {}
        for commit_link in build.params_id.commit_link_ids:
            commit = commit_link.commit_id
            repo_pr = prs.filtered(lambda pr: pr.remote_id.repo_id == commit_link.commit_id.repo_id)
            if repo_pr:
                if len(repo_pr) > 1:
                    build._log('', 'More than one open pr in this bundle for %s: %s' % (commit.repo_id.name, [pr.name for pr in repo_pr]), level='ERROR')
                    build.local_result = 'ko'
                    return {}
                build._log('', 'PR [%s](%s) found for repo **%s**', repo_pr.dname, repo_pr.branch_url, commit.repo_id.name, log_type='markdown')
                pr_by_commit[commit_link] = repo_pr
            else:
                build._log('', 'No pr for repo %s, skipping' % commit.repo_id.name)
        return pr_by_commit

    def _codeowners_regexes(self, codeowners, version_id):
        regexes = {}
        for codeowner in codeowners:
            github_teams = codeowner._get_github_teams()
            if github_teams and codeowner.regex and (codeowner._match_version(version_id)):
                regex = codeowner.regex.strip()
                teams, codeowner_ids = regexes.get(regex) or (set(), self.env['runbot.codeowner'])
                regexes[regex] = (teams | {t.strip() for t in github_teams}, codeowner_ids | codeowner)
        return [(regex, teams, codeowner_ids) for regex, (teams, codeowner_ids) in regexes.items()]

    def _reviewer_per_file(self, files, regexes, ownerships, repo, build=None):
        reviewer_per_file = {}
        reasons_per_file = {}
        for file in files:
            file_reviewers = set()
            reasons = defaultdict(list)
            for regex, teams, codeowner_ids in regexes:
                if re.match(regex, file):
                    if not teams or 'none' in teams:
                        file_reviewers = None
                        break  # blacklisted, break
                    file_reviewers |= teams
                    for team in teams:
                        reasons[team].append({'type': 'regex', 'codeowner_ids': codeowner_ids})
            if file_reviewers is None:
                continue

            file_module = repo._get_module(file)
            for ownership in ownerships:
                if file_module == ownership.module_id.name and not ownership.is_fallback and ownership.team_id.github_team not in file_reviewers:
                    file_reviewers.add(ownership.team_id.github_team)
                    reasons[ownership.team_id.github_team].append({'type': 'module', 'ownership_id': ownership})
            # fallback
            if not file_reviewers:
                for ownership in ownerships:
                    if file_module == ownership.module_id.name:
                        file_reviewers.add(ownership.team_id.github_team)
                        reasons[ownership.team_id.github_team].append({'type': 'fallback_module', 'ownership_id': ownership})
            if not file_reviewers:
                if len(file.split('/')) <= 2:
                    if build:
                        build._log('', 'File %s is at the root level and it looks like it could be a mistake, remove it or ensure that a codeowner rule is added for this file', file, log_type='markdown', level="ERROR")
                elif self.fallback_reviewer:
                    file_reviewers.add(self.fallback_reviewer)
                    reasons[self.fallback_reviewer].append({'type': 'fallback_reviewer'})
            reviewer_per_file[file] = file_reviewers
            reasons_per_file[file] = dict(reasons)
        return reviewer_per_file, reasons_per_file

    def _create_team_review_links(self, build, pr, new_reviewers, reviewer_per_file, restored_reviews=None):
        teams = self.env['runbot.team'].search([('github_team', 'in', list(new_reviewers))])
        restored_keys = {
            (review.team_id.id, review.filename)
            for review in (restored_reviews or self.env['runbot.team.review'])
        }

        vals_list = []
        for team in teams:
            for file in sorted(file for file, file_reviewers in reviewer_per_file.items() if team.github_team in file_reviewers and (team.id, file) not in restored_keys):
                vals_list.append({
                    'team_id': team.id,
                    'branch_id': pr.id,
                    'build_id': build.id,
                    'filename': file,
                })
        return self.env['runbot.team.review'].create(vals_list)

    def _update_removed_reviews(self, build, pr, files):
        current_files = set(files)
        review_model = self.env['runbot.team.review']
        for review in review_model.search([
            ('branch_id', '=', pr.id),
            ('filename', 'not in', sorted(current_files)),
            ('removal', '=', False),
        ]):
            review.write({'removal': True, 'reviewer_id': review.reviewer_id.id})

        restored_reviews = review_model
        restored_keys = set()
        for review in review_model.search([
            ('branch_id', '=', pr.id),
            ('removal', '=', True),
        ], order='build_id desc'):
            if review.filename not in current_files:
                continue
            key = (review.team_id.id, review.filename)
            if key in restored_keys:
                continue
            restored_keys.add(key)
            restored_reviews |= review
            review.write({'removal': False, 'build_id': build.id, 'reviewer_id': review.reviewer_id.id})
        return restored_reviews

    def _run_codeowner(self, build):
        bundle = build.params_id.create_batch_id.bundle_id
        if bundle.is_base:
            build._log('', 'Skipping base bundle')
            return

        if bundle.disable_codeowner:
            build._log('', 'Skipping explicitly, disabled codeowner')
            return

        if not self._check_limits(build):
            return

        build_repositories = build.params_id.commit_link_ids.commit_id.repo_id
        prs = bundle.branch_ids.filtered(lambda branch: branch.is_pr and branch.alive and (branch.remote_id.repo_id in build_repositories))

        # skip draft pr
        draft_prs = prs.filtered(lambda pr: pr.draft)
        if draft_prs:
            build._log('', 'Some pr are draft, skipping: %s' % ','.join([pr.name for pr in draft_prs]), level='WARNING')
            build.local_result = 'warn'
            return

        # remove forwardport pr
        ICP = self.env['ir.config_parameter'].sudo()

        fw_bot = ICP.get_param('runbot.runbot_forwardport_author')
        fw_prs = prs.filtered(lambda pr: pr.pr_author == fw_bot and len(pr.reflog_ids) <= 1)
        if fw_prs:
            build._log('', 'Ignoring forward port pull request: %s' % ','.join([pr.name for pr in fw_prs]))
            prs -= fw_prs

        if not prs:
            return

        # check prs targets
        valid_targets = set([(branch.remote_id, branch.name) for branch in bundle.base_id.branch_ids])
        invalid_target_prs = prs.filtered(lambda pr: (pr.remote_id, pr.target_branch_name) not in valid_targets)

        if invalid_target_prs:
            # this is not perfect but detects prs inside odoo-dev or with invalid target
            build._log('', 'Some pr have an invalid target: %s' % ','.join([pr.name for pr in invalid_target_prs]), level='ERROR')
            build.local_result = 'ko'
            return

        build._checkout()

        pr_by_commit = self._pr_by_commit(build, prs)
        ownerships = self.env['runbot.module.ownership'].search([('team_id.github_team', '!=', False)])
        codeowners = build.env['runbot.codeowner'].search([('project_id', '=', bundle.project_id.id)])
        regexes = self._codeowners_regexes(codeowners, build.params_id.version_id)
        modified_files = self._modified_files(build, pr_by_commit.keys())

        if not modified_files:
            for pr in pr_by_commit.values():
                self._update_removed_reviews(build, pr, [])
            return

        skippable_teams = self.env['runbot.team'].search(['|', ('skip_team_pr', '=', True), ('skip_fw_pr', '=', True)])
        for commit_link, files in modified_files.items():
            build._log('', 'Checking %s codeowner regexed on %s files' % (len(regexes), len(files)))
            pr = pr_by_commit[commit_link]
            restored_reviews = self._update_removed_reviews(build, pr, files)
            reviewers = set()
            reviewer_per_file, _reasons_per_file = self._reviewer_per_file(files, regexes, ownerships, commit_link.commit_id.repo_id, build)
            for file, file_reviewers in reviewer_per_file.items():
                href = 'https://%s/blob/%s/%s' % (commit_link.branch_id.remote_id.base_url, commit_link.commit_id.name, file.split('/', 1)[-1])
                if file_reviewers:
                    build._log('', 'Adding %s to reviewers for file [%s](%s)', ', '.join(sorted(file_reviewers)), file, href, log_type='markdown')
                    reviewers |= file_reviewers
                else:
                    build._log('', 'No reviewer for file [%s](%s)', file, href, log_type='markdown')

            if reviewers:
                new_reviewers = reviewers - set((pr.reviewers or '').split(','))
                if new_reviewers:
                    author_skippable_teams = skippable_teams.filtered(lambda team: team.skip_team_pr and team.github_team in new_reviewers and pr.pr_author and pr.pr_author.lower() in team._get_members_logins())
                    author_skipped_teams = set(author_skippable_teams.mapped('github_team'))
                    if author_skipped_teams:
                        new_reviewers = new_reviewers - author_skipped_teams
                        build._log('', 'Skipping teams %s since author is part of the team members', sorted(author_skipped_teams), log_type='markdown')

                    fw_skippable_teams = skippable_teams.filtered(lambda team: team.skip_fw_pr and team.github_team in new_reviewers and pr.pr_author == fw_bot)
                    fw_skipped_teams = set(fw_skippable_teams.mapped('github_team'))
                    if fw_skipped_teams:
                        new_reviewers = new_reviewers - fw_skipped_teams
                        build._log('', 'Skipping teams %s (ignore forwardport)', sorted(fw_skipped_teams), log_type='markdown')

                    new_reviewers = sorted(new_reviewers)

                    build._log('', 'Requesting review for pull request [%s](%s): %s', pr.dname, pr.branch_url, ', '.join(new_reviewers), log_type='markdown')
                    response = pr.remote_id._github('/repos/:owner/:repo/pulls/%s/requested_reviewers' % pr.name, {"team_reviewers": list(new_reviewers)}, ignore_errors=False)
                    pr._update_branch_infos(response)
                    pr['reviewers'] = ','.join(sorted(reviewers))
                    self._create_team_review_links(build, pr, new_reviewers, reviewer_per_file, restored_reviews=restored_reviews)
                else:
                    build._log('', 'All reviewers are already on pull request [%s](%s)', pr.dname, pr.branch_url, log_type='markdown')
