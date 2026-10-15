import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from client_config import ConfigurationError, read_config, configuration_summary, masked_key


def valid_config():
    return {'server_url': 'http://192.168.1.5:3333', 'api_key': 'private-api-key-' + 'x'*32,
            'master_password': 'private-staff-password', 'hostname': 'PC-01'}


class ClientConfigTests(unittest.TestCase):
    def test_accepts_utf8_bom_and_unicode_staff_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            data = {**valid_config(), 'master_password': 'pétugas-rahasia'}
            path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8-sig')
            self.assertEqual(read_config(path), data)

    def test_read_failures_have_specific_safe_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with self.assertRaisesRegex(ConfigurationError, 'tidak ditemukan'):
                read_config(path)
            with mock.patch.object(Path, 'open', side_effect=PermissionError):
                with self.assertRaisesRegex(ConfigurationError, 'izin akses'):
                    read_config(path)
            path.write_text('{"master_password": "very-private",\n bad json}')
            with self.assertRaises(ConfigurationError) as error:
                read_config(path)
            self.assertIn('baris 2', str(error.exception))
            self.assertNotIn('very-private', str(error.exception))
            path.write_bytes(b'\xff\xfe')
            with self.assertRaisesRegex(ConfigurationError, 'UTF-8'):
                read_config(path)

    def test_invalid_connection_password_and_option_types_are_rejected_before_startup(self):
        invalid = [[], None, {**valid_config(), 'server_url': 'http://[broken'},
                   {**valid_config(), 'server_url': 'ftp://server'},
                   {**valid_config(), 'server_url': 'https://user:secret@server/?token=secret'},
                   {**valid_config(), 'api_key': 'too short'},
                   {**valid_config(), 'api_key': 1234},
                   {**valid_config(), 'master_password': ''},
                   {**valid_config(), 'master_password': None},
                   {**valid_config(), 'autorun': 'false'},
                   {**valid_config(), 'screenshot_jpeg_quality': True},
                   {**valid_config(), 'hostname': 12}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for value in invalid:
                with self.subTest(value=type(value).__name__):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ConfigurationError):
                        read_config(path)

    def test_summary_never_exposes_password_keys_url_credentials_or_unknown_secrets(self):
        config = {**valid_config(), 'server_url': 'https://user:url-password@host:8443/base?token=query-secret#fragment-secret',
                  'unknown_token': 'unknown-secret', 'nested': {'secret': 'nested-secret'}}
        summary = '\n'.join(label + ': ' + value for label, value in configuration_summary(config, 'PC-01'))
        for secret in [config['api_key'], config['master_password'], 'url-password', 'query-secret',
                       'fragment-secret', 'unknown-secret', 'nested-secret']:
            self.assertNotIn(secret, summary)
        self.assertIn('••••xxxx', summary)
        self.assertIn('https://host:8443/base', summary)
        self.assertIn('Diatur · disembunyikan', summary)
        self.assertEqual(masked_key('1234'), '••••')
