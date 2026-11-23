"""Regression check between a base and a candidate revision.

Contract: @.archcore/regression/regression-check.spec.md. The core reads only normalized records
(symbol graph rows, route records, test records, contract versions, HTTP responses) and never
branches on a language or framework name
(@.archcore/regression/regression-evidence-comes-from-framework-capabilities.adr.md).
"""
