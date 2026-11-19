import { useState } from 'react';
import { ExternalLink, RefreshCw } from 'lucide-react';
import { mutate } from '../lib/api.js';
import { locale, t } from '../lib/i18n.jsx';
import { queryString, useResource } from '../lib/useResource.js';
import { Button, ErrorNotice, ResourceState } from './ui.jsx';
import '../styles/account-quotas.css';

const statusText = {
  not_checked: '尚未查询额度', available: '已取得额度快照', native_only: '在宿主内查看',
  unauthorized: '需要有效的账号或密钥', expired: '账号授权已过期', rate_limited: '查询受限，请稍后重试',
  offline: '连接超时或服务暂不可用', unreadable: '服务未返回可识别的额度', unsupported: '该账号暂无可读取的额度接口',
  not_installed: '未找到 Codex CLI', disabled: '服务商已停用', identity_changed: '查询期间登录账号已切换，请重试',
  keychain_locked: '请解锁本机钥匙串后重试',
};
const adapters = {
  codex: 'Codex App Server', claude: 'Claude Code', deepseek: 'DeepSeek', openrouter: 'OpenRouter',
  moonshot: 'Kimi', anthropic: 'Anthropic', openai: 'OpenAI', siliconflow: 'SiliconFlow',
};

function windowName(window) {
  if (!window.minutes) return window.rank === 'secondary' ? t('第二个额度窗口') : t('主要额度窗口');
  if (window.minutes % 1440 === 0) return t('{days} 天窗口', { days: window.minutes / 1440 });
  if (window.minutes % 60 === 0) return t('{hours} 小时窗口', { hours: window.minutes / 60 });
  return t('{minutes} 分钟窗口', { minutes: window.minutes });
}

function stamp(value) {
  return new Intl.DateTimeFormat(locale(), { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}

function Snapshot({ data, source }) {
  if (!data) return null;
  return <div className="quota-snapshot">
    {data.identity_label || data.plan ? <p>{[data.identity_label, data.plan].filter(Boolean).join(' · ')}</p> : null}
    {(data.windows || []).map(window => <div key={window.label + window.rank} className="quota-window">
      <div><strong>{windowName(window)}</strong><span>{window.label}</span></div>
      <span>{t('已用 {percent}%', { percent: window.used_percent })}</span>
      <progress max="100" value={window.used_percent} aria-label={t('已用额度比例')} />
      {window.resets_at ? <small>{t('重置于 {time}', { time: stamp(window.resets_at) })}</small> : null}
    </div>)}
    {(data.balances || []).map((balance, i) => <p key={i} className="quota-balance"><span>{source === 'openrouter' && balance.scope !== 'account' ? t('密钥预算剩余') : balance.unit === 'credits' ? t('剩余点数') : t('账户可用余额')}</span> <strong>{balance.amount} {balance.unit === 'credits' ? t('点数') : balance.unit}</strong></p>)}
    {data.account_balance_status ? <p>{t('本次未读到账户余额。')}{t(statusText[data.account_balance_status] || statusText.unreadable)}</p> : null}
    {data.no_key_limit && !data.balances?.some(b => b.scope === 'account') ? <p>{t('此密钥未单设预算上限，可用余额请查账户账单。')}</p> : null}
    {data.ordinary_usage_allowed === false ? <p>{t('Codex 报告此账号当前无法使用订阅内额度。')}</p> : null}
    {data.credits_unlimited ? <p>{t('服务端报告此账号点数不限。')}</p> : null}
  </div>;
}

function QuotaCard({ item, onRefresh }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function refresh() {
    setBusy(true); setError(null);
    try {
      await mutate('/api/account-quotas/' + encodeURIComponent(item.id) + '/refresh', 'POST', {
        confirmed: true, revision: item.revision,
      });
      onRefresh();
    } catch (e) {
      setError(e);
      if (e.status === 409) onRefresh();
    } finally {
      setBusy(false);
    }
  }
  const native = item.id === 'native-codex';
  const source = adapters[item.source] || t('自定义服务商');
  const provider = item.provider_name;
  const attribution = provider && provider !== item.name && provider !== source ? [provider, source].join(' · ') : source;
  return <article className="quota-card">
    <header><div><h3>{native ? t('Codex 原生登录账号') : item.name}</h3>
      <span>{attribution}</span></div>
      {item.refreshable ? <Button icon={RefreshCw} busy={busy} onClick={refresh}>{native ? t('读取 Codex 额度') : t('查询服务商额度')}</Button> : null}
    </header>
    <p className="quota-status" role="status">{t(statusText[item.status] || statusText.unreadable)}</p>
    {native && (item.status === 'not_checked' || item.status === 'unauthorized' || item.status === 'not_installed') ? <p>{t('使用本机 Codex CLI 中已登录的 ChatGPT 账号查询订阅窗口。')}</p> : null}
    {item.source === 'claude' ? <p>{t('在 Claude Code 会话中输入')} <code>/usage</code> {t('查看当前订阅窗口与重置时间。')}</p> : null}
    {item.refreshable && !native && item.status === 'not_checked' ? <p>{t('使用这份配置的密钥查询 {provider} 的官方额度接口。', { provider: adapters[item.source] })}</p> : null}
    {item.status === 'expired' ? <p>{t('在 Agent 管理的账号设置中重新授权。')}</p> : null}
    {item.status === 'unauthorized' && !native ? <p>{t('在 Agent 管理的模型与账号中检查绑定的服务商和密钥。')}</p> : null}
    {item.last_success_at ? <p className="quota-time">{t('最近成功查询 {time}', { time: stamp(item.last_success_at) })}</p> : null}
    <Snapshot data={item.snapshot} source={item.source} />
    {item.last_attempt_at && item.status !== 'available' ? <small>{t('最近尝试 {time}', { time: stamp(item.last_attempt_at) })}</small> : null}
    <ErrorNotice error={error} />
    {item.dashboard ? <a className="quota-official-link" href={item.dashboard} target="_blank" rel="noreferrer">{t('打开官方用量或账单页')}<ExternalLink size={13} aria-hidden="true" /></a> : null}
  </article>;
}

export default function AccountQuotaPanel({ hostId, accountId }) {
  const resource = useResource('/api/account-quotas' + queryString(accountId ? { account_id: accountId } : { host_id: hostId }));
  const items = resource.data?.items || [];
  return <section className="management-panel quota-panel" aria-label={t('账号额度')}>
    <header><div><h2>{t('账号额度')}</h2>
      <p>{t('额度来自账号或服务商。打开本页只显示上次查询，按需刷新。')}</p></div></header>
    <ResourceState resource={resource}>{items.length ? <div className="quota-cards">
      {items.map(item => <QuotaCard key={item.id + item.revision} item={item} onRefresh={resource.reload} />)}
    </div> : <p>{t('此范围还没有可查看的账号或服务商。')}</p>}</ResourceState>
  </section>;
}
