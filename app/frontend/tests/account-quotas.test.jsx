// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ request: vi.fn(), mutate: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), ...api }));
import AccountQuotaPanel from '../src/components/AccountQuotaPanel.jsx';
import { setLang } from '../src/lib/i18n.jsx';

let root, host;
const native = { id: 'native-codex', name: 'Codex', source: 'codex', status: 'not_checked', revision: 'current',
  refreshable: true, dashboard: 'https://chatgpt.com/codex/settings/usage', snapshot: null };

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('en'); host = document.createElement('div'); root = createRoot(host);
  api.request.mockReset(); api.mutate.mockReset();
});
afterEach(async () => { await act(async () => root.unmount()); setLang('zh'); });

it('reads only cached state on entry, queries the selected source after a click and reads back the result', async () => {
  api.request.mockResolvedValueOnce({ items: [native] }).mockResolvedValue({ items: [{ ...native, status: 'available',
    last_success_at: '2026-11-19T00:00:00Z', snapshot: { identity_label: 'j…@example.test', plan: 'plus',
      windows: [{ label: 'codex', rank: 'primary', minutes: 300, used_percent: 25, resets_at: '2026-11-19T05:00:00Z' }], balances: [] },
  }] });
  await act(async () => root.render(<AccountQuotaPanel hostId="codex" />));
  expect(api.request.mock.calls[0][0]).toBe('/api/account-quotas?host_id=codex');
  expect(api.mutate).not.toHaveBeenCalled();
  expect(host.textContent).toContain('Quota has not been checked');
  await act(async () => host.querySelector('button').click());
  expect(api.mutate).toHaveBeenCalledExactlyOnceWith('/api/account-quotas/native-codex/refresh', 'POST', { confirmed: true, revision: 'current' });
  expect(host.textContent).toContain('j…@example.test');
  expect(host.textContent).toContain('5-hour window');
  expect(host.textContent).toContain('25% used');
  expect(host.querySelector('progress').value).toBe(25);
});

it('keeps separate currencies and timestamps when the last provider refresh failed', async () => {
  setLang('zh');
  api.request.mockResolvedValue({ items: [{ id: 'account_demo', source: 'deepseek', name: '实验账号', refreshable: true,
    revision: 'bound', status: 'rate_limited', last_success_at: '2026-11-17T01:00:00Z', last_attempt_at: '2026-11-19T01:00:00Z',
    snapshot: { windows: [], balances: [{ unit: 'CNY', amount: '3.25' }, { unit: 'USD', amount: '0' }] },
  }] });
  await act(async () => root.render(<AccountQuotaPanel accountId="account_demo" />));
  expect(api.request.mock.calls[0][0]).toBe('/api/account-quotas?account_id=account_demo');
  expect(host.textContent).toContain('查询受限');
  expect(host.textContent).toContain('最近成功查询');
  expect(host.textContent).toContain('最近尝试');
  expect(host.textContent).toContain('3.25 CNY');
  expect(host.textContent).toContain('0 USD');
  expect(api.mutate).not.toHaveBeenCalled();
});
