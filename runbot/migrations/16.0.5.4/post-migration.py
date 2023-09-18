import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    private = [
        'set_hook_time',
        'set_ref_time',
        'check_token',
        'get_version_domain',
        'get_builds',
        'get_build_domain',
        'disable',
        'set_psql_conn_count',
        'get_running_max',
        'branch_groups',
        'consistency_warning',
        'fa_link_type',
        'make_python_ctx',
        'parse_config',
        'get_color_class',
        'get_formated_build_time',
        'filter_patterns',
        'http_log_url',
        'result_multi',
        'match_is_base',
        'link_errors',
        'clean_content',
        'test_tags_list',
        'disabling_tags',
        'step_ids',
        'recompute_infos',
        'warning',
        'is_file',
    ]
    removed = [
        "get_formated_build_age",
        "get_formated_job_time",
        "make_dirs",
        "build_type_label",
    ]
    for method in private:
        pattern = f'.{method}('
        replacepattern = f'._{method}('
        views = env['ir.ui.view'].search([('arch_db', 'like', pattern)])
        if views:
            _logger.info(f'Some views contains "{pattern}": {views}')
            for view in views:
                view.arch_db = view.arch_db.replace(pattern, replacepattern)

    for method in removed:
        pattern = f'.{method}('
        views = env['ir.ui.view'].search([('arch_db', 'like', pattern)])
        if views:
            _logger.error(f'Some views contains "{pattern}": {views}')

    for method in removed:
        pattern = f'.{method}('
        steps =env['runbot.build.config.step'].search(['|', ('python_code', 'like', pattern), ('python_result_code', 'like', pattern)])
        if steps:
            _logger.error(f'Some step contains "{pattern}": {steps}')

    for method in private:
        pattern = f'.{method}('
        replacepattern = f'._{method}('
        steps = env['runbot.build.config.step'].search(['|', ('python_code', 'like', pattern), ('python_result_code', 'like', pattern)])
        for step in steps:
            python_code = pattern in step.python_code
            python_result_code = pattern in step.python_result_code
            if replacepattern not in step.python_code and python_code:
                _logger.warning(f'Some step python_code contains "{pattern}": {step}')
                python_code = False
            if replacepattern not in step.python_result_code and python_result_code:
                _logger.warning(f'Some step python_result_code contains "{pattern}": {step}')
                python_result_code = False

            if python_code or python_result_code:
                _logger.info(f'Some step python_code contains "{pattern}": {step} but looks like it was adapted')
