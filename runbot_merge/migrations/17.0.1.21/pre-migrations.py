"""Convert non-queue cron models to queue / retryable supertypes.

Models converted:
- runbot_merge.issues_closer -> queue
- runbot_merge.pull_requests.check_commits -> queue
- runbot_merge.pull_requests.feedback -> retryable (tried -> sequence)
- runbot_merge.pull_requests.tagging -> queue
- runbot_merge.fetch_job -> retryable (active removed)
"""


def migrate(cr, _version):
    # feedback: rename 'tried' to 'sequence', ensure retry_after exists and is NOT NULL
    cr.execute("""
        ALTER TABLE runbot_merge_pull_requests_feedback
            RENAME COLUMN tried TO sequence;
        UPDATE runbot_merge_pull_requests_feedback
            SET sequence = 0 WHERE sequence IS NULL;
        ALTER TABLE runbot_merge_pull_requests_feedback
            ALTER COLUMN sequence SET DEFAULT 0;
        UPDATE runbot_merge_pull_requests_feedback
            SET retry_after = NOW() WHERE retry_after IS NULL;
        ALTER TABLE runbot_merge_pull_requests_feedback
            ALTER COLUMN retry_after SET NOT NULL;
    """)

    # fetch_job: remove processed fetches, drop active column
    cr.execute("""
        DELETE FROM runbot_merge_fetch_job
            WHERE not active;
        ALTER TABLE runbot_merge_fetch_job
            DROP COLUMN active;
    """)

    # tagging: drop unused remove feature
    cr.execute("""
    ALTER TABLE runbot_merge_pull_requests_tagging
        DROP COLUMN tags_remove;
    """)

    # Remove old cron records and their XML IDs that will be re-created by _init_cron
    cr.execute("""
        DELETE FROM ir_cron
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'runbot_merge'
              AND model = 'ir.cron'
              AND name IN ('feedback_cron', 'labels_cron', 'fetch_prs_cron',
                           'issues_closer_cron', 'cron_check_commits')
        )
    """)
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module = 'runbot_merge'
          AND model = 'ir.cron'
          AND name IN ('feedback_cron', 'labels_cron', 'fetch_prs_cron',
                       'issues_closer_cron', 'cron_check_commits')
    """)



