"""Versioned, verified market snapshots: actual closes and total-return research."""
from pathlib import Path
import hashlib,json,os,uuid,shutil
from datetime import datetime,timezone

NAMES={'raw':'source_close.csv','adjusted':'source_adjusted_close.csv','actions':'corporate_actions.json'}
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def resolve(path,kind='adjusted',required=False):
    path=Path(path);root=path.parent
    manifest=root/'market_prices_manifest.json'
    if manifest.exists():
        try:
            m=json.loads(manifest.read_text());entry=m['files'][kind]
            candidate=(root/entry['path']).resolve()
            if not candidate.is_relative_to((root/'market_snapshots').resolve()):raise ValueError('行情快照路径不正确')
            if digest(candidate)!=entry['sha256']:raise ValueError('行情快照校验失败，请重新更新行情')
            return candidate
        except (KeyError,OSError,json.JSONDecodeError) as exc:raise ValueError('行情快照无法读取，请恢复备份或重新更新行情') from exc
    candidate=root/NAMES[kind]
    if candidate.exists():return candidate
    if kind=='adjusted' and path.exists():return path
    if required:raise ValueError('缺少实盘收盘价，请先点击“更新行情并回测”获取独立报价')
    return None

def publish(root,raw,adjusted,actions):
    root=Path(root);committed=False;folder=root/'market_snapshots'/uuid.uuid4().hex;folder.mkdir(parents=True)
    try:
        raw.to_csv(folder/NAMES['raw'],index_label='Date');adjusted.to_csv(folder/NAMES['adjusted'],index_label='Date')
        (folder/NAMES['actions']).write_text(json.dumps(actions,ensure_ascii=False,indent=2))
        files={}
        for kind,name in NAMES.items():
            p=folder/name
            with p.open('rb') as f:os.fsync(f.fileno())
            files[kind]={'path':str(p.relative_to(root)),'sha256':digest(p)}
        manifest={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'as_of':raw.index[-1].date().isoformat(),'files':files}
        tmp=root/'market_prices_manifest.json.tmp';tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        with tmp.open('rb') as f:os.fsync(f.fileno())
        os.replace(tmp,root/'market_prices_manifest.json');committed=True
        # Compatibility exports; the committed manifest remains authoritative.
        for name in NAMES.values():
            tmp=root/(name+'.tmp');shutil.copyfile(folder/name,tmp);os.replace(tmp,root/name)
        generations=sorted((root/'market_snapshots').iterdir(),key=lambda p:p.stat().st_mtime,reverse=True)
        for old in generations[3:]:
            if old!=folder and old.is_dir() and not old.is_symlink():shutil.rmtree(old)
        return manifest
    except Exception:
        # Published snapshots must remain available even if compatibility export fails.
        if not committed:shutil.rmtree(folder,ignore_errors=True)
        raise

def actions_for(root):
    path=resolve(Path(root)/'source_adjusted_close.csv','actions')
    return json.loads(path.read_text()) if path else []
