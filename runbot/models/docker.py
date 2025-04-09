import getpass
import logging
import os
import re
import docker
from odoo import api, fields, models
from odoo.addons.base.models.ir_qweb import QWebException

from ..container import docker_build
from ..fields import JsonDictField

_logger = logging.getLogger(__name__)


USERUID = os.getuid()
USERGID = os.getgid()
USERNAME = getpass.getuser()

class DockerLayer(models.Model):
    _name = 'runbot.docker_layer'
    _inherit = 'mail.thread'
    _description = "Docker layer"
    _order = 'sequence,id'

    name = fields.Char("Name", required=True)
    sequence = fields.Integer("Sequence", default=100, tracking=True)
    dockerfile_id = fields.Many2one('runbot.dockerfile', index=True, tracking=True)
    layer_type = fields.Selection([
        ('raw', "Raw"),
        ('template', "Template"),
        ('reference_layer', "Reference layer"),
        ('reference_file', "Reference file"),
    ], string="Layer type", default='raw', tracking=True)
    content = fields.Text("Content", tracking=True)
    packages = fields.Text("Packages", help="List of package, can be on multiple lines with comments", tracking=True)
    rendered = fields.Text("Rendered", compute="_compute_rendered", recursive=True)
    reference_docker_layer_id = fields.Many2one('runbot.docker_layer', index=True, tracking=True)
    reference_dockerfile_id = fields.Many2one('runbot.dockerfile', index=True, tracking=True)
    values = JsonDictField()
    referencing_dockerlayer_ids = fields.One2many('runbot.docker_layer', 'reference_docker_layer_id', string='Layers referencing this one direcly', readonly=True)
    all_referencing_dockerlayer_ids = fields.One2many('runbot.docker_layer', compute="_compute_references", string='Layers referencing this one', readonly=True)
    reference_count = fields.Integer('Number of references', compute='_compute_references')
    has_xml_id = fields.Boolean(compute='_compute_has_xml_id')

    @api.depends('referencing_dockerlayer_ids', 'dockerfile_id.referencing_dockerlayer_ids')
    def _compute_references(self):
        for record in self:
            record.all_referencing_dockerlayer_ids = record.referencing_dockerlayer_ids | record.dockerfile_id.referencing_dockerlayer_ids
            record.reference_count = len(record.all_referencing_dockerlayer_ids)

    def _compute_has_xml_id(self):
        existing_xml_id = set(self.env['ir.model.data'].search([('model', '=', self._name)]).mapped('res_id'))
        for record in self:
            record.has_xml_id = record.id and record.id in existing_xml_id

    @api.depends('layer_type', 'content', 'reference_docker_layer_id.rendered', 'reference_dockerfile_id.layer_ids.rendered', 'values', 'packages', 'name')
    def _compute_rendered(self):
        for layer in self:
            rendered = layer._render_layer({})
            layer.rendered = rendered

    def _render_layer(self, custom_values):
        base_values = {
            'USERUID': USERUID,
            'USERGID': USERGID,
            'USERNAME': USERNAME,
        }
        if packages := self._parse_packages():
            base_values['$packages'] = packages

        values = {**base_values, **self.values, **custom_values}

        if self.layer_type == 'raw':
            rendered = self.content
        elif self.layer_type == 'reference_layer':
            if self.reference_docker_layer_id:
                rendered = self.reference_docker_layer_id._render_layer(values)
            else:
                rendered = 'ERROR: no reference_docker_layer_id defined'
        elif self.layer_type == 'reference_file':
            if self.reference_dockerfile_id:
                rendered = self.reference_dockerfile_id.layer_ids.render_layers(values)
            else:
                rendered = 'ERROR: no reference_docker_layer_id defined'
        elif self.layer_type == 'template':
            rendered = self._render_template(values)
        if not rendered or rendered[0] != '#':
            rendered = f'# {self.name}\n{rendered}'
        return rendered

    def render_layers(self, values=None):
        values = values or {}
        return "\n\n".join(layer._render_layer(values) or "" for layer in self) + '\n'

    def _render_template(self, values):
        values = {key: value for key, value in values.items() if f'{key}' in (self.content or '')}  # filter on keys mainly to have a nicer comment. All default must be defined in self.values
        rendered = self.content
        if self.values.keys() - ['$packages']:
            values_repr = str(values).replace("'", '"')
            rendered = f"# {self.name or 'Rendering'} with values {values_repr}\n{rendered}"

        for key, value in values.items():
            rendered = rendered.replace('{%s}' % key, str(value))
        return rendered

    def _parse_packages(self):
        packages = [packages.split('#')[0].strip() for packages in (self.packages or '').split('\n')]
        packages = [package for package in packages if package]
        return ' '.join(packages)

    def unlink(self):
        to_unlink = self
        for record in self:
            if record.reference_count and record.dockerfile_id and not record.has_xml_id:
                record.dockerfile_id = False
                to_unlink = to_unlink - record
        return super(DockerLayer, to_unlink).unlink()


class Dockerfile(models.Model):
    _name = 'runbot.dockerfile'
    _inherit = [ 'mail.thread' ]
    _description = "Dockerfile"

    name = fields.Char('Dockerfile name', required=True, help="Name of Dockerfile")
    active = fields.Boolean('Active', default=True, tracking=True)
    auto_sync = fields.Boolean('Auto sync', help='Automatically sync the identifier with the future identifier', default=False, tracking=True)
    pull_on_build = fields.Boolean('Pull on build ', help='Add pull option when building to get the latest version of the FROM', default=False, tracking=True)
    image_identifier = fields.Char('Identifier', tracking=True)
    image_future_identifier = fields.Char('Future Identifier', tracking=True)
    image_previous_identifier = fields.Char('Previous Identifier', tracking=True)
    image_tag = fields.Char(compute='_compute_image_tag', store=True)
    image_future_tag = fields.Char(compute='_compute_image_helper_tags')
    image_previous_tag = fields.Char(compute='_compute_image_helper_tags')
    template_id = fields.Many2one('ir.ui.view', string='Docker Template', domain=[('type', '=', 'qweb')], context={'default_type': 'qweb', 'default_arch_base': '<t></t>'})
    arch_base = fields.Text(related='template_id.arch_base', readonly=False, related_sudo=True)
    dockerfile = fields.Text(compute='_compute_dockerfile', tracking=True)
    in_error = fields.Boolean('In error', help='The last build failed.', default=False)
    to_build = fields.Boolean('To Build', help='Build Dockerfile. Check this when the Dockerfile is ready.', default=False)
    always_pull = fields.Boolean('Always pull', help='Always Pull on the hosts, not only at the use time', default=False, tracking=True, copy=False)
    version_ids = fields.One2many('runbot.version', 'dockerfile_id', string='Versions')
    description = fields.Text('Description')
    view_ids = fields.Many2many('ir.ui.view', compute='_compute_view_ids', groups="runbot.group_runbot_admin")
    project_ids = fields.One2many('runbot.project', 'dockerfile_id', string='Default for Projects')
    bundle_ids = fields.One2many('runbot.bundle', 'dockerfile_id', string='Used in Bundles')
    build_results = fields.One2many('runbot.docker_build_result', 'dockerfile_id', string='Build results')
    last_successful_result = fields.Many2one('runbot.docker_build_result', compute='_compute_last_successful_result')
    layer_ids = fields.One2many('runbot.docker_layer', 'dockerfile_id', string='Layers', copy=True)
    referencing_dockerlayer_ids = fields.One2many('runbot.docker_layer', 'reference_dockerfile_id', string='Layers referencing this one')
    use_count = fields.Integer('Used count', compute="_compute_use_count", store=True)
    # maybe we should have global values here? branch version, chrome version, ... then use a os layer when possible (jammy, ...)
    # we could also have a variant param, to use the version image in a specific trigger? Add a layer or change a param?

    public_visibility = fields.Boolean('Public', default=lambda self: self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_dockerfile_public_by_default'), help="Dockerfile is public and can be accessed by anyone with /runbot/dockerfile route")

    _sql_constraints = [('runbot_dockerfile_name_unique', 'unique(name)', 'A Dockerfile with this name already exists')]

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        copied_record = super().copy(default={'name': '%s (copy)' % self.name, 'to_build': False})
        #copied_record.template_id = self.template_id.copy()
        copied_record.template_id.name = '%s (copy)' % copied_record.template_id.name
        copied_record.template_id.key = '%s (copy)' % copied_record.template_id.key
        return copied_record

    def _compute_last_successful_result(self):
        rg = self.env['runbot.docker_build_result']._read_group(
            [('result', '=', 'success'), ('dockerfile_id', 'in', self.ids)],
            ['dockerfile_id'],
            ['id:max'],
        )
        result_ids = dict(rg)
        for record in self:
            record.last_successful_result = result_ids.get(record)

    def _get_last_successful_result_for_ident(self, identifier=None):
        domain = [('result', '=', 'success'), ('dockerfile_id', 'in', self.ids), ('identifier', '=', identifier)]
        return self.env['runbot.docker_build_result'].search(domain, order='id desc', limit=1)

    @api.depends('bundle_ids', 'referencing_dockerlayer_ids', 'project_ids', 'version_ids')
    def _compute_use_count(self):
        for record in self:
            record.use_count = len(record.bundle_ids) + len(record.referencing_dockerlayer_ids) + len(record.project_ids) + len(record.version_ids)

    @api.depends('template_id.arch_base', 'layer_ids.rendered', 'layer_ids.sequence')
    def _compute_dockerfile(self):
        for rec in self:
            content = ''
            if rec.template_id:
                try:
                    res = rec.template_id._render_template(rec.template_id.id) if rec.template_id else ''
                    dockerfile = re.sub(r'^\s*$', '', res, flags=re.M).strip()
                    create_user = f"""\nRUN groupadd -g {USERGID} {USERNAME} && useradd --create-home -u {USERUID} -g {USERNAME} -G audio,video {USERNAME}\n"""
                    content = dockerfile + create_user
                except QWebException:
                    content = ''
            else:
                content = rec.layer_ids.render_layers()

            switch_user = f"\nUSER {USERNAME}\n"
            if not content.endswith(switch_user):
                content = content + switch_user

            rec.dockerfile = content

    @api.onchange('dockerfile')
    def onchange_dockerfile(self):
        self.in_error = False

    @api.depends('name')
    def _compute_image_tag(self):
        for rec in self:
            if rec.name:
                rec.image_tag = 'odoo:%s' % re.sub(r'[ /:\(\)\[\]]', '', rec.name)

    @api.depends('image_tag')
    def _compute_image_helper_tags(self):
        for rec in self:
            rec.image_future_tag = f'{rec.image_tag}.future'
            rec.image_previous_tag = f'{rec.image_tag}.previous'

    @api.depends('template_id')
    def _compute_view_ids(self):
        for rec in self:
            keys = re.findall(r'<t.+t-call="(.+)".+', rec.arch_base or '')
            rec.view_ids = self.env['ir.ui.view'].search([('type', '=', 'qweb'), ('key', 'in', keys)]).ids

    def write(self, values):
        if 'image_identifier' in values and not 'image_previous_identifier' in values and self.image_identifier != values['image_identifier']:
            self.ensure_one()
            values['image_previous_identifier'] = self.image_identifier
        return super().write(values)

    def action_sync_identifiers(self):
        for dockerfile in self:
            if dockerfile.image_future_identifier and dockerfile.image_future_identifier != dockerfile.image_identifier:
                dockerfile.image_identifier = dockerfile.image_future_identifier

    def _template_to_layers(self):

        ##
        # Notes: This is working fine, but missing
        # - debian packages layer (multiline),
        # - setup tools and wheel pip (not usefull anymore? )
        # - args goole chrome (maybe we should introduce that in the layers management instead of values?)
        # - doc requirements
        # - geo
        ##
        def clean_comments(text):
            result = '\n'.join([line.strip() for line in text.split('\n') if not line.startswith('#')])
            result = result.replace('\\\n', '')
            return result

        env = self.env
        base_layers = env['runbot.docker_layer'].browse(env['ir.model.data'].search([('model', '=', 'runbot.docker_layer')]).mapped('res_id'))
        create_user_layer_id = env.ref('runbot.docker_layer_create_user_template').id
        for rec in self:
            if rec.template_id and not rec.layer_ids:
                _logger.info('Converting %s in layers', rec.name)
                layers = []
                comments = []
                previous_directive_add = False
                content = rec.template_id._render_template(rec.template_id.id) 
                for line in content.split('\n'):
                    # should we consider all layers instead of base_layersbase_layers ?
                    if not line.strip():
                        continue

                    if line.startswith('#'):
                        comments.append(line)
                        continue

                    if any(line.startswith(directive) for directive in ['FROM', 'ENV', 'USER', 'SET', 'ADD', 'RUN', 'COPY', 'ARG']):
                        if (previous_directive_add and line.startswith('RUN')):
                            _logger.info('Keeping ADD in same layer than RUN')
                        else:
                            layers.append([])
                        previous_directive_add = line.startswith('ADD')

                    layers[-1] += comments
                    comments = []
                    layers[-1].append(line)

                for layer in layers:
                    content = '\n'.join(layer)
                    values = {
                            'dockerfile_id': rec.id,
                            'name': f'{rec.name}: Migrated layer',
                    }

                    for base_layer in base_layers:
                        if clean_comments(base_layer.rendered) == clean_comments(content):
                            values['reference_docker_layer_id'] = base_layer.id
                            values['layer_type'] = 'reference_layer'
                            _logger.info('Matched existing layer')
                            break
                        if base_layer.layer_type == 'template':
                            regex = re.escape(clean_comments(base_layer.content)).replace('"', r'\"')  # for astrange reason, re.escape does not escape "
                            for key in base_layer.values:
                                regex = regex.replace(r'\{%s\}' % key, fr'(?P<{key}>.*)', 1)
                                regex = regex.replace(r'\{%s\}' % key, fr'.*')
                            if match := re.match(regex, clean_comments(content)):
                                new_values = {}
                                _logger.info('Matched existing template')
                                for key in base_layer.values:
                                    new_values[key] = match.group(key)
                                values['reference_docker_layer_id'] = base_layer.id
                                values['values'] = new_values
                                values['layer_type'] = 'reference_layer'
                                break
                    else:
                        values['content'] = content
                        values['layer_type'] = 'raw'
                    self.env['runbot.docker_layer'].create(values)

            # add finals user managementlayers
            self.env['runbot.docker_layer'].create({
                'dockerfile_id': rec.id,
                'name': f'Create user for [{rec.name}]',
                'layer_type': 'reference_layer',
                'reference_docker_layer_id': create_user_layer_id,
            })
            self.env['runbot.docker_layer'].create({
                'dockerfile_id': rec.id,
                'name': f'Switch user for [{rec.name}]',
                'layer_type': 'template',
                'content': 'USER {USERNAME}',
            })

    def _get_docker_metadata(self, image_id):
        _logger.info(f'Fetching metadata for image {image_id}')
        metadata = {}
        commands = {
            'release': 'lsb_release -ds',
            'python': 'python3 --version',
            'chrome': 'google-chrome --version',
            'psql': 'psql --version',
            'pip_packages': 'python3 -m pip freeze',
            'debian_packages': "dpkg-query -W -f '${Package}==${Version}\n'",
        }
        if image_id:
            try:
                docker_client = docker.from_env()
                for key, command in commands.items():
                    name = f"GetDockerInfos_{image_id}_{key}"
                    try:
                        result = docker_client.containers.run(image_id, name=name,command=['/bin/bash', '-c', command], detach=False, remove=True)
                        result = result.decode('utf-8').strip()
                        if 'packages' in key:
                            result = result.split('\n')
                    except docker.errors.ContainerError:
                        result = None
                    metadata[key] = result
            except Exception as e:
                _logger.exception(f'Error while fetching metadata for image {image_id}')
                return {'error': str(e)}
        return metadata

    def _build(self, host=None):
        docker_build_path = self.env['runbot.runbot']._path('docker', self.image_tag)
        os.makedirs(docker_build_path, exist_ok=True)

        content = self.dockerfile

        with open(self.env['runbot.runbot']._path('docker', self.image_tag, 'Dockerfile'), 'w') as Dockerfile:
            Dockerfile.write(content)
        result = docker_build(docker_build_path, self.image_future_tag, self.pull_on_build)
        duration = result['duration']
        msg = result['msg']
        success = image_id = result.get('image_id')
        docker_build_result_values = {'dockerfile_id': self.id, 'output': msg, 'duration': duration, 'content': content, 'host_id': host and host.id}
        if success:
            docker_build_result_values['result'] = 'success'
            docker_build_result_values['identifier'] = image_id
        else:
            docker_build_result_values['result'] = 'error'
            self.in_error = True

        should_save_result = not success  # always save in case of failure
        if not should_save_result:
            # check previous result anyway
            previous_result = self.env['runbot.docker_build_result'].search([
                ('dockerfile_id', '=', self.id),
                ('host_id', '=', host and host.id),
            ], order='id desc', limit=1)
            # identifier changed
            if image_id != previous_result.identifier:
                should_save_result = True
            def clean_output(output):
                if not output:
                    return ''
                return '\n'.join([line for line in output.split('\n') if not line.startswith('Downloading')])
            if clean_output(previous_result.output) != clean_output(docker_build_result_values['output']):  # to discuss
                should_save_result = True
            if previous_result.content != docker_build_result_values['content']:  # docker image changed
                should_save_result = True


        if should_save_result:
            if success:
                docker_build_result_values['metadata'] = self._get_docker_metadata(docker_build_result_values['identifier'])
            result = self.env['runbot.docker_build_result'].create(docker_build_result_values)
            if not success:
                message = f'Build failure, check results for more info ({result.summary})'
                self.message_post(body=message)
                _logger.error(message)
        return image_id


class DockerBuildOutput(models.Model):
    _name = 'runbot.docker_build_result'
    _description = "Result of a docker file build"
    _order = 'id desc'

    result = fields.Selection(string="Result", selection=[('error', 'Error'), ('success', 'Success')])
    host_id = fields.Many2one('runbot.host', string="Host")
    duration = fields.Float("Exec time")
    dockerfile_id = fields.Many2one('runbot.dockerfile', string="Docker file")
    output = fields.Text('Output')
    content = fields.Text('Content')
    identifier = fields.Char('Identifier')
    summary = fields.Char("Summary", compute='_compute_summary', store=True)
    metadata = JsonDictField("Metadata", help="Additionnal data about this image generated by nightly builds")

    @api.depends('output')
    def _compute_summary(self):
        for record in self:
            summary = ''
            for line in reversed(record.output.split('\n')):
                if len(line) > 5:
                    summary = line
                    break
            record.summary = summary
