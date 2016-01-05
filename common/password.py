#    Copyright (C) 2012-2016 Germar Reitze
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import sys
import os
import time
import atexit
import signal
import subprocess
import gettext
import re
import errno

import config
import configfile
import tools
import password_ipc
import logger
from applicationinstance import ApplicationInstance
from exceptions import Timeout

_=gettext.gettext

class Daemon:
    """
    A generic daemon class.

    Usage: subclass the Daemon class and override the run() method

    Daemon Copyright by Sander Marechal
    License CC BY-SA 3.0
    http://www.jejik.com/articles/2007/02/a_simple_unix_linux_daemon_in_python/
    """
    def __init__(self, pidfile, stdin='/dev/null', stdout='/dev/stdout', stderr='/dev/null'):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pidfile = pidfile
        self.appInstance = ApplicationInstance(pidfile, auto_exit = False, flock = False)

    def daemonize(self):
        """
        do the UNIX double-fork magic, see Stevens' "Advanced
        Programming in the UNIX Environment" for details (ISBN 0201563177)
        http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16
        """
        logger.debug('start', self)
        try:
            pid = os.fork()
            if pid > 0:
                # exit first parent
                sys.exit(0)
        except OSError as e:
            logger.error("fork #1 failed: %d (%s)" % (e.errno, str(e)), self)
            sys.exit(1)

        # decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as e:
            logger.error("fork #2 failed: %d (%s)" % (e.errno, str(e)), self)
            sys.exit(1)

        # redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        si = open(self.stdin, 'r')
        so = open(self.stdout, 'w')
        se = open(self.stderr, 'w')
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        # write pidfile
        logger.debug('write pidfile', self)
        atexit.register(self.appInstance.exit_application)
        signal.signal(signal.SIGTERM, self._cleanup_handler)
        self.appInstance.start_application()

    def _cleanup_handler(self, signum, frame):
        self.fifo.delfifo()
        self.appInstance.exit_application()
        sys.exit(0)

    def start(self):
        """
        Start the daemon
        """
        # Check for a pidfile to see if the daemon already runs
        if not self.appInstance.check():
            message = "pidfile %s already exist. Daemon already running?\n"
            logger.error(message % self.pidfile, self)
            sys.exit(1)

        # Start the daemon
        self.daemonize()
        self.run()

    def stop(self):
        """
        Stop the daemon
        """
        # Get the pid from the pidfile
        pid, procname = self.appInstance.readPidFile()

        if not pid:
            message = "pidfile %s does not exist. Daemon not running?\n"
            logger.error(message % self.pidfile, self)
            return # not an error in a restart

        # Try killing the daemon process
        try:
            while True:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
        except OSError as err:
            if err.errno == errno.ESRCH:
                #no such process
                self.appInstance.exit_application()
            else:
                logger.error(str(err), self)
                sys.exit(1)

    def restart(self):
        """
        Restart the daemon
        """
        self.stop()
        self.start()

    def reload(self):
        """
        send SIGHUP signal to process
        """
        # Get the pid from the pidfile
        pid, procname = self.appInstance.readPidFile()

        if not pid:
            message = "pidfile %s does not exist. Daemon not running?\n"
            logger.error(message % self.pidfile, self)
            return

        # Try killing the daemon process
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError as err:
            if err.errno == errno.ESRCH:
                #no such process
                self.appInstance.exit_application()
            else:
                sys.stderr.write(str(err))
                sys.exit(1)

    def status(self):
        """
        return status
        """
        return not self.appInstance.check()

    def run(self):
        """
        You should override this method when you subclass Daemon. It will be called after the process has been
        daemonized by start() or restart().
        """
        pass

class Password_Cache(Daemon):
    """
    Password_Cache get started on User login. It provides passwords for
    BIT cronjobs because keyring is not available when the User is not
    logged in. Does not start if there is no password to cache
    (e.g. no profile allows to cache).
    """
    PW_CACHE_VERSION = 3

    def __init__(self, cfg = None, *args, **kwargs):
        self.config = cfg
        if self.config is None:
            self.config = config.Config()
        pw_cache_path = self.config.get_password_cache_folder()
        if not os.path.isdir(pw_cache_path):
            os.mkdir(pw_cache_path, 0o700)
        else:
            os.chmod(pw_cache_path, 0o700)
        Daemon.__init__(self, self.config.get_password_cache_pid(), *args, **kwargs)
        self.db_keyring = {}
        self.db_usr = {}
        self.fifo = password_ipc.FIFO(self.config.get_password_cache_fifo())

        self.keyring_supported = tools.keyring_supported()

    def run(self):
        """
        wait for password request on FIFO and answer with password
        from self.db through FIFO.
        """
        info = configfile.ConfigFile()
        info.set_int_value('version', self.PW_CACHE_VERSION)
        info.save(self.config.get_password_cache_info())
        os.chmod(self.config.get_password_cache_info(), 0o600)

        logger.debug('Keyring supported: %s' %self.keyring_supported, self)

        tools.save_env(self.config.get_cron_env_file())

        if not self._collect_passwords():
            logger.debug('Nothing to cache. Quit.', self)
            sys.exit(0)
        self.fifo.create()
        atexit.register(self.fifo.delfifo)
        signal.signal(signal.SIGHUP, self._reload_handler)
        logger.debug('Start loop', self)
        while True:
            try:
                request = self.fifo.read()
                request = request.split('\n')[0]
                task, value = request.split(':', 1)
                if task == 'get_pw':
                    key = value
                    if key in list(self.db_keyring.keys()):
                        answer = 'pw:' + self.db_keyring[key]
                    elif key in list(self.db_usr.keys()):
                        answer = 'pw:' + self.db_usr[key]
                    else:
                        answer = 'none:'
                    self.fifo.write(answer, 5)
                elif task == 'set_pw':
                    key, value = value.split(':', 1)
                    self.db_usr[key] = value

            except IOError as e:
                logger.error('Error in writing answer to FIFO: %s' % str(e), self)
            except KeyboardInterrupt:
                logger.debug('Quit.', self)
                break
            except Timeout:
                logger.error('FIFO timeout', self)
            except Exception as e:
                logger.error('ERROR: %s' % str(e), self)

    def _reload_handler(self, signum, frame):
        """
        reload passwords during runtime.
        """
        time.sleep(2)
        cfgPath = self.config._LOCAL_CONFIG_PATH
        del(self.config)
        self.config = config.Config(cfgPath)
        del(self.db_keyring)
        self.db_keyring = {}
        self._collect_passwords()

    def _collect_passwords(self):
        """
        search all profiles in config and collect passwords from keyring.
        """
        run_daemon = False
        profiles = self.config.get_profiles()
        for profile_id in profiles:
            mode = self.config.get_snapshots_mode(profile_id)
            for pw_id in (1, 2):
                if self.config.mode_need_password(mode, pw_id):
                    if self.config.get_password_use_cache(profile_id):
                        run_daemon = True
                        if self.config.get_password_save(profile_id) and self.keyring_supported:
                            service_name = self.config.get_keyring_service_name(profile_id, mode, pw_id)
                            user_name = self.config.get_keyring_user_name(profile_id)

                            password = tools.get_password(service_name, user_name)
                            if password is None:
                                continue
                            self.db_keyring['%s/%s' %(service_name, user_name)] = password
        return run_daemon

    def check_version(self):
        info = configfile.ConfigFile()
        info.load(self.config.get_password_cache_info())
        if info.get_int_value('version') < self.PW_CACHE_VERSION:
            return False
        return True

class Password(object):
    """
    provide passwords for BIT either from keyring, Password_Cache or
    by asking User.
    """
    def __init__(self, cfg = None):
        self.config = cfg
        if self.config is None:
            self.config = config.Config()
        self.pw_cache = Password_Cache(self.config)
        self.fifo = password_ipc.FIFO(self.config.get_password_cache_fifo())
        self.db = {}

        self.keyring_supported = tools.keyring_supported()

    def get_password(self, parent, profile_id, mode, pw_id = 1, only_from_keyring = False):
        """
        based on profile settings return password from keyring,
        Password_Cache or by asking User.
        """
        if not self.config.mode_need_password(mode, pw_id):
            return ''
        service_name = self.config.get_keyring_service_name(profile_id, mode, pw_id)
        user_name = self.config.get_keyring_user_name(profile_id)
        try:
            return self.db['%s/%s' %(service_name, user_name)]
        except KeyError:
            pass
        password = ''
        if self.config.get_password_use_cache(profile_id) and not only_from_keyring:
            #from pw_cache
            password = self._get_password_from_pw_cache(service_name, user_name)
            if not password is None:
                self._set_password_db(service_name, user_name, password)
                return password
        if self.config.get_password_save(profile_id):
            #from keyring
            password = self._get_password_from_keyring(service_name, user_name)
            if not password is None:
                self._set_password_db(service_name, user_name, password)
                return password
        if not only_from_keyring:
            #ask user and write to cache
            password = self._get_password_from_user(parent, profile_id, mode, pw_id)
            if self.config.get_password_use_cache(profile_id):
                self._set_password_to_cache(service_name, user_name, password)
            self._set_password_db(service_name, user_name, password)
            return password
        return password

    def _get_password_from_keyring(self, service_name, user_name):
        """
        get password from system keyring (seahorse). The keyring is only
        available if User is logged in.
        """
        if self.keyring_supported:
            try:
                return tools.get_password(service_name, user_name)
            except Exception:
                logger.error('get password from Keyring failed', self)
        return None

    def _get_password_from_pw_cache(self, service_name, user_name):
        """
        get password from Password_Cache
        """
        if self.pw_cache.status():
            self.pw_cache.check_version()
            self.fifo.write('get_pw:%s/%s' %(service_name, user_name), timeout = 5)
            answer = self.fifo.read(timeout = 5)
            mode, pw = answer.split(':', 1)
            if mode == 'none':
                return None
            return pw
        else:
            return None

    def _get_password_from_user(self, parent, profile_id = None, mode = None, pw_id = 1, prompt = None):
        """
        ask user for password. This does even work when run as cronjob
        and user is logged in.
        """
        if prompt is None:
            prompt = _('Profile \'%(profile)s\': Enter password for %(mode)s: ') % {'profile': self.config.get_profile_name(profile_id), 'mode': self.config.SNAPSHOT_MODES[mode][pw_id + 1]}

        tools.register_backintime_path('qt4')

        x_server = tools.check_x_server()
        import_successful = False
        if x_server:
            try:
                import messagebox
                import_successful = True
            except ImportError:
                pass

        if not import_successful or not x_server:
            import getpass
            alarm = tools.Alarm()
            alarm.start(300)
            try:
                password = getpass.getpass(prompt)
                alarm.stop()
            except Timeout:
                password = ''
            return password

        password = messagebox.ask_password_dialog(parent, self.config.APP_NAME,
                    prompt = prompt,
                    timeout = 300)
        return password

    def _set_password_db(self, service_name, user_name, password):
        """
        internal Password cache. Prevent to ask password several times
        during runtime.
        """
        self.db['%s/%s' %(service_name, user_name)] = password

    def set_password(self, password, profile_id, mode, pw_id):
        """
        store password to keyring and Password_Cache
        """
        if self.config.mode_need_password(mode, pw_id):
            service_name = self.config.get_keyring_service_name(profile_id, mode, pw_id)
            user_name = self.config.get_keyring_user_name(profile_id)

            if self.config.get_password_save(profile_id):
                self._set_password_to_keyring(service_name, user_name, password)

            if self.config.get_password_use_cache(profile_id):
                self._set_password_to_cache(service_name, user_name, password)

            self._set_password_db(service_name, user_name, password)

    def _set_password_to_keyring(self, service_name, user_name, password):
        return tools.set_password(service_name, user_name, password)

    def _set_password_to_cache(self, service_name, user_name, password):
        if self.pw_cache.status():
            self.pw_cache.check_version()
            self.fifo.write('set_pw:%s/%s:%s' %(service_name, user_name, password), timeout = 5)
