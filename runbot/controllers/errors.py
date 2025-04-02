from odoo.http import Controller, Response, request, route


class ErrorController(Controller):

    @route('/runbot/error/merge/result/<filter_id>', type='http', auth='public', website=True)
    def error_filter_result(self, filter_id=None, **kwargs):
        merger = request.env['runbot.build.error.merge'].browse(int(filter_id))
        if not merger:
            return Response('Error merge not found', status=404)
        return request.render('runbot.error_merge_result', {'merger': merger, 'results': merger._get_matching_groups()})
