"""Local startup validation and an allowlisted, redacted configuration summary."""
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    pass


def read_config(path):
    try:
        # Windows editors can save a UTF-8 BOM; accept it explicitly.
        with Path(path).open(encoding='utf-8-sig') as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise ConfigurationError('File config.json tidak ditemukan.') from None
    except PermissionError:
        raise ConfigurationError('File config.json tidak dapat dibaca. Periksa izin akses file.') from None
    except UnicodeDecodeError:
        raise ConfigurationError('Simpan config.json dengan encoding UTF-8.') from None
    except json.JSONDecodeError as error:
        raise ConfigurationError(f'Format JSON tidak valid pada baris {error.lineno}, kolom {error.colno}.') from None
    except OSError:
        raise ConfigurationError('File config.json tidak dapat dibaca. Periksa lokasi dan kondisi file.') from None
    if not isinstance(data, dict):
        raise ConfigurationError('Isi config.json harus berupa objek JSON.')
    server = data.get('server_url')
    try:
        parsed = urlsplit(server) if isinstance(server, str) else None
        valid_server = (parsed and parsed.scheme in ('http', 'https') and parsed.hostname
                        and parsed.port != 0 and not parsed.username and not parsed.password
                        and not parsed.query and not parsed.fragment
                        and not any(character.isspace() for character in server))
    except ValueError:
        valid_server = False
    if not valid_server:
        raise ConfigurationError('Isi server_url dengan alamat HTTP/HTTPS yang valid, tanpa kredensial atau parameter URL.')
    key = data.get('api_key')
    if not isinstance(key, str) or len(key) < 32 or key != key.strip():
        raise ConfigurationError('api_key harus berupa teks minimal 32 karakter, tanpa spasi di awal/akhir.')
    password = data.get('master_password')
    if not isinstance(password, str) or not password.strip():
        raise ConfigurationError('master_password belum diatur. Isi password petugas agar akses lokal tersedia.')
    for field in ('verify_ssl', 'auto_update', 'autorun', 'auto_lock_on_run'):
        if field in data and not isinstance(data[field], bool):
            raise ConfigurationError(f'{field} harus berupa true atau false, tanpa tanda kutip.')
    quality = data.get('screenshot_jpeg_quality', 80)
    if isinstance(quality, bool) or not isinstance(quality, int) or not 40 <= quality <= 95:
        raise ConfigurationError('screenshot_jpeg_quality harus berupa angka bulat 40–95.')
    if 'hostname' in data and (not isinstance(data['hostname'], str) or not data['hostname'].strip()):
        raise ConfigurationError('hostname harus berupa nama komputer yang tidak kosong.')
    return data


def masked_key(value):
    if not isinstance(value, str) or not value:
        return 'Belum diatur'
    # Never reveal an entire key, including malformed short keys.
    return '••••' + (value[-4:] if len(value) > 4 else '')


def safe_server_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return 'Tidak valid'
        host = parsed.hostname
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
    except (TypeError, ValueError, AttributeError):
        return 'Tidak valid'


def configuration_summary(data, hostname):
    enabled = lambda field, default=False: 'Aktif' if data.get(field, default) else 'Nonaktif'
    return [
        ('Nama komputer', hostname),
        ('Server', safe_server_url(data.get('server_url', ''))),
        ('API key', masked_key(data.get('api_key'))),
        ('Password petugas', 'Diatur · disembunyikan' if data.get('master_password') else 'Belum diatur'),
        ('Verifikasi TLS', enabled('verify_ssl', True)),
        ('Pembaruan otomatis', enabled('auto_update')),
        ('Jalankan saat masuk', enabled('autorun')),
        ('Kunci saat mulai', enabled('auto_lock_on_run')),
        ('Kualitas JPEG', str(data.get('screenshot_jpeg_quality', 80))),
    ]
