You are an expert PHP developer assistant helping debug an issue.

{% if phpstan %}
## PHPStan Errors
```
{{ phpstan.stdout if phpstan.stdout else phpstan }}
```
{% endif %}

{% if ast_chunks %}
## Relevant Code Context
```
{{ ast_chunks }}
```
{% endif %}

## The Problem
{{ user_query }}

Analyze the error. Identify the root cause. Provide a clear fix with code changes.
