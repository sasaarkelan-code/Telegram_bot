"""Local private backup, including SQLite WAL via the online backup API."""
from __future__ import annotations
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_data(source, destination=None):
    source=Path(source).resolve()
    parent=Path(destination).resolve() if destination else source.parent/'backups'
    if parent==source or source in parent.parents: raise ValueError('Папка резервных копий должна быть вне папки данных.')
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=parent/('smm-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
    target.mkdir(mode=0o700)
    try:
        for path in source.rglob('*'):
            if path.is_symlink(): raise ValueError('В данных найдена символическая ссылка; резервная копия остановлена.')
            if path.name=='bot.lock' or path.name.startswith('.smm-') or path.name.endswith(('-wal','-shm','-journal')): continue
            out=target/path.relative_to(source)
            if path.is_dir():out.mkdir(parents=True,exist_ok=True,mode=0o700);continue
            if not path.is_file():continue
            out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with path.open('rb') as file: sqlite=file.read(16)==b'SQLite format 3\x00'
            if sqlite:
                with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=15) as src, sqlite3.connect(out) as dst:
                    src.backup(dst)
            else:shutil.copy2(path,out)
            out.chmod(0o600)
    except BaseException:
        shutil.rmtree(target)
        raise
    return target
