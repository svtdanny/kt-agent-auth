#!/usr/bin/env python3
"""Build our services and Mastra BYO image; upstream code is a pinned dependency."""
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)

def run(*args,env=None):subprocess.run(args,check=True,env=env)

def main():
    run('go','test','./...')
    run('go','vet','./...')
    run('python3','scripts/test_deploy_bundle.py')
    run('npm','ci','--prefix','agent','--ignore-scripts')
    run('node','--test','agent/server.test.mjs')
    (ROOT/'.cache/build').mkdir(parents=True,exist_ok=True)
    for name in ('control-plane','ext-proc','mastra-adapter','test-api','import-bbolt'):
        run('go','build','-trimpath','-ldflags=-s -w','-o',str(ROOT/'.cache/build'/name),'./cmd/'+name,env={**os.environ,'CGO_ENABLED':'0'})
    for name in ('control-plane','ext-proc','test-api','import-bbolt'):
        run('docker','build','-f','deploy/Dockerfile.services','--build-arg','BINARY='+name,'-t','localhost:5001/auth-demo/'+name+':demo','.')
        run('docker','push','localhost:5001/auth-demo/'+name+':demo')
    run('docker','build','-f','deploy/Dockerfile.mastra','-t','localhost:5001/auth-demo/mastra:demo','.')
    run('docker','push','localhost:5001/auth-demo/mastra:demo')
    run('docker','build','-f','deploy/Dockerfile.pod-runner','-t','localhost:5001/auth-demo/pod-runner:demo','.')
    run('docker','push','localhost:5001/auth-demo/pod-runner:demo')

if __name__=='__main__':main()
