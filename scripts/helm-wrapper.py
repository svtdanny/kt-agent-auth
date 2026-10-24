#!/usr/bin/env python3
"""Add our post-renderer only to the local demo's Substrate installation."""
import os
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]
args=sys.argv[1:]
if args[:3]==['upgrade','--install','substrate']:
    if Path(os.environ.get('KUBECONFIG','')).resolve()!=root/'.cache/kubeconfig':
        raise SystemExit('Gateway overlay is restricted to the demo kubeconfig')
    args+=['--post-renderer',str(root/'scripts/helm-post-renderer.py')]
os.execv(os.environ['AUTH_DEMO_REAL_HELM'],[os.environ['AUTH_DEMO_REAL_HELM'],*args])
