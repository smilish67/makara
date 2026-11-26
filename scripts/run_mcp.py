#!/usr/bin/env python3
"""Absolute-path launcher independent of the agent's working directory."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from makara.mcp import main
main()
