#!/usr/bin/env python3
"""One Envoy deployment: native Actor and managed Pod CONNECT listeners."""
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import yaml
from database import ROOT,ENV,kubectl,apply

ENVOY_IMAGE='envoyproxy/envoy:v1.38.6'
ATENET_IMAGE='localhost:5001/auth-demo/substrate-atenet:demo'
GATEWAY_HOST='atenet-egress.ate-system.svc'
EXTERNAL_HOST='external-api.auth-demo.test'
EXTERNAL_ORIGIN='https://'+EXTERNAL_HOST+':8443'
MCP_HOST='mock-mcp.test-services.svc.cluster.local'
IDENTITY="filter_state['dev.ate.actor.identity']"
CONNECT="filter_state['dev.ate.connect.authority']"
ATTRIBUTES=['xds.filter_chain_name',IDENTITY,CONNECT]


def own_processor():
    return {'name':'auth.demo.ext_proc','typed_config':{
        '@type':'type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExternalProcessor',
        'stat_prefix':'auth_demo','grpc_service':{'envoy_grpc':{'cluster_name':'auth_ext_proc'},'timeout':'10s'},
        'failure_mode_allow':False,'message_timeout':'10s','request_attributes':ATTRIBUTES,
        'processing_mode':{'request_header_mode':'SEND','response_header_mode':'SKIP','request_body_mode':'NONE',
                           'response_body_mode':'NONE','request_trailer_mode':'SKIP','response_trailer_mode':'SKIP'}}}


def endpoint_cluster(name,host,port,tls=False):
    value={'name':name,'type':'STRICT_DNS','connect_timeout':'5s','dns_lookup_family':'V4_ONLY',
        'load_assignment':{'cluster_name':name,'endpoints':[{'lb_endpoints':[{'endpoint':{
            'address':{'socket_address':{'address':host,'port_value':port}}}}]}]}}
    if tls:
        value['transport_socket']={'name':'envoy.transport_sockets.tls','typed_config':{
            '@type':'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext',
            'sni':host,'common_tls_context':{'validation_context':{'trusted_ca':{'filename':'/trust/upstream-ca.pem'},
                'match_typed_subject_alt_names':[{'san_type':'DNS','matcher':{'exact':host}}]}}}}
    return value


def hcm(chain):return chain['filters'][0]['typed_config']


def bootstrap():
    config=yaml.safe_load((ROOT/'deploy/envoy-native-base.yaml').read_text())
    resources=config['static_resources']
    outer=next(x for x in resources['listeners'] if x['name']=='egress')
    outer['address']['socket_address']['port_value']=8443
    filters=hcm(outer['filter_chains'][0])['http_filters']
    filters[:]=[f for f in filters if f['name']!='envoy.filters.http.dynamic_modules']
    for f in filters:
        if f['name']=='envoy.filters.http.set_filter_state':
            f['typed_config']['on_request_headers'][:]=[e for e in f['typed_config']['on_request_headers'] if e['object_key']!='dev.ate.policy.egress.hash']

    inner=next(x for x in resources['listeners'] if x['name']=='mitm_listener')
    # This MVP inspects all TLS/HTTP; it has no opaque TCP/TLS passthrough.
    # Retain the upstream Actor and HTTP policy processors, without requiring
    # Substrate's optional SNI/passthrough dynamic module in a stock Envoy image.
    inner.pop('filter_chain_matcher',None)
    inner['listener_filters']=[f for f in inner['listener_filters'] if f['name']!='envoy.filters.listener.dynamic_modules']
    inner['filter_chains']=[f for f in inner['filter_chains'] if f['name']!='egress_passthrough']
    for chain in inner['filter_chains']:
        tls=chain['name']=='egress_tls_mitm'
        chain['filter_chain_match']={'transport_protocol':'tls' if tls else 'raw_buffer'}
        if not tls:chain['filter_chain_match']['application_protocols']=['http/1.1','h2c']
        manager=hcm(chain)
        # Log all test requests with identity and chosen upstream; no keys.
        for log in manager.get('access_log',[]):log.pop('filter',None)
        filters=manager['http_filters']
        position=next(i for i,f in enumerate(filters) if f['name']=='envoy.filters.http.dynamic_forward_proxy')
        filters.insert(position,own_processor())
        if not tls:
            platform=copy.deepcopy(manager['route_config']['virtual_hosts'][0])
            platform.update({'name':'native_platform','domains':[
                'api.ate-system.svc','api.ate-system.svc:*',
                'kagent-controller.kagent.svc.cluster.local','kagent-controller.kagent.svc.cluster.local:*']})
            platform['typed_per_filter_config']={'auth.demo.ext_proc':{
                '@type':'type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExtProcPerRoute','disabled':True}}
            manager['route_config']['virtual_hosts'].insert(0,platform)

    # Same gateway, separate trust boundary for ordinary Pods. The outer
    # listener overwrites certificate metadata; nothing inside the tunnel can
    # choose the caller identity. Native Actor checks never accept Pod certs.
    pod_outer=copy.deepcopy(outer)
    pod_outer['name']='managed_pods'
    pod_outer['address']['socket_address']['port_value']=9443
    chain=pod_outer['filter_chains'][0];chain['name']='managed_pod_connect'
    chain['transport_socket']['typed_config']['common_tls_context'].update({
        'tls_certificates':[{'certificate_chain':{'filename':'/pod-tls/tls.crt'},'private_key':{'filename':'/pod-tls/tls.key'}}],
        'validation_context':{'trusted_ca':{'filename':'/pod-tls/ca.crt'}}})
    chain['transport_socket']['typed_config']['common_tls_context'].pop('tls_certificate_sds_secret_configs',None)
    manager=hcm(chain);manager['stat_prefix']='managed_pod_connect'
    manager['http_filters']=[
        copy.deepcopy(hcm(outer['filter_chains'][0])['http_filters'][0]),own_processor(),
        {'name':'envoy.filters.http.router','typed_config':{'@type':'type.googleapis.com/envoy.extensions.filters.http.router.v3.Router'}}]
    for vh in manager['route_config']['virtual_hosts']:
        for route in vh['routes']:route['route']['cluster']='managed_pod_internal'

    pod_inner=copy.deepcopy(inner);pod_inner['name']='managed_pod_inspection'
    pod_inner.pop('access_log',None)
    for chain in pod_inner['filter_chains']:
        tls=chain['name']=='egress_tls_mitm'
        chain['name']='managed_pod_tls' if tls else 'managed_pod_http'
        manager=hcm(chain);manager['stat_prefix']=chain['name']
        destinations=[('catalog-api.demo.svc.cluster.local','pod_catalog'),(EXTERNAL_HOST,'pod_external'),('example.com','pod_public')] if tls else [(MCP_HOST,'pod_mcp')]
        manager['route_config']={'name':chain['name'],'virtual_hosts':[{
            'name':name,'domains':[host,host+':*'],'routes':[{'match':{'prefix':'/'},'route':{'cluster':name,'timeout':'30s'}}]}
            for host,name in destinations]}
        manager['http_filters']=[own_processor(),{'name':'envoy.filters.http.router','typed_config':{
            '@type':'type.googleapis.com/envoy.extensions.filters.http.router.v3.Router'}}]

    internal=copy.deepcopy(next(c for c in resources['clusters'] if c['name']=='mitm_internal'))
    internal['name']=internal['load_assignment']['cluster_name']='managed_pod_internal'
    internal['load_assignment']['endpoints'][0]['lb_endpoints'][0]['endpoint']['address']['envoy_internal_address']['server_listener_name']='managed_pod_inspection'
    auth=endpoint_cluster('auth_ext_proc','auth-ext-proc.demo.svc.cluster.local',50051,True)
    auth['typed_extension_protocol_options']={'envoy.extensions.upstreams.http.v3.HttpProtocolOptions':{
        '@type':'type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions','explicit_http_config':{'http2_protocol_options':{}}}}
    context=auth['transport_socket']['typed_config']['common_tls_context']
    context['alpn_protocols']=['h2']
    context['tls_params']={'tls_minimum_protocol_version':'TLSv1_3','tls_maximum_protocol_version':'TLSv1_3'}
    context['tls_certificates']=[{'certificate_chain':{'filename':'/auth-client/tls.crt'},'private_key':{'filename':'/auth-client/tls.key'}}]
    context['validation_context']['trusted_ca']['filename']='/auth-client/ca.crt'
    resources['listeners'] += [pod_outer,pod_inner]
    resources['clusters'] += [internal,auth,endpoint_cluster('pod_catalog','catalog-api.demo.svc.cluster.local',443,True),
        endpoint_cluster('pod_mcp',MCP_HOST,8080),endpoint_cluster('pod_external',EXTERNAL_HOST,8443,True),
        endpoint_cluster('pod_public','example.com',443,True)]
    # Both private mock roots and public WebPKI roots, with verification enabled.
    def roots(value):
        if isinstance(value,dict):
            if value.get('filename')=='/etc/ssl/certs/ca-certificates.crt':value['filename']='/trust/upstream-ca.pem'
            for child in value.values():roots(child)
        elif isinstance(value,list):
            for child in value:roots(child)
    roots(config)
    return config


def prepare_images():
    found=subprocess.run(['docker','image','inspect',ATENET_IMAGE],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if found.returncode:
        source=json.loads(subprocess.check_output(['go','mod','download','-json','github.com/kagent-dev/substrate@v0.6.0-alpha3'],text=True))['Dir']
        subprocess.run(['go','build','-trimpath','-ldflags=-s -w','-o',str(ROOT/'.cache/build/substrate-atenet'),'./cmd/atenet'],
            cwd=source,env={**ENV,'CGO_ENABLED':'0'},check=True)
        subprocess.run(['docker','build','-f','deploy/Dockerfile.services','--build-arg','BINARY=substrate-atenet','-t',ATENET_IMAGE,'.'],cwd=ROOT,check=True)
    found=subprocess.run(['docker','image','inspect',ENVOY_IMAGE],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if found.returncode:subprocess.run(['docker','pull',ENVOY_IMAGE],check=True)
    minikube=shutil.which('minikube') or str(ROOT/'.cache/bin/minikube')
    subprocess.run([minikube,'-p','auth-demo','image','load','--daemon',ATENET_IMAGE,ENVOY_IMAGE],env=ENV,check=True)


def gateway_ingress_policy():
    # Selecting a Pod isolates every ingress port on an enforcing CNI. Keep
    # native Actor traffic and the existing ready/metrics listener reachable;
    # only the additional managed-Pod listener has the enrollment selector.
    return {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'auth-managed-pod-gateway','namespace':'ate-system'},'spec':{
        'podSelector':{'matchLabels':{'app':'atenet-egress'}},'policyTypes':['Ingress'],'ingress':[
            {'ports':[{'protocol':'TCP','port':8443},{'protocol':'TCP','port':15090}]},
            {'from':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'demo'}},
                      'podSelector':{'matchLabels':{'auth.demo.dev/egress':'enforced'}}}],
             'ports':[{'protocol':'TCP','port':9443}]}]}}


def install(directory=None):
    from deploy import certificates,tls_secret
    directory=directory or certificates()
    prepare_images()
    config=yaml.safe_dump(bootstrap(),sort_keys=False)
    sds=yaml.safe_load((ROOT/'deploy/envoy-native-sds.yaml').read_text())
    data={k:yaml.safe_dump(v,sort_keys=False) for k,v in sds.items()}
    data['envoy.yaml']=config
    ca=(directory/'ca.crt').read_text()+Path('/etc/ssl/certs/ca-certificates.crt').read_text()
    pod_ingress=gateway_ingress_policy()
    apply([pod_ingress,{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'auth-shared-envoy','namespace':'ate-system'},'data':data},
        {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'auth-upstream-trust','namespace':'ate-system'},'data':{'upstream-ca.pem':ca}},
        tls_secret('auth-gateway-client','agentgateway',directory,'ate-system'),
        tls_secret('auth-managed-pod-listener','shared-egress',directory,'ate-system')])
    # Start from upstream projected identities, native processor and SDS issuer.
    # The pinned bootstrap lives in the repo; the Pod spec is small and explicit.
    projection=lambda signer:[{'podCertificate':{'signerName':signer,'keyType':'ECDSAP256','credentialBundlePath':'credential-bundle.pem'}},
        {'clusterTrustBundle':{'signerName':signer,'labelSelector':{'matchLabels':{'podcert.ate.dev/canarying':'live'}},'path':'trust-bundle.pem'}}]
    volumes=[{'name':'config','configMap':{'name':'auth-shared-envoy'}},{'name':'sdsmint-sock','emptyDir':{}},
        {'name':'servicedns','projected':{'sources':projection('servicedns.podcert.ate.dev/identity')}},
        {'name':'podidentity','projected':{'sources':projection('podidentity.podcert.ate.dev/identity')}},
        {'name':'actor-id-ca-certs','secret':{'secretName':'actor-id-ca-certs'}},
        {'name':'ca-state','secret':{'secretName':'egress-mitm-ca-pool','items':[{'key':'pool','path':'mitm-pool.json'}]}},
        {'name':'auth-client','secret':{'secretName':'auth-gateway-client'}},
        {'name':'pod-tls','secret':{'secretName':'auth-managed-pod-listener'}},
        {'name':'trust','configMap':{'name':'auth-upstream-trust'}}]
    mount=lambda name,path:{'name':name,'mountPath':path,'readOnly':name!='sdsmint-sock'}
    mounts=[mount('config','/etc/envoy'),mount('sdsmint-sock','/var/run/sdsmint'),
        mount('servicedns','/run/servicedns.podcert.ate.dev'),mount('podidentity','/run/podidentity.podcert.ate.dev'),
        mount('actor-id-ca-certs','/run/actor-id-ca-certs'),mount('auth-client','/auth-client'),mount('pod-tls','/pod-tls'),mount('trust','/trust')]
    limits={'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'cpu':'2','memory':'512Mi'}}
    issuer={'name':'sdsmint','image':ATENET_IMAGE,'imagePullPolicy':'Never','restartPolicy':'Always',
        'args':['sdsmint','--uds-path=/var/run/sdsmint/sdsmint.sock','--ca-pool-path=/run/ca-state/mitm-pool.json','--leaf-cert-ttl=15m'],
        'volumeMounts':[mount('sdsmint-sock','/var/run/sdsmint'),mount('ca-state','/run/ca-state')],'resources':limits}
    native={'name':'native-egress','image':ATENET_IMAGE,'imagePullPolicy':'Never','args':['router','--mode=egress','--namespace=ate-system',
        '--port-extproc=50051','--extproc-address=127.0.0.1','--ateapi-address=dns:///api.ate-system.svc:443',
        '--ateapi-ca-file=/run/servicedns.podcert.ate.dev/trust-bundle.pem',
        '--ateapi-client-cert=/run/podidentity.podcert.ate.dev/credential-bundle.pem',
        '--actor-identity-ca-file=/run/actor-id-ca-certs/ca.crt','--otlp-collector-address=','--envoy-admin-address=127.0.0.1:15000'],
        'env':[{'name':'OTEL_SDK_DISABLED','value':'true'}],
        'volumeMounts':[m for m in mounts if m['name'] in ['servicedns','podidentity','actor-id-ca-certs']],'resources':limits}
    envoy={'name':'envoy','image':ENVOY_IMAGE,'args':['-c','/etc/envoy/envoy.yaml','--concurrency','2'],
        'volumeMounts':mounts,'resources':limits,'ports':[{'name':'https','containerPort':8443},{'name':'managed-pods','containerPort':9443},{'name':'readiness','containerPort':15090}],
        'readinessProbe':{'httpGet':{'path':'/ready','port':15090},'periodSeconds':2}}
    deployment={'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'atenet-egress','namespace':'ate-system'},'spec':{
        'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'atenet-egress'}},'template':{
            'metadata':{'labels':{'app':'atenet-egress'},'annotations':{'auth.demo.dev/config-hash':hashlib.sha256(config.encode()).hexdigest()}},
            'spec':{'serviceAccountName':'atenet-egress','automountServiceAccountToken':False,
                'securityContext':{'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532},
                'initContainers':[issuer],'containers':[envoy,native],'volumes':volumes}}}}
    live=json.loads(kubectl('-n','ate-system','get','deployment','atenet-egress','-o','json'))
    deployment['metadata'].update({k:live['metadata'][k] for k in ['resourceVersion','annotations','labels'] if k in live['metadata']})
    kubectl('replace','-f','-',input=json.dumps(deployment))
    service=json.loads(kubectl('-n','ate-system','get','service','atenet-egress','-o','json'))
    service['spec']['ports']=[{'name':'https','port':443,'targetPort':8443},{'name':'managed-pods','port':8443,'targetPort':9443}]
    kubectl('replace','-f','-',input=json.dumps(service))
    kubectl('-n','ate-system','rollout','status','deployment/atenet-egress','--timeout=180s')
    deployment['metadata']={'name':'atenet-egress','namespace':'ate-system'}
    public=[pod_ingress,{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'auth-upstream-trust','namespace':'ate-system'},'data':{'upstream-ca.pem':ca}},
        {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'auth-shared-envoy','namespace':'ate-system'},'data':data},deployment,
        {'apiVersion':'v1','kind':'Service','metadata':{'name':'atenet-egress','namespace':'ate-system'},'spec':{
            'selector':{'app':'atenet-egress'},'ports':service['spec']['ports']}}]
    (ROOT/'deploy/shared-envoy.yaml').write_text(yaml.safe_dump_all(public,sort_keys=False))


if __name__=='__main__':install()
