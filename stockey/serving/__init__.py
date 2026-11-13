"""Serving tables for the research platform (PLATFORM_PRD.md sections 4 and 10, tasks F3/F4/S3).

Built nightly from pipeline tables; the public API reads only these. Each build writes a new
table and swaps it in, so a failed build leaves yesterday's tables in place.
"""
