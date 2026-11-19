import { useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { list, mcpNeedsReview } from '../../lib/management.js';
import { Button, Field } from '../ui.jsx';
import { useResource } from '../../lib/useResource.js';
import { CatalogChoice, catalogPath } from './CatalogControls.jsx';

export function McpAccountFields({ draft, update, revision }) {
  const [adding, setAdding] = useState(false);
  const [account, setAccount] = useState('');
  const [name, setName] = useState('');
  const [bearer, setBearer] = useState(false);
  const review = mcpNeedsReview(draft);
  const location = (draft.config?.transport || 'stdio') === 'stdio' ? 'env' : 'headers';
  const bound = draft.mcp_account_bindings || [];
  const boundIds = [...new Set(bound.map(item => item.account_id))];
  const accounts = useResource(boundIds.length ? catalogPath('accounts', { scope: 'all', ids: boundIds }) : null, revision);
  const accountNames = new Map(list(accounts.data).map(item => [item.id, item.name]));
  const omitted = Array.isArray(draft.mcp_fields_to_review) ? draft.mcp_fields_to_review : [];
  function start(field) {
    setAccount(''); setName(field?.name || (location === 'headers' ? 'Authorization' : ''));
    setBearer(field?.bearer ?? location === 'headers'); setAdding(true);
  }
  function attach() {
    if (!name.trim() || !account) return;
    update({ mcp_account_bindings: [...bound, { name: name.trim(), account_id: account, location, bearer }], mcp_reviewed: false });
    setAdding(false);
  }
  return <div className="mcp-account-fields">
    {review ? <p role="status">{t('来源省略了部分凭据或环境设置。请核对目标程序或网址，补齐后确认。')}</p> : null}
    {omitted.filter(field => field?.location === location && typeof field.name === 'string' && !bound.some(item => item.name === field.name)).map(field =>
      <Button key={field.name} onClick={() => start(field)}>{t('为 {field} 绑定账号', { field: field.name })}</Button>)}
    {bound.map((item, index) => <p key={item.location + item.name}>{item.name} · {item.location === 'env' ? t('环境变量') : t('请求头')} · {accountNames.get(item.account_id) || item.account_id}
      <Button onClick={() => update({ mcp_account_bindings: bound.filter((_, i) => i !== index), mcp_reviewed: false })}>{t('移除绑定')}</Button></p>)}
    {adding ? <div className="management-form">
      <p className="management-explainer">{t('可选已设置密钥的本机账号。新账号可在“更多设置与共享资料库”的“账号授权”中添加。')}</p>
      <CatalogChoice collection="accounts" scope="all" revision={revision} label={t('本机 API Key 账号')} value={account} onChange={setAccount}
        eligible={item => item.kind === 'api_key' && item.has_credential} />
      <Field label={location === 'env' ? t('环境变量名称') : t('请求头名称')}><input value={name} onChange={e => setName(e.target.value)} /></Field>
      <label className="inline-checkbox"><input type="checkbox" checked={bearer} onChange={e => setBearer(e.target.checked)} />{t('在密钥前加 Bearer')}</label>
      <div className="toolbar-actions"><Button disabled={!account || !name.trim()} onClick={attach}>{t('加入字段绑定')}</Button><Button onClick={() => setAdding(false)}>{t('取消')}</Button></div>
    </div> : <Button onClick={() => start()}>{t('绑定工具所用账号')}</Button>}
    {review ? <label className="inline-checkbox"><input type="checkbox" checked={draft.mcp_reviewed === true} onChange={e => update({ mcp_reviewed: e.target.checked })} />{t('已核对工具连接、环境变量和请求头，包括无需凭据的情况')}</label> : null}
  </div>;
}
