"""Render a dedicated inspecting Envoy for one agent namespace."""
import copy
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
ENVOY_IMAGE='envoyproxy/envoy:v1.38.6'
ATENET_IMAGE='localhost:5001/auth-demo/substrate-atenet:dedicated'
IDENTITY="filter_state['dev.ate.actor.identity']"
CONNECT="filter_state['dev.ate.connect.authority']"
def processor():
    return {'name':'auth.agent.ext_proc','typed_config':{
      '@type':'type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExternalProcessor',
      'grpc_service':{'envoy_grpc':{'cluster_name':'agent_authorizer'},'timeout':'70s'},
      'failure_mode_allow':False,'message_timeout':'70s','request_attributes':['xds.filter_chain_name',IDENTITY,CONNECT],
      'processing_mode':{'request_header_mode':'SEND','response_header_mode':'SKIP','request_body_mode':'NONE',
        'response_body_mode':'NONE','request_trailer_mode':'SKIP','response_trailer_mode':'SKIP'}}}
def bootstrap():
    config=yaml.safe_load((ROOT/'deploy/envoy-native-base.yaml').read_text())
    resources=config['static_resources']
    outer=next(x for x in resources['listeners'] if x['name']=='egress')
    outer['address']['socket_address']['port_value']=8443
    filters=outer['filter_chains'][0]['filters'][0]['typed_config']['http_filters']
    filters[:]=[f for f in filters if f['name']!='envoy.filters.http.dynamic_modules']
    for f in filters:
      if f['name']=='envoy.filters.http.set_filter_state':
        f['typed_config']['on_request_headers'][:]=[e for e in f['typed_config']['on_request_headers'] if e['object_key']!='dev.ate.policy.egress.hash']
    inner=next(x for x in resources['listeners'] if x['name']=='mitm_listener')
    inner.pop('filter_chain_matcher',None)
    inner['listener_filters']=[f for f in inner['listener_filters'] if f['name']!='envoy.filters.listener.dynamic_modules']
    inner['filter_chains']=[f for f in inner['filter_chains'] if f['name']!='egress_passthrough']
    for chain in inner['filter_chains']:
      tls=chain['name']=='egress_tls_mitm'
      chain['filter_chain_match']={'transport_protocol':'tls' if tls else 'raw_buffer'}
      if not tls:chain['filter_chain_match']['application_protocols']=['http/1.1','h2c']
      manager=chain['filters'][0]['typed_config'];filters=manager['http_filters']
      position=next(i for i,f in enumerate(filters) if f['name']=='envoy.filters.http.dynamic_forward_proxy')
      filters.insert(position,processor())
      if not tls:
        platform=copy.deepcopy(manager['route_config']['virtual_hosts'][0])
        platform.update({'name':'native_platform','domains':['api.ate-system.svc','api.ate-system.svc:*',
           'kagent-controller.kagent.svc','kagent-controller.kagent.svc:*',
           'api.ate-system.svc.cluster.local','api.ate-system.svc.cluster.local:*',
           'kagent-controller.kagent.svc.cluster.local','kagent-controller.kagent.svc.cluster.local:*']})
        platform['typed_per_filter_config']={'auth.agent.ext_proc':{'@type':'type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExtProcPerRoute','disabled':True}}
        manager['route_config']['virtual_hosts'].insert(0,platform)
    resources['clusters'].append({'name':'agent_authorizer','type':'STATIC','connect_timeout':'5s',
      'load_assignment':{'cluster_name':'agent_authorizer','endpoints':[{'lb_endpoints':[{'endpoint':{
        'address':{'socket_address':{'address':'127.0.0.1','port_value':50052}}}}]}]},
      'typed_extension_protocol_options':{'envoy.extensions.upstreams.http.v3.HttpProtocolOptions':{
        '@type':'type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions','explicit_http_config':{'http2_protocol_options':{}}}},
      'transport_socket':{'name':'envoy.transport_sockets.tls','typed_config':{
        '@type':'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext',
        'sni':'authorizer','common_tls_context':{'alpn_protocols':['h2'],
          'tls_params':{'tls_minimum_protocol_version':'TLSv1_3','tls_maximum_protocol_version':'TLSv1_3'},
          'tls_certificates':[{'certificate_chain':{'filename':'/auth-client/tls.crt'},'private_key':{'filename':'/auth-client/tls.key'}}],
          'validation_context':{'trusted_ca':{'filename':'/auth-client/ca.crt'},'match_typed_subject_alt_names':[{
             'san_type':'DNS','matcher':{'exact':'authorizer'}}]}}}}})
    def roots(value):
      if isinstance(value,dict):
        if value.get('filename')=='/etc/ssl/certs/ca-certificates.crt':value['filename']='/trust/upstream-ca.pem'
        for child in value.values():roots(child)
      elif isinstance(value,list):
        for child in value:roots(child)
    roots(config)
    return config

def obj(kind,name,ns,**extra):
    api='rbac.authorization.k8s.io/v1' if kind in ('Role','RoleBinding','ClusterRole','ClusterRoleBinding') else 'apps/v1' if kind=='Deployment' else 'v1'
    return {'apiVersion':api,'kind':kind,'metadata':{'name':name,**({'namespace':ns} if ns else {})},**extra}
def projection(signer):
    return [{'podCertificate':{'signerName':signer,'keyType':'ECDSAP256','credentialBundlePath':'credential-bundle.pem'}},
      {'clusterTrustBundle':{'signerName':signer,'labelSelector':{'matchLabels':{'podcert.ate.dev/canarying':'live'}},'path':'trust-bundle.pem'}}]
def bundle(ns,agent,authorizer_image,ca_pem):
    resources=[]
    namespace=obj('Namespace',ns,None)
    namespace['metadata']['labels']={'ate.dev/api-access':'true'}
    resources.append(namespace)
    resources.append(obj('ServiceAccount','agent-egress',ns))
    resources.append(obj('Role','authorizer-watch',ns,rules=[{'apiGroups':[''],'resources':['serviceaccounts','configmaps'],'verbs':['get','list','watch']}]))
    resources.append(obj('RoleBinding','authorizer-watch',ns,subjects=[{'kind':'ServiceAccount','name':'agent-egress','namespace':ns}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'authorizer-watch'}))
    resources.append(obj('ClusterRole','agent-auth-reviewer',None,rules=[{'apiGroups':['authentication.k8s.io'],'resources':['tokenreviews'],'verbs':['create']},{'apiGroups':['authorization.k8s.io'],'resources':['subjectaccessreviews'],'verbs':['create']}]))
    resources.append(obj('ClusterRoleBinding',ns+'-reviewer',None,subjects=[{'kind':'ServiceAccount','name':'agent-egress','namespace':ns}],roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'agent-auth-reviewer'}))
    sds=yaml.safe_load((ROOT/'deploy/envoy-native-sds.yaml').read_text())
    data={k:yaml.safe_dump(v,sort_keys=False) for k,v in sds.items()};data['envoy.yaml']=yaml.safe_dump(bootstrap(),sort_keys=False)
    resources += [obj('ConfigMap','agent-envoy',ns,data=data),obj('ConfigMap','upstream-trust',ns,data={'upstream-ca.pem':ca_pem+Path('/etc/ssl/certs/ca-certificates.crt').read_text()})]
    selector={'app':'agent-egress'}
    resources += [obj('Service','agent-egress',ns,spec={'selector':selector,'ports':[{'name':'https','port':443,'targetPort':8443}]}),
      obj('Service','authorizer',ns,spec={'selector':selector,'ports':[{'port':8443,'targetPort':8444}]})]
    # HTTPS management and the local ext_proc server use distinct ports.
    volumes=[{'name':'config','configMap':{'name':'agent-envoy'}},{'name':'trust','configMap':{'name':'upstream-trust'}},
      {'name':'sdsmint-sock','emptyDir':{}},
      {'name':'servicedns','projected':{'sources':projection('servicedns.podcert.ate.dev/identity')}},
      {'name':'podidentity','projected':{'sources':projection('podidentity.podcert.ate.dev/identity')}},
      {'name':'postgres-ca','projected':{'sources':[{'clusterTrustBundle':{'signerName':'servicedns.podcert.ate.dev/identity','labelSelector':{'matchLabels':{'podcert.ate.dev/canarying':'live'}},'path':'trust-bundle.pem'}}]}},
      *[{'name':n,'secret':{'secretName':n}} for n in ['actor-id-ca-certs','egress-mitm-ca-pool','auth-client','authorizer-tls','application-credentials']]]
    mount=lambda n,p:{'name':n,'mountPath':p,'readOnly':n!='sdsmint-sock'}
    common=[mount('servicedns','/run/servicedns.podcert.ate.dev'),mount('podidentity','/run/podidentity.podcert.ate.dev'),mount('actor-id-ca-certs','/run/actor-id-ca-certs')]
    limits={'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'cpu':'1','memory':'512Mi'}}
    issuer={'name':'sdsmint','image':ATENET_IMAGE,'restartPolicy':'Always','args':['sdsmint','--uds-path=/var/run/sdsmint/sdsmint.sock','--ca-pool-path=/ca/pool','--leaf-cert-ttl=15m'],
      'volumeMounts':[mount('sdsmint-sock','/var/run/sdsmint'),mount('egress-mitm-ca-pool','/ca')],'resources':limits}
    native={'name':'native-egress','image':ATENET_IMAGE,'args':['router','--mode=egress','--namespace='+ns,
      '--port-extproc=50051','--extproc-address=127.0.0.1','--ateapi-address=dns:///api.ate-system.svc:443',
      '--ateapi-ca-file=/run/servicedns.podcert.ate.dev/trust-bundle.pem','--ateapi-client-cert=/run/podidentity.podcert.ate.dev/credential-bundle.pem',
      '--actor-identity-ca-file=/run/actor-id-ca-certs/ca.crt','--otlp-collector-address=','--envoy-admin-address=127.0.0.1:15000'],
      'env':[{'name':'OTEL_SDK_DISABLED','value':'true'}],'volumeMounts':common,'resources':limits}
    envoy={'name':'envoy','image':ENVOY_IMAGE,'args':['-c','/etc/envoy/envoy.yaml','--concurrency','2'],
      'volumeMounts':common+[mount('config','/etc/envoy'),mount('trust','/trust'),mount('sdsmint-sock','/var/run/sdsmint'),mount('auth-client','/auth-client')],
      'resources':limits,'readinessProbe':{'httpGet':{'path':'/ready','port':15090}},'ports':[{'containerPort':8443}]}
    auth={'name':'authorizer','image':authorizer_image,'env':[{'name':'DEMO_NAMESPACE','value':ns},{'name':'AGENT_NAME','value':agent},
      {'name':'CREDENTIALS_FILE','value':'/credentials/credentials.json'},{'name':'SSL_CERT_FILE','value':'/trust/upstream-ca.pem'},{'name':'DATABASE_URL','valueFrom':{'secretKeyRef':{'name':'authorizer-db','key':'DATABASE_URL'}}}],
      'volumeMounts':[mount('application-credentials','/credentials'),mount('trust','/trust'),mount('authorizer-tls','/tls'),mount('postgres-ca','/run/postgres-ca')],
      'resources':limits,'readinessProbe':{'httpGet':{'path':'/readyz','port':8444,'scheme':'HTTPS'}}}
    resources.append(obj('Deployment','agent-egress',ns,spec={'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':selector},'template':{
      'metadata':{'labels':selector},'spec':{'serviceAccountName':'agent-egress','securityContext':{'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532},
      'initContainers':[issuer],'containers':[envoy,native,auth],'volumes':volumes}}}))
    return resources
