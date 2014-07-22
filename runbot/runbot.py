# -*- encoding: utf-8 -*-

import datetime
import fcntl
import glob
import hashlib
import logging
import os
import re
import resource
import shutil
import signal
import simplejson
import subprocess
import time
import sys
from collections import OrderedDict
import itertools

import dateutil.parser
import requests
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath
import werkzeug

import openerp
from openerp import http
from openerp.http import request
from openerp.osv import fields, osv
from openerp.addons.website.models.website import slug
from openerp.addons.website_sale.controllers.main import QueryURL

_logger = logging.getLogger(__name__)

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

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
_re_job = re.compile('job_\d')

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
    return itertools.chain.from_iterable(list_of_lists)

def decode_utf(field):
    try:
        return field.decode('utf-8')
    except UnicodeDecodeError:
        return ''

def uniq_list(l):
    return OrderedDict.fromkeys(l).keys()

#----------------------------------------------------------
# RunBot Models
#----------------------------------------------------------

class runbot_repo(osv.osv):
    _name = "runbot.repo"
    _order = 'name'

    def _get_path(self, cr, uid, ids, field_name, arg, context=None):
        root = self.root(cr, uid)
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
            name = name.replace(':','/')
            result[repo.id] = name
        return result

    _columns = {
        'name': fields.char('Repository', required=True),
        'path': fields.function(_get_path, type='char', string='Directory', readonly=1),
        'base': fields.function(_get_base, type='char', string='Base URL', readonly=1),
        'testing': fields.integer('Concurrent Testing'),
        'running': fields.integer('Concurrent Running'),
        'jobs': fields.char('Jobs'),
        'nginx': fields.boolean('Nginx'),
        'auto': fields.boolean('Auto'),
        'duplicate_id': fields.many2one('runbot.repo', 'Repository for finding duplicate builds'),
        'fallback_id': fields.many2one('runbot.repo', 'Fallback repo'),
        'modules': fields.char("Modules to Install"),
        'token': fields.char("Github token"),
    }
    _defaults = {
        'testing': 1,
        'running': 1,
        'auto': True,
    }

    def domain(self, cr, uid, context=None):
        domain = self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.domain', 'runbot.odoo.com')
        return domain

    def root(self, cr, uid, context=None):
        """Return root directory of repository"""
        default = os.path.join(os.path.dirname(__file__), 'static')
        return self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.root', default)

    def git(self, cr, uid, ids, cmd, context=None):
        """Execute git command cmd"""
        for repo in self.browse(cr, uid, ids, context=context):
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.info("git: %s", ' '.join(cmd))
            return subprocess.check_output(cmd)

    def git_export(self, cr, uid, ids, treeish, dest, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            _logger.debug('checkout %s %s %s', repo.name, treeish, dest)
            p1 = subprocess.Popen(['git', '--git-dir=%s' % repo.path, 'archive', treeish], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(['tar', '-xC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
            p2.communicate()[0]

    def github(self, cr, uid, ids, url, payload=None, delete=False, context=None):
        """Return a http request to be sent to github"""
        for repo in self.browse(cr, uid, ids, context=context):
            if not repo.token:
                raise Exception('Repository does not have a token to authenticate')
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
                elif delete:
                    response = session.delete(url)
                else:
                    response = session.get(url)
                return response.json()

    def update(self, cr, uid, ids, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            self.update_git(cr, uid, repo)

    def update_git(self, cr, uid, repo, context=None):
        _logger.debug('repo %s updating branches', repo.name)

        Build = self.pool['runbot.build']
        Branch = self.pool['runbot.branch']

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            run(['git', 'clone', '--bare', repo.name, repo.path])
        else:
            repo.git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
            repo.git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])

        fields = ['refname','objectname','committerdate:iso8601','authorname','subject']
        fmt = "%00".join(["%("+field+")" for field in fields])
        git_refs = repo.git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[decode_utf(field) for field in line.split('\x00')] for line in git_refs.split('\n')]

        for name, sha, date, author, subject in refs:
            # create or get branch
            branch_ids = Branch.search(cr, uid, [('repo_id', '=', repo.id), ('name', '=', name)])
            if branch_ids:
                branch_id = branch_ids[0]
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
                if not branch.sticky:
                    to_be_skipped_ids = Build.search(cr, uid, [('branch_id', '=', branch.id), ('state', '=', 'pending')])
                    Build.skip(cr, uid, to_be_skipped_ids)

                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'subject': subject,
                }
                Build.create(cr, uid, build_info)

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', repo.id), ('state', '=', 'pending')]
        icp = self.pool['ir.config_parameter']
        running_max = int(icp.get_param(cr, uid, 'runbot.running_max', default=75))
        to_be_skipped_ids = Build.search(cr, uid, skippable_domain, order='sequence desc', offset=running_max)
        Build.skip(cr, uid, to_be_skipped_ids)

    def scheduler(self, cr, uid, ids=None, context=None):
        icp = self.pool['ir.config_parameter']
        workers = int(icp.get_param(cr, uid, 'runbot.workers', default=6))
        running_max = int(icp.get_param(cr, uid, 'runbot.running_max', default=75))

        Build = self.pool['runbot.build']
        domain = [('repo_id', 'in', ids)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        build_ids = Build.search(cr, uid, domain + [('state', 'in', ['testing', 'running'])])
        Build.schedule(cr, uid, build_ids)

        # launch new tests
        testing = Build.search_count(cr, uid, domain + [('state', '=', 'testing')])
        pending = Build.search_count(cr, uid, domain + [('state', '=', 'pending')])

        while testing < workers and pending > 0:

            # find sticky pending build if any, otherwise, last pending (by id, not by sequence) will do the job
            pending_ids = Build.search(cr, uid, domain + [('state', '=', 'pending'), ('branch_id.sticky', '=', True)], limit=1)
            if not pending_ids:
                pending_ids = Build.search(cr, uid, domain + [('state', '=', 'pending')], order="sequence", limit=1)

            pending_build = Build.browse(cr, uid, pending_ids[0])
            pending_build.schedule()

            # compute the number of testing and pending jobs again
            testing = Build.search_count(cr, uid, domain + [('state', '=', 'testing')])
            pending = Build.search_count(cr, uid, domain + [('state', '=', 'pending')])

        # terminate and reap doomed build
        build_ids = Build.search(cr, uid, domain + [('state', '=', 'running')])
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
        Build.terminate(cr, uid, build_ids[running_max:])
        Build.reap(cr, uid, build_ids)

    def reload_nginx(self, cr, uid, context=None):
        settings = {}
        settings['port'] = openerp.tools.config['xmlrpc_port']
        nginx_dir = os.path.join(self.root(cr, uid), 'nginx')
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
                run(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf'])

    def killall(self, cr, uid, ids=None, context=None):
        # kill switch
        Build = self.pool['runbot.build']
        build_ids = Build.search(cr, uid, [('state', 'not in', ['done', 'pending'])])
        Build.terminate(cr, uid, build_ids)
        Build.reap(cr, uid, build_ids)

    def cron(self, cr, uid, ids=None, context=None):
        ids = self.search(cr, uid, [('auto', '=', True)])
        self.update(cr, uid, ids)
        self.scheduler(cr, uid, ids)
        self.reload_nginx(cr, uid, context=context)

class runbot_branch(osv.osv):
    _name = "runbot.branch"
    _order = 'name'

    def _get_branch_name(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            r[branch.id] = branch.name.split('/')[-1]
        return r

    def _get_branch_url(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            if re.match('^[0-9]+$', branch.branch_name):
                r[branch.id] = "https://%s/pull/%s" % (branch.repo_id.base, branch.branch_name)
            else:
                r[branch.id] = "https://%s/tree/%s" % (branch.repo_id.base, branch.branch_name)
        return r

    _columns = {
        'repo_id': fields.many2one('runbot.repo', 'Repository', required=True, ondelete='cascade', select=1),
        'name': fields.char('Ref Name', required=True),
        'branch_name': fields.function(_get_branch_name, type='char', string='Branch', readonly=1, store=True),
        'branch_url': fields.function(_get_branch_url, type='char', string='Branch url', readonly=1),
        'sticky': fields.boolean('Sticky', select=1),
        'coverage': fields.boolean('Coverage'),
        'state': fields.char('Status'),
    }

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
        domain = self.pool['runbot.repo'].domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            if build.repo_id.nginx:
                result[build.id] = "%s.%s" % (build.dest, domain)
            else:
                result[build.id] = "%s:%s" % (domain, build.port)
        return result

    _columns = {
        'branch_id': fields.many2one('runbot.branch', 'Branch', required=True, ondelete='cascade', select=1),
        'repo_id': fields.related('branch_id', 'repo_id', type="many2one", relation="runbot.repo", string="Repository", readonly=True, store=True, ondelete='cascade', select=1),
        'name': fields.char('Revno', required=True, select=1),
        'port': fields.integer('Port'),
        'dest': fields.function(_get_dest, type='char', string='Dest', readonly=1, store=True),
        'domain': fields.function(_get_domain, type='char', string='URL'),
        'date': fields.datetime('Commit date'),
        'author': fields.char('Author'),
        'subject': fields.text('Subject'),
        'sequence': fields.integer('Sequence', select=1),
        'result': fields.char('Result'), # ok, ko, warn, skipped, killed
        'pid': fields.integer('Pid'),
        'state': fields.char('Status'), # pending, testing, running, done, duplicate
        'job': fields.char('Job'), # job_*
        'job_start': fields.datetime('Job start'),
        'job_end': fields.datetime('Job end'),
        'job_time': fields.function(_get_time, type='integer', string='Job time'),
        'job_age': fields.function(_get_age, type='integer', string='Job age'),
        'duplicate_id': fields.many2one('runbot.build', 'Corresponding Build'),
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

    def reset(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, { 'state' : 'pending' }, context=context)

    def logger(self, cr, uid, ids, *l, **kw):
        l = list(l)
        for build in self.browse(cr, uid, ids, **kw):
            l[0] = "%s %s" % (build.dest , l[0])
            _logger.debug(*l)

    def list_jobs(self):
        return sorted(job for job in dir(self) if _re_job.match(job))

    def find_port(self, cr, uid):
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

    def path(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            root = self.pool['runbot.repo'].root(cr, uid)
            return os.path.join(root, 'build', build.dest, *l)

    def server(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            if os.path.exists(build.path('odoo')):
                return build.path('odoo', *l)
            return build.path('openerp', *l)

    def checkout(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # starts from scratch
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

            # runbot log path
            mkdirs([build.path("logs"), build.server('addons')])

            # checkout branch
            build.branch_id.repo_id.git_export(build.name, build.path())

            # TODO use git log to get commit message date and author

            # v6 rename bin -> openerp
            if os.path.isdir(build.path('bin/addons')):
                shutil.move(build.path('bin'), build.server())

            # fallback for addons-only community/projet branches
            if not os.path.isfile(build.server('__init__.py')):
                l = glob.glob(build.path('*/__openerp__.py'))
                for i in l:
                    shutil.move(os.path.dirname(i), build.server('addons'))
                name = build.branch_id.branch_name.split('-',1)[0]
                if build.repo_id.fallback_id:
                    build.repo_id.fallback_id.git_export(name, build.path())

            # move all addons to server addons path
            for i in glob.glob(build.path('addons/*')):
                shutil.move(i, build.server('addons'))

    def pg_dropdb(self, cr, uid, dbname):
        pid_col = 'pid' if cr._cnx.server_version >= 90200 else 'procpid'
        cr.execute("select pg_terminate_backend(%s) from pg_stat_activity where datname = %%s" % pid_col, (dbname,))
        time.sleep(1)
        try:
            openerp.service.db.exp_drop(dbname)
        except Exception:
            pass

    def pg_createdb(self, cr, uid, dbname):
        self.pg_dropdb(cr, uid, dbname)
        _logger.debug("createdb %s",dbname)
        openerp.service.db._create_empty_database(dbname)

    def cmd(self, cr, uid, ids, context=None):
        """Return a list describing the command to start the build"""
        for build in self.browse(cr, uid, ids, context=context):
            # Server
            server_path = build.path("openerp-server")
            # for 7.0
            if not os.path.isfile(server_path):
                server_path = build.path("openerp-server.py")
            # for 6.0 branches
            if not os.path.isfile(server_path):
                server_path = build.path("bin/openerp-server.py")

            # modules
            if build.repo_id.modules:
                modules = build.repo_id.modules
            else:
                l = glob.glob(build.server('addons', '*', '__init__.py'))
                modules = set(os.path.basename(os.path.dirname(i)) for i in l)
                modules = modules - set(['auth_ldap', 'document_ftp', 'hw_escpos', 'hw_proxy', 'hw_scanner', 'base_gengo', 'website_gengo'])
                modules = ",".join(list(modules))

            # commandline
            cmd = [
                sys.executable,
                server_path,
                "--no-xmlrpcs",
                "--xmlrpc-port=%d" % build.port,
            ]
            # options
            if grep(build.server("tools/config.py"), "no-netrpc"):
                cmd.append("--no-netrpc")
            if grep(build.server("tools/config.py"), "log-db"):
                cmd += ["--log-db=%s" % cr.dbname] 

        # coverage
        #coverage_file_path=os.path.join(log_path,'coverage.pickle')
        #coverage_base_path=os.path.join(log_path,'coverage-base')
        #coverage_all_path=os.path.join(log_path,'coverage-all')
        #cmd = ["coverage","run","--branch"] + cmd
        #self.run_log(cmd, logfile=self.test_all_path)
        #run(["coverage","html","-d",self.coverage_base_path,"--ignore-errors","--include=*.py"],env={'COVERAGE_FILE': self.coverage_file_path})

        return cmd, modules

    def spawn(self, cmd, lock_path, log_path, cpu_limit=None, shell=False, showstderr=False):
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
        out=open(log_path,"w")
        _logger.debug("spawn: %s stdout: %s", ' '.join(cmd), log_path)
        if showstderr:
            stderr = out
        else:
            stderr = open(os.devnull, 'w')
        p=subprocess.Popen(cmd, stdout=out, stderr=stderr, preexec_fn=preexec_fn, shell=shell)
        return p.pid

    def github_status(self, cr, uid, ids, context=None):
        """Notify github of failed/successful builds"""
        runbot_domain = self.pool['runbot.repo'].domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            if build.state != 'duplicate' and build.duplicate_id:
                self.github_status(cr, uid, [build.duplicate_id.id], context=context)
            desc = "runbot build %s" % (build.dest,)
            real_build = build.duplicate_id if build.state == 'duplicate' else build
            if real_build.state == 'testing':
                state = 'pending'
            elif real_build.state in ('running', 'done'):
                state = {
                    'ok': 'success',
                    'killed': 'error',
                }.get(real_build.result, 'failure')
                desc += " (runtime %ss)" % (real_build.job_time,)
            else:
                continue

            status = {
                "state": state,
                "target_url": "http://%s/runbot/build/%s" % (runbot_domain, build.id),
                "description": desc,
                "context": "continuous-integration/runbot"
            }
            try:
                build.repo_id.github('/repos/:owner/:repo/statuses/%s' % build.name, status)
                _logger.debug("github status %s update to %s", build.name, state)
            except Exception:
                _logger.exception("github status error")

    def job_10_test_base(self, cr, uid, build, lock_path, log_path):
        build._log('test_base', 'Start test base module')
        build.github_status()
        # checkout source
        build.checkout()
        # run base test
        self.pg_createdb(cr, uid, "%s-base" % build.dest)
        cmd, mods = build.cmd()
        if grep(build.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-base' % build.dest, '-i', 'base', '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=300)

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        build._log('test_all', 'Start test all modules')
        self.pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd, mods = build.cmd()
        if grep(build.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-all' % build.dest, '-i', mods, '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        # reset job_start to an accurate job_20 job_time
        build.write({'job_start': now()})
        return self.spawn(cmd, lock_path, log_path, cpu_limit=2100)

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        log_all = build.path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        logger = 'odoo.modules.loading' if os.path.exists(build.path('odoo')) else 'openerp.modules.loading'
        if grep(log_all, "%s: Modules loaded." % logger):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"
        build.write(v)
        build.github_status()

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['--log-level=debug']
        cmd += ['-d', "%s-all" % build.dest]

        if grep(build.server("tools/config.py"), "db-filter"):
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

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, showstderr=True)

    def force(self, cr, uid, ids, context=None):
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
                build.write({'state': 'pending', 'sequence':sequence, 'result': '' })
            # or duplicate it
            elif build.state in ['running', 'done', 'duplicate']:
                new_build = {
                    'sequence': sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'author': build.author,
                    'subject': build.subject,
                }
                self.create(cr, 1, new_build, context=context)
            return build.repo_id.id

    def schedule(self, cr, uid, ids, context=None):
        jobs = self.list_jobs()
        icp = self.pool['ir.config_parameter']
        timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800))

        for build in self.browse(cr, uid, ids, context=context):
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self.find_port(cr, uid)
                values = {
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
                cr.commit()
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build.logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.kill()
                    continue
                build.logger('%s finished', build.job)
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
            if build.state != 'done':
                build.logger('running %s', build.job)
                job_method = getattr(self,build.job)
                lock_path = build.path('logs', '%s.lock' % build.job)
                log_path = build.path('logs', '%s.txt' % build.job)
                pid = job_method(cr, uid, build, lock_path, log_path)
                build.write({'pid': pid})
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

    def skip(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, {'state': 'done', 'result': 'skipped'}, context=context)
        to_unduplicate = self.search(cr, uid, [('id', 'in', ids), ('duplicate_id', '!=', False)])
        if len(to_unduplicate):
            self.force(cr, uid, to_unduplicate, context=context)

    def terminate(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            build.logger('killing %s', build.pid)
            try:
                os.killpg(build.pid, signal.SIGKILL)
            except OSError:
                pass
            build.write({'state': 'done'})
            cr.commit()
            self.pg_dropdb(cr, uid, "%s-base" % build.dest)
            self.pg_dropdb(cr, uid, "%s-all" % build.dest)
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

    def kill(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            build._log('kill', 'Kill build %s' % build.dest)
            build.terminate()
            build.write({'result': 'killed', 'job': False})
            build.github_status()

    def reap(self, cr, uid, ids):
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
        registry, cr, uid = request.registry, request.cr, 1

        branch_obj = registry['runbot.branch']
        build_obj = registry['runbot.build']
        icp = registry['ir.config_parameter']
        repo_obj = registry['runbot.repo']

        repo_ids = repo_obj.search(cr, uid, [], order='id')
        repos = repo_obj.browse(cr, uid, repo_ids)
        if not repo and repos:
            repo = repos[0] 

        context = {
            'repos': repos,
            'repo': repo,
            'workers': icp.get_param(cr, uid, 'runbot.workers', default=6),
            'running_max': icp.get_param(cr, uid, 'runbot.running_max', default=75),
            'pending_total': build_obj.search_count(cr, uid, [('state','=','pending')]),
            'testing_total': build_obj.search_count(cr, uid, [('state','=','testing')]),
            'running_total': build_obj.search_count(cr, uid, [('state','=','running')]),
            'limit': limit,
            'search': search,
            'refresh': refresh,
        }

        if repo:
            filters = {key: post.get(key, '1') for key in ['pending', 'testing', 'running', 'done']}
            domain = [('repo_id','=',repo.id)]
            domain += [('state', '!=', key) for key, value in filters.iteritems() if value == '0']
            if search:
                domain += ['|', ('dest', 'ilike', search), ('subject', 'ilike', search)]

            build_ids = build_obj.search(cr, uid, domain, limit=int(limit))
            branch_ids = []

            if build_ids:
                q = """
                SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id WHERE bu.id in %s
                ORDER BY bu.sequence DESC
                """
                sticky_dom = [('repo_id','=',repo.id), ('sticky', '=', True)]
                sticky_branch_ids = [] if search else branch_obj.search(cr, uid, sticky_dom)
                cr.execute(q, (tuple(build_ids),))
                branch_ids = uniq_list(sticky_branch_ids + [br[0] for br in cr.fetchall()])

            branches = branch_obj.browse(cr, uid, branch_ids, context=request.context)
            build_by_branch_ids = {b: build_obj.search(cr, uid, domain + [('branch_id','=',b)], limit=4) for b in branch_ids}
            build_ids = flatten(build_by_branch_ids.values())
            build_dict = {build.id: build for build in build_obj.browse(cr, uid, build_ids, context=request.context) }

            def branch_info(branch):
                return {
                    'branch': branch,
                    'builds': [self.build_info(build_dict[build_id]) for build_id in build_by_branch_ids[branch.id]]
                }

            context.update({
                'branches': [branch_info(b) for b in branches],
                'testing': build_obj.search_count(cr, uid, [('repo_id','=',repo.id), ('state','=','testing')]),
                'running': build_obj.search_count(cr, uid, [('repo_id','=',repo.id), ('state','=','running')]),
                'pending': build_obj.search_count(cr, uid, [('repo_id','=',repo.id), ('state','=','pending')]),
                'qu': QueryURL('/runbot/repo/'+slug(repo), search=search, limit=limit, refresh=refresh, **filters),
                'filters': filters,
            })

        return request.render("runbot.repo", context)

    def build_info(self, build):
        real_build = build.duplicate_id if build.state == 'duplicate' else build
        return {
            'id': build.id,
            'name': build.name,
            'state': real_build.state,
            'result': real_build.result,
            'subject': build.subject,
            'author': build.author,
            'dest': build.dest,
            'real_dest': real_build.dest,
            'job_age': s2human(real_build.job_age),
            'job_time': s2human(real_build.job_time),
            'job': real_build.job,
            'domain': real_build.domain,
            'port': real_build.port,
            'subject': build.subject,
        }


    @http.route(['/runbot/build/<build_id>'], type='http', auth="public", website=True)
    def build(self, build_id=None, search=None, **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context

        Build = registry['runbot.build']
        Logging = registry['ir.logging']

        build = Build.browse(cr, uid, [int(build_id)])[0]
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
        logging_ids = Logging.search(cr, uid, domain)

        context = {
            'repo': build.repo_id,
            'build': self.build_info(build),
            'br': {'branch': build.branch_id},
            'logs': Logging.browse(cr, uid, logging_ids),
            'other_builds': other_builds
        }
        #context['type'] = type
        #context['level'] = level
        return request.render("runbot.build", context)

    @http.route(['/runbot/build/<build_id>/force'], type='http', auth="public", website=True)
    def build_force(self, build_id, **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context
        repo_id = registry['runbot.build'].force(cr, uid, [int(build_id)])
        return werkzeug.utils.redirect('/runbot/repo/%s' % repo_id)

    @http.route(['/runbot/build/<build_id>/label/<label_id>'], type='http', auth="public", method='POST')
    def toggle_label(self, build_id=None, label_id=None, search=None, **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context

        build = registry['runbot.build'].browse(cr, uid, [int(build_id)])[0]
        issue_number = build.branch_id.name.split('/')
        if len(issue_number) == 3 and issue_number[0] == 'refs' and issue_number[1] == 'pull':
            issue_number = int(issue_number[2])
        else:
            # not a pull request
            return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id)

        label_id = int(label_id)
        if label_id not in LABELS:
            _logger.exception("unknown label")
        else:
            label_name = LABELS[label_id]
            found = False
            try:
                res = build.repo_id.github('/repos/:owner/:repo/issues/%s/labels' % issue_number)
                found = any([label for label in res if label['name'] == label_name])
            except Exception, e:
                _logger.exception("github error while fetching labels")

            if found:
                # removing existing label
                try:
                    build.repo_id.github('/repos/:owner/:repo/issues/%s/labels/%s' % (issue_number, label_name), delete=True)
                    _logger.debug("removed github label %s for %s: %s", (label_name, issue_number))
                except Exception, e:
                    _logger.exception("github error while removing label %s" % label_name)
            else:
                # adding the label
                try:
                    build.repo_id.github('/repos/:owner/:repo/issues/%s/labels' % issue_number, [label_name])
                    _logger.debug("added github label %s for %s: %s", (label_name, issue_number))
                except Exception, e:
                    _logger.exception("github error while adding label %s" % label_name)
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id)

    @http.route([
        '/runbot/badge/<model("runbot.repo"):repo>/<branch>.svg',
        '/runbot/badge/<any(default,flat):theme>/<model("runbot.repo"):repo>/<branch>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'])
    def badge(self, repo, branch, theme='default'):

        domain = [('repo_id', '=', repo.id),
                  ('branch_id.branch_name', '=', branch),
                  ('branch_id.sticky', '=', True),
                  ('state', 'in', ['testing', 'running', 'done']),
                  ('result', '!=', 'skipped'),
                  ]

        last_update = '__last_update'
        builds = request.registry['runbot.build'].search_read(
            request.cr, request.uid,
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


LABELS = {
    1: 'RDWIP',
    2: 'OE',
}

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
