import logging

_logger = logging.getLogger(__name__)

def migrate(cr, version):
    cr.execute("""
        UPDATE runbot_dockerfile
           SET image_identifier = subq.identifier, image_future_identifier = subq.identifier
          FROM (SELECT DISTINCT ON (dockerfile_id) dockerfile_id, identifier
                  FROM runbot_docker_build_result
                 WHERE result='success' order by dockerfile_id, create_date desc)
            AS subq
         WHERE runbot_dockerfile.id = subq.dockerfile_id;
        """)
