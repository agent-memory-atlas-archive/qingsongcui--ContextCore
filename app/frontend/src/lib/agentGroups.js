// Product-level Agent identities. Native targets remain explicit in configuration plans.
const names = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  opencode: 'OpenCode',
  omp: 'Oh My Pi (OMP)',
  cursor: 'Cursor',
  antigravity: 'Antigravity',
};

const variants = { 'codex-desktop': 'codex', 'antigravity-cli': 'antigravity' };

export const agentGroup = id => variants[id] || id;
export const isPrimaryAgent = id => Object.hasOwn(names, id);
export const agentGroupName = host => names[agentGroup(host?.id)] || nativeTargetName(host);
export const nativeTargetName = host => host?.id === 'omp' ? names.omp : host?.label || host?.name || host?.id || '';

export function primaryAgents(hosts) {
  return hosts.filter(host => isPrimaryAgent(host.id));
}

function sharesCategory(primary, variant, tab) {
  if (!primary || !variant) return false;
  if (['shared', 'advanced', 'library'].includes(tab)) return true;
  if (primary.id === 'codex') {
    return primary.shared_configuration === true && !['connections', 'plans', 'bundles'].includes(tab);
  }
  if (primary.id === 'antigravity') {
    if (['connections', 'accounts'].includes(tab)) return true;
    const path = { mcp: 'mcp_path', skill: 'skill_dir' }[tab];
    return !!path && !!primary.paths?.[path] && primary.paths[path] === variant.paths?.[path];
  }
  return false;
}

export function configurationTarget(hosts, id, tab) {
  const primary = hosts.find(host => host.id === agentGroup(id));
  const target = hosts.find(host => host.id === id);
  if (!sharesCategory(primary, target, tab)) return id;
  const owner = tab === 'providers' && primary.shared_provider_target;
  return owner && hosts.some(host => host.id === owner && agentGroup(host.id) === primary.id) ? owner : primary.id;
}

export function configurationVariants(hosts, group, tab) {
  return hosts.filter(host => agentGroup(host.id) === group
    && configurationTarget(hosts, host.id, tab) === host.id);
}

export function configurationChoices(hosts, tab) {
  return hosts.filter(host => isPrimaryAgent(agentGroup(host.id))
    && configurationTarget(hosts, host.id, tab) === host.id);
}
