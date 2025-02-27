import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # copy infor from ol build_reference_ids to slot_reference_ids
    # old table is runbot_build_params_references
    # new table is runbot_build_params_slot_references
    cr.execute("""
        ALTER TABLE runbot_build_params_slot_references DISABLE TRIGGER ALL;
        INSERT INTO runbot_build_params_slot_references (runbot_build_params_id, runbot_batch_slot_id)
            SELECT ref.runbot_build_params_id, slot.id
            FROM runbot_build_params_references ref
            JOIN LATERAL (
                SELECT id
                FROM runbot_batch_slot sl
                WHERE sl.build_id = ref.runbot_build_id
                LIMIT 1
            ) slot(id) ON TRUE;
        ALTER TABLE runbot_build_params_slot_references ENABLE TRIGGER ALL;
    """)
