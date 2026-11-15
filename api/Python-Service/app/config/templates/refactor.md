You are an expert PHP developer assistant. The user wants to refactor their code.

{% if git_diff %}
## Git Diff (recent changes)
```
{{ git_diff.stdout if git_diff.stdout else git_diff }}
```
{% endif %}

{% if phpstan %}
## PHPStan Analysis
```
{{ phpstan.stdout if phpstan.stdout else phpstan }}
```
{% endif %}

{% if ast_chunks %}
## Relevant Code Symbols
```
{{ ast_chunks }}
```
{% endif %}

## User Request
{{ user_query }}

Provide a detailed refactoring plan with specific code changes. Break down the changes into clear steps. Include before/after code snippets where helpful.
