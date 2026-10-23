import pytest

from butian.runtime.service import ButianService


def test_backup_rejects_directory_redirect_in_source(tmp_path, redirect_link):
    """目录重定向点必须被拒；建链走 fs_redirect，不依赖 cmd.exe/mklink。"""
    home = tmp_path / 'runtime'
    service = ButianService(home)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'sentinel.txt').write_text('must not be copied', encoding='utf-8')
    link = home / 'escape'
    try:
        redirect_link(link, external)
        assert link.lstat().st_file_attributes & 0x400
        with pytest.raises(ValueError, match='symlink'):
            service.create_backup(tmp_path / 'backup')
        assert not (tmp_path / 'backup').exists()
        assert (external / 'sentinel.txt').read_text(encoding='utf-8') == 'must not be copied'
    finally:
        service.close()
