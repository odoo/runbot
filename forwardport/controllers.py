import collections
import datetime
import pathlib

import werkzeug.urls

from odoo.http import route, request
from odoo.fields import Domain
from odoo.addons.runbot_merge.controllers.dashboard import MergebotDashboard

DEFAULT_DELTA = datetime.timedelta(days=7)
class Dashboard(MergebotDashboard):
    def _entries(self):
        changelog = pathlib.Path(__file__).parent / 'changelog'
        if not changelog.is_dir():
            return super()._entries()

        return super()._entries() + [
            (d.name, [f.read_text(encoding='utf-8') for f in d.iterdir() if f.is_file()])
            for d in changelog.iterdir()
        ]


    @route('/forwardport/outstanding', type='http', methods=['GET'], auth="user", website=True, sitemap=False)
    def outstanding(self, partner=0, authors=True, reviewers=True, group=0):
        Partners = request.env['res.partner']
        PullRequests = request.env['runbot_merge.pull_requests']
        partner = Partners.browse(int(partner))
        group = Partners.browse(int(group))
        authors = int(authors)
        reviewers = int(reviewers)
        link = lambda **kw: '?' + werkzeug.urls.url_encode({'partner': partner.id or 0, 'authors': authors, 'reviewers': reviewers, **kw, })
        groups = Partners.search([('is_company', '=', True), ('child_ids', '!=', False)])
        if not (authors or reviewers):
            return request.render('forwardport.outstanding', {
                'authors': 0,
                'reviewers': 0,
                'single': partner,
                'culprits': partner,
                'groups': groups,
                'current_group': group,
                'outstanding': [],
                'outstanding_per_author': {partner: 0},
                'outstanding_per_reviewer': {partner: 0},
                'link': link,
            })

        source_filter = [('merge_date', '<', datetime.datetime.now() - DEFAULT_DELTA)]
        partner_filter = []
        if partner or group:
            if partner:
                suffix = ''
                arg = partner.id
            else:
                suffix = '.commercial_partner_id'
                arg = group.id

            if authors:
                partner_filter.append([(f'author{suffix}', '=', arg)])
            if reviewers:
                partner_filter.append([(f'reviewed_by{suffix}', '=', arg)])

            source_filter.extend(Domain.OR(partner_filter))

        outstanding = PullRequests.search([
            ('state', 'in', ['opened', 'validated', 'approved', 'ready', 'error']),
            ('source_id', 'in', PullRequests._search(source_filter)),
        ])

        outstanding_per_group = collections.Counter()
        outstanding_per_author = collections.Counter()
        outstanding_per_reviewer = collections.Counter()
        outstandings = []
        for source in outstanding.mapped('source_id').sorted('merge_date'):
            prs = source.forwardport_ids.filtered(lambda p: p.state not in ['merged', 'closed'])
            outstandings.append({
                'source': source,
                'prs': prs,
            })
            if authors:
                outstanding_per_author[source.author] += len(prs)
                outstanding_per_group[source.author.commercial_partner_id] += len(prs)
            if reviewers and source:
                outstanding_per_reviewer[source.reviewed_by] += len(prs)
                outstanding_per_group[source.reviewed_by.commercial_partner_id] += len(prs)

        culprits = Partners.browse(p.id for p, _ in (outstanding_per_reviewer + outstanding_per_author).most_common())
        return request.render('forwardport.outstanding', {
            'authors': authors,
            'reviewers': reviewers,
            'single': partner,
            'culprits': culprits,
            'groups': groups,
            'current_group': group,
            'outstanding_per_author': outstanding_per_author,
            'outstanding_per_reviewer': outstanding_per_reviewer,
            'outstanding_per_group': outstanding_per_group,
            'outstanding': outstandings,
            'link': link,
        })
