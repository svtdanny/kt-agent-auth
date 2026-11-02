#!/usr/bin/env python3
"""Render the no-patch architecture; --exports also writes PNG and PDF."""
import argparse
from pathlib import Path

from render_scaling_diagrams import Drawing, MUTED, CONTROL
from render_credentials_diagram import table

ROOT = Path(__file__).resolve().parents[1]


def project(d, y, suffix):
    ns = f'auth-agent-{suffix}'
    d.boundary(50, y, 610, 550, f'namespace: {ns}')
    d.component(75, y+40, 330, 90, 'Agent / Harness CRs', 'Agent definitions', [
        'Many Agents → one project WorkerPool'])
    d.hpa(435, y+40, 200, f'workers-{suffix}', 'CPU; demand metrics later')
    d.boundary(75, y+175, 560, 125, 'WorkerPool', f'Pod replicas = W{suffix}')
    d.component(95, y+210, 280, 75, 'Runtime sandboxes', 'Sessions / Actors', [])
    d.rect(430, y+225, 180, 48, fill='#f4f7fa')
    d.text(451, y+255, 'atunnel', 17, bold=True)
    d.edge(f'M375 {y+249} H430')
    d.edge(f'M535 {y+108} V{y+175}', dashed=True, color=CONTROL)
    d.label(546, y+155, '/scale', 12, CONTROL)
    d.edge(f'M225 {y+130} V{y+175}', dashed=True)
    d.hpa(75, y+335, 210, f'authorizer-{suffix}', 'CPU / measured RPC demand')
    d.component(310, y+335, 325, 75, 'Service :50052', 'authorizer-extproc', [])
    d.boundary(75, y+440, 560, 75, 'Deployment/authorizer', f'Pod replicas = A{suffix}')
    d.text(95, y+484, 'ext_proc + Session bridge + local token cache', 15)
    d.edge(f'M180 {y+403} V{y+440}', dashed=True, color=CONTROL)
    d.edge(f'M472 {y+410} V{y+440}')
    d.text(75, y+539, 'Read-only mounts: source credentials + project key; DB URL Secret', 13, MUTED)


def scaling(exports):
    d = Drawing('No-patch deployment and independent scaling',
        'Proposed independent deployment boundaries with verified no-patch routing. '
        'Application traffic uses one shared native Envoy; namespace Services receive only ext_proc RPCs.',
        (0, 0, 2240, 1370))
    d.boundary(20, 25, 2200, 1315, 'Kubernetes cluster')
    d.boundary(730, 65, 1450, 175, 'platform namespaces: kagent / ate-system')
    d.component(755, 105, 310, 105, 'Stock Deployment', 'kagent controller', [
        'Agent / Session lifecycle', 'No source changes'])
    d.component(1110, 105, 1045, 105, 'Stock Substrate API', 'One configured egress gateway address', [
        '--default-egress-gateway-address=shared-egress.ate-system.svc:443',
        'No namespace-to-gateway API patch; native Actor identities and policy'])
    d.edge('M1065 157 H1110')
    project(d, 70, 'a')
    project(d, 720, 'b')

    d.boundary(730, 285, 950, 970, 'namespace: ate-system')
    d.component(780, 330, 420, 90, 'Service :443', 'shared-egress', [
        'Stable DNS → ready gateway endpoints'])
    d.hpa(1270, 330, 360, 'shared-egress', 'Envoy CPU / measured traffic concurrency')
    d.boundary(780, 485, 850, 290, 'Deployment/shared-egress', 'Pod replicas = G')
    d.component(810, 535, 390, 160, 'Container', 'Envoy', [
        'Actor mTLS CONNECT + native policy',
        'HTTP decode / HTTPS interception',
        'Inject token; TLS to upstream'])
    d.component(1240, 535, 360, 85, 'Native container', 'native-egress', [])
    d.component(1240, 655, 360, 85, 'Native sidecar', 'sdsmint', [])
    d.edge('M990 420 V485')
    d.edge('M1450 398 V485', dashed=True, color=CONTROL)
    d.label(1463, 453, 'Deployment /scale', 13, CONTROL)
    d.edge('M1200 615 H1240', dashed=True)
    d.label(1207, 599, 'native', 11)
    d.boundary(780, 845, 850, 245, 'Envoy route configuration')
    d.lines(805, 885, [
        'Trusted filter state: dev.ate.actor.identity',
        'Certificate URI SAN → atespace → fixed ext_proc cluster',
        'auth-agent-a → authorizer-extproc.auth-agent-a.svc:50052',
        'auth-agent-b → authorizer-extproc.auth-agent-b.svc:50052',
        'Native destination guards retained; no cross-project fallback',
        'Unavailable / mismatched authorizer → fail closed',
    ], size=15, gap=31)
    d.lines(800, 1130, [
        'G, Aa, Ab, Wa and Wb scale independently.',
        'HPA target: whole gateway Pod; Envoy --concurrency must track CPU.',
        'Authorizer throughput is also bounded by SQL pools and provider budgets.',
        'Existing HTTP/2 tunnels stay on their selected gateway replica.',
        'Independent gateway / authorizer HPAs and draining are not yet tested.',
    ], size=14, gap=28)

    d.boundary(1740, 320, 440, 405, 'namespace: kagent / shared database')
    d.database(1760, 355, 400, 230, 'PostgreSQL', [
        'kagent / Substrate platform state',
        'DB + LOGIN: auth_agent_a',
        'DB + LOGIN: auth_agent_b',
        'Bindings, launches, encrypted tokens',
        'One pgx pool / authorizer; max 4'])
    d.lines(1765, 625, [
        'Same project replicas → same DB / role.',
        'SQL TLS verify-full; role per project.',
        'Total pool cap = 4 × (Aa + Ab).',
    ], size=14, gap=27)
    d.component(1740, 825, 440, 110, 'Application destinations', 'MCP / HTTP APIs', [
        'One application proxy hop: shared Envoy',
        'HTTP or TLS re-originated by Envoy'])
    d.lines(1750, 980, [
        'Shared gateway is trusted across projects:',
        'it sees decoded HTTP and bearer tokens.',
        'It mounts project gateway mTLS identities,',
        'not source credentials or token-store keys.',
    ], size=14, gap=27)
    d.rect(1740, 1135, 440, 100, dash=True)
    d.text(1755, 1163, 'Optional Redis backend', 17, bold=True)
    d.lines(1755, 1190, ['Design alternative; not implemented.',
                           'Would need project ACLs + encrypted state.'], size=14, gap=23)

    # Connections run in the gutters between resource boundaries.
    d.edge('M610 320 H690 V365 H780')
    d.label(674, 299, 'mTLS CONNECT', 12)
    d.edge('M610 970 H710 V400 H780')
    d.label(671, 1000, 'mTLS CONNECT', 12)
    d.edge('M810 585 H750 V445 H635')
    d.label(670, 465, 'ext_proc / mTLS', 12)
    d.edge('M810 665 H765 V1095 H635')
    d.label(670, 1120, 'ext_proc / mTLS', 12)
    d.edge('M1200 685 H1215 V800 H1705 V875 H1740')
    d.label(1709, 801, 'HTTP(S)', 12)
    d.edge('M635 555 H682 V1305 H1717 V540 H1740')
    d.edge('M635 1205 H682', arrow=False)
    d.label(1160, 1298, 'Project authorizers → PostgreSQL / TLS', 14)
    d.edge('M1325 210 V260 H990 V330', dashed=True)
    d.label(1115, 255, 'gateway address configuration', 12)
    d.save('no-patch/architecture-scaling', exports)


def credentials(exports):
    d = Drawing('No-patch credential delivery, isolation and SQL tables',
        'Per-project Secrets are mounted only into authorizer Pods. Shared Envoy receives derived tokens. '
        'PostgreSQL uses separate databases and logins; table schemas match the implementation.',
        (0, 0, 2160, 1420))
    d.component(40, 35, 300, 110, 'Secret authority', 'Vault', [
        'Project source credentials', 'Separate encryption key'])
    d.component(400, 35, 300, 110, 'Delivery pipeline', 'GitLab CI', [
        'Reads project Vault paths', 'Applies namespace Secrets'])
    d.component(790, 35, 410, 110, 'Provisioner', 'Project database + LOGIN', [
        'Creates role, DB and password', 'Applies authorizer-db Secret'])
    d.component(1310, 35, 810, 150, 'namespace: ate-system / shared Envoy', 'Application HTTP and TLS terminate here', [
        'Actor identity → fixed namespace ext_proc endpoint, using project mTLS',
        'Receives derived bearer token → injects it → forwards to upstream API',
        'Sees all projects’ HTTP; holds no project source credentials or storage keys'])
    d.edge('M340 90 H400')
    d.boundary(40, 255, 1160, 950, 'namespace: auth-agent-a')
    d.component(70, 300, 350, 120, 'Secret/application-credentials', 'Source credentials', [
        '/credentials/credentials.json', 'OAuth client ID / client secret', 'Read-only projected volume'])
    d.component(450, 300, 300, 120, 'Secret/auth-state-key', 'Encryption key Ka', [
        '/auth-state-key/key', 'Base64 of 32 random bytes', 'Read-only projected volume'])
    d.component(780, 300, 390, 120, 'Secret/authorizer-db', 'DATABASE_URL', [
        'DB: auth_agent_a', 'LOGIN: auth_agent_a + password', 'Environment from Secret'])
    d.boundary(70, 520, 1100, 310, 'Deployment/authorizer', 'replicas = Aa; one logical project authorizer')
    for x, name in [(95, 'Pod 1'), (640, 'Pod 2 … Aa')]:
        d.component(x, 565, 500, 180, name, 'authorizer', [
            'ext_proc + Session bridge',
            'Source credential resolver + pkg/authstate',
            'Project Ka; request-driven local token cache',
            'One pgx pool: max 4 connections'])
    d.lines(95, 785, [
        'All replicas use the same project key, database and LOGIN.',
        'Service authorizer-extproc:50052 distributes RPCs across ready Pods.',
    ], size=14, gap=25)
    d.boundary(70, 890, 1100, 280, 'Encrypted token record')
    d.lines(95, 930, [
        'id = HMAC(project scope, credential reference, complete credential configuration)',
        'ciphertext = random nonce + AES-256-GCM({value, expires}, Ka)',
        'Authenticated additional data = id / version; Ka never enters PostgreSQL.',
        'expires_at = token expiry − min(TTL / 10, 30 s).',
        'Source credential change → different record. Key is loaded at startup.',
        'Automatic key rotation / re-encryption is not implemented.',
        'Restart → lazily decrypt and reuse a valid persisted token; no new login needed.',
    ], size=15, gap=31)

    d.boundary(1280, 255, 840, 950, 'PostgreSQL: kagent-postgresql.kagent.svc:5432')
    d.text(1305, 295, 'Database auth_agent_a / owner + LOGIN auth_agent_a', 17, bold=True)
    table(d, 1305, 325, 790, 'auth_state.tokens', [
        ('id', 'text', 'PRIMARY KEY'), ('version', 'bigint', 'NOT NULL; publication fence'),
        ('ciphertext', 'bytea', 'NULL until published / invalidated'),
        ('expires_at', 'timestamptz', 'NOT NULL; safe reuse deadline'),
        ('owner', 'text', 'NOT NULL; refresh owner UUID'),
        ('lease_until', 'timestamptz', 'NOT NULL; 12 s refresh lease'),
        ('retry_after', 'timestamptz', 'NOT NULL; shared retry cooldown'),
    ], purpose='Encrypted derived tokens')
    table(d, 1305, 560, 790, 'auth_bridge.bindings', [
        ('session_id', 'text', 'PRIMARY KEY'), ('data', 'jsonb', 'NOT NULL; trusted identity snapshot'),
        ('active', 'boolean', 'NOT NULL; authoritative revocation'),
        ('created_at', 'timestamptz', 'NOT NULL'), ('revoked_at', 'timestamptz', 'NULL allowed'),
    ], ['data: Session → user, Agent, logical ServiceAccount + UID, atespace.',
        'INDEX bindings_gc(active, created_at).'], purpose='Per-request identity / revocation')
    table(d, 1305, 800, 790, 'auth_bridge.launches', [
        ('caller', 'text', 'NOT NULL; PK part 1'), ('request_id', 'text', 'NOT NULL; PK part 2'),
        ('request', 'jsonb', 'NOT NULL; canonical creation request'),
        ('account_uid', 'text', 'NOT NULL'), ('binding', 'jsonb', 'NULL until Session is bound'),
        ('created_at', 'timestamptz', 'NOT NULL'), ('retired_at', 'timestamptz', 'NULL allowed'),
    ], ['Same caller + request_id → same Session across replicas.',
        'binding is a JSON snapshot, without a SQL foreign key.'],
        purpose='Creation retries → same Session')
    d.text(1305, 1083, 'Same schemas per DB; other project roles have no auth-schema access.', 14, MUTED)
    d.lines(1305, 1120, ['SQL over TLS verify-full + SCRAM password authentication.',
                          'DB grants isolate access; AES-GCM protects token payloads at rest.'], size=14, gap=26)
    d.boundary(40, 1255, 2080, 125, 'namespace: auth-agent-b')
    d.lines(65, 1295, [
        'Same authorizer structure with independent source credentials, key Kb and authorizer-extproc Service.',
        'DB + LOGIN auth_agent_b. Shared Envoy selects it only for an authenticated auth-agent-b Actor.',
        'Each authorizer also checks Actor atespace + Session binding against its own project; a deliberate A → B misroute is rejected.',
    ], size=15, gap=29)

    # Delivery, mounts and RPC are deliberately separate paths.
    d.edge('M550 145 V220 H245 V300', dashed=True)
    d.edge('M550 220 H600 V300', dashed=True)
    d.edge('M985 145 V300', dashed=True)
    d.label(650, 214, 'Secret delivery', 12)
    d.edge('M245 420 V475 H985 V420', arrow=False, dashed=True)
    d.edge('M600 420 V475', arrow=False, dashed=True)
    d.edge('M350 475 V565', dashed=True)
    d.edge('M885 475 V565', dashed=True)
    d.label(95, 455, 'Read-only mounts / Secret env → every replica', 13)
    d.edge('M1310 110 H1230 V615 H1140')
    d.label(1220, 243, 'ext_proc / mTLS', 13)
    d.edge('M1170 805 H1280')
    d.label(1183, 791, 'SQL / TLS', 12)
    d.save('no-patch/architecture-credentials', exports)


def refresh(exports):
    d = Drawing('PostgreSQL token refresh across authorizer replicas',
        'Request-driven local cache; conditional SQL refresh lease; fenced encrypted publication; '
        'peers observe changes on use without pub-sub or background polling.',
        (0, 0, 2040, 1400))
    positions = [250, 760, 1260, 1790]
    for x, name, body in [
        (250, 'Authorizer replica A', 'Same project key Ka'),
        (760, 'Authorizer replica B', 'Same project key Ka'),
        (1260, 'PostgreSQL', 'auth_state.tokens[id]'),
        (1790, 'OAuth provider', 'HTTPS / client_credentials'),
    ]:
        d.component(x-200, 35, 400, 90, 'Protocol participant', name, [body])
        d.edge(f'M{x} 125 V970', arrow=False, dashed=True, width=1)

    def message(x1, x2, y, label, **kw):
        d.edge(f'M{x1} {y} H{x2}', **kw)
        d.label(min(x1, x2)+16, y-10, label, 14)

    message(250, 1260, 195, '1  Token needed; local miss / freshness expired → SELECT id')
    message(760, 1260, 260, '2  Competing request reads the same id')
    message(250, 1260, 325, '3  Conditional UPDATE → owner UUID; version++; lease = 12 s')
    message(1260, 250, 380, 'Lease granted to A; commit before external HTTP')
    message(760, 1260, 440, '4  B cannot claim unexpired lease')
    message(250, 1790, 505, '5  A acquires derived token; timeout 8 s; no SQL transaction held')
    message(1260, 760, 565, 'B waits 100 ms, then re-reads; bounded wait')
    message(1790, 250, 635, '6  Provider returns token + lifetime')
    message(250, 1260, 705, '7  Encrypt; publish WHERE owner/version match AND lease valid')
    message(1260, 250, 765, 'Commit ciphertext + safe expiry; release lease')
    message(760, 1260, 835, '8  Next re-read sees published version')
    message(1260, 760, 895, 'B decrypts with Ka; both replicas reuse the same token')
    d.label(66, 965, 'Mutation result → shared Envoy injects Authorization; upstream application request resumes.', 15)

    d.boundary(45, 1020, 945, 335, 'Local cache and database traffic')
    d.lines(70, 1065, [
        'Only a request that needs this token triggers a cache check.',
        'Valid memory entry + successful SQL check < 1 s ago → reuse locally.',
        'Otherwise SELECT this token id; idle entries do not poll.',
        'Steady traffic: ≈ 1 token-state read / s / active id / replica.',
        'Pool connections do not each run a one-second query.',
        'Separately: 2 binding reads per successful credentialed request.',
        'Refresh contenders use temporary 100 ms polls; work timeout 20 s.',
        'Restarted replicas reuse a valid stored token on first use.',
        'No pub-sub / NOTIFY; peers observe changes on their next check.',
    ], size=15, gap=30)
    d.boundary(1040, 1020, 955, 335, 'Failure, invalidation and rotation')
    d.lines(1065, 1065, [
        'OAuth failure → release lease; shared retry_after = now + 2 s.',
        'Owner crash → next contender may claim after lease expiry.',
        'Owner/version fence rejects a late publication by an old owner.',
        'An ambiguous external timeout may still cause another login.',
        'Admin invalidation: version++; clear token, expiry and lease.',
        'Peers stop reusing it after request-driven local freshness expires.',
        'Mounted source config change → new HMAC id; Secret updates are eventual.',
        'Storage-key rotation and upstream-401 refresh/replay are not implemented.',
        'No SQL row lock or connection is held across the provider call.',
    ], size=15, gap=30)
    d.save('no-patch/refresh-protocol', exports)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exports', action='store_true')
    args = parser.parse_args()
    (ROOT / 'docs' / 'no-patch').mkdir(parents=True, exist_ok=True)
    scaling(args.exports)
    credentials(args.exports)
    refresh(args.exports)
