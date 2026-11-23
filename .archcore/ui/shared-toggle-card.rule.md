---
title: "Use the shared toggle card for binary checkbox controls"
status: draft
tags:
  - "frontend"
---

## Rule

1. WHEN a single-value preference or filter uses a binary checkbox, the UI MUST render it with the shared `ToggleCard` component.
2. WHEN the UI renders a `ToggleCard`, it MUST provide a clear label, keyboard operation, an `aria-pressed` state, and visible On/Off text.

## Rationale

The Settings & Connection System Monitor control establishes the shared card-style toggle. Reusing it keeps binary preferences and filters visually consistent while preserving clear state and keyboard access.

## Examples (Good/Bad)

Good:

```vue
<ToggleCard v-model="autoRefresh" label="Auto-refresh" description="Live updates" />
```

Bad:

```vue
<label><input v-model="autoRefresh" type="checkbox" /> Auto-refresh</label>
```

## Enforcement

Search the frontend for native `type="checkbox"` controls during review. Replace single-value preference and filter checkboxes with `ToggleCard`; retain checkbox semantics for multi-select form inputs.