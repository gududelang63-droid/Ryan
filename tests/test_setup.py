import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import ssl
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('installer', Path(__file__).parents[1] / 'setup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SafetyTests(unittest.TestCase):
    def test_ssh_and_public_ip_validation(self):
        self.assertEqual(m.parse_ssh_connection('203.0.113.5 50000 192.0.2.1 2222'), 2222)
        for value in ('', 'garbage', '1.2.3.4 50 1.2.3.5 999999'):
            with self.assertRaises(ValueError):
                m.parse_ssh_connection(value)
        for value in ('127.0.0.1', '10.0.0.1', '::1', 'example.com'):
            with self.assertRaises(ValueError):
                m.public_ipv4(value)

    def test_archive_rejects_escape_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, kind in (('../outside', tarfile.REGTYPE), ('x-ui/evil', tarfile.SYMTYPE)):
                archive = Path(tmp) / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    entry = tarfile.TarInfo(name)
                    entry.type, entry.linkname = kind, '/etc/passwd'
                    tar.addfile(entry, io.BytesIO(b''))
                with self.assertRaises(ValueError):
                    m.extract_archive(archive, tmp)

    def test_private_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'secret'
            dest.symlink_to(Path(tmp) / 'elsewhere')
            m.write_private(dest, 'test-only')
            self.assertFalse(dest.is_symlink())
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    def test_firewall_preserves_ssh_before_enable(self):
        commands = []
        with patch.object(m, 'run', side_effect=lambda a, **kw: commands.append(a)):
            m.configure_firewall({'ssh_ports': [22, 2222], 'panel_port': 8848})
        activation = commands.index(['ufw', '--force', 'enable'])
        for p in (22, 2222):
            allow = ['ufw', 'insert', '1', 'allow', f'{p}/tcp', 'comment', 'SSH preserved']
            self.assertLess(commands.index(allow), activation)
        self.assertFalse(any('reset' in c for c in commands))

    def test_acme_failure_stops_and_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'ROOT', Path(tmp)), \
             patch.object(m, 'CERT', Path(tmp)/'cert'), \
             patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'test-error')), \
             patch.object(m, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, '证书申请失败'):
                m.issue_certificate('8.8.8.8')
            self.assertEqual((Path(tmp)/'certificate.log').read_text(), 'test-error')
            command.assert_not_called()

    def test_management_rejects_protected_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, recovery = Path(tmp)/'state', Path(tmp)/'recovery'
            state.write_text('{}')
            recovery.write_text(json.dumps({'ssh_ports':[2222], 'panel_port':8848}))
            with patch.object(m, 'STATE', state), patch.object(m, 'RECOVERY', recovery), \
                 patch.object(m, 'ssh_ports', return_value=[22]), patch.object(m, 'run') as run:
                for p in (22,2222,80,8848):
                    with self.assertRaises(RuntimeError):
                        m.change_node_port('close-port', p, 'tcp')
                run.assert_not_called()

    def test_completed_install_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, info = Path(tmp)/'state', Path(tmp)/'info'
            state.write_text('{}')
            info.write_text('test-only')
            with patch.object(m,'STATE',state), patch.object(m,'INFO',info), patch.object(m,'run') as run:
                m.install()
                run.assert_not_called()


@unittest.skipUnless(os.environ.get('XUI_TEST_APP'), 'set XUI_TEST_APP to extracted official release')
class RealBinaryTest(unittest.TestCase):
    def test_https_panel_empty_inbounds_and_password_change(self):
        app = Path(os.environ['XUI_TEST_APP']).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db, cert, key = tmp/'db', tmp/'cert.pem', tmp/'key.pem'
            port = m.free_port(20000,30000)
            user, password, base = 'integration', 'test-' + str(uuid.uuid4()), '/integration/'
            # Test-only trusted cert: exercises real TLS and IP SAN verification.
            m.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-keyout',key,'-out',cert,'-subj','/CN=8.8.8.8','-addext','subjectAltName=IP:8.8.8.8'])
            m.initialize_panel(app,db,user,password,port,base)
            m.configure_tls(app,db,cert,key,listen='127.0.0.1')
            ctx = ssl.create_default_context(cafile=str(cert))
            env = dict(os.environ,XUI_DB_FOLDER=str(db),XUI_BIN_FOLDER=str(app/'bin'),XUI_LOG_FOLDER=str(tmp/'logs'))
            with (tmp/'panel.log').open('w+') as log:
                proc = subprocess.Popen([str(app/'x-ui')],cwd=app,env=env,stdout=log,stderr=log,start_new_session=True)
                try:
                    panel = m.Panel(port,base,'8.8.8.8',context=ctx)
                    panel.login(user,password)
                    self.assertEqual(panel.call('panel/api/inbounds/list'), [])
                    with self.assertRaises(Exception):
                        m.Panel(port,base,'1.1.1.1',context=ctx).call('login',{'username':user,'password':password})
                    with self.assertRaises(Exception):
                        m.Panel(port,base,'8.8.8.8').call('login',{'username':user,'password':password})
                    with self.assertRaises(RuntimeError):
                        panel.call('login',{'username':'admin','password':'admin'})
                    with sqlite3.connect(db/'x-ui.db') as con:
                        settings = dict(con.execute('SELECT key,value FROM settings'))
                    self.assertEqual(settings['webListen'],'127.0.0.1')
                    self.assertEqual(settings['webCertFile'],str(cert))
                    self.assertEqual(settings['subEnable'],'false')
                    new_password = 'rotated-' + str(uuid.uuid4())
                    m.run([app/'x-ui','setting','-username',user,'-password',new_password],cwd=app,env=env)
                    m.Panel(port,base,'8.8.8.8',context=ctx).login(user,new_password)
                    with self.assertRaises(RuntimeError):
                        m.Panel(port,base,'8.8.8.8',context=ctx).call('login',{'username':user,'password':password})
                finally:
                    os.killpg(proc.pid,signal.SIGTERM)
                    proc.wait(timeout=15)
