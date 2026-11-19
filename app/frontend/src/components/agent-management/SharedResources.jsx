import { useEffect, useRef, useState } from 'react';
import { request, mutate } from '../../lib/api.js';
import { t } from '../../lib/i18n.jsx';
import { useResource } from '../../lib/useResource.js';
import { list, safeEvidence, mcpNeedsReview } from '../../lib/management.js';
import { Button, EmptyState, ErrorNotice, Field, Modal, ResourceState } from '../ui.jsx';
import { AssetTools } from '../AssetTools.jsx';
import { StatusBadge } from '../ManagementUI.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';
import { configuredLabel } from './ConfigurationState.jsx';
import '../../styles/shared-resources.css';

export { ConfigurationBackup } from './ConfigurationBackup.jsx';

export function ProfileScope({ hosts = [] }) {
  if (hosts.some(host => (host.id === 'codex' || host.id === 'codex-desktop') && (host.shared_configuration === false || host.config_root))) return <p className="muted">{t('方案按助手应用到各自的用户级配置文件。')}</p>;
  return <p className="muted">{t('方案按助手应用到用户级配置文件。Codex CLI 和桌面版共用原生配置。')}</p>;
}

const capability = { mcp: 'mcp', skill: 'skills', prompt: 'prompts' };
const cellKey = (host, asset) => host + '/' + asset;

export function SharedResourceMatrix({ hostId, allHosts, revision, onChanged }) {
  const [kind, setKind] = useState('mcp');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [firstHost, setFirstHost] = useState('');
  const [selection, setSelection] = useState([]);
  const [operation, setOperation] = useState('install');
  const [plans, setPlans] = useState([]);
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState(null);
  const [submitted, setSubmitted] = useState(false);
  const mounted = useRef(false);
  const lock = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const sorted = allHosts.slice().sort((a, b) => Number(b.id === hostId) - Number(a.id === hostId));
  const start = Math.max(0, sorted.findIndex(host => host.id === firstHost));
  const visible = sorted.slice(start, start + 3);
  const hostIds = visible.map(host => host.id).join(',');
  const matrix = useResource(hostIds ? '/api/manage/shared-resource-matrix?' + new URLSearchParams({
    host_ids: hostIds, kind, q: search, offset: String(offset)
  }) : null, revision);

  function selectCell(host, asset, checked) {
    const key = cellKey(host.id, asset.id);
    setSelection(prev => checked ? [...prev.filter(entry => entry.key !== key), { key, host: host.id, asset: asset.id, name: asset.name, version: asset.version }] : prev.filter(entry => entry.key !== key));
  }

  async function preview() {
    if (lock.current || !selection.length) return;
    lock.current = true; setBusy(true); setError(null); setSubmitted(false); setReviewed(false);
    try {
      const prepared = [];
      const targets = new Set();
      for (const host of new Set(selection.map(entry => entry.host))) {
        if (!mounted.current) return;
        const cells = selection.filter(entry => entry.host === host);
        const plan = await mutate('/api/manage/plans', 'POST', { host_id: host, operation, asset_ids: cells.map(cell => cell.asset) });
        if (plan?.status !== 'planned' || plan.payload?.metadata?.host_id !== host || cells.some(cell => plan.payload.metadata.entity_versions?.[cell.asset] !== cell.version)) {
          throw new Error(t('资源已变更，请刷新并重新选择。'));
        }
        for (const file of plan.payload.files) {
          if (targets.has(file.path)) throw new Error(t('所选助手共用同一目标文件。请选择其中一个入口应用，再核对另一个入口的状态。'));
          targets.add(file.path);
        }
        prepared.push({ host, plan, cells, receipt: null });
      }
      if (mounted.current) setPlans(prepared);
    } catch (failure) { if (mounted.current) setError(failure); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }

  async function apply() {
    if (lock.current || !reviewed || submitted || !plans.length) return;
    lock.current = true; setBusy(true); setSubmitted(true); setError(null);
    try {
      for (const row of plans) {
        if (!mounted.current) break;
        let failure;
        try { await mutate('/api/manage/plans/' + encodeURIComponent(row.plan.id) + '/apply', 'POST', { expected_version: row.plan.version, confirmed: true }); }
        catch (error) { failure = error; }
        const receipt = await request('/api/manage/plans/' + encodeURIComponent(row.plan.id));
        if (receipt.id !== row.plan.id || receipt.payload?.metadata?.host_id !== row.host) throw new Error(t('更改回执与所选助手不一致。'));
        if (mounted.current) setPlans(previous => previous.map(entry => entry.host === row.host ? { ...entry, receipt } : entry));
        if (failure || receipt.status !== 'applied') throw failure || new Error(t('此助手尚未完成应用，后续助手已暂停。'));
      }
      if (mounted.current) setSelection([]);
    } catch (failure) { if (mounted.current) setError(failure); }
    finally {
      lock.current = false;
      if (mounted.current) { setBusy(false); matrix.reload(); onChanged?.(); }
    }
  }

  function close() { if (!busy) { setPlans([]); setError(null); if (submitted) setSelection([]); } }
  const names = Object.fromEntries(allHosts.map(host => [host.id, host.name]));
  return <section className="management-panel resource-matrix">
    <h2>{t('共享资源')}</h2>
    <div className="toolbar-actions">{[['mcp', 'MCP'], ['skill', 'Skills'], ['prompt', t('使用规则')]].map(([id, label]) =>
      <Button key={id} aria-pressed={kind === id} onClick={() => { setKind(id); setOffset(0); }}>{label}</Button>)}</div>
    <div className="matrix-controls">
      <Field label={t('搜索共享资料')}><input type="search" maxLength={200} value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></Field>
      <Field label={t('查看助手列，从此处开始')}><select value={visible[0]?.id || ''} onChange={e => setFirstHost(e.target.value)}>{sorted.map(host => <option key={host.id} value={host.id}>{host.name}</option>)}</select></Field>
      <Field label={t('选中项的操作')}><select disabled={busy} value={operation} onChange={e => { setOperation(e.target.value); setSelection([]); }}><option value="install">{t('安装或更新')}</option><option value="uninstall">{t('移除受管资源')}</option></select></Field>
    </div>
    <ResourceState resource={matrix}>
      {list(matrix.data).length ? <table className="resource-grid"><thead><tr><th scope="col">{t('共享资源')}</th>{visible.map(host => <th key={host.id} scope="col">{host.name}</th>)}</tr></thead>
        <tbody>{list(matrix.data).map(item => <tr key={item.id}><th scope="row"><details><summary>{item.name}</summary>
          <small>{t('版本')} {item.version}{item.source?.type ? ' · ' + item.source.type : ''}</small>
          {item.source?.url || item.source?.path ? <small>{item.source.url || item.source.path}</small> : null}
          {item.kind === 'skill' ? <small>{item.install_mode === 'symlink' ? t('共享版本链接') : t('独立副本（推荐）')}</small> : null}
          <AssetTools item={item} selectedAgent={t('共享资源')} isCurrent={() => mounted.current} onChanged={() => { matrix.reload(); onChanged?.(); }} />
        </details></th>
          {visible.map(host => {
            const status = item.cells?.[host.id]?.status || 'not_checked';
            const supported = Boolean(host.capabilities?.[capability[kind]]);
            const reviewRequired = mcpNeedsReview(item);
            const rootBlocked = host.config_root?.status === 'blocked';
            const label = rootBlocked ? t('配置目录需要核对') : !supported ? t('不支持') : reviewRequired && operation === 'install' ? t('请先核对工具定义') : configuredLabel(status);
            const disabled = rootBlocked || !supported || (reviewRequired && operation === 'install') || ['externally_changed', 'unreadable', 'not_checked', 'native_read_only', 'native_source_changed', 'native_source_missing'].includes(status) || (operation === 'uninstall' && ['not_managed', 'removed', 'native_available'].includes(status));
            return <td key={host.id}><span className="matrix-mobile-host">{host.name}</span><label>
              <input type="checkbox" disabled={busy || disabled} aria-label={t('{resource} · {agent} · {status}', { resource: item.name, agent: host.name, status: label })}
                checked={selection.some(entry => entry.key === cellKey(host.id, item.id))} onChange={e => selectCell(host, item, e.target.checked)} />
              <span>{label}{item.cells?.[host.id]?.applied_version != null ? <small>{t('文件版本 {version}', { version: item.cells[host.id].applied_version })}</small> : null}</span>
            </label></td>;
          })}</tr>)}</tbody></table> : <EmptyState title={t('没有匹配的资料')} />}
    </ResourceState>
    <ResourcePagination label={t('共享资料分页')} resource={matrix} onPage={setOffset} />
    {!plans.length ? <ErrorNotice error={error} /> : null}
    <div className="toolbar-actions"><Button variant="primary" busy={busy && !plans.length} disabled={!selection.length} onClick={preview}>{t('预览所选更改（{count}）', { count: selection.length })}</Button>
      {selection.length ? <Button disabled={busy} onClick={() => setSelection([])}>{t('清空选择')}</Button> : null}</div>
    {plans.length ? <Modal wide title={t('共享资源更改')} busy={busy} onClose={close}>
      <p>{t('每个助手分别写入并核对文件。出现冲突时会暂停后续更改。')}</p>
      {plans.map(({ host, plan, cells, receipt }) => <section className="matrix-receipt" key={host}><h3>{names[host]} · {cells.map(cell => cell.name).join(', ')}</h3>
        <StatusBadge value={receipt?.status || plan.status} />
        <details><summary>{t('查看文件差异')}</summary>{plan.payload.files.map(file => <div key={file.path}><code>{file.path}</code><pre>{safeEvidence(file.diff)}</pre></div>)}</details>
      </section>)}
      <ErrorNotice error={error} />
      {!submitted ? <label className="inline-checkbox"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />{t('已核对所选助手、资源和文件差异')}</label> : null}
      <footer className="modal-actions"><Button disabled={busy} onClick={close}>{t('关闭')}</Button>{!submitted ? <Button variant="primary" disabled={!reviewed} busy={busy} onClick={apply}>{t('确认应用设置')}</Button> : null}</footer>
    </Modal> : null}
  </section>;
}
