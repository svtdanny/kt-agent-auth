"""Explicit local-cluster helpers; never changes the user's default context."""
import json,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.cache'
CONTEXT=os.environ.get('AUTH_CONTEXT','auth-demo')
ENV={**os.environ,'KUBECONFIG':os.environ.get('AUTH_KUBECONFIG',str(CACHE/'kubeconfig'))}
def kubectl(*args,input=None):
    return subprocess.check_output(['kubectl','--context',CONTEXT,*args],env=ENV,input=input,text=True)
def apply(items):
    return kubectl('apply','-f','-',input=json.dumps({'apiVersion':'v1','kind':'List','items':items}))
