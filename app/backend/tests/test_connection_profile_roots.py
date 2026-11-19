from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.database import create_schema, seed_data
from app.integrations.config_transactions import ConfigurationError
from app.integrations.installer import apply_host_integration, uninstall_host_integration
from app.routers import connections
from app.services.connection_plans import ConnectionPlans
from app.services.connection_service import get_connection
from app.services.management_service import ManagementService


@pytest.fixture
def client():
    create_schema()
    seed_data()
    api = FastAPI()
    api.include_router(connections.router)
    return TestClient(api, base_url='http://127.0.0.1:8420')


def plan_body(surface):
    return {'surface': surface, 'operation': 'install', 'capabilities': ['context_tools']}


@pytest.mark.parametrize(('host', 'variable', 'folder'), [
    ('codex', 'CODEX_HOME', '.alternate-codex'),
    ('claude-code', 'CLAUDE_CONFIG_DIR', '.claude'),
    ('claude-code', 'CLAUDE_CONFIG_DIR', '.alternate-claude'),
])
def test_custom_cli_never_connects_to_or_recovers_default_profile(client, monkeypatch, host, variable, folder):
    home = Path.home().resolve()
    prefix = f'/api/connections/{host}'
    monkeypatch.delenv(variable, raising=False)
    normal = client.post(prefix + '/plans', json=plan_body('cli'))
    assert normal.status_code == 200, normal.text
    plan = normal.json()
    kept = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
    original = get_connection(host)

    monkeypatch.setenv(variable, str(home / folder))
    setup = client.get(prefix + '/setup').json()
    assert 'cli' in setup['blocked_surfaces']
    assert setup['config_root']['path'] == str(home / folder)
    if host == 'claude-code':
        assert setup['plan_supported'] is False and not setup['instructions']
    for path, body in [
        ('/one-click-connect', {}),
        ('/one-click-disconnect', {}),
        ('/one-click-connect', {'surface': 'cli'}),
        ('/plans', plan_body('cli')),
        (f"/plans/{plan['id']}/apply", {'version': plan['version'], 'confirmed': True}),
        (f"/plans/{plan['id']}/recover", {'confirmed': True}),
    ]:
        response = client.post(prefix + path, json=body)
        assert response.status_code == 409, response.text
        assert variable in response.json()['detail']
    for operation in (
        lambda: apply_host_integration(host, confirmed=True, token='synthetic'),
        lambda: uninstall_host_integration(host, confirmed=True),
    ):
        with pytest.raises(ConfigurationError, match=variable):
            operation()
    assert get_connection(host)['enabled'] == original['enabled']
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()} == kept


def test_desktop_keeps_its_own_profile(client, monkeypatch):
    home = Path.home().resolve()
    custom = home / '.other'
    monkeypatch.setenv('CODEX_HOME', str(custom))
    response = client.post('/api/connections/codex/plans', json=plan_body('desktop'))
    assert response.status_code == 200, response.text
    plan = response.json()
    applied = client.post(f"/api/connections/codex/plans/{plan['id']}/apply",
                          json={'version': plan['version'], 'confirmed': True})
    assert applied.status_code == 200, applied.text
    assert (home / '.codex/config.toml').is_file()
    assert not custom.exists()


def test_explicit_sandbox_ignores_runtime_cli_root(client, monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_HOME', str(Path.home() / 'other'))
    sandbox = tmp_path / 'explicit'
    sandbox.mkdir()
    preview = ConnectionPlans(home=sandbox).create('codex', plan_body('cli'), 'http://127.0.0.1:8420')
    paths = [Path(f['path']) for f in preview['payload']['files']]
    assert sandbox / '.codex/config.toml' in paths
    assert all(p.is_relative_to(sandbox) for p in paths)


def test_connection_plan_cannot_use_generic_management_recovery(client):
    plan = client.post('/api/connections/codex/plans', json=plan_body('cli')).json()
    manager = ManagementService()
    for operation in (lambda: manager.apply(plan['id'], plan['version'], True),
                      lambda: manager.recover_plan(plan['id'], True)):
        with pytest.raises(ConfigurationError, match='connection plan'):
            operation()


def test_disconnect_removes_only_recorded_components_and_reports_conflicts(client, monkeypatch):
    monkeypatch.delenv('CODEX_HOME', raising=False)
    prefix = '/api/connections/codex'
    plan = client.post(prefix + '/plans', json=plan_body('cli')).json()
    assert client.post(prefix + '/plans/' + plan['id'] + '/apply', json={
        'version': plan['version'], 'confirmed': True}).status_code == 200
    config = Path.home() / '.codex/config.toml'
    original = config.read_bytes()
    config.write_text('broken = [')
    failed = client.post(prefix + '/one-click-disconnect', json={'surface': 'cli'})
    assert failed.status_code == 409
    assert config.read_text() == 'broken = ['
    config.write_bytes(original)
    result = client.post(prefix + '/one-click-disconnect', json={'surface': 'cli'})
    assert result.status_code == 200, result.text
    assert result.json()['plan']['capabilities'] == ['context_tools']
    assert result.json()['plan']['readback'] == 'verified'
