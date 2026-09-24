import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('META_APP_SECRET', 'test-meta-secret')
os.environ.setdefault('VERIFY_TOKEN', 'test-verify-token')
os.environ.setdefault('DASHBOARD_USER', 'tester')
os.environ.setdefault('DASHBOARD_PASSWORD', 'strong-test-password')

import webhook_server_viewer as app_module


class WebhookSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_module.DB_PATH = os.path.join(self.tempdir.name, 'test.sqlite3')
        app_module.DATABASE_URL = ''
        app_module.META_APP_SECRET = 'test-meta-secret'
        app_module.APPS_SCRIPT_SYNC_TOKEN = 'test-sync-token'
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def signed_headers(self, body):
        signature = hmac.new(b'test-meta-secret', body, hashlib.sha256).hexdigest()
        return {'Content-Type': 'application/json', 'X-Hub-Signature-256': 'sha256=' + signature}

    def test_postgres_query_without_params_preserves_like_wildcards(self):
        calls = []
        class Cursor:
            def execute(self, *args):
                calls.append(args)
        connection = object.__new__(app_module.PGConnection)
        connection.cur = Cursor()
        query = "SELECT body FROM messages WHERE body LIKE '%grúa%'"
        connection.execute(query)
        connection.execute('SELECT id FROM messages WHERE sender=?', ('573001234567',))
        self.assertEqual(calls[0], (query,))
        self.assertEqual(calls[1], ('SELECT id FROM messages WHERE sender=%s', ('573001234567',)))

    def test_signature_validation(self):
        body = b'{"entry":[]}'
        sig = 'sha256=' + hmac.new(b'test-meta-secret', body, hashlib.sha256).hexdigest()
        self.assertTrue(app_module.valid_meta_signature(body, sig))
        self.assertFalse(app_module.valid_meta_signature(body + b' ', sig))
        self.assertFalse(app_module.valid_meta_signature(body, ''))

    def test_webhook_rejects_missing_or_bad_signature(self):
        body = b'{"entry":[]}'
        self.assertEqual(self.client.post('/webhook', data=body).status_code, 401)
        self.assertEqual(self.client.post('/webhook', data=body, headers={'X-Hub-Signature-256': 'sha256=bad'}).status_code, 401)

    def test_valid_event_is_processed_once(self):
        event = {'entry': [{'changes': [{'value': {'messages': [
            {'from': '573001234567', 'id': 'wamid.test-unique', 'type': 'text', 'text': {'body': 'hola'}}
        ]}}]}]}
        body = json.dumps(event, separators=(',', ':')).encode()
        with patch.object(app_module, 'process', return_value='respuesta') as process_mock, \
             patch.object(app_module, 'send_text', return_value=True), \
             patch.object(app_module, 'send_admin_alert', return_value=True):
            first = self.client.post('/webhook', data=body, headers=self.signed_headers(body))
            second = self.client.post('/webhook', data=body, headers=self.signed_headers(body))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(process_mock.call_count, 1)

    def test_media_is_not_downloaded_or_persisted_and_preserves_pending_question(self):
        sender = '573001234567'
        event = {'entry': [{'changes': [{'value': {'messages': [
            {'from': sender, 'id': 'wamid.media-test', 'type': 'image', 'image': {
                'id': 'customer-media-id-should-not-be-saved', 'filename': 'private-customer-file.pdf'
            }}
        ]}}]}]}
        body = json.dumps(event, separators=(',', ':')).encode()
        state = {'step': 'work', 'data': {'service': 'Alquiler de montacargas'}}
        with patch.object(app_module, 'load_conversation_state', return_value=state), \
             patch.object(app_module, 'save_conversation_state'), \
             patch.object(app_module, 'save') as save_mock, \
             patch.object(app_module.urllib.request, 'urlopen', side_effect=AssertionError('attachment download attempted')) as download_mock, \
             patch.object(app_module, 'send_text', return_value=True) as send_mock, \
             patch.object(app_module, 'send_admin_alert', return_value=True) as alert_mock:
            response = self.client.post('/webhook', data=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app_module.conversations[sender]['step'], 'work')
        self.assertNotIn('attachments', state['data'])
        download_mock.assert_not_called()
        generic_notice = '📎 El cliente envió un adjunto; no se almacenó por privacidad.'
        save_mock.assert_any_call(sender, 'in', generic_notice, 'received')
        self.assertIn('Por privacidad no almacenamos archivos adjuntos', send_mock.call_args.args[1])
        self.assertNotIn('private-customer-file.pdf', alert_mock.call_args.args[0])
        self.assertNotIn('customer-media-id-should-not-be-saved', alert_mock.call_args.args[0])
        c = app_module.db()
        self.assertEqual(c.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)
        c.close()

    def test_attachment_prompt_offers_text_instead_of_file_upload(self):
        sender = '573001234567'
        state = {'step': 'attachments_choice', 'data': {}}
        with patch.object(app_module, 'load_conversation_state', return_value=state), \
             patch.object(app_module, 'save_conversation_state'):
            reply = app_module.process(sender, 'sí')
        self.assertEqual(state['step'], 'additional_info')
        self.assertIn('no almacenamos archivos adjuntos', reply)
        self.assertNotIn('adjuntar fotos', reply.lower())

    def test_admin_alert_retries_once_and_records_success(self):
        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"messages":[{"id":"wamid.mock"}]}'

        with patch.object(app_module, 'META_ACCESS_TOKEN', 'test-token'), \
             patch.object(app_module, 'ADMIN_PHONE', '573000000000'), \
             patch.object(app_module.urllib.request, 'urlopen', side_effect=[app_module.urllib.error.URLError('mock failure'), FakeResponse()]) as send_mock, \
             patch.object(app_module.time, 'sleep'):
            self.assertTrue(app_module.send_admin_alert('alerta simulada', '573001234567'))
        self.assertEqual(send_mock.call_count, 2)
        c = app_module.db()
        row = c.execute('SELECT status,attempts,error FROM admin_alerts').fetchone()
        self.assertEqual((row['status'], row['attempts'], row['error']), ('sent', 2, ''))
        c.close()

    def test_admin_alert_records_failure_after_two_attempts(self):
        with patch.object(app_module, 'META_ACCESS_TOKEN', 'test-token'), \
             patch.object(app_module, 'ADMIN_PHONE', '573000000000'), \
             patch.object(app_module.urllib.request, 'urlopen', side_effect=app_module.urllib.error.URLError('mock failure')) as send_mock, \
             patch.object(app_module.time, 'sleep'):
            self.assertFalse(app_module.send_admin_alert('alerta simulada', '573001234567'))
        self.assertEqual(send_mock.call_count, 2)
        c = app_module.db()
        row = c.execute('SELECT status,attempts,error FROM admin_alerts').fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['attempts'], 2)
        self.assertIn('mock failure', row['error'])
        c.close()

    def test_failed_event_can_be_retried(self):
        self.assertTrue(app_module.claim_webhook_message('wamid.failed-test'))
        app_module.mark_webhook_message('wamid.failed-test', 'failed')
        self.assertTrue(app_module.claim_webhook_message('wamid.failed-test'))
        self.assertFalse(app_module.claim_webhook_message('wamid.failed-test'))

    def test_sync_endpoint_fails_closed_without_secret(self):
        with patch.object(app_module, 'APPS_SCRIPT_SYNC_TOKEN', ''):
            response = self.client.post('/marketing/sync-sheets', json={'prospect': {}})
        self.assertEqual(response.status_code, 503)

    def test_sync_endpoint_rejects_wrong_secret(self):
        response = self.client.post('/marketing/sync-sheets', json={'prospect': {}, 'token': 'wrong'})
        self.assertEqual(response.status_code, 403)

    def test_dashboard_blocks_cross_site_post(self):
        auth = base64.b64encode(b'tester:strong-test-password').decode()
        response = self.client.post('/dashboard/content', headers={
            'Authorization': 'Basic ' + auth,
            'Origin': 'https://evil.example',
        }, data={'title': 'x', 'copy': 'y', 'channel': 'z'})
        self.assertEqual(response.status_code, 403)

    def test_webhook_verify_requires_configured_token(self):
        with patch.object(app_module, 'VERIFY_TOKEN', ''):
            response = self.client.get('/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=test')
        self.assertEqual(response.status_code, 503)


    def test_sheets_queue_duplicate_enqueue_keeps_one_job(self):
        payload = {'sync_id': 'stable-1', 'name': 'Original'}
        self.assertEqual(app_module.enqueue_sheets_sync('stable-1', payload), (True, 'queued'))
        self.assertEqual(app_module.enqueue_sheets_sync('stable-1', {'sync_id': 'stable-1', 'name': 'Changed'}), (True, 'queued'))
        c = app_module.db()
        rows = c.execute('SELECT sync_id,payload FROM sheets_sync_queue').fetchall()
        c.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sync_id'], 'stable-1')
        self.assertEqual(json.loads(rows[0]['payload'])['name'], 'Original')

    def test_sheets_sender_requires_explicit_receiver_acknowledgement(self):
        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"ok":true,"duplicate":true}'
        with patch.object(app_module, 'APPS_SCRIPT_WEBHOOK_URL', 'https://example.invalid'), \
             patch.object(app_module, 'APPS_SCRIPT_SYNC_TOKEN', 'secret'), \
             patch.object(app_module.urllib.request, 'urlopen', return_value=FakeResponse()):
            self.assertEqual(app_module.sync_prospect_to_sheets({'sync_id': 'stable-1'}), (True, 'duplicado ya registrado'))
        class BadAck(FakeResponse):
            def read(self): return b'{"status":"success"}'
        with patch.object(app_module, 'APPS_SCRIPT_WEBHOOK_URL', 'https://example.invalid'), \
             patch.object(app_module, 'APPS_SCRIPT_SYNC_TOKEN', 'secret'), \
             patch.object(app_module.urllib.request, 'urlopen', return_value=BadAck()):
            ok, detail = app_module.sync_prospect_to_sheets({'sync_id': 'stable-1'})
        self.assertFalse(ok)
        self.assertIn('no confirmó', detail)

    def test_sheets_retry_retains_failure_then_marks_acknowledged_success(self):
        self.assertTrue(app_module.enqueue_sheets_sync('retry-1', {'sync_id': 'retry-1'})[0])
        with patch.object(app_module, 'sync_prospect_to_sheets', return_value=(False, 'temporary network error')):
            failed = app_module.process_sheets_sync_queue(limit=1)
        self.assertEqual((failed['attempted'], failed['synced']), (1, 0))
        c = app_module.db()
        row = c.execute('SELECT status,attempts,last_error FROM sheets_sync_queue WHERE sync_id=?', ('retry-1',)).fetchone()
        c.execute("UPDATE sheets_sync_queue SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE sync_id=?", ('retry-1',))
        c.commit(); c.close()
        self.assertEqual((row['status'], row['attempts'], row['last_error']), ('pending', 1, 'temporary network error'))
        with patch.object(app_module, 'sync_prospect_to_sheets', return_value=(True, 'guardado confirmado')):
            succeeded = app_module.process_sheets_sync_queue(limit=1)
        self.assertEqual((succeeded['attempted'], succeeded['synced']), (1, 1))
        c = app_module.db()
        row = c.execute('SELECT status,attempts,synced_at,last_error FROM sheets_sync_queue WHERE sync_id=?', ('retry-1',)).fetchone()
        c.close()
        self.assertEqual(row['status'], 'synced')
        self.assertEqual(row['attempts'], 2)
        self.assertTrue(row['synced_at'])
        self.assertEqual(row['last_error'], '')

    def test_finish_persists_stable_id_before_queueing(self):
        events = []
        original_save_state = app_module.save_conversation_state
        original_enqueue = app_module.enqueue_sheets_sync
        def record_state(sender, state):
            events.append(('state', state['data'].get('_prospect_sync_id')))
            return original_save_state(sender, state)
        def record_enqueue(sync_id, payload):
            events.append(('queue', sync_id))
            return original_enqueue(sync_id, payload)
        state = {'step': 'additional_choice', 'data': {'contact': 'Test', 'service': 'Visita técnica'}}
        with patch.object(app_module, 'save_conversation_state', side_effect=record_state), \
             patch.object(app_module, 'enqueue_sheets_sync', side_effect=record_enqueue), \
             patch.object(app_module, 'sync_prospect_to_sheets', return_value=(False, 'offline')):
            app_module.finish_request(state, '573001234567')
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0][0], 'state')
        self.assertEqual(events[1], ('queue', events[0][1]))
        self.assertTrue(events[0][1])

if __name__ == '__main__':
    unittest.main()
