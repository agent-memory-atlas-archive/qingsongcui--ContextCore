import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { t } from '../../lib/i18n.jsx';
import { mutate, errorFeedback } from '../../lib/api.js';
import { list, number } from '../../lib/management.js';
import { originName } from '../../lib/usageView.js';
import { useResource } from '../../lib/useResource.js';
import { Button, ErrorNotice, ResourceState, formatTime } from '../ui.jsx';

export function UsageSources({ overview, revision, onChanged, navigate, hostLabel }) {
  const connections = useResource('/api/connections', revision);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  async function sync(item) {
    setError(null); setNotice(''); setBusy(item.id);
    try {
      const result = await mutate('/api/connections/' + encodeURIComponent(item.id) + '/sync', 'POST', {});
      if (result?.error) setError(new Error(errorFeedback(500, result.error)));
      else setNotice(t('{name} 已完成本次刷新。', { name: item.label }));
      onChanged();
    } catch (err) { setError(err); } finally { setBusy(''); }
  }
  return <>
    <section className="management-panel"><header><h2>{t('采集覆盖范围')}</h2></header><ResourceState resource={overview}>
      {overview.data && <><p className="management-explainer">{t('所选时间和筛选条件内，共观测 {attempts} 次尝试，其中 {metered} 次有完整用量。', { attempts: number(overview.data.attempts), metered: number(overview.data.coverage?.metered_attempts) })}</p>
      {list(overview.data.coverage?.hosts).map(row => <div key={row.host_id ?? 'unassigned'} className="usage-source-row"><div><strong>{hostLabel(row.host_id)}</strong><small>{row.origins?.map(originName).join(' / ') || t('未知')}</small></div>
        <div><span>{number(row.observed_attempts)} {t('次')}</span><small>{formatTime(row.first_observed_at)} — {formatTime(row.last_observed_at)}</small></div></div>)}
      </>}</ResourceState></section>
    <section className="management-panel"><header><div><h2>{t('原生日志采集')}</h2><p>{t('此处刷新对应助手已授权的聊天记录，并导入其中可读取的用量。')}</p></div></header>
      <ErrorNotice error={error} />{notice && <p role="status" className="operation-notice">{notice}</p>}
      <ResourceState resource={connections}>{list(connections.data).map(item => {
        const status = !item.enabled ? t('未授权') : item.paused ? t('采集已暂停') : item.error || item.discovery?.usage_sync?.status === 'partial' ? t('部分覆盖') : item.discovery?.usage_sync?.status === 'ok' ? t('已同步') : t('尚未验证');
        return <div key={item.id} className="usage-source-row"><div><strong>{item.label}</strong><small>{status} · {t('最近同步：')}{formatTime(item.last_sync)}</small></div><div className="toolbar-actions">
          {item.enabled && !item.paused && <Button icon={RefreshCw} busy={busy === item.id} disabled={!!busy} onClick={() => sync(item)}>{t('刷新采集')}</Button>}
          {navigate && <Button onClick={() => navigate('connections', {agent: item.id})}>{t('接入与权限')}</Button>}
        </div></div>;
      })}</ResourceState>
    </section>
  </>;
}
