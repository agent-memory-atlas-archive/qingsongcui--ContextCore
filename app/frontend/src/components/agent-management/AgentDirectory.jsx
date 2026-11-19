import { useState } from 'react';
import { Star } from 'lucide-react';
import { t } from '../../lib/i18n.jsx';
import { Button, EmptyState, Field } from '../ui.jsx';

function discoveryLabel(host) {
  const discovery = host.discovery;
  if (!discovery) return t('本机状态待检查');
  const surfaceId = host.id === 'cursor' ? 'ide' : ['codex-desktop', 'antigravity'].includes(host.id) ? 'desktop' : 'cli';
  if (discovery.surfaces?.some(surface => surface.id === surfaceId && surface.installation === 'detected')) return t('已检测到安装');
  if (discovery.discovered || discovery.evidence?.some(entry => ['present', 'executable_present', 'executable_link_present'].includes(entry.status))) return t('检测到本机记录');
  return t('未检测到本机记录');
}

export function AgentDirectory({ hosts, nameOf, favorites, recent, onChoose, onPin, busy }) {
  const [query, setQuery] = useState('');
  const order = [...new Set([...favorites, ...recent])];
  const rank = id => order.includes(id) ? order.indexOf(id) : order.length;
  const entries = hosts.filter(host => (nameOf(host) + ' ' + host.id).toLowerCase().includes(query.toLowerCase().trim())).sort((a, b) => rank(a.id) - rank(b.id));
  return <>
    <Field label={t('查找助手')}><input type="search" value={query} onChange={event => setQuery(event.target.value)} maxLength={100} /></Field>
    <div className="agent-chooser">{entries.map(host => <div className="agent-directory-row" key={host.id}>
      <button className="agent-choice" onClick={() => onChoose(host.id)}>
        <strong className="agent-choice-name">{nameOf(host)}</strong><span>{discoveryLabel(host)}</span>
        {recent.includes(host.id) ? <span>{t('最近打开')}</span> : null}<span className="agent-choice-action">{t('管理这个助手')} →</span>
      </button>
      <Button icon={Star} aria-pressed={favorites.includes(host.id)} aria-label={t('常用助手 · {name}', { name: nameOf(host) })} disabled={busy} onClick={() => onPin(host.id, !favorites.includes(host.id))} />
    </div>)}</div>
    {!entries.length ? <EmptyState title={t('没有匹配的助手')} body={t('试试其他名称，或清空搜索。')} action={query ? <Button onClick={() => setQuery('')}>{t('清空搜索')}</Button> : null} /> : null}
  </>;
}
