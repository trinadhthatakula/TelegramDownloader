"""FastAPI endpoint and middleware tests with DemoService."""
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from api.app import create_app
from engine.demo import DemoService
from telegram_auth import AccountStore


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AccountStore(self.tmp.name)
        self.store.data = {
            'active': 'd' * 32,
            'profiles': {
                'd' * 32: {
                    'label': 'Demo',
                    'api_id': 42,
                    'api_hash': 'a' * 32,
                    'session': str(self.root / 'session'),
                }
            },
        }
        self.service = DemoService(
            42,
            'a' * 32,
            str(self.root / 'session'),
            self.store,
            str(self.root / 'catalog.sqlite3'),
            str(self.root / 'config.json'),
        )
        self.service.config['output_dir'] = str(self.root / 'out')
        self.run_async(self.service.init_client())
        self.run_async(self.service.fetch_dialogs())

        self.launch_token = 'test-token-1234567890123456789012'
        self.app = create_app(
            self.service,
            token=self.launch_token,
            port=8765,
            dev_origin='http://127.0.0.1:5173',
        )
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')

    def tearDown(self):
        self.run_async(self.service.close_service())
        self.service.shutdown()
        self.tmp.cleanup()

    def run_async(self, coro):
        return self.service.run_coro(coro).result(15)

    def test_security_headers_and_host_validation(self):
        # Invalid Host header rejected
        res = self.client.get('/', headers={'host': 'evil.com'})
        self.assertEqual(res.status_code, 403)

        # Invalid Origin rejected
        res = self.client.get(
            '/',
            headers={'host': '127.0.0.1:8765', 'origin': 'http://attacker.com'},
        )
        self.assertEqual(res.status_code, 403)

        # Valid host reaches static bundle
        res = self.client.get('/', headers={'host': '127.0.0.1:8765'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('Content-Security-Policy', res.headers)
        self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(res.headers.get('Referrer-Policy'), 'no-referrer')

    def test_session_authentication_flow(self):
        # Accessing protected /api/state without cookie returns 401
        res = self.client.get('/api/state', headers={'host': '127.0.0.1:8765'})
        self.assertEqual(res.status_code, 401)
        self.assertIn('Open the app using its launcher link', res.json()['detail'])

        # Invalid token to /api/session
        res = self.client.post(
            '/api/session',
            headers={'host': '127.0.0.1:8765', 'authorization': 'Bearer wrong'},
        )
        self.assertEqual(res.status_code, 401)

        # Valid token sets cookie
        res = self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['connected'])
        self.assertIn('tgdl_session', self.client.cookies)

        # Subsequent call to /api/state succeeds
        res = self.client.get('/api/state', headers={'host': '127.0.0.1:8765'})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['authorized'])
        self.assertEqual(body['account'], 'user:42')
        self.assertTrue(len(body['chats']) > 0)

    def test_api_directory_and_settings_endpoints(self):
        # Authenticate first
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )

        # Directory listing
        res = self.client.get(
            f'/api/directories?path={self.root}',
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['path'], str(self.root.resolve()))
        self.assertTrue(data['writable'])

        # Directory check
        check_dir = self.root / 'custom_down'
        res = self.client.post(
            '/api/directories/check',
            json={'path': str(check_dir)},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(check_dir.is_dir())

        # Update settings
        res = self.client.post(
            '/api/settings',
            json={'max_parallel_files': 5, 'scan_depth': 100},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['max_parallel_files'], 5)
        self.assertEqual(res.json()['scan_depth'], 100)

    def wait_operation(self, op_id, timeout=5):
        import time
        start = time.time()
        while time.time() - start < timeout:
            op = self.service.operations.get(op_id)
            if op and op['status'] in ('complete', 'failed', 'cancelled'):
                return op
            time.sleep(0.05)
        return self.service.operations.get(op_id)

    def test_scan_and_preview_workflow(self):
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )

        # Preview without scan should fail (incomplete inventory)
        preview_payload = {
            'scopes': [{'chat_id': 1001}],
            'quantities': {'images': 2, 'videos': 1},
            'order': 'newest',
            'skip_existing': True,
        }
        res = self.client.post(
            '/api/jobs/preview',
            json=preview_payload,
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        op_id = res.json()['operation']
        op = self.wait_operation(op_id)
        self.assertIsNotNone(op)
        self.assertEqual(op['status'], 'failed')

        # Now trigger scan synchronously in backend
        self.run_async(self.service.scan([1001]))

        # Inventory endpoint
        inv_res = self.client.post(
            '/api/inventory',
            json=preview_payload,
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(inv_res.status_code, 200)
        self.assertTrue(len(inv_res.json()) > 0)

        # Save selection preference
        pref_res = self.client.post(
            '/api/selection',
            json={'chats': [1001], 'quantities': {'images': 2}},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(pref_res.status_code, 200)
        self.assertTrue(pref_res.json()['saved'])

        # Now preview should succeed
        res = self.client.post(
            '/api/jobs/preview',
            json=preview_payload,
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        op_id = res.json()['operation']
        op = self.wait_operation(op_id)
        self.assertIsNotNone(op)
        self.assertEqual(op['status'], 'complete')
        preview_data = op['result']
        self.assertEqual(preview_data['counts'], {'images': 2, 'videos': 1})
        self.assertEqual(preview_data['total'], 3)
        self.assertIn('free_bytes', preview_data)
        self.assertIsInstance(preview_data['free_bytes'], int)
        self.assertGreaterEqual(preview_data['free_bytes'], 0)

        # Resume job
        job_id = preview_data['id']
        resume_res = self.client.post(
            f'/api/jobs/{job_id}/resume',
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(resume_res.status_code, 200)
        resume_op = self.wait_operation(resume_res.json()['operation'])
        self.assertIsNotNone(resume_op)
        self.assertEqual(resume_op['status'], 'complete')
        # Drain workers
        self.run_async(self.service.wait_for_scheduler_idle())
        self.run_async(self.service.flush_jobs())

        # Check queue
        state_res = self.client.get('/api/state', headers={'host': '127.0.0.1:8765'})
        queue = state_res.json()['queue']
        self.assertEqual(len(queue), 3)
        self.assertTrue(all(item['status'] == 'Completed' for item in queue))

    def test_partials_and_issues_endpoints(self):
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )

        # Partials get
        res = self.client.get('/api/partials', headers={'host': '127.0.0.1:8765'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['count'], 0)

        # Partials clean requires confirm=True
        res = self.client.post(
            '/api/partials/clean',
            json={},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('Confirm removal', res.json()['detail'])

        res = self.client.post(
            '/api/partials/clean',
            json={'confirm': True},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)

        # Clear issues
        self.service.issues.append({'id': 'iss1', 'message': 'err', 'time': 123})
        res = self.client.post(
            '/api/issues/clear',
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.service.issues), 0)

    def test_auth_actions_validation(self):
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )
        # Invalid action raises 400
        res = self.client.post(
            '/api/auth',
            json={'action': 'invalid_action'},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('Unknown account action', res.json()['detail'])

    def test_control_actions_validation(self):
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )
        # Invalid action raises 400
        res = self.client.post(
            '/api/control',
            json={'action': 'explode'},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('Unknown download action', res.json()['detail'])

        # Valid action starts operation
        res = self.client.post(
            '/api/control',
            json={'action': 'pause'},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn('operation', res.json())

    def test_quit_endpoint(self):
        shutdown_called = False

        def on_shutdown():
            nonlocal shutdown_called
            shutdown_called = True

        app = create_app(
            self.service,
            token=self.launch_token,
            port=8765,
            shutdown=on_shutdown,
        )
        client = TestClient(app, base_url='http://127.0.0.1:8765')
        client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )
        res = client.post('/api/quit', headers={'host': '127.0.0.1:8765'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['stopped'])
        self.assertTrue(shutdown_called)

    def test_preview_includes_free_bytes(self):
        self.run_async(self.service.scan([1001]))
        preview_payload = {
            'scopes': [{'chat_id': 1001}],
            'quantities': {'images': 1},
            'order': 'newest',
            'skip_existing': True,
        }
        preview_data = self.run_async(self.service.preview(preview_payload))
        self.assertIn('free_bytes', preview_data)
        self.assertIsInstance(preview_data['free_bytes'], int)
        self.assertGreaterEqual(preview_data['free_bytes'], 0)

    def test_sensitive_directory_restricted(self):
        self.client.post(
            '/api/session',
            headers={
                'host': '127.0.0.1:8765',
                'authorization': f'Bearer {self.launch_token}',
            },
        )
        res = self.client.get(
            '/api/directories?path=~/.ssh',
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('restricted', res.json()['detail'].lower())
        self.assertEqual(res.json()['detail'], 'Access to sensitive system directories is restricted.')

        # Also verify POST /api/directories/check
        res_check = self.client.post(
            '/api/directories/check',
            json={'path': '~/.ssh'},
            headers={'host': '127.0.0.1:8765'},
        )
        self.assertEqual(res_check.status_code, 400)
        self.assertEqual(res_check.json()['detail'], 'Access to sensitive system directories is restricted.')


if __name__ == '__main__':
    unittest.main()
