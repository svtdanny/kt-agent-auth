#!/usr/bin/env python3
"""Install only the enrolled Pod, its private gateway, and cross-namespace fixture."""
import base64
import hashlib
import http.client
import json
import shutil
import ssl
import subprocess
import time

import yaml
from database import ROOT,ENV,kubectl,apply
from gateway_overlay import MCP_HOST,processor

CATALOG='https://catalog-api.demo.svc.cluster.local'
MCP='http://'+MCP_HOST+':8080'
GATEWAY_IMAGE='ghcr.io/kagent-dev/substrate/agentgateway:3554db38a0b2@sha256:309f6811560635471958db67b0f68dd083d84048dffc681967599d159ebfc83d'
ENVOY_IMAGE='envoyproxy/envoy:v1.37.7'


def permission_set(names):
    rules={'catalog':{'id':'catalog-read','type':'http-request/v1','config':{
                'origin':CATALOG,'methods':['GET'],'paths':['/catalog'],'credential':'read-token'}},
           'mcp':{'id':'internal-mcp','type':'http-request/v1','config':{
                'origin':MCP,'methods':['POST'],'paths':['/mcp'],'credential':'mcp-token'}}}
    return {'permissions':[rules[name] for name in names]}


def set_permissions(names):
    patch={'metadata':{'annotations':{'auth.demo.dev/policy-type':'permission-set/v1',
                                     'auth.demo.dev/policy-config':json.dumps(permission_set(names))}}}
    kubectl('-n','demo','patch','serviceaccount','multi-reader','--type=merge','-p',json.dumps(patch))


def relay_config():
    return {'static_resources':{
        'listeners':[{'name':'capture','address':{'socket_address':{'address':'127.0.0.1','port_value':15001}},
          'listener_filters':[{'name':'envoy.filters.listener.original_dst','typed_config':{
              '@type':'type.googleapis.com/envoy.extensions.filters.listener.original_dst.v3.OriginalDst'}}],
          'filter_chains':[{'filters':[{'name':'envoy.filters.network.tcp_proxy','typed_config':{
              '@type':'type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy',
              'stat_prefix':'enrolled_pod','cluster':'dedicated_egress',
              'tunneling_config':{'hostname':'%DOWNSTREAM_LOCAL_ADDRESS%'},
              'access_log':[{'name':'envoy.access_loggers.stdout','typed_config':{
                  '@type':'type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog',
                  'log_format':{'text_format_source':{'inline_string':'captured %DOWNSTREAM_LOCAL_ADDRESS% %RESPONSE_FLAGS%\n'}}}}]}}]}]}],
        'clusters':[{'name':'dedicated_egress','type':'STRICT_DNS','connect_timeout':'5s','dns_lookup_family':'V4_ONLY',
          'load_assignment':{'cluster_name':'dedicated_egress','endpoints':[{'lb_endpoints':[{'endpoint':{
              'address':{'socket_address':{'address':'pod-egress.demo.svc.cluster.local','port_value':8443}}}}]}]},
          'typed_extension_protocol_options':{'envoy.extensions.upstreams.http.v3.HttpProtocolOptions':{
              '@type':'type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions',
              'explicit_http_config':{'http_protocol_options':{}}}},
          'transport_socket':{'name':'envoy.transport_sockets.tls','typed_config':{
              '@type':'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext',
              'sni':'pod-egress.demo.svc.cluster.local','common_tls_context':{
                  'alpn_protocols':['http/1.1'],
                  'tls_certificates':[{'certificate_chain':{'filename':'/identity/tls.crt'},'private_key':{'filename':'/identity/tls.key'}}],
                  'validation_context':{'trusted_ca':{'filename':'/identity/ca.crt'},'match_typed_subject_alt_names':[{
                      'san_type':'DNS','matcher':{'exact':'pod-egress.demo.svc.cluster.local'}}]}}}}} ]}}


def gateway_config():
    # Backends are selected from gateway-owned routes. A forged HTTP Host can
    # never authorize credentials for an arbitrary CONNECT target IP.
    https={'protocol':'HTTPS','tls':{'mode':'dynamicCa','cert':'/intercept/tls.crt','key':'/intercept/tls.key'},
           'routes':[{'hostnames':['catalog-api.demo.svc.cluster.local'],
               'backends':[{'host':'catalog-api.demo.svc.cluster.local:443','policies':{'backendTLS':{'root':'/tls/ca.crt'}}}],
               'policies':{'extProc':processor('https')}}]}
    http={'protocol':'HTTP','routes':[{'hostnames':[MCP_HOST],
           'backends':[{'host':MCP_HOST+':8080'}],'policies':{'extProc':processor('http')}}]}
    return {'frontendPolicies':{'connect':{'mode':'tunnel'}},
        'gateways':{'pod-egress':{'port':8443,'bindAddress':'$POD_IP','protocol':'HTTPS',
            'tls':{'cert':'/tls/tls.crt','key':'/tls/tls.key','root':'/tls/ca.crt'}}},
        'binds':[{'mode':'internal','protocol':'AUTO','listeners':[https,http]}]}


def install():
    from deploy import obj,certificates,tls_secret,deployment,service
    from vault_demo import configure_client,client_env
    directory=certificates()
    ca=directory/'pod-intercept-ca.crt'
    if not ca.exists():
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','7','-subj','/CN=auth-pod-interception',
            '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
            '-keyout',str(directory/'pod-intercept-ca.key'),'-out',str(ca)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        (directory/'pod-intercept-ca.key').chmod(0o600)
    config=yaml.safe_dump(gateway_config(),sort_keys=False)
    gateway=deployment('pod-egress','ext-proc','pod-egress-tls',port=8443)
    pod=gateway['spec']['template']['spec'];container=pod['containers'][0]
    container.update({'image':GATEWAY_IMAGE,'command':['/app/agentgateway'],'args':['-f','/config/config.yaml'],
        'env':[{'name':'POD_IP','valueFrom':{'fieldRef':{'fieldPath':'status.podIP'}}}],
        'readinessProbe':{'httpGet':{'path':'/healthz/ready','port':15021},'periodSeconds':2}})
    gateway['spec']['template']['metadata']['annotations']['auth.demo.dev/config-hash']=hashlib.sha256(config.encode()).hexdigest()
    pod['volumes'] += [{'name':'config','configMap':{'name':'pod-egress-config'}},
                       {'name':'processor','secret':{'secretName':'auth-pod-gateway-client'}},
                       {'name':'intercept','secret':{'secretName':'auth-pod-intercept-ca'}}]
    container['volumeMounts'] += [{'name':'config','mountPath':'/config','readOnly':True},
                                {'name':'processor','mountPath':'/run/auth-extproc','readOnly':True},
                                {'name':'intercept','mountPath':'/intercept','readOnly':True}]
    sa=obj('ServiceAccount','multi-reader',automountServiceAccountToken=False)
    sa['metadata'].update({'labels':{'auth.demo.dev/managed':'true'},'annotations':{
        'auth.demo.dev/policy-type':'permission-set/v1','auth.demo.dev/policy-config':json.dumps(permission_set(['catalog','mcp']))}})
    objects=[sa,tls_secret('pod-egress-tls','pod-egress',directory),
        tls_secret('auth-pod-gateway-client','agentgateway',directory),
        tls_secret('auth-pod-intercept-ca','pod-intercept-ca',directory),
        obj('ConfigMap','pod-egress-config',data={'config.yaml':config}),gateway,service('pod-egress',8443,8443),
        {'apiVersion':'v1','kind':'Namespace','metadata':{'name':'test-services'}}]
    apply(objects)
    mock=configure_client(deployment('mock-mcp','test-api',None,port=8080,account='mock-mcp',env=client_env('auth-mcp')+[
        {'name':'MOCK_PLAINTEXT','value':'true'},{'name':'MOCK_READER_REF','value':'mock/mcp-reader'},
        {'name':'MOCK_WRITER_REF','value':'mock/mcp-writer'}]))
    mock['metadata']['namespace']='test-services'
    mock_svc=service('mock-mcp',8080,8080);mock_svc['metadata']['namespace']='test-services'
    mock_sa={'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'mock-mcp','namespace':'test-services'}}
    apply([mock_sa,
           {'apiVersion':'v1','kind':'Secret','metadata':{'name':'auth-control-plane-tls','namespace':'test-services'},
            'data':{'ca.crt':base64.b64encode((directory/'ca.crt').read_bytes()).decode()}},mock,mock_svc])
    kubectl('-n','demo','rollout','status','deployment/pod-egress','--timeout=120s')
    kubectl('-n','test-services','rollout','status','deployment/mock-mcp','--timeout=120s')

    # Our explicit fixture launcher replaces only its own enrolled deployment.
    found=subprocess.run(['kubectl','--context','auth-demo','-n','demo','get','deployment','command-runner'],env=ENV,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if found.returncode==0:
        kubectl('-n','demo','scale','deployment/command-runner','--replicas=0')
        kubectl('-n','demo','wait','--for=delete','pod','-l','app=command-runner','--timeout=90s')
    kubectl('-n','demo','delete','secret','command-runner-identity','--ignore-not-found=true')
    gateway_ip=json.loads(kubectl('-n','demo','get','service','pod-egress','-o','json'))['spec']['clusterIP']
    dns_ip=json.loads(kubectl('-n','kube-system','get','service','kube-dns','-o','json'))['spec']['clusterIP']
    runner=runner_deployment(gateway_ip,dns_ip)
    (ROOT/'deploy/command-runner.yaml').write_text(yaml.safe_dump(runner,sort_keys=False))
    runner_objects=[obj('ConfigMap','command-runner-relay',data={'envoy.yaml':yaml.safe_dump(relay_config(),sort_keys=False)}),
        obj('ConfigMap','pod-egress-trust',data={'ca-bundle.pem':(directory/'ca.crt').read_text()+ca.read_text()}),
        runner]
    apply(runner_objects)
    # Reviewable resources contain no private keys or credential values.
    public=[o for o in objects if o['kind']!='Secret']+[mock_sa,mock,mock_svc]+runner_objects
    (ROOT/'deploy/pod-egress-demo.yaml').write_text(yaml.safe_dump_all(public,sort_keys=False))
    for _ in range(60):
        pods=json.loads(kubectl('-n','demo','get','pods','-l','app=command-runner','-o','json'))['items']
        if len(pods)==1:break
        time.sleep(1)
    else:raise RuntimeError('specific agent Pod was not created')
    identity=pods[0]['metadata'];uid=identity['uid']
    issue_pod_certificate(directory,uid)
    apply([tls_secret('command-runner-identity','command-runner',directory)])
    kubectl('-n','demo','rollout','status','deployment/command-runner','--timeout=180s')
    register_pod(identity,directory)
    return identity


def register_pod(identity,directory):
    """Trusted demo launcher enrolls the observed UID before permitting traffic."""
    context=ssl.create_default_context(cafile=str(directory/'ca.crt'))
    context.load_cert_chain(str(directory/'ext-proc.crt'),str(directory/'ext-proc.key'))
    with open(ROOT/'.cache/pod-registration-forward.log','w') as log:
        forward=subprocess.Popen(['kubectl','--context','auth-demo','-n','demo','port-forward',
            'service/auth-control-plane','18447:8443'],env=ENV,stdout=log,stderr=log)
    try:
        for _ in range(60):
            if forward.poll() is not None:raise RuntimeError('Pod registration port-forward exited')
            connection=http.client.HTTPSConnection('localhost',18447,context=context,timeout=10)
            try:
                connection.request('POST','/internal/pods',json.dumps({'namespace':'demo','name':identity['name'],'uid':identity['uid']}),
                    {'Content-Type':'application/json'})
                response=connection.getresponse();response.read()
                if response.status!=200:raise RuntimeError('Pod enrollment rejected: HTTP '+str(response.status))
                return
            except OSError:time.sleep(1)
            finally:connection.close()
        raise RuntimeError('Pod registration did not become reachable')
    finally:
        forward.terminate()
        try:forward.wait(timeout=10)
        except subprocess.TimeoutExpired:forward.kill();forward.wait()


def prepare_images():
    found=subprocess.run(['docker','image','inspect',ENVOY_IMAGE],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if found.returncode:subprocess.run(['docker','pull',ENVOY_IMAGE],check=True)
    minikube=shutil.which('minikube') or str(ROOT/'.cache/bin/minikube')
    subprocess.run([minikube,'-p','auth-demo','image','load','--daemon',
        'localhost:5001/auth-demo/pod-runner:demo',ENVOY_IMAGE],check=True,env=ENV)


def issue_pod_certificate(directory,uid):
    ext=directory/'command-runner.ext'
    ext.write_text('extendedKeyUsage=clientAuth\nsubjectAltName=URI:spiffe://auth-demo.local/ns/demo/pod/'+uid+'\n')
    subprocess.run(['openssl','req','-newkey','rsa:2048','-nodes','-subj','/CN=command-runner',
        '-keyout',str(directory/'command-runner.key'),'-out',str(directory/'command-runner.csr')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['openssl','x509','-req','-in',str(directory/'command-runner.csr'),'-CA',str(directory/'ca.crt'),
        '-CAkey',str(directory/'ca.key'),'-CAcreateserial','-days','7','-extfile',str(ext),
        '-out',str(directory/'command-runner.crt')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (directory/'command-runner.key').chmod(0o600)


def runner_deployment(gateway_ip,dns_ip):
    image='localhost:5001/auth-demo/pod-runner:demo'
    digest=json.loads(subprocess.check_output(['docker','inspect',image],text=True))[0]['Id']
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'command-runner','namespace':'demo'},'spec':{
        'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'command-runner'}},'template':{
            'metadata':{'labels':{'app':'command-runner','auth.demo.dev/egress':'enforced'},'annotations':{'auth.demo.dev/image-id':digest}},
            'spec':{'serviceAccountName':'multi-reader','automountServiceAccountToken':False,
                'securityContext':{'fsGroup':1337,'seccompProfile':{'type':'RuntimeDefault'}},
                'initContainers':[{'name':'capture','image':image,'imagePullPolicy':'Never',
                    'env':[{'name':'AUTH_EGRESS_IP','value':gateway_ip},{'name':'AUTH_DNS_IP','value':dns_ip}],
                    'command':['/bin/sh','/app/pod-capture.sh'],'securityContext':{'runAsUser':0,'allowPrivilegeEscalation':False,
                        'capabilities':{'drop':['ALL'],'add':['NET_ADMIN','NET_RAW']}}},
                    {'name':'relay','image':ENVOY_IMAGE,'restartPolicy':'Always','args':['-c','/relay/envoy.yaml'],
                     'securityContext':{'runAsUser':1337,'runAsGroup':1337,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
                     'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'1','memory':'256Mi'}},
                     'volumeMounts':[{'name':'relay','mountPath':'/relay','readOnly':True},{'name':'identity','mountPath':'/identity','readOnly':True}]}],
                'containers':[{'name':'mastra','image':image,'imagePullPolicy':'Never','command':['node','/app/agent/server.mjs'],
                    'securityContext':{'runAsUser':65532,'runAsGroup':65532,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
                    'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
                    'env':[{'name':'DEMO_API_ORIGIN','value':CATALOG},{'name':'NODE_EXTRA_CA_CERTS','value':'/trust/ca-bundle.pem'},
                           {'name':'CURL_CA_BUNDLE','value':'/trust/ca-bundle.pem'}],
                    'volumeMounts':[{'name':'trust','mountPath':'/trust','readOnly':True}]}],
                'volumes':[{'name':'relay','configMap':{'name':'command-runner-relay'}},
                           {'name':'identity','secret':{'secretName':'command-runner-identity','defaultMode':0o440}},
                           {'name':'trust','configMap':{'name':'pod-egress-trust'}}]}}}}


if __name__=='__main__':
    prepare_images()
    result=install();print('Specific Pod deployed:',result['name'],result['uid'])
