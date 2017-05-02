# -*- encoding: utf-8 -*-

import contextlib
import datetime
import fcntl
import glob
import hashlib
import itertools
import logging
import operator
import os
import psycopg2
import re
import resource
import shutil
import signal
import simplejson
import socket
import subprocess
import sys
import time
from collections import OrderedDict

import dateutil.parser
from dateutil.relativedelta import relativedelta
import requests
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath
import werkzeug

import openerp
from openerp import http, SUPERUSER_ID
from openerp.http import request
from openerp.modules import get_module_resource
from openerp.osv import fields, osv
from openerp.tools import config, appdirs
from openerp.addons.website.models.website import slug
from openerp.addons.website_sale.controllers.main import QueryURL

_logger = logging.getLogger(__name__)

#----------------------------------------------------------
# Runbot Const
#----------------------------------------------------------

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
_re_job = re.compile('_job_\d')
_re_coverage = re.compile(r'\bcoverage\b')

# increase cron frequency from 0.016 Hz to 0.1 Hz to reduce starvation and improve throughput with many workers
# TODO: find a nicer way than monkey patch to accomplish this
openerp.service.server.SLEEP_INTERVAL = 10
openerp.addons.base.ir.ir_cron._intervalTypes['minutes'] = lambda interval: relativedelta(seconds=interval*10)

#----------------------------------------------------------
# RunBot helpers
#----------------------------------------------------------

def log(*l, **kw):
    out = [i if isinstance(i, basestring) else repr(i) for i in l] + \
          ["%s=%r" % (k, v) for k, v in kw.items()]
    _logger.debug(' '.join(out))

def dashes(string):
    """Sanitize the input string"""
    for i in '~":\'':
        string = string.replace(i, "")
    for i in '/_. ':
        string = string.replace(i, "-")
    return string

def mkdirs(dirs):
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d)

def grep(filename, string):
    if os.path.isfile(filename):
        return open(filename).read().find(string) != -1
    return False

def rfind(filename, pattern):
    """Determine in something in filename matches the pattern"""
    if os.path.isfile(filename):
        regexp = re.compile(pattern, re.M)
        with open(filename, 'r') as f:
            if regexp.findall(f.read()):
                return True
    return False

def lock(filename):
    fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0600)
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

def locked(filename):
    result = False
    try:
        fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            result = True
        os.close(fd)
    except OSError:
        result = False
    return result

def nowait():
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

def run(l, env=None):
    """Run a command described by l in environment env"""
    log("run", l)
    env = dict(os.environ, **env) if env else None
    if isinstance(l, list):
        if env:
            rc = os.spawnvpe(os.P_WAIT, l[0], l, env)
        else:
            rc = os.spawnvp(os.P_WAIT, l[0], l)
    elif isinstance(l, str):
        tmp = ['sh', '-c', l]
        if env:
            rc = os.spawnvpe(os.P_WAIT, tmp[0], tmp, env)
        else:
            rc = os.spawnvp(os.P_WAIT, tmp[0], tmp)
    log("run", rc=rc)
    return rc

def now():
    return time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT)

def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(time.strptime(datetime, openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT))

def s2human(time):
    """Convert a time in second into an human readable string"""
    for delay, desc in [(86400,'d'),(3600,'h'),(60,'m')]:
        if time >= delay:
            return str(int(time / delay)) + desc
    return str(int(time)) + "s"

def flatten(list_of_lists):
    return list(itertools.chain.from_iterable(list_of_lists))

def decode_utf(field):
    try:
        return field.decode('utf-8')
    except UnicodeDecodeError:
        return ''

def uniq_list(l):
    return OrderedDict.fromkeys(l).keys()

def fqdn():
    return socket.getfqdn()

@contextlib.contextmanager
def local_pgadmin_cursor():
    cnx = None
    try:
        cnx = psycopg2.connect("dbname=postgres")
        cnx.autocommit = True # required for admin commands
        yield cnx.cursor()
    finally:
        if cnx: cnx.close()

#----------------------------------------------------------
# RunBot Models
#----------------------------------------------------------

class runbot_repo(osv.osv):
    _name = "runbot.repo"
    _order = 'sequence, name, id'

    def _get_path(self, cr, uid, ids, field_name, arg, context=None):
        root = self._root(cr, uid)
        result = {}
        for repo in self.browse(cr, uid, ids, context=context):
            name = repo.name
            for i in '@:/':
                name = name.replace(i, '_')
            result[repo.id] = os.path.join(root, 'repo', name)
        return result

    def _get_base(self, cr, uid, ids, field_name, arg, context=None):
        result = {}
        for repo in self.browse(cr, uid, ids, context=context):
            name = re.sub('.+@', '', repo.name)
            name = re.sub('.git$', '', name)
            name = name.replace(':','/')
            result[repo.id] = name
        return result

    _columns = {
        'name': fields.char('Repository', required=True),
        'sequence': fields.integer('Sequence', select=True),
        'path': fields.function(_get_path, type='char', string='Directory', readonly=1),
        'base': fields.function(_get_base, type='char', string='Base URL', readonly=1),
        'nginx': fields.boolean('Nginx'),
        'mode': fields.selection([('disabled', 'Disabled'),
                                  ('poll', 'Poll'),
                                  ('hook', 'Hook')],
                                  string="Mode", required=True, help="hook: Wait for webhook on /runbot/hook/<id> i.e. github push event"),
        'hook_time': fields.datetime('Last hook time'),
        'duplicate_id': fields.many2one('runbot.repo', 'Duplicate repo', help='Repository for finding duplicate builds'),
        'modules': fields.char("Modules to install", help="Comma-separated list of modules to install and test."),
        'modules_auto': fields.selection([('none', 'None (only explicit modules list)'),
                                          ('repo', 'Repository modules (excluding dependencies)'),
                                          ('all', 'All modules (including dependencies)')],
                                         string="Other modules to install automatically"),
        'dependency_ids': fields.many2many(
            'runbot.repo', 'runbot_repo_dep_rel',
            id1='dependant_id', id2='dependency_id',
            string='Extra dependencies',
            help="Community addon repos which need to be present to run tests."),
        'token': fields.char("Github token", groups="runbot.group_runbot_admin"),
        'group_ids': fields.many2many('res.groups', string='Limited to groups'),
    }
    _defaults = {
        'mode': 'poll',
        'modules_auto': 'repo',
        'job_timeout': 30,
    }

    def _domain(self, cr, uid, context=None):
        domain = self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.domain', fqdn())
        return domain

    def _root(self, cr, uid, context=None):
        """Return root directory of repository"""
        default = os.path.join(os.path.dirname(__file__), 'static')
        return self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.root', default)

    def _git(self, cr, uid, ids, cmd, context=None):
        """Execute git command cmd"""
        for repo in self.browse(cr, uid, ids, context=context):
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.info("git: %s", ' '.join(cmd))
            return subprocess.check_output(cmd)

    def _git_export(self, cr, uid, ids, treeish, dest, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            _logger.debug('checkout %s %s %s', repo.name, treeish, dest)
            p1 = subprocess.Popen(['git', '--git-dir=%s' % repo.path, 'archive', treeish], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(['tar', '-xmC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
            p2.communicate()[0]

    def _github(self, cr, uid, ids, url, payload=None, ignore_errors=False, context=None):
        """Return a http request to be sent to github"""
        for repo in self.browse(cr, uid, ids, context=context):
            if not repo.token:
                return
            try:
                match_object = re.search('([^/]+)/([^/]+)/([^/.]+(.git)?)', repo.base)
                if match_object:
                    url = url.replace(':owner', match_object.group(2))
                    url = url.replace(':repo', match_object.group(3))
                    url = 'https://api.%s%s' % (match_object.group(1),url)
                    session = requests.Session()
                    session.auth = (repo.token,'x-oauth-basic')
                    session.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
                    if payload:
                        response = session.post(url, data=simplejson.dumps(payload))
                    else:
                        response = session.get(url)
                    response.raise_for_status()
                    return response.json()
            except Exception:
                if ignore_errors:
                    _logger.exception('Ignored github error %s %r', url, payload)
                else:
                    raise

    def _update(self, cr, uid, ids, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            self._update_git(cr, uid, repo)

    def _update_git(self, cr, uid, repo, context=None):
        _logger.debug('repo %s updating branches', repo.name)

        Build = self.pool['runbot.build']
        Branch = self.pool['runbot.branch']

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            run(['git', 'clone', '--bare', repo.name, repo.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook' and repo.hook_time and dt2time(repo.hook_time) < fetch_time:
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                              repo.name, int(t0 - fetch_time), int(t0 - dt2time(repo.hook_time)))
                return

        repo._git(['gc', '--auto', '--prune=all'])
        repo._git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
        repo._git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])

        fields = ['refname','objectname','committerdate:iso8601','authorname','authoremail','subject','committername','committeremail']
        fmt = "%00".join(["%("+field+")" for field in fields])
        git_refs = repo._git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[decode_utf(field) for field in line.split('\x00')] for line in git_refs.split('\n')]

        cr.execute("""
            WITH t (branch) AS (SELECT unnest(%s))
          SELECT t.branch, b.id
            FROM t LEFT JOIN runbot_branch b ON (b.name = t.branch)
           WHERE b.repo_id = %s;
        """, ([r[0] for r in refs], repo.id))
        ref_branches = {r[0]: r[1] for r in cr.fetchall()}

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            # create or get branch
            if ref_branches.get(name):
                branch_id = ref_branches[name]
            else:
                _logger.debug('repo %s found new branch %s', repo.name, name)
                branch_id = Branch.create(cr, uid, {'repo_id': repo.id, 'name': name})
            branch = Branch.browse(cr, uid, [branch_id], context=context)[0]
            # skip build for old branches
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) < datetime.datetime.now():
                continue
            # create build (and mark previous builds as skipped) if not found
            build_ids = Build.search(cr, uid, [('branch_id', '=', branch.id), ('name', '=', sha)])
            if not build_ids:
                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }

                if not branch.sticky:
                    skipped_build_sequences = Build.search_read(cr, uid, [('branch_id', '=', branch.id), ('state', '=', 'pending')],
                                                                fields=['sequence'], order='sequence asc', context=context)
                    if skipped_build_sequences:
                        to_be_skipped_ids = [build['id'] for build in skipped_build_sequences]
                        Build._skip(cr, uid, to_be_skipped_ids, context=context)
                        # new order keeps lowest skipped sequence
                        build_info['sequence'] = skipped_build_sequences[0]['sequence']
                Build.create(cr, uid, build_info)

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', repo.id), ('state', '=', 'pending')]
        icp = self.pool['ir.config_parameter']
        running_max = int(icp.get_param(cr, uid, 'runbot.running_max', default=75))
        to_be_skipped_ids = Build.search(cr, uid, skippable_domain, order='sequence desc', offset=running_max)
        Build._skip(cr, uid, to_be_skipped_ids)

    def _scheduler(self, cr, uid, ids=None, context=None):
        icp = self.pool['ir.config_parameter']
        workers = int(icp.get_param(cr, uid, 'runbot.workers', default=6))
        running_max = int(icp.get_param(cr, uid, 'runbot.running_max', default=75))
        host = fqdn()

        Build = self.pool['runbot.build']
        domain = [('repo_id', 'in', ids)]
        domain_host = domain + [('host', '=', host)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        build_ids = Build.search(cr, uid, domain_host + [('state', 'in', ['testing', 'running'])])
        Build._schedule(cr, uid, build_ids)

        # launch new tests
        testing = Build.search_count(cr, uid, domain_host + [('state', '=', 'testing')])
        pending = Build.search_count(cr, uid, domain + [('state', '=', 'pending')])

        while testing < workers and pending > 0:

            # find sticky pending build if any, otherwise, last pending (by id, not by sequence) will do the job
            pending_ids = Build.search(cr, uid, domain + [('state', '=', 'pending'), ('branch_id.sticky', '=', True)], limit=1)
            if not pending_ids:
                pending_ids = Build.search(cr, uid, domain + [('state', '=', 'pending')], order="sequence", limit=1)

            pending_build = Build.browse(cr, uid, pending_ids[0])
            pending_build._schedule()

            # compute the number of testing and pending jobs again
            testing = Build.search_count(cr, uid, domain_host + [('state', '=', 'testing')])
            pending = Build.search_count(cr, uid, domain + [('state', '=', 'pending')])

        # terminate and reap doomed build
        build_ids = Build.search(cr, uid, domain_host + [('state', '=', 'running')])
        # sort builds: the last build of each sticky branch then the rest
        sticky = {}
        non_sticky = []
        for build in Build.browse(cr, uid, build_ids):
            if build.branch_id.sticky and build.branch_id.id not in sticky:
                sticky[build.branch_id.id] = build.id
            else:
                non_sticky.append(build.id)
        build_ids = sticky.values()
        build_ids += non_sticky
        # terminate extra running builds
        Build._kill(cr, uid, build_ids[running_max:])
        Build._reap(cr, uid, build_ids)

    def _reload_nginx(self, cr, uid, context=None):
        settings = {}
        settings['port'] = config['xmlrpc_port']
        settings['runbot_static'] = os.path.join(get_module_resource('runbot', 'static'), '')
        nginx_dir = os.path.join(self._root(cr, uid), 'nginx')
        settings['nginx_dir'] = nginx_dir
        ids = self.search(cr, uid, [('nginx','=',True)], order='id')
        if ids:
            build_ids = self.pool['runbot.build'].search(cr, uid, [('repo_id','in',ids), ('state','=','running')])
            settings['builds'] = self.pool['runbot.build'].browse(cr, uid, build_ids)

            nginx_config = self.pool['ir.ui.view'].render(cr, uid, "runbot.nginx_config", settings)
            mkdirs([nginx_dir])
            open(os.path.join(nginx_dir, 'nginx.conf'),'w').write(nginx_config)
            try:
                _logger.debug('reload nginx')
                pid = int(open(os.path.join(nginx_dir, 'nginx.pid')).read().strip(' \n'))
                os.kill(pid, signal.SIGHUP)
            except Exception:
                _logger.debug('start nginx')
                if run(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf']):
                    # obscure nginx bug leaving orphan worker listening on nginx port
                    if not run(['pkill', '-f', '-P1', 'nginx: worker']):
                        _logger.debug('failed to start nginx - orphan worker killed, retrying')
                        run(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf'])
                    else:
                        _logger.debug('failed to start nginx - failed to kill orphan worker - oh well')

    def killall(self, cr, uid, ids=None, context=None):
        return

    def _cron(self, cr, uid, ids=None, context=None):
        ids = self.search(cr, uid, [('mode', '!=', 'disabled')], context=context)
        self._update(cr, uid, ids, context=context)
        self._scheduler(cr, uid, ids, context=context)
        self._reload_nginx(cr, uid, context=context)

    # backwards compatibility
    def cron(self, cr, uid, ids=None, context=None):
        if uid == SUPERUSER_ID:
            return self._cron(cr, uid, ids=ids, context=context)

class runbot_branch(osv.osv):
    _name = "runbot.branch"
    _order = 'name'

    def _get_branch_name(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            r[branch.id] = branch.name.split('/')[-1]
        return r

    def _get_pull_head_name(self, cr, uid, ids, field_name, arg, context=None):
        r = dict.fromkeys(ids, False)
        for bid in ids:
            pi = self._get_pull_info(cr, SUPERUSER_ID, [bid], context=context)
            if pi:
                r[bid] = pi['head']['ref']
        return r

    def _get_branch_url(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            if re.match('^[0-9]+$', branch.branch_name):
                r[branch.id] = "https://%s/pull/%s" % (branch.repo_id.base, branch.branch_name)
            else:
                r[branch.id] = "https://%s/tree/%s" % (branch.repo_id.base, branch.branch_name)
        return r
        
    def _get_branch_quickconnect_url(self, cr, uid, ids, fqdn, dest, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            if branch.branch_name.startswith('7'):
                r[branch.id] = "http://%s/login?db=%s-all&login=admin&key=admin" % (fqdn, dest)
            elif branch.name.startswith('8'):
                r[branch.id] = "http://%s/login?db=%s-all&login=admin&key=admin&redirect=/web?debug=1" % (fqdn, dest)
            else:
                r[branch.id] = "http://%s/web/login?db=%s-all&login=admin&redirect=/web?debug=1" % (fqdn, dest)
        return r
            
    _columns = {
        'repo_id': fields.many2one('runbot.repo', 'Repository', required=True, ondelete='cascade', select=1),
        'name': fields.char('Ref Name', required=True),
        'branch_name': fields.function(_get_branch_name, type='char', string='Branch', readonly=1, store=True),
        'branch_url': fields.function(_get_branch_url, type='char', string='Branch url', readonly=1),
        'pull_head_name': fields.function(_get_pull_head_name, type='char', string='PR HEAD name', readonly=1, store=True),
        'sticky': fields.boolean('Sticky', select=1),
        'coverage': fields.boolean('Coverage'),
        'state': fields.char('Status'),
        'modules': fields.char("Modules to Install", help="Comma-separated list of modules to install and test."),
        'job_timeout': fields.integer('Job Timeout (minutes)', help='For default timeout: Mark it zero'),
    }

    def _get_pull_info(self, cr, uid, ids, context=None):
        assert len(ids) == 1
        branch = self.browse(cr, uid, ids[0], context=context)
        repo = branch.repo_id
        if repo.token and branch.name.startswith('refs/pull/'):
            pull_number = branch.name[len('refs/pull/'):]
            return repo._github('/repos/:owner/:repo/pulls/%s' % pull_number, ignore_errors=True) or {}
        return {}

    def _is_on_remote(self, cr, uid, ids, context=None):
        # check that a branch still exists on remote
        assert len(ids) == 1
        branch = self.browse(cr, uid, ids[0], context=context)
        repo = branch.repo_id
        try:
            repo._git(['ls-remote', '-q', '--exit-code', repo.name, branch.name])
        except subprocess.CalledProcessError:
            return False
        return True

    def create(self, cr, uid, values, context=None):
        values.setdefault('coverage', _re_coverage.search(values.get('name') or '') is not None)
        return super(runbot_branch, self).create(cr, uid, values, context=context)

class runbot_build(osv.osv):
    _name = "runbot.build"
    _order = 'id desc'

    def _get_dest(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            nickname = dashes(build.branch_id.name.split('/')[2])[:32]
            r[build.id] = "%05d-%s-%s" % (build.id, nickname, build.name[:6])
        return r

    def _get_time(self, cr, uid, ids, field_name, arg, context=None):
        """Return the time taken by the tests"""
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            r[build.id] = 0
            if build.job_end:
                r[build.id] = int(dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                r[build.id] = int(time.time() - dt2time(build.job_start))
        return r

    def _get_age(self, cr, uid, ids, field_name, arg, context=None):
        """Return the time between job start and now"""
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            r[build.id] = 0
            if build.job_start:
                r[build.id] = int(time.time() - dt2time(build.job_start))
        return r

    def _get_domain(self, cr, uid, ids, field_name, arg, context=None):
        result = {}
        domain = self.pool['runbot.repo']._domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            if build.repo_id.nginx:
                result[build.id] = "%s.%s" % (build.dest, build.host)
            else:
                result[build.id] = "%s:%s" % (domain, build.port)
        return result

    _columns = {
        'branch_id': fields.many2one('runbot.branch', 'Branch', required=True, ondelete='cascade', select=1),
        'repo_id': fields.related(
            'branch_id', 'repo_id', type="many2one", relation="runbot.repo",
            string="Repository", readonly=True, ondelete='cascade', select=1,
            store={
                'runbot.build': (lambda s, c, u, ids, ctx: ids, ['branch_id'], 20),
                'runbot.branch': (
                    lambda self, cr, uid, ids, ctx: self.pool['runbot.build'].search(
                        cr, uid, [('branch_id', 'in', ids)]),
                    ['repo_id'],
                    10,
                ),
            }),
        'name': fields.char('Revno', required=True, select=1),
        'host': fields.char('Host'),
        'port': fields.integer('Port'),
        'dest': fields.function(_get_dest, type='char', string='Dest', readonly=1, store=True),
        'domain': fields.function(_get_domain, type='char', string='URL'),
        'date': fields.datetime('Commit date'),
        'author': fields.char('Author'),
        'author_email': fields.char('Author Email'),
        'committer': fields.char('Committer'),
        'committer_email': fields.char('Committer Email'),
        'subject': fields.text('Subject'),
        'sequence': fields.integer('Sequence', select=1),
        'modules': fields.char("Modules to Install"),
        'result': fields.char('Result'), # ok, ko, warn, skipped, killed
        'pid': fields.integer('Pid'),
        'state': fields.char('Status'), # pending, testing, running, done, duplicate
        'job': fields.char('Job'), # job_*
        'job_start': fields.datetime('Job start'),
        'job_end': fields.datetime('Job end'),
        'job_time': fields.function(_get_time, type='integer', string='Job time'),
        'job_age': fields.function(_get_age, type='integer', string='Job age'),
        'duplicate_id': fields.many2one('runbot.build', 'Corresponding Build'),
        'server_match': fields.selection([('builtin', 'This branch includes Odoo server'),
                                          ('exact', 'branch/PR exact name'),
                                          ('prefix', 'branch whose name is a prefix of current one'),
                                          ('fuzzy', 'Fuzzy - common ancestor found'),
                                          ('default', 'No match found - defaults to master')],
                                        string='Server branch matching')
    }

    _defaults = {
        'state': 'pending',
        'result': '',
    }

    def create(self, cr, uid, values, context=None):
        build_id = super(runbot_build, self).create(cr, uid, values, context=context)
        build = self.browse(cr, uid, build_id)
        extra_info = {'sequence' : build_id}

        # detect duplicate
        domain = [
            ('repo_id','=',build.repo_id.duplicate_id.id), 
            ('name', '=', build.name), 
            ('duplicate_id', '=', False), 
            '|', ('result', '=', False), ('result', '!=', 'skipped')
        ]
        duplicate_ids = self.search(cr, uid, domain, context=context)

        if len(duplicate_ids):
            extra_info.update({'state': 'duplicate', 'duplicate_id': duplicate_ids[0]})
            self.write(cr, uid, [duplicate_ids[0]], {'duplicate_id': build_id})
        self.write(cr, uid, [build_id], extra_info, context=context)

    def _reset(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, { 'state' : 'pending' }, context=context)

    def _logger(self, cr, uid, ids, *l, **kw):
        l = list(l)
        for build in self.browse(cr, uid, ids, **kw):
            l[0] = "%s %s" % (build.dest , l[0])
            _logger.debug(*l)

    def _list_jobs(self):
        return sorted(job[1:] for job in dir(self) if _re_job.match(job))

    def _find_port(self, cr, uid):
        # currently used port
        ids = self.search(cr, uid, [('state','not in',['pending','done'])])
        ports = set(i['port'] for i in self.read(cr, uid, ids, ['port']))

        # starting port
        icp = self.pool['ir.config_parameter']
        port = int(icp.get_param(cr, uid, 'runbot.starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 2

        return port

    def _get_closest_branch_name(self, cr, uid, ids, target_repo_id, context=None):
        """Return (repo, branch name) of the closest common branch between build's branch and
           any branch of target_repo or its duplicated repos.

        Rules priority for choosing the branch from the other repo is:
        1. Same branch name
        2. A PR whose head name match
        3. Match a branch which is the dashed-prefix of current branch name
        4. Common ancestors (git merge-base)
        Note that PR numbers are replaced by the branch name of the PR target
        to prevent the above rules to mistakenly link PR of different repos together.
        """
        assert len(ids) == 1
        branch_pool = self.pool['runbot.branch']

        build = self.browse(cr, uid, ids[0], context=context)
        branch, repo = build.branch_id, build.repo_id
        pi = branch._get_pull_info()
        name = pi['base']['ref'] if pi else branch.branch_name

        target_repo = self.pool['runbot.repo'].browse(cr, uid, target_repo_id, context=context)

        target_repo_ids = [target_repo.id]
        r = target_repo.duplicate_id
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        _logger.debug('Search closest of %s (%s) in repos %r', name, repo.name, target_repo_ids)

        sort_by_repo = lambda d: (not d['sticky'],      # sticky first
                                  target_repo_ids.index(d['repo_id'][0]),
                                  -1 * len(d.get('branch_name', '')),
                                  -1 * d['id'])
        result_for = lambda d, match='exact': (d['repo_id'][0], d['name'], match)
        branch_exists = lambda d: branch_pool._is_on_remote(cr, uid, [d['id']], context=context)
        fields = ['name', 'repo_id', 'sticky']

        # 1. same name, not a PR
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('branch_name', '=', name),
            ('name', '=like', 'refs/heads/%'),
        ]
        targets = branch_pool.search_read(cr, uid, domain, fields, order='id DESC',
                                          context=context)
        targets = sorted(targets, key=sort_by_repo)
        if targets and branch_exists(targets[0]):
            return result_for(targets[0])

        # 2. PR with head name equals
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('pull_head_name', '=', name),
            ('name', '=like', 'refs/pull/%'),
        ]
        pulls = branch_pool.search_read(cr, uid, domain, fields, order='id DESC',
                                        context=context)
        pulls = sorted(pulls, key=sort_by_repo)
        for pull in pulls:
            pi = branch_pool._get_pull_info(cr, uid, [pull['id']], context=context)
            if pi.get('state') == 'open':
                return result_for(pull)

        # 3. Match a branch which is the dashed-prefix of current branch name
        branches = branch_pool.search_read(
            cr, uid,
            [('repo_id', 'in', target_repo_ids), ('name', '=like', 'refs/heads/%')],
            fields + ['branch_name'], order='id DESC', context=context
        )
        branches = sorted(branches, key=sort_by_repo)

        for branch in branches:
            if name.startswith(branch['branch_name'] + '-') and branch_exists(branch):
                return result_for(branch, 'prefix')

        # 4. Common ancestors (git merge-base)
        for target_id in target_repo_ids:
            common_refs = {}
            cr.execute("""
                SELECT b.name
                  FROM runbot_branch b,
                       runbot_branch t
                 WHERE b.repo_id = %s
                   AND t.repo_id = %s
                   AND b.name = t.name
                   AND b.name LIKE 'refs/heads/%%'
            """, [repo.id, target_id])
            for common_name, in cr.fetchall():
                try:
                    commit = repo._git(['merge-base', branch['name'], common_name]).strip()
                    cmd = ['log', '-1', '--format=%cd', '--date=iso', commit]
                    common_refs[common_name] = repo._git(cmd).strip()
                except subprocess.CalledProcessError:
                    # If merge-base doesn't find any common ancestor, the command exits with a
                    # non-zero return code, resulting in subprocess.check_output raising this
                    # exception. We ignore this branch as there is no common ref between us.
                    continue
            if common_refs:
                b = sorted(common_refs.iteritems(), key=operator.itemgetter(1), reverse=True)[0][0]
                return target_id, b, 'fuzzy'

        # 5. last-resort value
        return target_repo_id, 'master', 'default'

    def _path(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            root = self.pool['runbot.repo']._root(cr, uid)
            return os.path.join(root, 'build', build.dest, *l)

    def _server(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            if os.path.exists(build._path('odoo')):
                return build._path('odoo', *l)
            return build._path('openerp', *l)

    def _filter_modules(self, cr, uid, modules, available_modules, explicit_modules):
        blacklist_modules = set(['auth_ldap', 'document_ftp', 'base_gengo',
                                 'website_gengo', 'website_instantclick',
                                 'pad', 'pad_project', 'note_pad',
                                 'pos_cache', 'pos_blackbox_be'])

        mod_filter = lambda m: (
            m in available_modules and
            (m in explicit_modules or (not m.startswith(('hw_', 'theme_', 'l10n_'))
                                       and m not in blacklist_modules))
        )
        return uniq_list(filter(mod_filter, modules))

    def _checkout(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # starts from scratch
            if os.path.isdir(build._path()):
                shutil.rmtree(build._path())

            # runbot log path
            mkdirs([build._path("logs"), build._server('addons')])

            # checkout branch
            build.branch_id.repo_id._git_export(build.name, build._path())

            # v6 rename bin -> openerp
            if os.path.isdir(build._path('bin/addons')):
                shutil.move(build._path('bin'), build._server())

            has_server = os.path.isfile(build._server('__init__.py'))
            server_match = 'builtin'

            # build complete set of modules to install
            modules_to_move = []
            modules_to_test = ((build.branch_id.modules or '') + ',' +
                               (build.repo_id.modules or ''))
            modules_to_test = filter(None, modules_to_test.split(','))
            explicit_modules = set(modules_to_test)
            _logger.debug("manual modules_to_test for build %s: %s", build.dest, modules_to_test)

            if not has_server:
                if build.repo_id.modules_auto == 'repo':
                    modules_to_test += [
                        os.path.basename(os.path.dirname(a))
                        for a in (glob.glob(build._path('*/__openerp__.py')) +
                                  glob.glob(build._path('*/__manifest__.py')))
                    ]
                    _logger.debug("local modules_to_test for build %s: %s", build.dest, modules_to_test)

                for extra_repo in build.repo_id.dependency_ids:
                    repo_id, closest_name, server_match = build._get_closest_branch_name(extra_repo.id)
                    repo = self.pool['runbot.repo'].browse(cr, uid, repo_id, context=context)
                    _logger.debug('branch %s of %s: %s match branch %s of %s',
                                  build.branch_id.name, build.repo_id.name,
                                  server_match, closest_name, repo.name)
                    build._log(
                        'Building environment',
                        '%s match branch %s of %s' % (server_match, closest_name, repo.name)
                    )
                    repo._git_export(closest_name, build._path())

                # Finally mark all addons to move to openerp/addons
                modules_to_move += [
                    os.path.dirname(module)
                    for module in (glob.glob(build._path('*/__openerp__.py')) +
                                   glob.glob(build._path('*/__manifest__.py')))
                ]

            # move all addons to server addons path
            for module in uniq_list(glob.glob(build._path('addons/*')) + modules_to_move):
                basename = os.path.basename(module)
                addon_path = build._server('addons', basename)
                if os.path.exists(addon_path):
                    build._log(
                        'Building environment',
                        'You have duplicate modules in your branches "%s"' % basename
                    )
                    if os.path.islink(addon_path) or os.path.isfile(addon_path):
                        os.remove(addon_path)
                    else:
                        shutil.rmtree(addon_path)
                shutil.move(module, build._server('addons'))

            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            if build.repo_id.modules_auto == 'all' or (build.repo_id.modules_auto != 'none' and has_server):
                modules_to_test += available_modules

            modules_to_test = self._filter_modules(cr, uid, modules_to_test,
                                                   set(available_modules), explicit_modules)
            _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
            build.write({'server_match': server_match,
                         'modules': ','.join(modules_to_test)})

    def _local_pg_dropdb(self, cr, uid, dbname):
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname) for pn in 'OpenERP Odoo'.split()]
        run(['rm', '-rf'] + paths)

    def _local_pg_createdb(self, cr, uid, dbname):
        self._local_pg_dropdb(cr, uid, dbname)
        _logger.debug("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % dbname)

    def _cmd(self, cr, uid, ids, context=None):
        """Return a list describing the command to start the build"""
        for build in self.browse(cr, uid, ids, context=context):
            bins = [
                'odoo-bin',                 # >= 10.0
                'openerp-server',           # 9.0, 8.0
                'openerp-server.py',        # 7.0
                'bin/openerp-server.py',    # < 7.0
            ]
            for server_path in map(build._path, bins):
                if os.path.isfile(server_path):
                    break

            # commandline
            cmd = [
                sys.executable,
                server_path,
                "--xmlrpc-port=%d" % build.port,
            ]
            # options
            if grep(build._server("tools/config.py"), "no-xmlrpcs"):
                cmd.append("--no-xmlrpcs")
            if grep(build._server("tools/config.py"), "no-netrpc"):
                cmd.append("--no-netrpc")
            if grep(build._server("tools/config.py"), "log-db"):
                logdb = cr.dbname
                if config['db_host'] and grep(build._server('sql_db.py'), 'allow_uri'):
                    logdb = 'postgres://{cfg[db_user]}:{cfg[db_password]}@{cfg[db_host]}/{db}'.format(cfg=config, db=cr.dbname)
                cmd += ["--log-db=%s" % logdb]

            if grep(build._server("tools/config.py"), "data-dir"):
                datadir = build._path('datadir')
                if not os.path.exists(datadir):
                    os.mkdir(datadir)
                cmd += ["--data-dir", datadir]

        return cmd, build.modules

    def _spawn(self, cmd, lock_path, log_path, cpu_limit=None, shell=False, env=None):
        def preexec_fn():
            os.setsid()
            if cpu_limit:
                # set soft cpulimit
                soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
                r = resource.getrusage(resource.RUSAGE_SELF)
                cpu_time = r.ru_utime + r.ru_stime
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_time + cpu_limit, hard))
            # close parent files
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))
            lock(lock_path)
        out = open(log_path, "w")
        _logger.debug("spawn: %s stdout: %s", ' '.join(cmd), log_path)
        p = subprocess.Popen(cmd, stdout=out, stderr=out, preexec_fn=preexec_fn, shell=shell, env=env)
        return p.pid

    def _github_status(self, cr, uid, ids, context=None):
        """Notify github of failed/successful builds"""
        runbot_domain = self.pool['runbot.repo']._domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            desc = "runbot build %s" % (build.dest,)
            if build.state == 'testing':
                state = 'pending'
            elif build.state in ('running', 'done'):
                state = 'error'
                if build.result == 'ok':
                    state = 'success'
                if build.result == 'ko':
                    state = 'failure'
                desc += " (runtime %ss)" % (build.job_time,)
            else:
                continue
            status = {
                "state": state,
                "target_url": "http://%s/runbot/build/%s" % (runbot_domain, build.id),
                "description": desc,
                "context": "ci/runbot"
            }
            _logger.debug("github updating status %s to %s", build.name, state)
            build.repo_id._github('/repos/:owner/:repo/statuses/%s' % build.name, status, ignore_errors=True)

    def _job_00_init(self, cr, uid, build, lock_path, log_path):
        build._log('init', 'Init build environment')
        # notify pending build - avoid confusing users by saying nothing
        build._github_status()
        build._checkout()
        return -2

    def _job_10_test_base(self, cr, uid, build, lock_path, log_path):
        build._log('test_base', 'Start test base module')
        # run base test
        self._local_pg_createdb(cr, uid, "%s-base" % build.dest)
        cmd, mods = build._cmd()
        if grep(build._server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-base' % build.dest, '-i', 'base', '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        return self._spawn(cmd, lock_path, log_path, cpu_limit=300)

    def _job_20_test_all(self, cr, uid, build, lock_path, log_path):
        build._log('test_all', 'Start test all modules')
        self._local_pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd, mods = build._cmd()
        if grep(build._server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-all' % build.dest, '-i', openerp.tools.ustr(mods), '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        env = None
        if build.branch_id.coverage:
            env = self._coverage_env(build)
            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            bad_modules = set(available_modules) - set((mods or '').split(','))
            omit = ['--omit', ','.join(build._server('addons', m) for m in bad_modules)] if bad_modules else []
            cmd = ['coverage', 'run', '--branch', '--source', build._server()] + omit + cmd[1:]
        # reset job_start to an accurate job_20 job_time
        build.write({'job_start': now()})
        return self._spawn(cmd, lock_path, log_path, cpu_limit=2100, env=env)

    def _coverage_env(self, build):
        return dict(os.environ, COVERAGE_FILE=build._path('.coverage'))

    def _job_21_coverage(self, cr, uid, build, lock_path, log_path):
        if not build.branch_id.coverage:
            return -2
        cov_path = build._path('coverage')
        mkdirs([cov_path])
        cmd = ["coverage", "html", "-d", cov_path, "--ignore-errors"]
        return self._spawn(cmd, lock_path, log_path, env=self._coverage_env(build))

    def _job_30_run(self, cr, uid, build, lock_path, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        log_all = build._path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(build._server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"
        build.write(v)
        build._github_status()

        # run server
        cmd, mods = build._cmd()
        if os.path.exists(build._server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build._server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*$']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]

        ## Web60
        #self.client_web_path=os.path.join(self.running_path,"client-web")
        #self.client_web_bin_path=os.path.join(self.client_web_path,"openerp-web.py")
        #self.client_web_doc_path=os.path.join(self.client_web_path,"doc")
        #webclient_config % (self.client_web_port+port,self.server_net_port+port,self.server_net_port+port)
        #cfgs = [os.path.join(self.client_web_path,"doc","openerp-web.cfg"), os.path.join(self.client_web_path,"openerp-web.cfg")]
        #for i in cfgs:
        #    f=open(i,"w")
        #    f.write(config)
        #    f.close()
        #cmd=[self.client_web_bin_path]

        return self._spawn(cmd, lock_path, log_path, cpu_limit=None)

    def _force(self, cr, uid, ids, context=None):
        """Force a rebuild"""
        for build in self.browse(cr, uid, ids, context=context):
            domain = [('state', '=', 'pending')]
            pending_ids = self.search(cr, uid, domain, order='id', limit=1)
            if len(pending_ids):
                sequence = pending_ids[0]
            else:
                sequence = self.search(cr, uid, [], order='id desc', limit=1)[0]

            # Force it now
            if build.state == 'done' and build.result == 'skipped':
                values = {'state': 'pending', 'sequence':sequence, 'result': ''}
                self.write(cr, SUPERUSER_ID, [build.id], values, context=context)
            # or duplicate it
            elif build.state in ['running', 'done', 'duplicate']:
                new_build = {
                    'sequence': sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                }
                self.create(cr, SUPERUSER_ID, new_build, context=context)
            return build.repo_id.id

    def _schedule(self, cr, uid, ids, context=None):
        jobs = self._list_jobs()

        icp = self.pool['ir.config_parameter']
        # For retro-compatibility, keep this parameter in seconds
        default_timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800)) / 60

        for build in self.browse(cr, uid, ids, context=context):
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self._find_port(cr, uid)
                values = {
                    'host': fqdn(),
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
            else:
                # check if current job is finished
                lock_path = build._path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    timeout = (build.branch_id.job_timeout or default_timeout) * 60
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build._logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.write({'job_end': now()})
                        build._kill(result='killed')
                    continue
                build._logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            pid = None
            if build.state != 'done':
                build._logger('running %s', build.job)
                job_method = getattr(self, '_' + build.job)
                mkdirs([build._path('logs')])
                lock_path = build._path('logs', '%s.lock' % build.job)
                log_path = build._path('logs', '%s.txt' % build.job)
                try:
                    pid = job_method(cr, uid, build, lock_path, log_path)
                    build.write({'pid': pid})
                except Exception:
                    _logger.exception('%s failed running method %s', build.dest, build.job)
                    build._log(build.job, "failed running job method, see runbot log")
                    build._kill(result='ko')
                    continue
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build._schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build._local_cleanup()

    def _skip(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, {'state': 'done', 'result': 'skipped'}, context=context)
        to_unduplicate = self.search(cr, uid, [('id', 'in', ids), ('duplicate_id', '!=', False)])
        if len(to_unduplicate):
            self._force(cr, uid, to_unduplicate, context=context)

    def _local_cleanup(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # Cleanup the *local* cluster
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname LIKE %s
                """, [build.dest + '%'])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(cr, uid, db)

        # cleanup: find any build older than 7 days.
        root = self.pool['runbot.repo']._root(cr, uid)
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        cr.execute("""
            SELECT dest
              FROM runbot_build
             WHERE dest IN %s
               AND (state != 'done' OR job_end > (now() - interval '7 days'))
        """, [tuple(builds)])
        actives = set(b[0] for b in cr.fetchall())

        for b in builds:
            path = os.path.join(build_dir, b)
            if b not in actives and os.path.isdir(path):
                shutil.rmtree(path)
        
        # cleanup old unused databases
        cr.execute("select id from runbot_build where state in ('testing', 'running')")
        db_ids = [id[0] for id in cr.fetchall()]
        if db_ids:
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname ~ '^[0-9]+-.*'
                       AND SUBSTRING(datname, '^([0-9]+)-.*')::int not in %s
                           
                """, [tuple(db_ids)])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(cr, uid, db)

    def _kill(self, cr, uid, ids, result=None, context=None):
        host = fqdn()
        for build in self.browse(cr, uid, ids, context=context):
            if build.host != host:
                continue
            build._log('kill', 'Kill build %s' % build.dest)
            if build.pid:
                build._logger('killing %s', build.pid)
                try:
                    os.killpg(build.pid, signal.SIGKILL)
                except OSError:
                    pass
            v = {'state': 'done', 'job': False}
            if result:
                v['result'] = result
            build.write(v)
            cr.commit()
            build._github_status()
            build._local_cleanup()

    def _reap(self, cr, uid, ids):
        while True:
            try:
                pid, status, rusage = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

    def _log(self, cr, uid, ids, func, message, context=None):
        assert len(ids) == 1
        _logger.debug("Build %s %s %s", ids[0], func, message)
        self.pool['ir.logging'].create(cr, uid, {
            'build_id': ids[0],
            'level': 'INFO',
            'type': 'runbot',
            'name': 'odoo.runbot',
            'message': message,
            'path': 'runbot',
            'func': func,
            'line': '0',
        }, context=context)

class runbot_event(osv.osv):
    _inherit = 'ir.logging'
    _order = 'id'

    TYPES = [(t, t.capitalize()) for t in 'client server runbot'.split()]
    _columns = {
        'build_id': fields.many2one('runbot.build', 'Build'),
        'type': fields.selection(TYPES, string='Type', required=True, select=True),
    }

#----------------------------------------------------------
# Runbot Controller
#----------------------------------------------------------

class RunbotController(http.Controller):

    @http.route(['/runbot', '/runbot/repo/<model("runbot.repo"):repo>'], type='http', auth="public", website=True)
    def repo(self, repo=None, search='', limit='100', refresh='', **post):
        registry, cr, uid = request.registry, request.cr, request.uid

        branch_obj = registry['runbot.branch']
        build_obj = registry['runbot.build']
        icp = registry['ir.config_parameter']
        repo_obj = registry['runbot.repo']
        count = lambda dom: build_obj.search_count(cr, uid, dom)

        repo_ids = repo_obj.search(cr, uid, [])
        repos = repo_obj.browse(cr, uid, repo_ids)
        if not repo and repos:
            repo = repos[0] 

        context = {
            'repos': repos,
            'repo': repo,
            'host_stats': [],
            'pending_total': count([('state','=','pending')]),
            'limit': limit,
            'search': search,
            'refresh': refresh,
        }

        build_ids = []
        if repo:
            filters = {key: post.get(key, '1') for key in ['pending', 'testing', 'running', 'done']}
            domain = [('repo_id','=',repo.id)]
            domain += [('state', '!=', key) for key, value in filters.iteritems() if value == '0']
            if search:
                domain += ['|', '|', ('dest', 'ilike', search), ('subject', 'ilike', search), ('branch_id.branch_name', 'ilike', search)]

            build_ids = build_obj.search(cr, uid, domain, limit=int(limit))
            branch_ids, build_by_branch_ids = [], {}

            if build_ids:
                branch_query = """
                SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id WHERE bu.id in %s
                ORDER BY bu.sequence DESC
                """
                sticky_dom = [('repo_id','=',repo.id), ('sticky', '=', True)]
                sticky_branch_ids = [] if search else branch_obj.search(cr, uid, sticky_dom)
                cr.execute(branch_query, (tuple(build_ids),))
                branch_ids = uniq_list(sticky_branch_ids + [br[0] for br in cr.fetchall()])

                build_query = """
                    SELECT 
                        branch_id, 
                        max(case when br_bu.row = 1 then br_bu.build_id end),
                        max(case when br_bu.row = 2 then br_bu.build_id end),
                        max(case when br_bu.row = 3 then br_bu.build_id end),
                        max(case when br_bu.row = 4 then br_bu.build_id end)
                    FROM (
                        SELECT 
                            br.id AS branch_id, 
                            bu.id AS build_id,
                            row_number() OVER (PARTITION BY branch_id) AS row
                        FROM 
                            runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id 
                        WHERE 
                            br.id in %s
                        GROUP BY br.id, bu.id
                        ORDER BY br.id, bu.id DESC
                    ) AS br_bu
                    WHERE
                        row <= 4
                    GROUP BY br_bu.branch_id;
                """
                cr.execute(build_query, (tuple(branch_ids),))
                build_by_branch_ids = {
                    rec[0]: [r for r in rec[1:] if r is not None] for rec in cr.fetchall()
                }

            branches = branch_obj.browse(cr, uid, branch_ids, context=request.context)
            build_ids = flatten(build_by_branch_ids.values())
            build_dict = {build.id: build for build in build_obj.browse(cr, uid, build_ids, context=request.context) }

            def branch_info(branch):
                return {
                    'branch': branch,
                    'builds': [self.build_info(build_dict[build_id]) for build_id in build_by_branch_ids[branch.id]]
                }

            context.update({
                'branches': [branch_info(b) for b in branches],
                'testing': count([('repo_id','=',repo.id), ('state','=','testing')]),
                'running': count([('repo_id','=',repo.id), ('state','=','running')]),
                'pending': count([('repo_id','=',repo.id), ('state','=','pending')]),
                'qu': QueryURL('/runbot/repo/'+slug(repo), search=search, limit=limit, refresh=refresh, **filters),
                'filters': filters,
            })

        # consider host gone if no build in last 100
        build_threshold = max(build_ids or [0]) - 100

        for result in build_obj.read_group(cr, uid, [('id', '>', build_threshold)], ['host'], ['host']):
            if result['host']:
                context['host_stats'].append({
                    'host': result['host'],
                    'testing': count([('state', '=', 'testing'), ('host', '=', result['host'])]),
                    'running': count([('state', '=', 'running'), ('host', '=', result['host'])]),
                })

        return request.render("runbot.repo", context)

    @http.route(['/runbot/hook/<int:repo_id>'], type='http', auth="public", website=True)
    def hook(self, repo_id=None, **post):
        # TODO if repo_id == None parse the json['repository']['ssh_url'] and find the right repo
        repo = request.registry['runbot.repo'].browse(request.cr, SUPERUSER_ID, [repo_id])
        repo.hook_time = datetime.datetime.now().strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT)
        return ""

    @http.route(['/runbot/dashboard'], type='http', auth="public", website=True)
    def dashboard(self, refresh=None):
        cr = request.cr
        RB = request.env['runbot.build']
        repos = request.env['runbot.repo'].search([])   # respect record rules

        cr.execute("""SELECT bu.id
                        FROM runbot_branch br
                        JOIN LATERAL (SELECT *
                                        FROM runbot_build bu
                                       WHERE bu.branch_id = br.id
                                    ORDER BY id DESC
                                       LIMIT 3
                                     ) bu ON (true)
                        JOIN runbot_repo r ON (r.id = br.repo_id)
                       WHERE br.sticky
                         AND br.repo_id in %s
                    ORDER BY r.sequence, r.name, br.branch_name, bu.id DESC
                   """, [tuple(repos._ids)])

        builds = RB.browse(map(operator.itemgetter(0), cr.fetchall()))

        count = RB.search_count
        qctx = {
            'refresh': refresh,
            'host_stats': [],
            'pending_total': count([('state', '=', 'pending')]),
        }

        repos_values = qctx['repo_dict'] = OrderedDict()
        for build in builds:
            repo = build.repo_id
            branch = build.branch_id
            r = repos_values.setdefault(repo.id, {'branches': OrderedDict()})
            if 'name' not in r:
                r.update({
                    'name': repo.name,
                    'base': repo.base,
                    'testing': count([('repo_id', '=', repo.id), ('state', '=', 'testing')]),
                    'running': count([('repo_id', '=', repo.id), ('state', '=', 'running')]),
                    'pending': count([('repo_id', '=', repo.id), ('state', '=', 'pending')]),
                })
            b = r['branches'].setdefault(branch.id, {'name': branch.branch_name, 'builds': list()})
            b['builds'].append(self.build_info(build))

        # consider host gone if no build in last 100
        build_threshold = max(builds.ids or [0]) - 100
        for result in RB.read_group([('id', '>', build_threshold)], ['host'], ['host']):
            if result['host']:
                qctx['host_stats'].append({
                    'host': result['host'],
                    'testing': count([('state', '=', 'testing'), ('host', '=', result['host'])]),
                    'running': count([('state', '=', 'running'), ('host', '=', result['host'])]),
                })

        return request.render("runbot.sticky-dashboard", qctx)

    def build_info(self, build):
        real_build = build.duplicate_id if build.state == 'duplicate' else build
        return {
            'id': build.id,
            'name': build.name,
            'state': real_build.state,
            'result': real_build.result,
            'subject': build.subject,
            'author': build.author,
            'committer': build.committer,
            'dest': build.dest,
            'real_dest': real_build.dest,
            'job_age': s2human(real_build.job_age),
            'job_time': s2human(real_build.job_time),
            'job': real_build.job,
            'domain': real_build.domain,
            'host': real_build.host,
            'port': real_build.port,
            'subject': build.subject,
            'server_match': real_build.server_match,
            'duplicate_of': build.duplicate_id if build.state == 'duplicate' else False,
        }

    @http.route(['/runbot/build/<build_id>'], type='http', auth="public", website=True)
    def build(self, build_id=None, search=None, **post):
        registry, cr, uid, context = request.registry, request.cr, request.uid, request.context

        Build = registry['runbot.build']
        Logging = registry['ir.logging']

        build = Build.browse(cr, uid, [int(build_id)])[0]
        if not build.exists():
            return request.not_found()

        real_build = build.duplicate_id if build.state == 'duplicate' else build

        # other builds
        build_ids = Build.search(cr, uid, [('branch_id', '=', build.branch_id.id)])
        other_builds = Build.browse(cr, uid, build_ids)

        domain = ['|', ('dbname', '=like', '%s-%%' % real_build.dest), ('build_id', '=', real_build.id)]
        #if type:
        #    domain.append(('type', '=', type))
        #if level:
        #    domain.append(('level', '=', level))
        if search:
            domain.append(('name', 'ilike', search))
        logging_ids = Logging.search(cr, SUPERUSER_ID, domain)

        context = {
            'repo': build.repo_id,
            'build': self.build_info(build),
            'br': {'branch': build.branch_id},
            'logs': Logging.browse(cr, SUPERUSER_ID, logging_ids),
            'other_builds': other_builds
        }
        #context['type'] = type
        #context['level'] = level
        return request.render("runbot.build", context)

    @http.route(['/runbot/build/<build_id>/force'], type='http', auth="public", methods=['POST'], csrf=False)
    def build_force(self, build_id, **post):
        registry, cr, uid, context = request.registry, request.cr, request.uid, request.context
        repo_id = registry['runbot.build']._force(cr, uid, [int(build_id)])
        return werkzeug.utils.redirect('/runbot/repo/%s' % repo_id)

    @http.route([
        '/runbot/badge/<int:repo_id>/<branch>.svg',
        '/runbot/badge/<any(default,flat):theme>/<int:repo_id>/<branch>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'])
    def badge(self, repo_id, branch, theme='default'):

        domain = [('repo_id', '=', repo_id),
                  ('branch_id.branch_name', '=', branch),
                  ('branch_id.sticky', '=', True),
                  ('state', 'in', ['testing', 'running', 'done']),
                  ('result', '!=', 'skipped'),
                  ]

        last_update = '__last_update'
        builds = request.registry['runbot.build'].search_read(
            request.cr, SUPERUSER_ID,
            domain, ['state', 'result', 'job_age', last_update],
            order='id desc', limit=1)

        if not builds:
            return request.not_found()

        build = builds[0]
        etag = request.httprequest.headers.get('If-None-Match')
        retag = hashlib.md5(build[last_update]).hexdigest()

        if etag == retag:
            return werkzeug.wrappers.Response(status=304)

        if build['state'] == 'testing':
            state = 'testing'
            cache_factor = 1
        else:
            cache_factor = 2
            if build['result'] == 'ok':
                state = 'success'
            elif build['result'] == 'warn':
                state = 'warning'
            else:
                state = 'failed'

        # from https://github.com/badges/shields/blob/master/colorscheme.json
        color = {
            'testing': "#dfb317",
            'success': "#4c1",
            'failed': "#e05d44",
            'warning': "#fe7d37",
        }[state]

        def text_width(s):
            fp = FontProperties(family='DejaVu Sans', size=11)
            w, h, d = TextToPath().get_text_width_height_descent(s, fp, False)
            return int(w + 1)

        class Text(object):
            __slot__ = ['text', 'color', 'width']
            def __init__(self, text, color):
                self.text = text
                self.color = color
                self.width = text_width(text) + 10

        data = {
            'left': Text(branch, '#555'),
            'right': Text(state, color),
        }
        five_minutes = 5 * 60
        headers = [
            ('Content-Type', 'image/svg+xml'),
            ('Cache-Control', 'max-age=%d' % (five_minutes * cache_factor,)),
            ('ETag', retag),
        ]
        return request.render("runbot.badge_" + theme, data, headers=headers)

    @http.route(['/runbot/b/<branch_name>', '/runbot/<model("runbot.repo"):repo>/<branch_name>'], type='http', auth="public", website=True)
    def fast_launch(self, branch_name=False, repo=False, **post):
        pool, cr, uid, context = request.registry, request.cr, request.uid, request.context
        Build = pool['runbot.build']

        domain = [('branch_id.branch_name', '=', branch_name)]

        if repo:
            domain.extend([('branch_id.repo_id', '=', repo.id)])
            order="sequence desc"
        else:
            order = 'repo_id ASC, sequence DESC'

        # Take the 10 lasts builds to find at least 1 running... Else no luck
        builds = Build.search(cr, uid, domain, order=order, limit=10, context=context)

        if builds:
            last_build = False
            for build in Build.browse(cr, uid, builds, context=context):
                if build.state == 'running' or (build.state == 'duplicate' and build.duplicate_id.state == 'running'):
                    last_build = build if build.state == 'running' else build.duplicate_id
                    break

            if not last_build:
                # Find the last build regardless the state to propose a rebuild
                last_build = Build.browse(cr, uid, builds[0], context=context)

            if last_build.state != 'running':
                url = "/runbot/build/%s?ask_rebuild=1" % last_build.id
            else:
                url = build.branch_id._get_branch_quickconnect_url(last_build.domain, last_build.dest)[build.branch_id.id]
        else:
            return request.not_found()
        return werkzeug.utils.redirect(url)

# kill ` ps faux | grep ./static  | awk '{print $2}' `
# ps faux| grep Cron | grep -- '-all'  | awk '{print $2}' | xargs kill
# psql -l | grep " 000" | awk '{print $1}' | xargs -n1 dropdb

# - commit/pull more info
# - v6 support
# - host field in build
# - unlink build to remove ir_logging entires # ondelete=cascade
# - gc either build or only old ir_logging
# - if nginx server logfiles via each virtual server or map /runbot/static to root

# vim:
