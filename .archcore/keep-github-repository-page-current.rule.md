---
title: "Keep the GitHub repository page current after development"
status: accepted
---

## Rule

1. WHEN completing development, the developer MUST review @README.md against the resulting application behavior.
2. WHEN completing development, the developer MUST update @README.md to describe the resulting change.
3. WHEN publishing development to GitHub, the developer MUST include the corresponding @README.md updates in the published revision.

## Rationale

The GitHub repository page introduces the application and provides its installation and usage instructions. Outdated package names or setup steps can prevent users from installing the application.

## Examples

### Good

A new Laravel package changes installation: the developer updates its Composer command in @README.md and publishes it with the feature.

### Bad

A development change replaces a Composer package, but the GitHub repository page continues to show the old package name.

## Enforcement

Manual review of the development change and @README.md before publication.