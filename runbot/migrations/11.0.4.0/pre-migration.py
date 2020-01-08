# -*- coding: utf-8 -*-


def migrate(cr, version):
    # delete oldies
    old_models = tuple([
        'ir.qweb.widget', 'ir.qweb.widget.monetary', 'base.module.import',
        'res.currency.rate.type', 'website.converter.test.sub',
        'website.converter.test', 'runbot.config.settings',
        'report.abstract_report',  'base.action.rule.lead.test',
        'base.action.rule.line.test'
    ])
    cr.execute("DELETE FROM ir_model WHERE model IN %s", [old_models])

    # pre-create the log_list column
    cr.execute("ALTER TABLE runbot_build ADD COLUMN log_list character varying")
    cr.execute("UPDATE runbot_build SET log_list='base,all,run' WHERE job_type = 'all' or job_type is null")
    cr.execute("UPDATE runbot_build SET log_list='base,all' WHERE job_type = 'testing'")
    cr.execute("UPDATE runbot_build SET log_list='all,run' WHERE job_type = 'running'")

    # pre-create config_id column
    cr.execute('ALTER TABLE runbot_build ADD COLUMN config_id integer')

    # pre-fill global result column for old builds
    cr.execute("ALTER TABLE runbot_build ADD COLUMN global_result character varying")
    cr.execute("ALTER TABLE runbot_build ADD COLUMN global_state character varying")
    cr.execute("UPDATE runbot_build SET global_result=result, global_state=state WHERE duplicate_id is null")

    # set correct values on duplicates too
    cr.execute("UPDATE runbot_build AS updated_build SET global_result = fb.global_result, global_state = fb.global_state FROM runbot_build AS fb WHERE updated_build.duplicate_id = fb.id")

    # pre-fill nb_ fields to avoid a huge recompute
    cr.execute("ALTER TABLE runbot_build ADD COLUMN nb_pending INTEGER DEFAULT 0")
    cr.execute("ALTER TABLE runbot_build ADD COLUMN nb_testing INTEGER DEFAULT 0")
    cr.execute("ALTER TABLE runbot_build ADD COLUMN nb_running INTEGER DEFAULT 0")
    cr.execute("ALTER TABLE runbot_build ALTER COLUMN nb_pending DROP DEFAULT")
    cr.execute("ALTER TABLE runbot_build ALTER COLUMN nb_testing DROP DEFAULT")
    cr.execute("ALTER TABLE runbot_build ALTER COLUMN nb_running DROP DEFAULT")
