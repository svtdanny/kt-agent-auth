#!/usr/bin/env python3
import sys
import yaml
from gateway_overlay import render

yaml.safe_dump_all(render([document for document in yaml.safe_load_all(sys.stdin) if document]),sys.stdout,sort_keys=False)
