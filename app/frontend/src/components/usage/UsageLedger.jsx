import { useState } from 'react';
import { ChevronRight, Download } from 'lucide-react';
import { t } from '../../lib/i18n.jsx';
import { list, number, safeEvidence } from '../../lib/management.js';
import { amountText, costText, originName, tokenCount, usagePath } from '../../lib/usageView.js';
import { useResource, queryString } from '../../lib/useResource.js';
import { Button, Modal, ResourceState, formatTime } from '../ui.jsx';
import { EvidenceDetails, Facts, StatusBadge } from '../ManagementUI.jsx';

function Pager({ offset, total = 0, limit, onPage, loading }) {
  return <div className="management-pagination"><span>{t('共 {count} 条', { count: number(total) })}</span>
    {(total > limit || offset > 0) && <div className="toolbar-actions">
      <Button disabled={loading || !offset} onClick={() => onPage(Math.max(0, offset - limit))}>{t('上一页')}</Button>
      <Button disabled={loading || offset + limit >= total} onClick={() => onPage(offset + limit)}>{t('下一页')}</Button>
    </div>}
  </div>;
}

export function UsageLedger({ params, revision, hostLabel, onSelect }) {
  const [offset, setOffset] = useState(0);
  const limit = 25;
  const resource = useResource(usagePath('logical-requests', params, { limit, offset }), revision);
  const rows = list(resource.data);
  return <section className="management-panel usage-ledger">
    <header><div><h2>{t('请求账本')}</h2><p>{t('每行是一条请求，展开可以查看重试和备用线路。')}</p></div>
      <a className="button" href={usagePath('export', params)} download="senrelay-usage.csv"><Download size={15} aria-hidden="true" />{t('导出尝试明细')}</a>
    </header>
    <ResourceState resource={resource}>{rows.length ? <ul className="usage-request-list">{rows.map(row =>
      <li key={row.logical_request_id}><button type="button" className="usage-request-row" onClick={() => onSelect(row.logical_request_id, row.unattributed_sample === true)}>
        <span className="usage-cell"><strong>{row.model || t('模型未知')}</strong><small>{hostLabel(row.host_id)} · {formatTime(row.started_at)}</small></span>
        <span className="usage-cell">{row.unattributed_sample ? <><strong>{t('用量快照')}</strong><small>{t('无法归属到具体请求')}</small></> : <><StatusBadge value={row.request_status || row.status} /><small>{t('{count} 次匹配尝试', { count: number(row.attempts) })}</small></>}</span>
        <span className="usage-cell usage-amount"><strong>{costText(row.cost_by_currency)}</strong><small>{number(tokenCount(row))} Token</small>{row.unknown_cost_count > 0 && Object.keys(row.cost_by_currency || {}).length > 0 && <small>{t('费用部分已知')}</small>}</span>
        <ChevronRight size={16} aria-hidden="true" />
      </button></li>)}</ul> : <p className="table-empty">{t('当前条件下没有请求记录。')}</p>}
      <Pager offset={offset} limit={limit} total={resource.data?.total} loading={resource.loading} onPage={setOffset} />
    </ResourceState>
  </section>;
}

const costStatuses = {
  estimated: '按定价估算', unpriced: '尚无定价', incomplete_usage: '用量不完整', incomplete_cache_usage: '缓存用量缺失', incomplete_price: '定价不完整',
};

function timingText(performance, metric, unit) {
  const value = performance[metric];
  if (value == null) return t('未知');
  const source = performance.measurement_by_metric?.[metric] === 'stream' ? t('路由实测') : t('来源报告');
  return number(value, { maximumFractionDigits: 1 }) + ' ' + unit + ' · ' + source;
}

function Attempt({ attempt, index, sample }) {
  const cost = attempt.cost || {};
  const performance = attempt.performance || {};
  const costLabel = costStatuses[cost.status];
  return <article className="usage-attempt"><header><div><strong>{sample ? t('用量快照') : t('尝试 {index}', { index: number(attempt.attempt_number || index) })}</strong> · {attempt.provider_name || attempt.provider_id || t('未归属')}</div>{!sample && <StatusBadge value={attempt.status} />}</header>
    <p>{formatTime(attempt.started_at)} · {attempt.origins?.length ? attempt.origins.map(originName).join(' / ') : t('未知')}</p>
    {attempt.error_code && <p className="usage-error">{t('失败原因')} · {safeEvidence(attempt.error_code)}</p>}
    <Facts values={[
      [t('请求模型'), attempt.requested_model || attempt.model], [t('转发模型'), attempt.model], [t('实际模型'), attempt.actual_model], [t('计费模型'), attempt.billing_model || attempt.model],
      [t('账号'), attempt.account_name || attempt.account_id || (attempt.credential_source === 'provider' ? t('服务商密钥') : t('未归属'))], [t('路由版本'), attempt.route_version],
      [t('输入 Token'), number(attempt.usage?.input_tokens)], [t('输出 Token'), number(attempt.usage?.output_tokens)],
      [t('缓存读取 / 创建'), number(attempt.usage?.cache_read_tokens) + ' / ' + number(attempt.usage?.cache_write_tokens)],
      [t('估算费用'), cost.amount == null ? t('未知') : cost.currency + ' ' + amountText(cost.amount)], [t('计费依据'), costLabel ? t(costLabel) : t('未知')],
      [t('首字延迟'), timingText(performance, 'ttft_ms', 'ms')],
      [t('端到端耗时'), timingText(performance, 'end_to_end_ms', 'ms')],
      [t('输出速度'), timingText(performance, 'output_tokens_per_second', 'tok/s')],
    ]} />
    <EvidenceDetails value={attempt} title={t('尝试与观察证据')} />
  </article>;
}

export function RequestDetails({ requestId, revision, sampleHint = false, onClose }) {
  const [offset, setOffset] = useState(0);
  const limit = 20;
  const resource = useResource('/api/usage/attempts' + queryString({ logical_request_id: requestId, limit, offset }), revision);
  const sample = sampleHint || list(resource.data).some(attempt => attempt.unattributed_sample === true);
  return <Modal wide title={sample ? t('用量快照') : t('请求详情')} onClose={onClose}>
    <p className="usage-detail-ref">{sample ? t('快照编号') : t('逻辑请求 ID')} · {requestId}</p>
    <p className="management-explainer">{sample ? t('原生日志没有提供可核对的请求编号。') : t('按实际发生顺序显示此请求的全部尝试，包含筛选范围外的记录。')}</p>
    <ResourceState resource={resource}>{list(resource.data).map((attempt, index) => <Attempt key={attempt.id} attempt={attempt} index={offset + index + 1} sample={sample} />)}
      {resource.data && !list(resource.data).length && <p className="table-empty">{t('当前条件下没有请求记录。')}</p>}
      <Pager offset={offset} limit={limit} total={resource.data?.total} loading={resource.loading} onPage={setOffset} />
    </ResourceState>
  </Modal>;
}
