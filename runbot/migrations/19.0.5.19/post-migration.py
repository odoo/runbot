def migrate(cr, version):
    cr.execute("SELECT id, file_destination, content FROM runbot_docker_layer WHERE layer_type = 'file'")
    res = cr.fetchall()
    for layer_id, destination, content in res:
        cr.execute(
            "INSERT INTO runbot_scriptfile (dest_path, content, chmod) VALUES (%s, %s, '0744') RETURNING id",
            (destination, content),
        )
        scriptfile_id = (cr.fetchone()[0])
        cr.execute(
            "UPDATE runbot_docker_layer SET scriptfile_id = %s, content = NULL WHERE id = %s",
            (scriptfile_id, layer_id),
        )
