def migrate(cr, version):
    cr.execute('ALTER TABLE runbot_branch ADD COLUMN forwardport_of_id INT')
    # not mandatory and slow to compute, can be computed in a shell later if needed.
    # env['runbot.branch'].search([('is_pr', '=', True), ('pull_head_name', '=like', '%-fw'), ('pr_body', 'like', 'Forward-Port-Of:')])._compute_forwardport_of_id()
