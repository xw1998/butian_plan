import os
import subprocess

import pytest

from butian.runtime.service import ButianService


def test_backup_rejects_directory_redirect_in_source(tmp_path):
    home = tmp_path / 'runtime'
    service = ButianService(home)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'sentinel.txt').write_text('must not be copied', encoding='utf-8')
    link = home / 'escape'
    try:
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(external)], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            assert link.lstat().st_file_attributes & 0x400
        else:
            link.symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match='symlink'):
            service.create_backup(tmp_path / 'backup')
        assert not (tmp_path / 'backup').exists()
        assert (external / 'sentinel.txt').read_text(encoding='utf-8') == 'must not be copied'
    finally:
        service.close()
