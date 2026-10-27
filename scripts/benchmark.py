#!/usr/bin/env python3
"""Run the dedicated-path E2E suite with bounded HTTP load."""
import os
os.environ['AUTH_BENCHMARK']='1'
from test_e2e import main
if __name__=='__main__':main()
