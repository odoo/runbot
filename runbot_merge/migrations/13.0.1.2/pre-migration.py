def migrate(cr, version):
    cr.execute("""
    create table res_partner_review (
        id serial primary key,
        partner_id integer not null references res_partner (id),
        repository_id integer not null references runbot_merge_repository (id),
        review bool,
        self_review bool
    )
    """)
    cr.execute("""
    insert into res_partner_review (partner_id, repository_id, review, self_review)
    select p.id, r.id, reviewer, self_reviewer
    from res_partner p, runbot_merge_repository r
    where p.reviewer or p.self_reviewer
    """)
