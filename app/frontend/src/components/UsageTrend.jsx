import { useEffect, useRef, useState } from 'react';
import { t } from '../lib/i18n.jsx';
import { number } from '../lib/management.js';
import { amountText, tokenCount } from '../lib/usageView.js';
import { Field } from './ui.jsx';

function stamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const date = Date.parse(value + 'T00:00:00Z');
  return Number.isFinite(date) && new Date(date).toISOString().slice(0, 10) === value ? date : NaN;
}
export function trendDays(rows) {
  return rows.filter(row => Number.isFinite(stamp(row.date || row.day)))
    .map(row => ({ ...row, date: row.date || row.day, attempts: Number.isFinite(row.attempts) && row.attempts >= 0 ? row.attempts : null }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

export function UsageTrend({ rows, currencies = [] }) {
  const [metric, setMetric] = useState('requests');
  const [chosenCurrency, setChosenCurrency] = useState('');
  const currency = currencies.includes(chosenCurrency) ? chosenCurrency : currencies[0];
  const scroll = useRef(null);
  const days = trendDays(rows);
  const latest = days.at(-1)?.date;
  useEffect(() => { if (scroll.current) scroll.current.scrollLeft = scroll.current.scrollWidth; }, [latest]);
  const metrics = { requests: t('已记录请求'), tokens: t('已知 Token'), cost: t('估算费用') };
  function chartValue(day) {
    if (day.observed === false) return null;
    if (metric === 'tokens') return tokenCount(day);
    if (metric === 'cost') return day.cost_by_currency?.[currency] ?? null;
    return day.logical_requests ?? null;
  }
  const max = days.reduce((largest, day) => Math.max(largest, Number(chartValue(day)) || 0), 0) || 1;
  const label = metric === 'cost' && currency ? metrics[metric] + ' · ' + currency : metrics[metric];
  return <section className="management-panel usage-chart">
    <header><h2>{t('每日趋势')}</h2><div className="usage-chart-controls">
      <Field label={t('趋势指标')}><select value={metric} onChange={e => setMetric(e.target.value)}>{Object.entries(metrics).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></Field>
      {metric === 'cost' && currencies.length > 1 && <Field label={t('币种')}><select value={currency} onChange={e => setChosenCurrency(e.target.value)}>{currencies.map(ccy => <option key={ccy}>{ccy}</option>)}</select></Field>}
    </div></header>
    {!days.some(day => day.observed !== false) ? <p className="table-empty">{t('所选范围还没有可绘制的请求观察。')}</p> : <>
      <p className="usage-chart-toolbar"><strong>{days[0].date} — {latest}</strong><span>{label}</span></p>
      <div className="usage-chart-canvas"><div className="usage-chart-yaxis" aria-hidden="true">{[max, max / 2, 0].map((value, index) => <span key={index}>{metric === 'cost' ? amountText(value) : number(value, { notation: 'compact' })}</span>)}</div>
        <div className="usage-chart-scroll" ref={scroll} tabIndex={0} role="region" aria-label={t('请求趋势，左右滚动查看')}>
          <div className="usage-chart-columns" style={{ gridTemplateColumns: 'repeat(' + days.length + ', minmax(42px, 1fr))' }}>
            {days.map(day => {
              const value = chartValue(day);
              const description = day.date + ' · ' + label + ' · ' + (value == null ? t('未记录') : metric === 'cost' ? amountText(value) : number(value));
              return <div className="usage-chart-day" key={day.date} title={description} aria-label={description}><div className="usage-chart-track">{value == null ? <span className="usage-chart-gap" /> : <span className="usage-chart-column" style={{ height: (Number(value) / max * 100) + '%' }} />}</div><span className="usage-chart-date">{day.date.slice(5)}</span></div>;
            })}
          </div>
        </div>
      </div>
      <details><summary>{t('查看每日明细')}</summary><p className="management-explainer">{t('虚线表示缺少观测。每个请求计入首次匹配尝试当天，用量和费用计入实际尝试当天。')}</p>
        <table className="usage-daily"><thead><tr><th>{t('日期')}</th><th>{metrics[metric]}</th><th>{t('物理尝试')}</th></tr></thead><tbody>{days.map(day => <tr key={day.date}><td>{day.date}</td><td>{chartValue(day) == null ? t('未记录') : metric === 'cost' ? currency + ' ' + amountText(chartValue(day)) : number(chartValue(day))}</td><td>{number(day.attempts)}</td></tr>)}</tbody></table>
      </details>
    </>}
  </section>;
}
