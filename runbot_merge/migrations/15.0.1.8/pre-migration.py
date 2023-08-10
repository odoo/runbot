from pathlib import Path

def migrate(cr, version):
    sql = Path(__file__).parent.joinpath('upgrade.sql')\
        .read_text(encoding='utf-8')
    cr.execute(sql)
