import { useRef, useState } from 'react';
import { mutate, request } from '../../lib/api.js';
import { t } from '../../lib/i18n.jsx';
import { Button, ErrorNotice, Field, Modal } from '../ui.jsx';

function restoreAction(code) {
  return { create: t('新增'), replace: t('替换本机定义'), unchanged: t('定义相同'), keep: t('保留本机定义'), keep_protected: t('保留本机绑定和凭据') }[code] || t('未知');
}

export function ConfigurationBackup({ onChanged }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState(null);
  const [replace, setReplace] = useState(false);
  const [preview, setPreview] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [page, setPage] = useState(0);
  const sequence = useRef(0);
  const lock = useRef(false);
  function reset() {
    sequence.current += 1;
    setFile(null); setPreview(null); setReceipt(null); setError(null); setReplace(false); setPage(0);
  }
  async function download() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null); setNotice('');
    try {
      const backup = await request('/api/manage/export');
      if (backup?.format !== 'senrelay-management' || !Array.isArray(backup.objects) || backup.managed_credentials_excluded !== true) throw new Error(t('备份格式无法核对。'));
      const url = URL.createObjectURL(new Blob([JSON.stringify(backup)], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'senrelay-configuration-' + new Date().toISOString().slice(0, 10) + '.json';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(t('已导出 {count} 项配置定义。', { count: backup.objects.length }));
    } catch (failure) { setError(failure); }
    finally { lock.current = false; setBusy(false); }
  }

  async function choose(input) {
    const generation = ++sequence.current;
    setFile(null); setPreview(null); setReceipt(null); setError(null); setPage(0);
    if (!input) return;
    if (input.size > 32 * 1024 * 1024) { setError(new Error(t('配置备份超过 32 MiB。'))); return; }
    try {
      const data = JSON.parse(await input.text());
      if (generation === sequence.current) setFile({ name: input.name, data });
    } catch { if (generation === sequence.current) setError(new Error(t('JSON 格式不正确，请检查后再预览。'))); }
  }
  async function check(apply) {
    if (lock.current || !file || (apply && !preview)) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const result = await mutate('/api/manage/configuration-backup/' + (apply ? 'restore' : 'preview'), 'POST',
        { data: file.data, replace_existing: replace, ...(apply ? { preview_id: preview.preview_id, confirmed: true } : {}) });
      if (apply) {
        if (result.status !== 'restored' || result.preview_id !== preview.preview_id) throw new Error(t('备份恢复回执尚未核对。'));
        setReceipt(result); onChanged?.();
      } else {
        if (result.status !== 'preview' || !result.preview_id || !Array.isArray(result.items)) throw new Error(t('备份格式无法核对。'));
        setPreview(result); setPage(0);
      }
    } catch (failure) { setError(failure); if (failure.status === 409) setPreview(null); }
    finally { lock.current = false; setBusy(false); }
  }
  return <section className="management-panel">
    <h2>{t('Senrelay 配置备份')}</h2><p>{t('备份包含服务商、模型、账号定义、资源和配置方案。钥匙串凭据与宿主文件不随备份导出，自由文本请在分享前核对。')}</p>
    <div className="toolbar-actions"><Button busy={busy && !open} onClick={download}>{t('导出配置备份')}</Button><Button disabled={busy} onClick={() => { reset(); setOpen(true); }}>{t('恢复配置备份')}</Button></div>
    {notice ? <p role="status">{notice}</p> : null}{!open ? <ErrorNotice error={error} /> : null}
    {open ? <Modal wide title={t('恢复配置备份')} busy={busy} onClose={() => { reset(); setOpen(false); }}>
      <Field label={t('选择 Senrelay 备份文件')}><input type="file" accept=".json,application/json" disabled={busy || Boolean(receipt)} onChange={e => choose(e.target.files?.[0])} /></Field>
      <label className="inline-checkbox"><input type="checkbox" checked={replace} disabled={busy || Boolean(receipt)} onChange={e => { setReplace(e.target.checked); setPreview(null); }} />{t('用备份替换同一编号的本机定义')}</label>
      {preview ? <><h3>{file?.name}</h3><p>{t('预计写入 {count} 项。已有凭据的定义保留在本机，恢复后可逐项重新绑定。', { count: preview.changed })}</p>
        <ul className="backup-entries">{preview.items.slice(page * 30, (page + 1) * 30).map(item => <li key={item.id}>{item.name} · {restoreAction(item.action)}
          {item.reattach_required ? ' · ' + t('需重新绑定凭据') : ''}{item.project_detached ? ' · ' + t('本机没有对应项目，保存为未绑定') : ''}</li>)}</ul>
        {preview.items.length > 30 ? <div className="toolbar-actions"><Button disabled={page === 0} onClick={() => setPage(p => p - 1)}>{t('上一页')}</Button><span>{page + 1} / {Math.ceil(preview.items.length / 30)}</span><Button disabled={(page + 1) * 30 >= preview.items.length} onClick={() => setPage(p => p + 1)}>{t('下一页')}</Button></div> : null}
      </> : null}
      {receipt ? <p role="status">{t('已恢复 {count} 项配置定义。可在对应助手中预览并应用。', { count: receipt.changed })}</p> : null}
      <ErrorNotice error={error} /><footer className="modal-actions"><Button disabled={busy} onClick={() => { reset(); setOpen(false); }}>{t('关闭')}</Button>
        {!receipt ? preview ? <Button variant="primary" busy={busy} disabled={!preview.changed} onClick={() => check(true)}>{t('确认恢复定义')}</Button>
          : <Button variant="primary" busy={busy} disabled={!file} onClick={() => check(false)}>{t('预览恢复差异')}</Button> : null}</footer>
    </Modal> : null}
  </section>;
}
