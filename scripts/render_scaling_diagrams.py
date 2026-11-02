#!/usr/bin/env python3
"""Render architecture SVGs; --exports also writes PNG/PDF using CairoSVG."""
from pathlib import Path
from html import escape
import argparse

ROOT = Path(__file__).resolve().parents[1]
INK, MUTED, LINE, CONTROL = '#17212b', '#526170', '#536778', '#9a5714'

class Drawing:
    def __init__(self, title, subtitle, viewport):
        x, y, width, height = viewport
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="{x} {y} {width} {height}" role="img" aria-labelledby="title desc"><title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}</desc>',
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10" fill="#536778"/></marker><marker id="control" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10" fill="#9a5714"/></marker></defs>']
        self.rect(x, y, width, height, fill='white', stroke='none')

    def rect(self, x, y, w, h, fill='white', stroke=LINE, dash=False, width=1.4):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"'+(' stroke-dasharray="8 5"' if dash else '')+'/>')

    def text(self, x, y, value, size=17, color=INK, bold=False, mono=False, anchor='start'):
        font = 'DejaVu Sans Mono' if mono else 'DejaVu Sans'
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{font}, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}" text-anchor="{anchor}">{escape(value)}</text>')

    def lines(self, x, y, values, size=16, gap=25, color=MUTED):
        for i, val in enumerate(values): self.text(x, y + i * gap, val, size, color)

    def edge(self, d, arrow=True, dashed=False, color=LINE, width=1.7):
        marker = 'control' if color == CONTROL else 'arrow'
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"'+(' stroke-dasharray="7 5"' if dashed else '')+(f' marker-end="url(#{marker})"' if arrow else '')+'/>')

    def label(self, x, y, value, size=14, color=MUTED):
        width = len(value) * size * .57 + 12
        self.rect(x-5, y-size, width, size+6, stroke='none')
        self.text(x, y, value, size, color)

    def boundary(self, x, y, w, h, label, detail='', dashed=False):
        self.rect(x, y, w, h, stroke='#9aa7b2', dash=dashed)
        self.rect(x+12, y-11, max(150, len(label)*10.4+24), 23, stroke='none')
        self.text(x+22, y+6, label, 17, bold=True, mono=True)
        if detail: self.text(x+w-16, y+27, detail, 14, MUTED, anchor='end')

    def component(self, x, y, w, h, kind, name, body=(), fill='white'):
        self.rect(x,y,w,h,fill=fill)
        self.text(x+13,y+23,kind,12,MUTED,mono=True)
        self.text(x+13,y+49,name,18,bold=True)
        self.lines(x+13,y+74,body,size=14,gap=21)

    def hpa(self, x, y, w, name, signal):
        self.rect(x,y,w,68,stroke=CONTROL,fill='#fffcf6')
        self.text(x+13,y+22,'HorizontalPodAutoscaler',12,CONTROL,mono=True)
        self.text(x+13,y+44,name,17,CONTROL,bold=True)
        self.text(x+13,y+61,signal,12,MUTED)

    def database(self, x,y,w,h,name,body):
        self.parts.append(f'<path d="M{x} {y+15} C{x} {y-5},{x+w} {y-5},{x+w} {y+15} V{y+h-15} C{x+w} {y+h+5},{x} {y+h+5},{x} {y+h-15} Z" fill="#f8fafc" stroke="{LINE}" stroke-width="1.4"/><path d="M{x} {y+15} C{x} {y+35},{x+w} {y+35},{x+w} {y+15}" fill="none" stroke="{LINE}" stroke-width="1.4"/>')
        self.text(x+17,y+55,name,21,bold=True)
        self.lines(x+17,y+82,body,size=15,gap=23)

    def save(self, name, exports):
        svg='\n'.join(self.parts + ['</svg>'])
        dest=ROOT/'docs'/name
        dest.with_suffix('.svg').write_text(svg)
        if exports:
            import cairosvg
            cairosvg.svg2png(bytestring=svg.encode(),write_to=str(dest.with_suffix('.png')))
            # Keep the PDF cropped to the same aspect ratio as the diagram.
            cairosvg.svg2pdf(bytestring=svg.encode(),write_to=str(dest.with_suffix('.pdf')),
                            output_width=1190.55)

def deployment(exports):
    d=Drawing('Agent authorization — deployment and scaling',
        'Current Pod composition with proposed HPAs and shared authentication state. Selected runtime and control dependencies.', (0,125,1840,970))
    d.boundary(30,150,1350,920,'Kubernetes cluster')
    d.boundary(60,205,1280,150,'platform namespaces','Shared by all projects')
    d.component(80,240,260,88,'Deployment','kagent controller', ['Session / Agent API'])
    d.component(450,240,290,88,'Deployments','Substrate APIs', ['Actor lifecycle / scheduling'])
    d.component(830,240,280,88,'Shared infrastructure','Metrics + adapter', ['Resource / external metrics APIs'])
    d.component(1140,240,180,88,'Cluster controller','Node autoscaler', ['Node pool capacity'])
    d.edge('M340 283 H450'); d.label(360,273,'gRPC')
    d.edge('M595 328 V400',dashed=True); d.label(610,384,'Actor lifecycle')

    d.boundary(60,400,1280,620,'namespace: <project>','× P project namespaces',dashed=True)
    d.component(80,465,350,90,'Agent + Harness CRs  × D_p','Per-Agent definitions', ['Image, configuration, workerPoolRef'])
    d.component(1000,465,300,100,'Secret volumes','Project credentials + key', ['Source credentials; auth-state key', 'Key shared by this project’s replicas'],fill='#fafafa')
    d.hpa(100,585,260,'workers','Active demand / resources / backlog')
    d.hpa(700,585,270,'agent-egress','Envoy + authorizer container metrics')
    # Metrics feed both independent HPA objects.
    d.edge('M970 328 V371 H445 V619 H360',dashed=True)
    d.edge('M970 371 H985 V619 H970',dashed=True)
    d.label(455,391,'metrics')
    d.edge('M230 653 V685',dashed=True,color=CONTROL)
    d.edge('M835 653 V665 H985 V685',dashed=True,color=CONTROL)
    d.label(242,676,'/scale',color=CONTROL)
    d.label(995,678,'/scale',color=CONTROL)
    d.edge('M430 510 H435 V671 H392 V685',dashed=True)
    d.label(279,570,'references pool',size=12)

    d.boundary(80,685,340,290,'WorkerPool','replicas = W_p')
    d.boundary(100,740,300,215,'Pod × W_p')
    d.component(120,777,260,80,'Runtime sandbox × active Actors','Agent Sessions', ['Session identity remains distinct'])
    d.rect(120,891,260,40,fill='#f4f7fa')
    d.text(135,918,'atunnel',17,bold=True)
    d.edge('M250 857 V891')
    d.edge('M380 911 H440 V842 H460')
    d.label(450,802,'mTLS CONNECT',size=13)
    d.rect(460,815,140,55,fill='#f4f7fa')
    d.text(471,836,'Service',12,MUTED,mono=True)
    d.text(471,858,'agent-egress',16,bold=True)
    d.edge('M600 842 H695')

    d.boundary(650,685,650,290,'Deployment/agent-egress','replicas = G_p; proposed minimum 2')
    d.boundary(670,740,610,215,'Pod × G_p')
    d.component(695,790,155,80,'Container','Envoy', ['Traffic proxy'])
    d.component(1010,790,245,80,'Container','authorizer', ['ext_proc + Session bridge'])
    d.edge('M850 832 H1010'); d.label(874,817,'gRPC / mTLS')
    d.rect(695,905,255,35,fill='#f4f7fa')
    d.text(708,928,'native-egress  [container]',14,mono=True)
    d.rect(1010,905,245,35,fill='#f4f7fa')
    d.text(1022,928,'sdsmint  [native sidecar]',14,mono=True)
    d.edge('M1150 565 V790',dashed=True)
    d.label(1163,768,'mount',size=13)
    d.text(80,1004,'W_p and G_p are independent. All four egress processes share G_p.',14,MUTED)
    d.text(80,1050,'Node pools host platform, worker and gateway Pods; node capacity is managed separately from HPA.',15,MUTED)

    d.boundary(1420,150,390,920,'Shared dependencies','In-cluster or managed services')
    d.database(1450,235,330,170,'PostgreSQL',[
        'kagent / Substrate databases',
        'Per-project bindings DB + role',
        'Auth-state backend A (optional)',
        'Ciphertext, versions, refresh leases'])
    d.database(1450,490,330,150,'Redis (optional)',[
        'Auth-state backend B',
        'Per-project ACL + key prefix',
        'Ciphertext, versions, refresh leases'])
    d.component(1450,780,330,90,'Upstream services','MCP / application APIs', ['Internal or external destinations'])
    d.component(1450,925,330,90,'Upstream service','Authentication endpoint', ['Coordinated token acquisition'])
    d.edge('M1255 808 H1355 V320 H1450')
    d.label(1364,307,'SQL / TLS',size=12)
    d.edge('M1255 832 H1400 V565 H1450')
    d.label(1410,551,'TLS',size=12)
    d.edge('M815 870 V883 H980 V992 H1388 V821 H1450')
    d.label(1005,1012,'Allowed application traffic',size=13)
    d.edge('M1255 855 H1325 V970 H1450')
    d.label(1335,959,'HTTPS',size=12)
    d.text(1450,697,'Vault → GitLab → project Secrets',14,MUTED)
    d.text(1450,720,'Delivery path omitted from runtime view.',13,MUTED)

    d.save('architecture-scaling',exports)

def split(exports):
    d=Drawing('Authorizer isolation — independent scaling alternative',
        'Alternative deployment boundary. Each project owns three independently scaled resources; shared infrastructure is unchanged.', (10,140,1800,880))
    d.boundary(40,165,1740,830,'namespace: <project>',dashed=True)
    d.hpa(100,245,330,'workers','Active demand / resources / backlog')
    d.hpa(655,245,430,'agent-egress','Envoy CPU / measured proxy load')
    d.hpa(1255,245,430,'authorizer','Processor CPU / measured authorization load')
    d.edge('M265 313 V415',dashed=True,color=CONTROL)
    d.edge('M870 313 V390 H1040 V415',dashed=True,color=CONTROL)
    d.edge('M1470 313 V390 H1665 V415',dashed=True,color=CONTROL)
    d.label(277,365,'/scale',color=CONTROL)
    d.label(882,365,'/scale',color=CONTROL)
    d.label(1482,365,'/scale',color=CONTROL)

    d.boundary(80,415,370,390,'WorkerPool','replicas = W_p')
    d.boundary(105,480,320,285,'Pod × W_p')
    d.component(130,525,270,95,'Runtime sandboxes','Agent Sessions', ['All Agents using this pool'])
    d.rect(130,675,270,45,fill='#f4f7fa'); d.text(146,705,'atunnel',18,bold=True)
    d.edge('M265 620 V675')
    d.rect(495,565,130,60,fill='#f4f7fa')
    d.text(506,586,'Service',12,MUTED,mono=True); d.text(506,611,'agent-egress',15,bold=True)
    d.edge('M400 697 H470 V595 H495')
    d.label(470,654,'CONNECT',size=13)

    d.boundary(655,415,430,390,'Deployment/agent-egress','replicas = G_p')
    d.boundary(680,480,380,285,'Pod × G_p')
    d.component(705,550,330,85,'Container','Envoy',['Request forwarding / TLS'])
    d.rect(705,660,330,36,fill='#f4f7fa'); d.text(720,684,'native-egress  [container]',14,mono=True)
    d.rect(705,710,330,36,fill='#f4f7fa'); d.text(720,734,'sdsmint  [native sidecar]',14,mono=True)
    d.edge('M625 595 H705')

    d.rect(1125,565,130,60,fill='#f4f7fa')
    d.text(1137,586,'Service',12,MUTED,mono=True); d.text(1137,611,'authorizer',15,bold=True)
    d.edge('M1035 593 H1125'); d.label(1050,549,'gRPC / mTLS',size=12)
    d.boundary(1300,415,430,390,'Deployment/authorizer','replicas = A_p')
    d.boundary(1325,480,380,285,'Pod × A_p')
    d.component(1350,550,330,85,'Container','authorizer',['ext_proc + Session bridge'])
    d.edge('M1255 595 H1350')
    d.rect(1350,660,330,85,fill='#fafafa')
    d.lines(1365,685,['Dedicated ServiceAccount','Mounted project credentials + key','Local token cache'],size=15,gap=24)

    d.component(80,860,370,95,'Per-Agent resources','Agent + Harness × agents',['References this project’s WorkerPool'])
    d.component(655,860,430,95,'Egress workload identity','ServiceAccount: agent-egress',['Native gateway identity; no auth-state key'])
    d.component(1300,860,430,95,'Authorizer workload identity','ServiceAccount: authorizer',['Project DB / Redis access; optional KMS role'])
    d.edge('M265 860 V805',dashed=True)
    d.edge('M870 860 V805',dashed=True)
    d.edge('M1515 860 V805',dashed=True)

    d.save('architecture-scaling-split',exports)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exports',action='store_true',help='also export PNG and cropped PDF; requires CairoSVG')
    args=parser.parse_args()
    deployment(args.exports)
    split(args.exports)
    print('Rendered architecture-scaling and architecture-scaling-split')
