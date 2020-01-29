def migrate(cr, version):
    """ Moved the required_statuses field from the project to the repository so
    different repos can have different CI requirements within a project
    """
    # create column on repo
    cr.execute("ALTER TABLE runbot_merge_repository ADD COLUMN required_statuses varchar")
    # copy data from project
    cr.execute("""
    UPDATE runbot_merge_repository r 
    SET required_statuses = (
        SELECT required_statuses 
        FROM runbot_merge_project 
        WHERE id = r.project_id
    )
    """)
    # drop old column on project
    cr.execute("ALTER TABLE runbot_merge_project DROP COLUMN required_statuses")
