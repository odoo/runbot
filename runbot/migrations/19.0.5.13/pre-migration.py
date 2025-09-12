
from odoo.upgrade import util

# Some fields and model were not cleanup correctly in previous migrations.
# This is mainly because of noupdate ir.model.data records.


def migrate(cr, version):

    util.remove_model(cr, 'runbot.error.log')
    util.remove_model(cr, 'runbot.build.config.multi.wizard')
    util.remove_model(cr, 'runbot.build.dependency')
    util.remove_model(cr, 'decimal.precision.test')
    util.remove_model(cr, 'mail.blacklist.mixin')

    old_fields = """
ir.model.osv_memory
ir.model.constraint.date_update
ir.model.constraint.date_init
ir.model.relation.date_update
ir.model.relation.date_init
ir.ui.menu.needaction_enabled
ir.ui.menu.icon_pict
ir.ui.menu.mail_group_id
ir.ui.menu.icon
ir.ui.menu.web_icon_hover
ir.ui.menu.web_icon_hover_data
ir.ui.view.application
ir.ui.view.inherit_option_id
ir.ui.view.inherited_option_ids
ir.actions.act_window.auto_refresh
ir.actions.server.body_html
ir.actions.server.email_from
ir.actions.server.partner_to
ir.actions.server.email_to
ir.actions.server.subject
ir.actions.report.report_rml_content
ir.actions.report.report_sxw
ir.actions.report.report_sxw_content
ir.attachment.file_type
ir.attachment.file_type_icon
ir.rule.domain
ir.autovacuum.write_uid
ir.autovacuum.write_date
ir.autovacuum.create_date
ir.autovacuum.create_uid
ir.fields.converter.create_date
ir.fields.converter.write_date
ir.fields.converter.create_uid
ir.fields.converter.write_uid
res.partner.category.complete_name
res.partner.fax
res.partner.country
res.partner.birthdate
res.partner.has_image
res.partner.ean13
res.partner.use_parent_address
res.partner.message_summary
res.bank.fax
res.partner.bank.footer
res.partner.bank.owner_name
res.partner.bank.street
res.partner.bank.city
res.partner.bank.name
res.partner.bank.zip
res.partner.bank.country_id
res.partner.bank.state_id
res.config.settings.runbot_domain
res.config.settings.runbot_logdb_uri
res.currency.rate_silent
res.currency.company_id
res.currency.base
res.currency.accuracy
res.currency.rate.currency_rate_type_id
res.company.currency_ids
res.groups.is_portal
res.users.fax
res.users.user_email
res.users.display_groups_suggestions
res.users.image_medium
res.users.image_small
res.users.message_last_post
res.users.commercial_partner_country_id
res.users.supplier
res.users.image
res.users.opt_out
res.users.notify_email
res.users.customer
resource.calendar.leaves.tz
mail.thread.message_summary
mail.message.to_read
mail.message.vote_user_ids
mail.message.needaction_partner_ids
mail.message.type
mail.message.same_thread
mail.mail.needaction_partner_ids
publisher_warranty.contract.create_uid
publisher_warranty.contract.create_date
publisher_warranty.contract.write_date
publisher_warranty.contract.write_uid
discuss.channel.message_summary
discuss.channel.menu_id
mail.compose.message.vote_user_ids
mail.compose.message.to_read
mail.compose.message.notified_partner_ids
mail.compose.message.same_thread
mail.compose.message.type
portal.mixin.portal_url
website.seo.metadata.create_uid
website.seo.metadata.write_date
website.seo.metadata.create_date
website.seo.metadata.write_uid
runbot.branch.previous_version
runbot.branch.state
runbot.branch.branch_config_id
runbot.branch.branch_name
runbot.branch.coverage_result
runbot.branch.priority
runbot.branch.no_build
runbot.branch.no_auto_build
runbot.branch.config_id
runbot.branch.sticky
runbot.branch.coverage
runbot.branch.modules
runbot.branch.job_timeout
runbot.branch.rebuild_requested
runbot.branch.duplicate_repo_id
runbot.branch.closest_sticky
runbot.branch.defined_sticky
runbot.branch.intermediate_stickies
runbot.build.name
runbot.build.log
runbot.build.modules
runbot.build.commit_path_mode
runbot.build.branch_id
runbot.build.date
runbot.build.author
runbot.build.subject
runbot.build.sequence
runbot.build.result
runbot.build.pid
runbot.build.state
runbot.build.hidden
runbot.build.dependency_ids
runbot.build.real_build
runbot.build.nb_pending
runbot.build.job_age
runbot.build.nb_testing
runbot.build.extra_params
runbot.build.committer
runbot.build.server_match
runbot.build.repo_id
runbot.build.revdep_build_ids
runbot.build.committer_email
runbot.build.author_email
runbot.build.nb_running
runbot.build.guess_result
runbot.build.triggered_result
runbot.build.duplicate_id
runbot.build.config.message_unread
runbot.build.config.update_github_state
runbot.build.config.message_unread_counter
runbot.build.config.message_channel_ids
runbot.build.config.step.ignore_triggered_result
runbot.build.config.step.hide_build
runbot.build.config.step.force_build
runbot.build.config.step.message_unread_counter
runbot.build.config.step.message_unread
runbot.build.config.step.message_channel_ids
runbot.build.error.cleaned_content
runbot.build.error.summary
runbot.build.error.module_name
runbot.build.error.function
runbot.build.error.fingerprint
runbot.build.error.parent_id
runbot.build.error.child_ids
runbot.build.error.children_build_ids
runbot.build.error.error_history_ids
runbot.build.error.branch_ids
runbot.build.error.Children_build_ids
runbot.build.error.repo_ids
runbot.build.error.message_unread_counter
runbot.build.error.message_unread
runbot.build.error.message_channel_ids
runbot.build.error.tag.error_ids
runbot.error.regex.message_unread_counter
runbot.error.regex.message_unread
runbot.error.regex.message_channel_ids
runbot.host.message_unread_counter
runbot.host.message_unread
runbot.host.message_channel_ids
runbot.repo.nginx
runbot.repo.testing
runbot.repo.running
runbot.repo.no_build
runbot.repo.short_name
runbot.repo.config_id
runbot.repo.group_ids
runbot.repo.token
runbot.repo.duplicate_id
runbot.repo.auto
runbot.repo.fallback_id
runbot.repo.job_timeout
runbot.repo.repo_config_id
runbot.repo.dependency_ids
runbot.repo.modules_auto
runbot.repo.jobs
runbot.repo.base
    """

    for field in old_fields.strip().split("\n"):
        if field:
            model, fname = field.rsplit(".", 1)
            util.remove_field(cr, model, fname)
