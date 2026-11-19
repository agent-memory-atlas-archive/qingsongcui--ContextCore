import { useState } from 'react';
import { Plus } from 'lucide-react';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { useResource } from '../../lib/useResource.js';
import { list } from '../../lib/management.js';
import { Button, ErrorNotice, Field, Modal, ResourceState, formatTime } from '../ui.jsx';
import './pricing.css';

const rateLabels = {
  input_per_million: '每百万新增输入', output_per_million: '每百万输出',
  cache_read_per_million: '每百万缓存读取', cache_write_per_million: '每百万缓存创建',
};

function localTime() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function amounts(values) {
  return Object.entries(values).map(([currency, amount]) => currency + ' ' + amount).join(' · ') || t('未知');
}

function PriceEditor({ providers, onClose, onSave }) {
  const observed = useResource('/api/usage/filter-options?scope=all');
  const providerNames = new Map(list(observed.data?.provider_id).filter(item => item.value).map(item => [item.value, item.label || item.value]));
  for (const provider of list(providers.data)) providerNames.set(provider.id, provider.name);
  const [form, setForm] = useState({ provider_id: '', model: '', currency: 'USD',
    effective_from: localTime(), effective_to: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const update = (key, value) => setForm(current => ({ ...current, [key]: value }));
  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const body = { ...form, model: form.model.trim(), effective_from: new Date(form.effective_from).toISOString() };
      if (form.effective_to) body.effective_to = new Date(form.effective_to).toISOString();
      else delete body.effective_to;
      for (const key of Object.keys(rateLabels)) if (!body[key] && body[key] !== '0') delete body[key];
      await mutate('/api/usage/prices', 'POST', body);
      onSave();
    } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Modal title={t('添加定价')} busy={busy} onClose={onClose}>
    <form className="management-form" onSubmit={save}>
      <ResourceState resource={providers}><Field label={t('供应商')}><select required value={form.provider_id} onChange={e => update('provider_id', e.target.value)}>
        <option value="">{t('请选择')}</option>{Array.from(providerNames, ([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select></Field></ResourceState>
      <ResourceState resource={observed}><datalist id="price-observed-models">{list(observed.data?.model).filter(item => item.value).map(item => <option key={item.value} value={item.value} />)}</datalist></ResourceState>
      <div className="form-columns">
        <Field label={t('计费模型')}><input required list="price-observed-models" value={form.model} onChange={e => update('model', e.target.value)} /></Field>
        <Field label={t('币种')}><input required maxLength={3} pattern="[A-Z]{3}" value={form.currency} onChange={e => update('currency', e.target.value.toUpperCase())} /></Field>
        <Field label={t('生效时间')}><input required type="datetime-local" value={form.effective_from} onChange={e => update('effective_from', e.target.value)} /></Field>
        <Field label={t('失效时间（可选）')}><input type="datetime-local" min={form.effective_from} value={form.effective_to} onChange={e => update('effective_to', e.target.value)} /></Field>
        {Object.entries(rateLabels).map(([key, label]) => <Field key={key} label={t(label)}>
          <input type="number" step="any" min="0" value={form[key] || ''} onChange={e => update(key, e.target.value)} />
        </Field>)}
      </div>
      <p>{t('输入和输出留空表示未知。缓存未单独定价时沿用输入价。')}</p>
      <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={onClose}>{t('取消')}</Button>
        <Button type="submit" variant="primary" busy={busy} disabled={!form.provider_id || !form.model.trim() || !Object.keys(rateLabels).some(key => form[key])}>{t('保存定价版本')}</Button>
      </footer>
    </form>
  </Modal>;
}

export default function PricingPanel({ onChanged }) {
  const prices = useResource('/api/usage/prices');
  const providers = useResource('/api/manage/providers');
  const [editing, setEditing] = useState(false);
  const [review, setReview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const names = new Map(list(providers.data).map(provider => [provider.id, provider.name]));
  async function recalculate(price, confirmation) {
    if (busy) return;
    setError(null); setBusy(true); setReceipt(null);
    try {
      const base = '/api/usage/prices/' + encodeURIComponent(price.id);
      if (!confirmation) {
        const result = await mutate(base + '/preview', 'POST', {});
        setReview({ price, ...result });
      } else {
        const result = await mutate(base + '/apply', 'POST', { confirmed: true, fingerprint: confirmation });
        setReview(null); setReceipt(result.count); onChanged?.();
      }
    } catch (failure) {
      if (failure.status === 409) {
        setReview(null);
        setError(new Error('账本已变化，请重新预览历史重算。'));
      } else {
        setError(failure);
      }
    } finally { setBusy(false); }
  }
  return <section className="management-panel">
    <header><div><h2>{t('定价版本')}</h2><p>{t('手动维护每百万 Token 单价。修改历史估算前可以核对影响。')}</p></div>
      <Button icon={Plus} onClick={() => { setEditing(true); setError(null); }}>{t('添加定价')}</Button></header>
    {!review && <ErrorNotice error={error} />}{receipt !== null && <p role="status">{t('已更新 {count} 条历史估算。', { count: receipt })}</p>}
    <ResourceState resource={prices}>
      {list(prices.data).length === 0 && <p>{t('尚无定价版本。')}</p>}
      {list(prices.data).map(price => <div className="price-catalog-row" key={price.id}>
        <div><strong>{names.get(price.provider_id) || price.provider_id} · {price.model}</strong>
          <small>{formatTime(price.effective_from)}{price.effective_to && ' — ' + formatTime(price.effective_to)} · {t('手动定价')}</small>
          <dl>{Object.entries(rateLabels).map(([key, label]) => <div key={key}><dt>{t(label)}</dt><dd>{price[key] == null ? '—' : price.currency + ' ' + price[key]}</dd></div>)}</dl>
        </div><Button disabled={busy} onClick={() => recalculate(price)}>{t('预览历史重算')}</Button>
      </div>)}
    </ResourceState>
    {editing && <PriceEditor providers={providers} onClose={() => setEditing(false)} onSave={() => { setEditing(false); prices.reload(); }} />}
    {review && <Modal title={t('核对历史费用估算')} busy={busy} onClose={() => setReview(null)}>
      <p><strong>{names.get(review.price.provider_id) || review.price.provider_id} · {review.price.model}</strong></p>
      <p>{t('重算涵盖此价格生效期间的全部匹配尝试。')} {formatTime(review.price.effective_from)} — {review.price.effective_to ? formatTime(review.price.effective_to) : t('无结束日期')}</p>
      <p>{review.count ? t('将更新 {count} 次尝试的定价快照。', { count: review.count }) : t('当前没有需要更新的估算。')}</p>
      {review.count > 0 && <dl className="price-totals">
        <dt>{t('当前估算')}</dt><dd>{amounts(review.before)} · {t('{count} 项费用未知', { count: review.before_unknown })}</dd>
        <dt>{t('重算后')}</dt><dd>{amounts(review.after)} · {t('{count} 项费用未知', { count: review.after_unknown })}</dd>
      </dl>}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setReview(null)}>{t('关闭')}</Button>
        <Button variant="primary" busy={busy} disabled={!review.count} onClick={() => recalculate(review.price, review.fingerprint)}>{t('更新历史估算')}</Button></footer>
    </Modal>}
  </section>;
}
