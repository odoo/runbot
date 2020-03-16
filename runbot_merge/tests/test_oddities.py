def test_partner_merge(env):
    p_src = env['res.partner'].create({
        'name': 'kfhsf',
        'github_login': 'tyu'
    }) |  env['res.partner'].create({
        'name': "xxx",
        'github_login': 'xxx'
    })
    # proper login with useful info
    p_dest = env['res.partner'].create({
        'name': 'Partner P. Partnersson',
        'github_login': ''
    })

    env['base.partner.merge.automatic.wizard'].create({
        'state': 'selection',
        'partner_ids': (p_src + p_dest).ids,
        'dst_partner_id': p_dest.id,
    })._call('action_merge')
    assert not p_src.exists()
    assert p_dest.name == 'Partner P. Partnersson'
    assert p_dest.github_login == 'xxx'
