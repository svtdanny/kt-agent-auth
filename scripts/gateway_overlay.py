"""The deployment-only gateway overlay, shared by Helm rendering and verification."""
import hashlib
import copy
import yaml

MCP_HOST='mock-mcp.test-services.svc.cluster.local'

def processor(scheme):
    return {'host':'auth-ext-proc.demo.svc.cluster.local:50051','failureMode':'failClosed',
            'policies':{'backendTLS':{'root':'/run/auth-extproc/ca.crt','cert':'/run/auth-extproc/tls.crt','key':'/run/auth-extproc/tls.key'}},
            'processingOptions':{'requestHeaderMode':'send','requestBodyMode':'none','responseHeaderMode':'skip','responseBodyMode':'none','requestTrailerMode':'skip','responseTrailerMode':'skip'},
            'requestAttributes':{'actorSpiffeId':'source.spiffeId','destinationPort':'destination.port','scheme': '"'+scheme+'"'}}

def config_overlay(config):
    for bind in config['binds']:
        for listener in bind['listeners']:
            if listener['protocol']=='HTTP':
                # Only the explicit test service receives HTTP processing. Native
                # platform/TaskStore routes retain their original behavior.
                original=next(r for r in listener['routes'] if r.get('hostnames')!=[MCP_HOST])
                route=copy.deepcopy(original)
                route['hostnames']=[MCP_HOST]
                # Substrate requires a dynamic backend: its native policy
                # resolves the authorized HTTP authority and CONNECT port.
                route['backends']=[{'dynamic':{}}]
                route['policies']['extProc']=processor('http')
                listener['routes']=[route]+[r for r in listener['routes'] if r.get('hostnames')!=[MCP_HOST]]
            if listener['protocol']!='HTTPS':continue
            for route in listener['routes']:
                route['policies']['extProc']=processor('https')
                route['backends'][0]['policies']['backendTLS']['root']='/run/auth-extproc/ca.crt'
    return yaml.safe_dump(config,sort_keys=False)

def render(documents):
    cm=next(x for x in documents if x and x.get('kind')=='ConfigMap' and x['metadata']['name'].endswith('atenet-egress-agentgateway-config'))
    raw=config_overlay(yaml.safe_load(cm['data']['config.yaml']))
    cm['data']['config.yaml']=raw
    deployment=next(x for x in documents if x and x.get('kind')=='Deployment' and x['metadata']['name']=='atenet-egress')
    template=deployment['spec']['template']
    template.setdefault('metadata',{}).setdefault('annotations',{})['auth.demo.dev/config-hash']=hashlib.sha256(raw.encode()).hexdigest()
    pod=template['spec']
    pod.setdefault('volumes',[]).append({'name':'auth-extproc','secret':{'secretName':'auth-gateway-client'}})
    container=next(x for x in pod['containers'] if x['name']=='agentgateway')
    container.setdefault('volumeMounts',[]).append({'name':'auth-extproc','mountPath':'/run/auth-extproc','readOnly':True})
    return documents
