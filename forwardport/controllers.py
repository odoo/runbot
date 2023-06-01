import pathlib

from odoo.addons.runbot_merge.controllers.dashboard import MergebotDashboard

class Dashboard(MergebotDashboard):
    def _entries(self):
        changelog = pathlib.Path(__file__).parent / 'changelog'
        if not changelog.is_dir():
            return super()._entries()

        return super()._entries() + [
            (d.name, [f.read_text(encoding='utf-8') for f in d.iterdir() if f.is_file()])
            for d in changelog.iterdir()
        ]

