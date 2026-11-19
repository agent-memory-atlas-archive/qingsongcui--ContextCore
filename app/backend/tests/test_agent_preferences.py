import pytest

from app.integrations.config_transactions import ConfigurationError
from app.services import agent_preferences
from tests.test_management_experience import manager


def test_shortcuts_are_device_local_and_rank_before_catalog_paging(manager):
    assert agent_preferences.read(manager)['favorite_hosts'] == ['claude-code', 'codex', 'opencode']
    agent_preferences.update(manager, 'codex', 'unpin')
    agent_preferences.update(manager, 'omp', 'pin')
    for host in ['codex', 'omp', 'codex']:
        prefs = agent_preferences.update(manager, host, 'visit')
    assert prefs['recent_hosts'] == ['codex', 'omp']
    assert prefs['favorite_hosts'] == ['claude-code', 'opencode', 'omp']
    p = [manager.save('provider', {'name': str(i), 'protocol': 'openai', 'base_url': 'https://fixture.example/v1',
                                   'agent_ids': ['codex', 'omp']}) for i in range(5)]
    for i in (4, 3):
        agent_preferences.update(manager, 'codex', 'pin', p[i]['id'])
    assert manager.catalog_page('provider', agent_id='codex', limit=1)['items'][0]['id'] == p[4]['id']
    assert manager.catalog_page('provider', agent_id='omp', limit=1)['items'][0]['id'] == p[0]['id']
    agent_preferences.update(manager, 'codex', 'before', p[3]['id'], p[4]['id'])
    assert manager.catalog_page('provider', agent_id='codex', limit=1)['items'][0]['id'] == p[3]['id']
    with pytest.raises(ConfigurationError):
        agent_preferences.update(manager, 'codex', 'after', p[3]['id'], p[1]['id'])


def test_new_provider_can_explicitly_use_an_existing_account(manager):
    account = manager.save('account', {'name': 'Work', 'kind': 'api_key'})
    body = {'request_id': 'repeatable-account-setup', 'host_id': 'codex', 'model_id': 'model-a',
            'provider': {'name': 'Local', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai',
                         'account_id': account['id'], 'credential_source': 'account'}}
    assert manager.setup_provider(body)['provider']['account_id'] == account['id']
    assert manager.setup_provider(body)['provider']['credential_source'] == 'account'
    with pytest.raises(ConfigurationError):
        manager.setup_provider({**body, 'secret': 'test-credential'})
