from __future__ import annotations

import contextlib
import json
import logging
import typing

from requests import HTTPError

from odoo import fields, models

from ... import utils

if typing.TYPE_CHECKING:
    from ..pull_requests import Repository

_logger = logging.getLogger(__name__)

class Feedback(models.Model):
    """ Queue of feedback comments to send to PR users
    """
    _name = _description = 'runbot_merge.pull_requests.feedback'
    _inherit = ['runbot_merge.queue.retryable']
    _cron_name = 'runbot_merge.feedback_cron'
    _logger = _logger

    repository = fields.Many2one('runbot_merge.repository', required=True, index=True)
    # store the PR number (not id) as we may want to send feedback to PR
    # objects on non-handled branches
    pull_request = fields.Integer(group_operator=None, index=True)
    message = fields.Char()
    close = fields.Boolean()
    reaction = fields.Char()
    token_field = fields.Selection(
        [('github_token', "Mergebot"), ('fp_github_token', 'Forwardport Bot')],
        default='github_token',
        string="Bot User",
        help="Token field (from repo's project) to use to post messages"
    )

    def _process_item(self):
        repo = self.repository
        gh = repo.github(self.token_field)

        message = self.message
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(message or '')
            message = data.get('message')

            if data.get('base'):
                gh('PATCH', f'pulls/{self.pull_request}', json={'base': data['base']})

            if self.close:
                pr_to_notify = self.env['runbot_merge.pull_requests'].search([
                    ('repository', '=', repo.id),
                    ('number', '=', self.pull_request),
                ])
                if pr_to_notify:
                    pr_to_notify._notify_merged(gh, data)

        if self.close:
            gh.close(self.pull_request)

        if message:
            gh.comment(self.pull_request, message)

        if self.reaction:
            gh('POST', self.reaction, json={'content': 'eyes'})

    def _on_failure(self) -> bool:
        import sys
        _, e, _ = sys.exc_info()

        if isinstance(e, HTTPError) and e.response.status_code == 500:
            self.sequence = self.RETRY_LIMIT
            self.with_user(1).message_notify(
                subject=f"{e.response.reason}, disabled feedback cron",
                body=e.response.text or str(e),
                partner_ids=self.env.ref('runbot_merge.group_admin').users.partner_id.ids,
            )

        if isinstance(e, HTTPError) and e.response.status_code == 404 and self.reaction:
            _logger.info(
                "Comment not found (%s) when trying to send a reaction to %s#%s (%s)",
                e, self.repository.name, self.pull_request, self.reaction,
            )
            return True

        _logger.exception(
            "Error while trying to %s %s#%s (%s)",
            'close' if self.close else 'send a comment to',
            self.repository.name, self.pull_request,
            utils.shorten(self.message, 200)
        )
        return super()._on_failure()


class FeedbackTemplate(models.Model):
    _name = 'runbot_merge.pull_requests.feedback.template'
    _description = "str.format templates for feedback messages, no integration," \
                   "but that's their purpose"
    _inherit = ['mail.thread']

    template = fields.Text(tracking=True)
    help = fields.Text(readonly=True)

    def _format(self, **args):
        return self.template.format_map(args)

    def _send(self, *, repository: Repository, pull_request: int, format_args: dict, token_field: Optional[str] = None) -> Optional[Feedback]:
        try:
            feedback = {
                'repository': repository.id,
                'pull_request': pull_request,
                'message': self.template.format_map(format_args),
            }
            if token_field:
                feedback['token_field'] = token_field
            return self.env['runbot_merge.pull_requests.feedback'].create(feedback)
        except Exception:
            _logger.exception("Failed to render template %s", self.get_external_id())
            raise