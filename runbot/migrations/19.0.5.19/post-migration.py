def migrate(cr, version):
    # Odoo never drops an index when `index=True` is removed from a field
    for index in (
        'ir_logging__active_step_id_index',
        'ir_logging__type_index',
        'ir_logging__level_index',
        'ir_logging__dbname_index',
        'runbot_build_params__fingerprint_index',  # duplicate of runbot_build_params_unique_fingerprint
        'runbot_build__priority_level_index',  # replaced by runbot_build_pending_priority_level_idx
        'runbot_build_stat__build_id_index',  # covered by runbot_build_stat_build_config_key_unique
    ):
        cr.execute(f'DROP INDEX IF EXISTS {index}')
