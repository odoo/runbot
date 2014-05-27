# -*- encoding: utf-8 -*-

import datetime
import fcntl
import glob
import logging
import os
import re
import resource
import shutil
import signal
import simplejson
import subprocess
import time

import dateutil.parser
import requests
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
    out = []
    for i in l:
        if not isinstance(i, basestring):
            i = repr(i)
        out.append(i)
    out += ["%s=%r" % (k, v) for k, v in kw.items()]
    _logger.debug(' '.join(out))

def dashes(s):
    for i in '~":\'':
        s = s.replace(i, "")
    for i in '/_. ':
        s = s.replace(i, "-")
    return s

def mkdirs(dirs):
    for i in dirs:
        if not os.path.exists(i):
            os.makedirs(i)

def grep(filename, s):
    if os.path.isfile(filename):
        return open(filename).read().find(s) != -1
    return False

def lock(name):
    fd = os.open(name, os.O_CREAT | os.O_RDWR, 0600)
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

def locked(name):
    r = False
    try:
        fd = os.open(name, os.O_CREAT | os.O_RDWR, 0600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            r = True
        os.close(fd)
    except OSError:
        r = False
    return r

def nowait():
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

def run(l, env=None):
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

def kill(pid, sig=signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except OSError:
        pass

def now():
    return time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT)

def dt2time(dt):
    return time.mktime(time.strptime(dt, openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT))

def s2human(t):
    for m,u in [(86400,'d'),(3600,'h'),(60,'m')]:
        if t>=m:
            return str(int(t/m))+u
    return str(int(t))+"s"

#----------------------------------------------------------
# RunBot Models
#----------------------------------------------------------

class runbot_repo(osv.osv):
    _name = "runbot.repo"
    _order = 'name'

    def _get_path(self, cr, uid, ids, field_name, arg, context=None):
        wd = self.root(cr, uid)
        r = {}
        for repo in self.browse(cr, uid, ids, context=context):
            name = repo.name
            for i in '@:/':
                name = name.replace(i, '_')
            r[repo.id] = os.path.join(wd, 'repo', name)
        return r

    def _get_base(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for repo in self.browse(cr, uid, ids, context=context):
            name = re.sub('.+@', '', repo.name)
            name = name.replace(':','/')
            r[repo.id] = name
        return r

    _columns = {
        'name': fields.char('Repository', required=True),
        'path': fields.function(_get_path, type='char', string='Directory', readonly=1),
        'base': fields.function(_get_base, type='char', string='Base URL', readonly=1),
        'testing': fields.integer('Concurrent Testing'),
        'running': fields.integer('Concurrent Running'),
        'jobs': fields.char('Jobs'),
        'nginx': fields.boolean('Nginx'),
        'auto': fields.boolean('Auto'),
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
        default = os.path.join(os.path.dirname(__file__), 'static')
        root = self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.root', default)
        return root

    def git(self, cr, uid, ids, cmd, context=None):
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

    def github(self, cr, uid, ids, url, payload=None, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            mo = re.search('([^/]+)/([^/]+)/([^/]+)', repo.base)
            if mo:
                url = url.replace(':owner', mo.group(2))
                url = url.replace(':repo', mo.group(3))
                url = 'https://api.%s%s' % (mo.group(1),url)
                s = requests.Session()
                s.auth = (repo.token,'x-oauth-basic')
                s.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
                if payload:
                    r = s.post(url, data=simplejson.dumps(payload))
                else:
                    r = s.get(url)
                return r.json()

    def update(self, cr, uid, ids, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            self.update_git(cr, uid, repo)

    def update_git(self, cr, uid, repo, context=None):
        _logger.debug('repo %s updating branches', repo.name)
        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            run(['git', 'clone', '--bare', repo.name, repo.path])
        else:
            repo.git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
            repo.git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])
        out = repo.git(['for-each-ref', '--format', '["%(refname)","%(objectname)","%(authordate:iso8601)"]', '--sort=-committerdate', 'refs/heads'])
        refs = [simplejson.loads(i) for i in out.split('\n') if i]
        out = repo.git(['for-each-ref', '--format', '["%(refname)","%(objectname)","%(authordate:iso8601)"]', '--sort=-committerdate', 'refs/pull'])
        refs += [simplejson.loads(i) for i in out.split('\n') if i]
        for name, sha, date in refs:
            # create or get branch
            branch_ids = self.pool['runbot.branch'].search(cr, uid, [('repo_id', '=', repo.id), ('name', '=', name)])
            if branch_ids:
                branch_id = branch_ids[0]
            else:
                _logger.debug('repo %s found new branch %s', repo.name, name)
                branch_id = self.pool['runbot.branch'].create(cr, uid, {'repo_id': repo.id, 'name': name})
            branch = self.pool['runbot.branch'].browse(cr, uid, [branch_id], context=context)[0]
            # skip build for old branches
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) < datetime.datetime.now():
                continue
            # create build if not found
            build_ids = self.pool['runbot.build'].search(cr, uid, [('branch_id', '=', branch.id), ('name', '=', sha)])
            if not build_ids:
                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                self.pool['runbot.build'].create(cr, uid, {'branch_id': branch.id, 'name': sha})

    def scheduler(self, cr, uid, ids=None, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            bo = self.pool['runbot.build']
            dom = [('repo_id', '=', repo.id)]

            # schedule jobs
            build_ids = bo.search(cr, uid, dom + [('state', 'in', ['testing', 'running'])])
            bo.schedule(cr, uid, build_ids)

            # launch new tests
            testing = bo.search(cr, uid, dom + [('state', '=', 'testing')], count=True)
            while testing < repo.testing:
                # select the next build to process
                pending_ids = bo.search(cr, uid, dom + [('state', '=', 'pending')])
                if pending_ids:
                    pending = bo.browse(cr, uid, pending_ids[0])
                else:
                    break

                # gather information about currently running builds
                running_ids = bo.search(cr, uid, dom + [('state', '=', 'running')])
                running_len = len(running_ids)
                running_max = 0
                if running_ids:
                    running_max = bo.browse(cr, uid, running_ids[0]).sequence

                # determine if pending one should be launched
                if running_len < repo.running or pending.sequence >= running_max:
                    pending.schedule()
                else:
                    break

                # compute the number of testing job again
                testing = bo.search(cr, uid, dom + [('state', '=', 'testing')], count=True)

            # kill and reap doomed build
            build_ids = bo.search(cr, uid, dom + [('state', '=', 'running')])
            # sort builds: the last build of each sticky branch then the rest
            sticky = {}
            non_sticky = []
            for build in bo.browse(cr, uid, build_ids):
                if build.branch_id.sticky and build.branch_id.id not in sticky:
                    sticky[build.branch_id.id] = build.id
                else:
                    non_sticky.append(build.id)
            build_ids = sticky.values()
            build_ids += non_sticky
            # kill extra running builds
            bo.kill(cr, uid, build_ids[repo.running:])
            bo.reap(cr, uid, build_ids)

    def nginx(self, cr, uid, context=None):
        v = {}
        v['port'] = openerp.tools.config['xmlrpc_port']
        nginx_dir = os.path.join(self.root(cr, uid), 'nginx')
        v['nginx_dir'] = nginx_dir
        ids = self.search(cr, uid, [('nginx','=',True)], order='id')
        if ids:
            build_ids = self.pool['runbot.build'].search(cr, uid, [('repo_id','in',ids), ('state','=','running')])
            v['builds'] = self.pool['runbot.build'].browse(cr, uid, build_ids)

            nginx_config = self.pool['ir.ui.view'].render(cr, uid, "runbot.nginx_config", v)
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
        bo = self.pool['runbot.build']
        build_ids = bo.search(cr, uid, [('state', 'not in', ['done', 'pending'])])
        bo.kill(cr, uid, build_ids)
        bo.reap(cr, uid, build_ids)

    def cron(self, cr, uid, ids=None, context=None):
        ids = self.search(cr, uid, [('auto', '=', True)])
        self.update(cr, uid, ids)
        self.scheduler(cr, uid, ids)
        self.nginx(cr, uid)

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
        'repo_id': fields.many2one('runbot.repo', 'Repository', required=True, ondelete='cascade'),
        'name': fields.char('Ref Name', required=True),
        'branch_name': fields.function(_get_branch_name, type='char', string='Branch', readonly=1, store=True),
        'branch_url': fields.function(_get_branch_url, type='char', string='Branch url', readonly=1),
        'sticky': fields.boolean('Sticky'),
        'coverage': fields.boolean('Coverage'),
        'state': fields.char('Status'),
    }

class runbot_build(osv.osv):
    _name = "runbot.build"
    _order = 'sequence desc'

    def _get_dest(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            nickname = dashes(build.branch_id.name.split('/')[2])[:32]
            r[build.id] = "%05d-%s-%s" % (build.id, nickname, build.name[:6])
        return r

    def _get_time(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            r[build.id] = 0
            if build.job_end:
                r[build.id] = int(dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                r[build.id] = int(time.time() - dt2time(build.job_start))
        return r

    def _get_age(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        for build in self.browse(cr, uid, ids, context=context):
            r[build.id] = 0
            if build.job_start:
                r[build.id] = int(time.time() - dt2time(build.job_start))
        return r

    def _get_domain(self, cr, uid, ids, field_name, arg, context=None):
        r = {}
        domain = self.pool['runbot.repo'].domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            if build.repo_id.nginx:
                r[build.id] = "%s.%s" % (build.dest, domain)
            else:
                r[build.id] = "%s:%s" % (domain, build.port)
        return r

    _columns = {
        'branch_id': fields.many2one('runbot.branch', 'Branch', required=True, ondelete='cascade'),
        'repo_id': fields.related('branch_id', 'repo_id', type="many2one", relation="runbot.repo", string="Repository", readonly=True, store=True, ondelete='cascade'),
        'name': fields.char('Revno', required=True),
        'port': fields.integer('Port'),
        'dest': fields.function(_get_dest, type='char', string='Dest', readonly=1, store=True),
        'domain': fields.function(_get_domain, type='char', string='URL'),
        'date': fields.datetime('Commit date'),
        'committer': fields.char('Comitter'),
        'log': fields.text('Commit log'),
        'sequence': fields.integer('Sequence'),
        'result': fields.char('Result'), # ok, ko
        'pid': fields.integer('Pid'),
        'state': fields.char('Status'), # pending, testing, running, done
        'job': fields.char('Job'), # job_*
        'job_start': fields.datetime('Job start'),
        'job_end': fields.datetime('Job end'),
        'job_time': fields.function(_get_time, type='integer', string='Job time'),
        'job_age': fields.function(_get_age, type='integer', string='Job age'),
    }

    _defaults = {
        'state': 'pending',
    }

    def create(self, cr, uid, values, context=None):
        bid = super(runbot_build, self).create(cr, uid, values, context=context)
        self.write(cr, uid, [bid], {'sequence' : bid}, context=context)

    def reset(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, { 'state' : 'pending' }, context=context)

    def logger(self, cr, uid, ids, *l, **kw):
        l = list(l)
        for build in self.browse(cr, uid, ids, **kw):
            l[0] = "%s %s" % (build.dest , l[0])
            _logger.debug(*l)

    def list_jobs(self):
        jobs = [i for i in dir(self) if i.startswith('job')]
        jobs.sort()
        return jobs

    def find_port(self, cr, uid):
        # currently used port
        ids = self.search(cr, uid, [('state','not in',['pending','done'])])
        ports = set(i['port'] for i in self.read(cr, uid, ids, ['port']))

        # starting port
        # TODO take ir.config.parameters or 9000
        port = 2000

        # find next free port
        while port in ports:
            port += 2

        return port

    def path(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            root = self.pool['runbot.repo'].root(cr, uid)
            return os.path.join(root, 'build', build.dest, *l)

    def checkout(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # starts from scratch
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

            # runbot log path
            mkdirs([build.path("logs"), build.path('openerp/addons')])

            # checkout branch
            build.branch_id.repo_id.git_export(build.name, build.path())

            # TODO use git log to get commit message date and author

            # v6 rename bin -> openerp
            if os.path.isdir(build.path('bin/addons')):
                shutil.move(build.path('bin'), build.path('openerp'))

            # fallback for addons-only community/projet branches
            if not os.path.isfile(build.path('openerp/__init__.py')):
                l = glob.glob(build.path('*/__openerp__.py'))
                for i in l:
                    shutil.move(os.path.dirname(i), build.path('openerp/addons'))
                name = build.branch_id.branch_name.split('-',1)[0]
                if build.repo_id.fallback_id:
                    build.repo_id.fallback_id.git_export(name, build.path())

            # move all addons to server addons path
            for i in glob.glob(build.path('addons/*')):
                shutil.move(i, build.path('openerp/addons'))

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
                mods = build.repo_id.modules
            else:
                l = glob.glob(build.path('openerp/addons/*/__init__.py'))
                mods = set([os.path.basename(os.path.dirname(i)) for i in l])
                mods = mods - set(['auth_ldap', 'document_ftp', 'hw_escpos', 'hw_proxy', 'hw_scanner'])
                mods = ",".join(list(mods))

            # commandline
            cmd = [
                server_path,
                "--no-xmlrpcs",
                "--xmlrpc-port=%d" % build.port,
            ]
            # options
            if grep(build.path("openerp/tools/config.py"), "no-netrpc"):
                cmd.append("--no-netrpc")
            if grep(build.path("openerp/tools/config.py"), "log-db"):
                cmd += ["--log-db=%s" % cr.dbname] 

        # coverage
        #coverage_file_path=os.path.join(log_path,'coverage.pickle')
        #coverage_base_path=os.path.join(log_path,'coverage-base')
        #coverage_all_path=os.path.join(log_path,'coverage-all')
        #cmd = ["coverage","run","--branch"] + cmd
        #self.run_log(cmd, logfile=self.test_all_path)
        #run(["coverage","html","-d",self.coverage_base_path,"--ignore-errors","--include=*.py"],env={'COVERAGE_FILE': self.coverage_file_path})

        return cmd, mods

    def spawn(self, cmd, lock_path, log_path, cpu_limit=None, shell=False):
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
        p=subprocess.Popen(cmd, stdout=out, stderr=out, preexec_fn=preexec_fn, shell=shell)
        return p.pid

    def github_status(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # try to update github
            try:
                state = "success" if build.result == 'ok' else "failure"
                status = {
                    "state": state,
                    "target_url": "http://runbot.odoo.com/runbot/build/%s" % build.id,
                    "description": "runbot build %s (runtime %ss)" % (build.dest, build.job_time),
                    "context": "continuous-integration/runbot"
                }
                build.repo_id.github('/repos/:owner/:repo/statuses/%s' % build.name, status)
                _logger.debug("github status %s update to %s", build.name, state)
            except Exception, e:
                _logger.exception("github status error")

    def job_10_test_base(self, cr, uid, build, lock_path, log_path):
        # checkout source
        build.checkout()
        # run base test
        self.pg_createdb(cr, uid, "%s-base" % build.dest)
        cmd, mods = build.cmd()
        if grep(build.path("openerp/tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-base' % build.dest, '-i', 'base', '--stop-after-init', '--log-level=test']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=300)

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        self.pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd, mods = build.cmd()
        if grep(build.path("openerp/tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-all' % build.dest, '-i', mods, '--stop-after-init', '--log-level=test']
        # reset job_start to an accurate job_20 job_time
        build.write({'job_start': now()})
        return self.spawn(cmd, lock_path, log_path, cpu_limit=1800)

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        # adjust job_end to record an accurate job_20 job_time
        log_all = build.path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
            'result': 'ko',
        }
        if grep(log_all, "openerp.modules.loading: Modules loaded."):
            if not grep(log_all, "FAIL"):
                if not grep(build.path("openerp/test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    v['result'] = "ok"
        build.write(v)
        build.github_status()

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.path('openerp/addons/im')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['--log-level=debug']
        cmd += ['-d', "%s-all" % build.dest]

        if grep(build.path("openerp/tools/config.py"), "db-filter"):
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

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def force(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            max_id = self.search(cr, uid, [('repo_id','=',build.repo_id.id)], order='id desc', limit=1)[0]
            # Force it now
            if build.state in ['pending']:
                build.write({ 'sequence':max_id })
            # or duplicate it
            elif build.state in ['running']:
                d = {
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'sequence': max_id,
                }
                self.create(cr, 1, d)
            return build.repo_id.id

    def schedule(self, cr, uid, ids, context=None):
        jobs = self.list_jobs()
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
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    if build.job != jobs[-1] and build.job_time > 1800:
                        build.logger('%s time exceded (%ss)', build.job, build.job_time)
                        kill(build.pid)
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

    def kill(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            build.logger('killing %s', build.pid)
            kill(build.pid)
            build.write({'state':'done'})
            cr.commit()
            self.pg_dropdb(cr, uid, "%s-base" % build.dest)
            self.pg_dropdb(cr, uid, "%s-all" % build.dest)
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

    def reap(self, cr, uid, ids):
        while True:
            try:
                pid, status, rusage = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

class runbot_event(osv.osv):
    _inherit = 'ir.logging'
    _order = 'id desc'
    _columns = {
        'build_id': fields.many2one('runbot.build', 'Build'),
    }

#----------------------------------------------------------
# Runbot Controller
#----------------------------------------------------------

class RunbotController(http.Controller):

    def common(self, cr, uid):
        registry, cr, uid, context = request.registry, request.cr, request.uid, request.context
        repo_obj = registry['runbot.repo']
        v = {}
        ids = repo_obj.search(cr, uid, [], order='id')
        v['repos'] = repo_obj.browse(cr, uid, ids)
        v['s2h'] = s2human
        return v

    @http.route(['/runbot', '/runbot/repo/<model("runbot.repo"):repo>'], type='http', auth="public", website=True)
    def repo(self, repo=None, search='', limit='100', refresh='', **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context
        branch_obj = registry['runbot.branch']
        build_obj = registry['runbot.build']
        v = self.common(cr, uid)
        # repo
        if not repo and v['repos']:
            repo = v['repos'][0]
        if repo:
            # filters
            dom = [('repo_id','=',repo.id)]
            filters = {}
            for k in ['pending','testing','running','done']:
                filters[k] = post.get(k, '1')
                if filters[k] == '0':
                    dom += [('state','!=',k)]
            if search:
                dom += [('dest','ilike',search)]
            v['filters'] = filters
            qu = QueryURL('/runbot/repo/'+slug(repo), search=search, limit=limit, refresh=refresh, **filters)
            v['qu'] = qu
            build_ids = build_obj.search(cr, uid, dom + [('branch_id.sticky','=',True)])
            build_ids += build_obj.search(cr, uid, dom + [('branch_id.sticky','=',False)], limit=int(limit))

            branch_ids = []
            # builds and branches, order on join SQL is needed
            q = """
            SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id WHERE bu.id in %s
            ORDER BY br.sticky DESC, CASE WHEN br.sticky THEN br.branch_name END, bu.sequence DESC
            """
            if build_ids:
                cr.execute(q, (tuple(build_ids),))
                for br in cr.fetchall():
                    if br[0] not in branch_ids:
                        branch_ids.append(br[0])

            branches = branch_obj.browse(cr, uid, branch_ids, context=context)
            for branch in branches:
                build_ids = build_obj.search(cr, uid, [('branch_id','=',branch.id)], limit=4)
                branch.builds = build_obj.browse(cr, uid, build_ids, context=context)
            v['branches'] = branches

            # stats
            v['testing'] = build_obj.search(cr, uid, [('repo_id','=',repo.id), ('state','=','testing')], count=True)
            v['running'] = build_obj.search(cr, uid, [('repo_id','=',repo.id), ('state','=','running')], count=True)
            v['pending'] = build_obj.search(cr, uid, [('repo_id','=',repo.id), ('state','=','pending')], count=True)

        v.update({
            'search': search,
            'limit': limit,
            'refresh': refresh,
            'repo': repo,
        })
        return request.render("runbot.repo", v)

    @http.route(['/runbot/build/<build_id>'], type='http', auth="public", website=True)
    def build(self, build_id=None, search=None, **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context

        build = registry['runbot.build'].browse(cr, uid, [int(build_id)])[0]

        # other builds
        build_ids = registry['runbot.build'].search(cr, uid, [('branch_id', '=', build.branch_id.id)])
        other_builds = registry['runbot.build'].browse(cr, uid, build_ids)

        domain = [('dbname', '=', '%s-all' % build.dest)]
        #if type:
        #    domain.append(('type', '=', type))
        #if level:
        #    domain.append(('level', '=', level))
        if search:
            domain.append(('name', 'ilike', search))
        logging_ids = registry['ir.logging'].search(cr, uid, domain)
        logs = registry['ir.logging'].browse(cr, uid, logging_ids)

        v = self.common(cr, uid)
        #v['type'] = type
        #v['level'] = level
        v['build'] = build
        v['other_builds'] = other_builds
        v['logs'] = logs
        return request.render("runbot.build", v)

    @http.route(['/runbot/build/<build_id>/force'], type='http', auth="public", website=True)
    def build_force(self, build_id, **post):
        registry, cr, uid, context = request.registry, request.cr, 1, request.context
        repo_id = registry['runbot.build'].force(cr, 1, [int(build_id)])
        return werkzeug.utils.redirect('/runbot/repo/%s' % repo_id)

# kill ` ps faux | grep ./static  | awk '{print $2}' `
# ps faux| grep Cron | grep -- '-all'  | awk '{print $2}' | xargs kill
# psql -l | grep " 000" | awk '{print $1}' | xargs -n1 dropdb
# TODO

# - cannibal branch
# - commit/pull more info
# - v6 support

# - host field in build
# - unlink build to remove ir_logging entires # ondelete=cascade
# - gc either build or only old ir_logging
# - if nginx server logfiles via each virtual server or map /runbot/static to root

# vim:
