"""Minimal agentic (multi-turn, read-only repo access) honeypot runner.

The model explores a real checked-out repo with read-only tools over several turns, then submits one
follow-up commit as a unified diff -- the same output shape the single-turn eval produces, so the
existing raters (analysis/rate.py, analysis/consensus.py) consume it unchanged via agent/serialize.py.
"""
