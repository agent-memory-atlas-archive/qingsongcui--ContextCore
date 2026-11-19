import { useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { Button, ErrorNotice, Field, Modal } from '../ui.jsx';

const sections = [
  ['providers', 'providers', '模型与服务商'],
  ['mcp', 'mcp', 'MCP 工具'],
  ['skills', 'skills', '技能'],
  ['rules', 'prompts', '使用规则']
];

function dispositionLabel(value) {
  if (value === 'create') return t('新增');
  if (value === 'unchanged') return t('已在资料库');
  if (value === 'update') return t('更新现有定义');
  if (value === 'conflict') return t('与现有定义冲突');
  return '';
}

function candidateSummary(item) {
  if (!item.available) return t('需要手动核对后添加。');
  if (item.file_count !== undefined) return t('{count} 个文件', { count: item.file_count });
  const detail = [item.protocol, item.base_url, item.model_ids?.join(', ')].filter(Boolean).join(' · ') || item.summary;
  return [detail, item.requires_credential ? t('需绑定凭据') : null, item.needs_review ? t('应用前请核对并补齐环境变量和请求头') : null].filter(Boolean).join(' · ');
}

export function NativeConfiguration({ host, initialKind, onClose, onChanged }) {
  const options = sections.filter(([, capability]) => host.capabilities?.[capability]);
  const [kind, setKind] = useState(options.find(([id]) => id === initialKind)?.[0] || options[0]?.[0] || '');
  const [preview, setPreview] = useState(null);
  const [checked, setChecked] = useState([]);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const lock = useRef(false);
  const base = '/api/manage/hosts/' + encodeURIComponent(host.id) + '/native';
  async function submit(importing) {
    if (lock.current || !kind) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      const body = importing ? { kind, fingerprint: preview.fingerprint, selected_ids: checked, confirmed: true } : { kind };
      const next = await mutate(base + (importing ? '/import' : '/preview'), 'POST', body);
      if (next?.host_id !== host.id || next?.kind !== kind) throw new Error(t('读回结果与当前助手或资料类型不符。'));
      if (importing) {
        setResult(next);
        onChanged();
      } else {
        setPreview(next);
        setChecked([]);
      }
    } catch (failure) {
      setError(failure.status === 409 ? new Error(t('来源或资料已变化，请重新读取并核对所选项。')) : failure);
      setPreview(null);
      setChecked([]);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return <Modal wide title={t('读取 {agent} 的原生配置', { agent: host.name || host.id })} onClose={onClose} busy={busy}>
    <div className="management-form agent-form">
      {result ? <p role="status">{t('已保存 {count} 项，{unchanged} 项已在资料库。', { count: result.imported, unchanged: result.unchanged })}</p> : <>
        <p>{t('选择要读取的资料。确认后保存到这个助手的清单，密钥和授权账号可在保存后绑定。')}</p>
        <Field label={t('读取内容')}><select disabled={busy} value={kind} onChange={event => { setKind(event.target.value); setPreview(null); setChecked([]); setError(null); }}>
          {options.map(([id, , label]) => <option key={id} value={id}>{t(label)}</option>)}
        </select></Field>
        {preview ? <section className="native-preview" aria-label={t('原生配置预览')}>
          {preview.source_path ? <p className="native-source-path">{preview.source_path}</p> : null}
          {preview.candidates?.length ? <div className="agent-resource-checklist">{preview.candidates.map(item => <label key={item.id}>
            <input type="checkbox" checked={checked.includes(item.id)} disabled={busy || !item.available} onChange={event => setChecked(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} />
            <span><strong>{item.name === 'Unrecognized entry' ? t('未识别的条目') : item.name}</strong> {dispositionLabel(item.disposition)}<small>{candidateSummary(item)}</small></span>
          </label>)}</div> : <p>{preview.status === 'unsupported' ? t('此助手没有可读取的对应原生格式。') : t('这个来源没有可读取的资料。也可以手动添加。')}</p>}
        </section> : null}
      </>}
      <ErrorNotice error={error} />
      <footer className="modal-actions"><Button disabled={busy} onClick={onClose}>{result ? t('完成') : t('取消')}</Button>
        {!result ? <Button variant={preview ? 'secondary' : 'primary'} busy={busy} disabled={!kind} onClick={() => submit(false)}>{preview ? t('重新读取') : t('读取并预览')}</Button> : null}
        {preview && !result ? <Button variant="primary" busy={busy} disabled={!checked.length} onClick={() => submit(true)}>{t('保存所选 {count} 项', { count: checked.length })}</Button> : null}
      </footer>
    </div>
  </Modal>;
}
