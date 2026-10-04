"""Local health checks and verified portable backups; never restore over a live DB."""
from pathlib import Path
import sqlite3,json,zipfile,tempfile,hashlib,os
from datetime import datetime
import investment_tool as ledger
import market_prices

def inspect(config,root):
    checks=[]
    con=ledger.connect(config['database_path'])
    try:
        integrity=con.execute('PRAGMA integrity_check').fetchone()[0]
        checks.append({'name':'数据库完整性','ok':integrity=='ok','detail':integrity})
        records=ledger.rows(con);state=ledger.calculate(records,ledger.get_quotes(con))
        ledger.validate_cash_history(records)
        checks.append({'name':'交易现金与持仓约束','ok':True})
        identity=abs(state['account_value']-state['net_contributed']-state['total_pnl'])<ledger.D('0.000001')
        checks.append({'name':'账户资产与收益对账','ok':identity})
        warnings=ledger.va_budget_reference_warnings(con,records)
        checks.append({'name':'历史预算基准','ok':not warnings,'detail':'；'.join(warnings)})
        splits=ledger.pending_stock_splits(con,config)
        checks.append({'name':'拆股核对','ok':not splits,'detail':'存在尚未核对的拆股' if splits else '无待核对拆股'})
        try:
            for kind in ('raw','adjusted'):market_prices.resolve(Path(root)/'data/source_adjusted_close.csv',kind,required=True)
            checks.append({'name':'实盘与回测行情快照','ok':True})
        except ValueError as exc:checks.append({'name':'实盘与回测行情快照','ok':False,'detail':str(exc)})
    except (ValueError,sqlite3.Error) as exc:checks.append({'name':'账本检查','ok':False,'detail':str(exc)})
    finally:con.close()
    return checks

def backup(config,root):
    root=Path(root).resolve();program=root.parent;directory=root/'data/backups';directory.mkdir(exist_ok=True)
    name='full-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.zip';path=directory/name;temporary=path.with_suffix('.zip.tmp')
    with tempfile.TemporaryDirectory(prefix='voo-backup-') as staging:
        db=Path(staging)/'investments.sqlite3'
        with sqlite3.connect(config['database_path']) as source,sqlite3.connect(db) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库备份完整性检查失败')
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for entry in program.rglob('*'):
                if not entry.is_file():continue
                if entry.is_symlink() and not (entry.parent == program/'.venv/bin' and entry.name.startswith('python')):continue
                rel=entry.relative_to(program)
                if any(p in ('backups','__pycache__','.git','.aws','.codex','.agents') for p in rel.parts):continue
                if entry.suffix in ('.pyc','.tmp') or entry.name.startswith('.investment_gui') or entry.name=='.DS_Store':continue
                if entry.resolve()==Path(config['database_path']).resolve():continue
                if entry.name.startswith('investments.sqlite3-'):continue
                z.write(entry,str(rel))
            z.write(db,'程序文件/data/investments.sqlite3')
            z.writestr('恢复备份说明.txt','先关闭投资工具。解压到新的目录进行检查，双击管理投资记录.command。确认账户和交易正确后再替换旧目录；不要在运行时覆盖数据库。跨电脑时需要重建 .venv 并修改 Obsidian 路径。行情快照、拆股记录和预算修订均已包含。')
            z.writestr('backup_manifest.json',json.dumps({'database_sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'schema_version':7,'created_at':datetime.now().isoformat()},indent=2))
        with zipfile.ZipFile(temporary) as z:
            if z.testzip() is not None:raise ValueError('压缩包校验失败')
            metadata=json.loads(z.read('backup_manifest.json'))
            if hashlib.sha256(z.read('程序文件/data/investments.sqlite3')).hexdigest()!=metadata['database_sha256']:raise ValueError('备份数据库哈希不一致')
        os.replace(temporary,path)
    # Only this feature's explicitly named complete packages are retained.
    packages=sorted(directory.glob('full-backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in packages[3:]:old.unlink()
    return path
