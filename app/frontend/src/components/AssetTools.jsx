import { useRef, useState } from 'react';
import { History, ShieldCheck } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { list, safeEvidence, mcpNeedsReview } from '../lib/management.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { DataTable, EvidenceDetails, Facts, StatusBadge } from './ManagementUI.jsx';
import { ResourcePagination } from './agent-management/ResourcePagination.jsx';

export function SkillInstallMode({ value, onChange }) {
  return <div className="agent-skill-mode"><p><strong>{value === 'symlink' ? t('共享版本链接') : t('独立副本（推荐）')}</strong></p><p>{t('独立副本不会跟着原始来源改变；安装不会运行技能中的脚本。')}</p><details className="agent-advanced" open={value === 'symlink' || undefined}><summary>{t('高级：共享版本链接')}</summary><p>{t('共享版本链接指向续境校验过的版本库，不指向原始目录。更新仍需新的预览与确认；旧版本保留用于恢复，卸载不删除版本库。')}</p><Field label={t('技能安装方式')}><select value={value || 'copy'} onChange={event => onChange(event.target.value)}><option value="copy">{t('独立副本（推荐）')}</option><option value="symlink">{t('共享版本链接')}</option></select></Field></details></div>;
}

export function AssetTools({ item, onChanged, selectedAgent, isCurrent = () => true }) {
  const [mode, setMode] = useState(null);
  const [selected, setSelected] = useState(null);
  const [versionOffset, setVersionOffset] = useState(0);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const lock = useRef(false);
  const detail = useResource(mode && item._summary ? `/api/manage/assets/${encodeURIComponent(item.id)}` : null, item.version);
  const full = item._summary ? detail.data : item;
  const detailMatches = !item._summary || (full?.id === item.id && full.kind === item.kind && !full._summary);
  const detailError = detail.error || (full && !detailMatches ? new Error(t('资料详情与当前选择不一致，请重新读取列表。')) : null);
  const versions = useResource(mode === 'versions' ? `/api/manage/assets/${encodeURIComponent(item.id)}/versions?summary=true&limit=20&offset=${versionOffset}` : null);
  const revision = useResource(mode === 'versions' && selected && !selected.data ? `/api/manage/assets/${encodeURIComponent(item.id)}/versions/${selected.version}` : null);
  const prior = selected?.data ? selected : revision.data;
  const blockedProbe = mode === 'probe' && (mcpNeedsReview(item) || mcpNeedsReview(full));
  const canRestore = selected && prior?.version === selected.version && prior.data?.kind === item.kind && !revision.loading && !revision.error && selected.version !== full?.version;
  function open(value) { setMode(value); setSelected(null); setVersionOffset(0); setResult(null); setError(null); }
  async function run() {
    if (lock.current || !isCurrent() || !full || detail.loading || detailError || !detailMatches || blockedProbe || (mode === 'versions' && !canRestore)) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const body = mode === 'probe' ? { confirmed: true, expected_version: full.version } : { version: selected.version, expected_version: full.version };
      const output = await mutate(`/api/manage/assets/${encodeURIComponent(item.id)}/${mode === 'probe' ? 'probe' : 'restore'}`, 'POST', body);
      if (isCurrent()) { setResult(output); onChanged(); }
    } catch (failure) { if (isCurrent()) setError(failure); } finally { lock.current = false; setBusy(false); }
  }
  const title = mode === 'probe' ? t('验证工具发现') : t('版本与恢复');
  const probeFacts = [[t('名称'), full?.name], [t('程序名称或路径'), full?.config?.command], [t('启动参数'), (full?.config?.args || []).join(' ')], [t('服务网址'), full?.config?.url]].map(([label, value]) => [label, safeEvidence(value)]);
  return <>
    <div className="toolbar-actions"><Button icon={History} onClick={() => open('versions')}>{t('版本与恢复')}</Button>{item.kind === 'mcp' ? <Button icon={ShieldCheck} disabled={mcpNeedsReview(item)} title={mcpNeedsReview(item) ? t('请先核对工具定义') : undefined} onClick={() => open('probe')}>{t('验证工具发现')}</Button> : null}</div>
    {mode ? <Modal wide title={`${selectedAgent ? `${selectedAgent} · ` : ''}${item.name} · ${title}`} busy={busy} onClose={() => setMode(null)}>
      {mode === 'probe' ? <>
        <p className="modal-copy">{t('确认后将连接 HTTP / SSE 服务，或启动下方 command / args 指定的本地进程，完成 MCP 初始化并读取工具清单。不会执行工具；工具发现与宿主加载分别验证。')}</p>
        {blockedProbe && <p role="status">{t('请先核对工具定义')}</p>}
        <ResourceState resource={detail}><Facts values={probeFacts} /><EvidenceDetails value={{ name: full?.name, config: full?.config }} title={t('将连接的配置')} /></ResourceState>
      </> : <>
        <p className="modal-copy">{t('选择旧版以检查恢复差异。恢复只更新资产定义，宿主文件仍需新的应用计划。')}</p>
        <p className="modal-copy">{t('恢复的是共享资料库定义，不会自动更改任何助手；其他引用也会看到恢复后的定义。')}</p>
        <ResourcePagination label={t('资料版本分页')} resource={versions} busy={busy} onPage={offset => { setVersionOffset(offset); setSelected(null); }} /><ResourceState resource={versions}><DataTable rows={list(versions.data)} keyName="version" onRow={setSelected} columns={[{ key: 'version', label: t('版本') }, { key: 'created_at', label: t('创建时间') }]} /></ResourceState>
        {selected ? <><p className="modal-copy">{t('将共享定义从版本 {before} 恢复为版本 {after} 的内容。', { before: full?.version, after: selected.version })}</p><ResourceState resource={revision}>{prior ? <EvidenceDetails value={{ before: full, after: prior.data }} title={t('变更前后')} /> : null}</ResourceState></> : null}
      </>}
      {result ? <><StatusBadge value={result.status || 'configured'} /><EvidenceDetails value={result} title={t('操作回读结果')} /></> : null}
      <ErrorNotice error={error || detailError || revision.error} />
      <footer className="modal-actions"><Button onClick={() => setMode(null)}>{t('关闭')}</Button><Button variant="primary" busy={busy} disabled={!!result || !full || detail.loading || Boolean(detailError) || !detailMatches || blockedProbe || (mode === 'versions' && !canRestore)} onClick={run}>{mode === 'probe' ? t('确认连接并发现工具') : t('确认恢复此版本')}</Button></footer>
    </Modal> : null}
  </>;
}
