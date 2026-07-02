import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_update


class TestParseVersion:
    def test_plain(self):
        assert app_update.parse_version('2.2.0') == (2, 2, 0)

    def test_v_prefix(self):
        assert app_update.parse_version('v2.10.3') == (2, 10, 3)

    def test_garbage(self):
        assert app_update.parse_version('not-a-version') == (0, 0, 0)

    def test_ordering(self):
        assert app_update.parse_version('v2.10.0') > app_update.parse_version('2.9.9')


class TestSelectAsset:
    def _assets(self, *names):
        return [{'name': n, 'browser_download_url': f'https://x/{n}'} for n in names]

    def test_windows_prefers_stable_name(self):
        assets = self._assets(
            'TTC_Positions_Report_2.2.0_Windows.exe',
            'TTC_Positions_Report_Windows.exe',
            'SHA256SUMS.txt',
        )
        asset = app_update.select_asset(assets, system='Windows')
        assert asset['name'] == 'TTC_Positions_Report_Windows.exe'

    def test_windows_falls_back_to_versioned_exe(self):
        assets = self._assets('TTC_Positions_Report_2.1.0_Windows.exe', 'foo.dmg')
        asset = app_update.select_asset(assets, system='Windows')
        assert asset['name'] == 'TTC_Positions_Report_2.1.0_Windows.exe'

    def test_mac_picks_dmg(self):
        assets = self._assets('TTC_Positions_Report_Windows.exe', 'TTC_2.2.0_Mac.dmg')
        asset = app_update.select_asset(assets, system='Darwin')
        assert asset['name'] == 'TTC_2.2.0_Mac.dmg'

    def test_no_match(self):
        assert app_update.select_asset(self._assets('README.md'), system='Windows') is None


class TestChecksums:
    def test_parse_checksums(self):
        text = (
            'abc123' + '0' * 58 + '  TTC_Positions_Report_Windows.exe\n'
            'def456' + '1' * 58 + ' *TTC_Positions_Report_2.2.0_Mac.dmg\n'
            'not a checksum line\n'
        )
        parsed = app_update.parse_checksums(text)
        assert parsed['TTC_Positions_Report_Windows.exe'].startswith('abc123')
        assert parsed['TTC_Positions_Report_2.2.0_Mac.dmg'].startswith('def456')
        assert len(parsed) == 2

    def test_parse_checksums_strips_paths(self):
        text = 'a' * 64 + '  dist/some.exe\n'
        assert 'some.exe' in app_update.parse_checksums(text)

    def test_sha256_of_file(self, tmp_path):
        f = tmp_path / 'blob.bin'
        f.write_bytes(b'hello world')
        expected = hashlib.sha256(b'hello world').hexdigest()
        assert app_update.sha256_of_file(str(f)) == expected

    def test_verify_refuses_without_checksums_url(self, tmp_path):
        f = tmp_path / 'x.exe'
        f.write_bytes(b'data')
        assert app_update.verify_download(str(f), 'x.exe', None, 'UA') is False

    def test_verify_matches(self, tmp_path, monkeypatch):
        f = tmp_path / 'x.exe'
        f.write_bytes(b'data')
        digest = hashlib.sha256(b'data').hexdigest()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return f'{digest}  x.exe\n'.encode()

        monkeypatch.setattr(app_update, '_http_get', lambda *a, **k: FakeResponse())
        assert app_update.verify_download(str(f), 'x.exe', 'https://x/sums', 'UA') is True

    def test_verify_rejects_mismatch(self, tmp_path, monkeypatch):
        f = tmp_path / 'x.exe'
        f.write_bytes(b'data')

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return ('f' * 64 + '  x.exe\n').encode()

        monkeypatch.setattr(app_update, '_http_get', lambda *a, **k: FakeResponse())
        assert app_update.verify_download(str(f), 'x.exe', 'https://x/sums', 'UA') is False


class TestUpdateScript:
    def test_script_contents(self):
        script = app_update.build_update_script(
            1234,
            r'C:\Temp\ttc_update\TTC_new.exe',
            r'C:\Users\ron\Dropbox\TTC\TTC.exe',
            r'C:\Users\ron\Dropbox\TTC\TTC.exe.old.exe',
            r'C:\Users\ron\Dropbox\TTC\update_failed.txt',
        )
        # Waits for our PID to exit before touching the exe
        assert 'PID eq 1234' in script
        # Backs up the old exe, retries the copy for Dropbox file locks
        assert 'copy /y "%DST%" "%BAK%"' in script
        assert 'geq 10' in script
        # Relaunches whichever exe is at the target path, then self-deletes
        assert 'start "" "%DST%"' in script
        assert 'del "%~f0"' in script
        # Failure writes the marker the app reports on next start
        assert 'update_failed.txt' in script

    def test_install_refuses_unfrozen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_update.platform, 'system', lambda: 'Windows')
        assert getattr(sys, 'frozen', False) is False
        result = app_update.install_update(str(tmp_path / 'new.exe'))
        assert result is False


class TestPostUpdateState:
    def test_no_marker(self, tmp_path):
        assert app_update.check_post_update_state(str(tmp_path)) is None

    def test_marker_read_and_removed(self, tmp_path):
        marker = tmp_path / app_update.FAIL_MARKER_NAME
        marker.write_text('Update could not replace the application file.')
        message = app_update.check_post_update_state(str(tmp_path))
        assert 'could not replace' in message
        assert not marker.exists()
