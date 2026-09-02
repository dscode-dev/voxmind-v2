"""Offline evaluation harness for ClipFlow's cut intelligence.

Not shipped in the worker image: this is a development and CI tool. It exercises the real
editorial code with the IO boundaries stubbed, so cut-quality changes can be measured
instead of eyeballed.

    python -m evaluation --out evaluation_before.json
    python -m evaluation --compare evaluation_before.json evaluation_after.json
"""
