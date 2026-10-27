#!/usr/bin/env python3
"""Build only the dedicated agent authorization runtime and pinned routing overlay."""
import os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    env={**os.environ,'GOCACHE':str(ROOT/'.cache/go-build'),'CGO_ENABLED':'0'}
    (ROOT/'.cache/build').mkdir(parents=True,exist_ok=True)
    for args in [['go','test','./...'],['go','vet','./...'],['python3','scripts/test_deploy_bundle.py'],['python3','k8s/test_render.py']]:subprocess.run(args,cwd=ROOT,env=env,check=True)
    for name in ['authorizer','test-api','mastra-adapter']:
      subprocess.run(['go','build','-trimpath','-ldflags=-s -w','-o',str(ROOT/'.cache/build'/name),'./cmd/'+name],cwd=ROOT,env=env,check=True)
    subprocess.run(['npm','ci','--prefix','agent'],cwd=ROOT,check=True)
    subprocess.run(['node','--test','agent/server.test.mjs'],cwd=ROOT,check=True)
    subprocess.run(['docker','build','-f','deploy/Dockerfile.mastra','-t','localhost:5001/auth-demo/mastra:dedicated','.'],cwd=ROOT,check=True)
    subprocess.run(['docker','push','localhost:5001/auth-demo/mastra:dedicated'],check=True)
    subprocess.run(['python3','scripts/build_substrate.py'],cwd=ROOT,env=env,check=True)
    for name in ['authorizer','test-api','substrate-api','substrate-atenet']:
      image='localhost:5001/auth-demo/'+name+':dedicated'
      subprocess.run(['docker','build','-f','deploy/Dockerfile.services','--build-arg','BINARY='+name,'-t',image,'.'],cwd=ROOT,check=True)
      subprocess.run(['docker','push',image],check=True)
if __name__=='__main__':main()
