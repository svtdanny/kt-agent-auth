#!/usr/bin/env python3
"""Render the implemented credential flow, SQL tables and refresh protocol."""
import argparse
from render_scaling_diagrams import Drawing, INK, MUTED, LINE, CONTROL


def table(d, x, y, w, name, columns, notes=(), purpose=''):
    height = 36 + 23 * len(columns) + 23 * len(notes) + 8
    d.rect(x, y, w, height)
    d.rect(x, y, w, 32, fill='#f1f4f7')
    d.text(x+12, y+22, name, 16, bold=True, mono=True)
    if purpose:
        d.text(x+w-12, y+22, purpose, 12, MUTED, anchor='end')
    for i, (column, kind, constraint) in enumerate(columns):
        row = y + 54 + i * 23
        d.text(x+12, row, column, 14, mono=True)
        d.text(x+242, row, kind, 14, mono=True)
        d.text(x+432, row, constraint, 13, MUTED)
    for i, note in enumerate(notes):
        d.text(x+12, y+54+23*(len(columns)+i), note, 13, MUTED)
    return height


def replica(d, x, label):
    d.boundary(x, 525, 440, 260, label)
    d.component(x+30, 560, 380, 145, 'Container', 'authorizer', [
        'OAuth resolver + pkg/authstate',
        'On token use: SQL recheck after 1 s',
        'One pgx pool: ≤ 4 SQL connections'])
    d.rect(x+30, 742, 380, 28, fill='#f4f7fa')
    d.text(x+43, 762, 'Envoy: inject derived bearer token', 14)
    d.edge(f'M{x+220} 705 V742')


def render(exports):
    d = Drawing('Credential management and PostgreSQL schemas',
        'Implemented MVP: per-project PostgreSQL database and login; mounted source credentials and encryption key; encrypted shared OAuth state and SQL refresh leases.',
        (0, 0, 2160, 2180))

    # External delivery and authentication dependencies.
    d.component(55, 30, 270, 105, 'Secret authority', 'Vault', [
        'Project source credentials', 'Project encryption key'])
    d.component(390, 30, 280, 105, 'Deployment pipeline', 'GitLab CI', [
        'Reads project Vault paths', 'Applies Kubernetes Secrets'])
    d.edge('M325 83 H390')
    d.component(765, 30, 425, 105, 'Database provisioning', 'Project DB + LOGIN', [
        'Creates database, role and password', 'Delivers authorizer-db Secret'])
    d.component(1460, 30, 610, 105, 'External provider', 'OAuth authentication endpoint', [
        'HTTPS / client_credentials', 'Returns a derived access token'])

    d.boundary(30, 200, 2100, 1190, 'Kubernetes cluster')
    d.boundary(55, 250, 1190, 745, 'namespace: auth-agent-a')
    d.component(85, 300, 330, 100, 'Secret / application-credentials', 'Source credentials', [
        '/credentials/credentials.json', 'OAuth client ID + client secret'])
    d.component(450, 300, 280, 100, 'Secret / auth-state-key', 'Encryption key Kₐ', [
        '/auth-state-key/key', 'Base64 of 32 random bytes'])
    d.component(765, 300, 425, 100, 'Secret / authorizer-db', 'DATABASE_URL', [
        'Database: auth_agent_a', 'Login: auth_agent_a + password'])
    d.edge('M530 135 V173 H250 V300', dashed=True)
    d.edge('M530 173 H590 V300', dashed=True)
    d.edge('M977 135 V300', dashed=True)

    # Mounts and environment are distinct inputs, shared by project replicas.
    d.boundary(85, 490, 1105, 315, 'Deployment/agent-egress', 'replicas: Gₐ')
    replica(d, 110, 'egress Pod 1')
    replica(d, 650, 'egress Pod N')
    d.edge('M250 400 V438 H870', arrow=False, dashed=True)
    d.edge('M590 400 V438', arrow=False, dashed=True)
    d.edge('M400 438 V560', dashed=True)
    d.edge('M870 438 V560', dashed=True)
    d.label(430, 430, 'Read-only file mounts → authorizer', 14)
    d.edge('M977 400 V462 H505 V560', dashed=True, color=CONTROL)
    d.edge('M1045 462 V560', dashed=True, color=CONTROL)
    d.edge('M977 462 H1045', arrow=False, dashed=True, color=CONTROL)
    d.label(785, 453, 'Environment → authorizer', 13, CONTROL)

    # SQL traffic converges on the same project database.
    d.edge('M520 680 H570 V835 H1230', arrow=False)
    d.edge('M1060 680 H1230 V835', arrow=False)
    d.edge('M1230 835 V420 H1340')
    d.label(1070, 655, 'SQL / TLS', 13)
    d.label(1080, 858, 'Same project database', 13)

    # Only a refresh owner calls the external authentication system.
    d.edge('M140 680 H100 V910 H1270', arrow=False)
    d.edge('M680 680 H620 V910', arrow=False)
    d.edge('M1270 910 V175 H1765 V135')
    d.label(850, 902, 'HTTPS · refresh lease holder only', 14)
    d.lines(120, 950, ['Restart: decrypt and reuse valid shared state.',
                      'Source credentials and Kₐ stay in mounted files.'], 15, 23)

    # A second project has its own inputs, role and database on the same server.
    d.boundary(55, 1035, 1190, 330, 'namespace: auth-agent-b')
    d.component(85, 1100, 390, 145, 'Project Secrets', 'Independent credentials + Kᵦ', [
        'Own OAuth client credentials',
        'Own 32-byte encryption key',
        'Own DATABASE_URL + password'])
    d.component(650, 1100, 440, 145, 'authorizer replicas × Gᵦ', 'Project B proxy bundle', [
        'Same Kᵦ within project B',
        'Login / database: auth_agent_b',
        'Own local caches and connection pools'])
    d.edge('M475 1172 H650', dashed=True)
    d.edge('M1090 1172 H1340')
    d.label(1120, 1158, 'SQL / TLS', 13)
    d.text(85, 1325, 'Project replicas share auth state. Projects have separate databases, logins and encryption keys.', 15, MUTED)

    # Physical server shared by projects; schemas and tables are database-local.
    d.boundary(1300, 250, 790, 1115, 'namespace: kagent')
    d.text(1330, 302, 'PostgreSQL server', 23, bold=True)
    d.text(1330, 330, 'kagent-postgresql.kagent.svc:5432', 16, mono=True)
    d.text(1330, 357, 'Verified TLS + SCRAM-SHA-256 password authentication', 15, MUTED)
    d.boundary(1340, 395, 710, 715, 'database: auth_agent_a')
    d.text(1360, 431, 'LOGIN / OWNER: auth_agent_a', 16, bold=True, mono=True)
    table(d, 1360, 451, 670, 'auth_state.tokens', [
        ('id', 'text', 'PK / HMAC context ID'),
        ('version', 'bigint', 'NOT NULL'),
        ('ciphertext', 'bytea', 'NULL until acquired'),
        ('expires_at', 'timestamptz', 'NOT NULL / safe deadline'),
        ('owner', 'text', 'NOT NULL'),
        ('lease_until', 'timestamptz', 'NOT NULL'),
        ('retry_after', 'timestamptz', 'NOT NULL'),
    ], ['ciphertext = nonce + AES-256-GCM({value, expires}); AAD = id/version'])
    table(d, 1360, 695, 670, 'auth_bridge.bindings', [
        ('session_id', 'text', 'PRIMARY KEY'),
        ('data', 'jsonb', 'NOT NULL'),
        ('active', 'boolean', 'NOT NULL'),
        ('created_at', 'timestamptz', 'NOT NULL'),
        ('revoked_at', 'timestamptz', 'NULL allowed'),
    ], ['INDEX bindings_gc (active, created_at)'], purpose='Per-request identity / revocation')
    table(d, 1360, 878, 670, 'auth_bridge.launches', [
        ('caller', 'text', 'NOT NULL'),
        ('request_id', 'text', 'NOT NULL'),
        ('request', 'jsonb', 'NOT NULL'),
        ('account_uid', 'text', 'NOT NULL'),
        ('binding', 'jsonb', 'NULL allowed'),
        ('created_at', 'timestamptz', 'NOT NULL'),
        ('retired_at', 'timestamptz', 'NULL allowed'),
    ], ['PRIMARY KEY (caller, request_id)'], purpose='Creation retries → same Session')
    d.component(1340, 1140, 710, 95, 'Database / auth_agent_b', 'LOGIN / OWNER: auth_agent_b', [
        'Own auth_state.tokens + auth_bridge.bindings / launches'])
    d.component(1340, 1260, 710, 80, 'Platform databases', 'kagent / Substrate', [
        'Separate platform tables and roles'])

    # Protocol sequence; all SQL arrows are short, independent operations.
    d.boundary(55, 1460, 2035, 675, 'Refresh: same project + credential configuration')
    columns = [(230, 'authorizer A'), (650, 'authorizer B'), (1140, 'PostgreSQL'), (1700, 'OAuth provider')]
    for x, name in columns:
        d.rect(x-130, 1490, 260, 46, fill='#f4f7fa')
        d.text(x, 1520, name, 18, bold=True, anchor='middle')
        d.edge(f'M{x} 1536 V2090', arrow=False, dashed=True, width=1)
    # Initial local miss / expired state is the trigger for this branch.
    d.text(90, 1569, 'Local miss / expiry', 14, MUTED)
    d.edge('M230 1595 H1140'); d.label(285, 1586, '1  Read shared token → missing / expired', 15)
    d.edge('M650 1637 H1140'); d.label(690, 1628, '1  Read → missing / expired', 14)
    d.edge('M230 1682 H1140'); d.label(285, 1673, '2  Conditional UPDATE: claim 12 s lease; increment version', 15)
    d.edge('M1140 1717 H230'); d.label(285, 1708, 'Won: owner UUID + version v', 14)
    d.edge('M650 1759 H1140'); d.label(690, 1750, '2  Claim fails: lease is held', 14)
    d.edge('M230 1804 H1700'); d.label(285, 1795, '3  Authenticate over HTTPS · 8 s timeout · no SQL transaction / connection held', 15)
    d.edge('M1700 1840 H230'); d.label(285, 1831, 'Derived token + lifetime; refresh margin = min(TTL / 10, 30 s)', 14)
    d.edge('M230 1885 H1140'); d.label(285, 1876, '4  Encrypt + publish WHERE owner/version match AND lease is valid', 15)
    d.edge('M650 1930 H1140'); d.label(690, 1921, '5  Re-read after 100 ms waits', 14)
    d.edge('M1140 1966 H650'); d.label(690, 1957, 'Same token version v → decrypt', 14)
    d.rect(90, 2010, 940, 89, fill='#fafafa')
    d.lines(105, 2035, ['Acquisition failure: release lease; retry_after = now() + 2 s.',
                       'Crash: lease expires. Superseded or expired owners cannot publish.',
                       'Leases fence SQL publication; external login is not exactly once after a crash.'], 14, 23)
    d.rect(1070, 2010, 980, 89, fill='#fafafa')
    d.lines(1085, 2035, ['On token use: memory if checked < 1 s ago; otherwise read SQL.',
                        'Explicit invalidation: version++; clear token and lease. Peers re-read on use.',
                        'Source credential change: new record ID. No notification / pub-sub channel.'], 14, 23)

    d.save('architecture-credentials', exports)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exports', action='store_true', help='also write PNG/PDF with CairoSVG')
    args = parser.parse_args()
    render(args.exports)
    print('Rendered docs/architecture-credentials.svg' + (' / .png / .pdf' if args.exports else ''))
