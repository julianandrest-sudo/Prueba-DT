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


if __name__ == '__main__':
    unittest.main()
