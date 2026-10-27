#!/usr/bin/env python3
"""Apply the auditable routing-only overlay to the pinned Substrate module."""
from pathlib import Path
import os,shutil,subprocess
ROOT=Path(__file__).resolve().parents[1]
def source():
    info=__import__('json').loads(subprocess.check_output(['go','list','-m','-json','go.ate.dev/substrate'],cwd=ROOT,text=True))
    pinned=info.get('Replace',info)
    if pinned['Version']!='v0.6.0-alpha3':raise RuntimeError('Routing overlay requires Substrate v0.6.0-alpha3')
    return Path(pinned['Dir'])
def main():
    dest=ROOT/'.cache/substrate-source'
    if dest.exists():
        for p in dest.rglob('*'):
            if p.is_dir():p.chmod(0o755)
        dest.chmod(0o755)
        shutil.rmtree(dest)
    shutil.copytree(source(),dest,copy_function=shutil.copyfile)
    dest.chmod(0o755)
    for p in dest.rglob('*'):
        if p.is_dir():p.chmod(0o755)
    subprocess.run(['patch','-p1','-i',str(ROOT/'upstream/substrate.patch')],cwd=dest,check=True)
    shutil.copytree(ROOT/'upstream/agentegress',dest/'internal/agentegress')
    for p in (dest/'internal/agentegress').glob('*.go.in'):p.rename(p.with_suffix(''))
    env={**os.environ,'CGO_ENABLED':'0','GOTOOLCHAIN':subprocess.check_output(['go','env','GOVERSION'],cwd=ROOT,text=True).strip(),'GOCACHE':str(ROOT/'.cache/go-build')}
    subprocess.run(['gofmt','-w','internal/agentegress','cmd/ateapi/main.go','cmd/ateapi/internal/controlapi/workflow_resume.go','cmd/ateapi/internal/authz/authz_util.go'],cwd=dest,check=True)
    subprocess.run(['go','test','./internal/agentegress','./cmd/ateapi/internal/authz'],cwd=dest,env=env,check=True)
    for name,command in [('substrate-api','ateapi'),('substrate-atenet','atenet')]:
      subprocess.run(['go','build','-trimpath','-ldflags=-s -w','-o',str(ROOT/'.cache/build'/name),'./cmd/'+command],cwd=dest,env=env,check=True)
if __name__=='__main__':main()
