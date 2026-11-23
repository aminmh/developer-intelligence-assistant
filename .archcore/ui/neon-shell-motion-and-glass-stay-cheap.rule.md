---
title: "Neon shell motion and glass stay cheap to render"
status: draft
tags:
  - "frontend"
---

## Rule

1. WHEN the UI animates continuously, the animation MUST change only `transform` or `opacity`.
2. WHEN a hover or active state adds a glow, the component MUST fade a pre-rendered shadow layer's opacity instead of animating `box-shadow`.
3. WHEN a surface uses `backdrop-filter`, the surface MUST be part of the frame: sidebar, header, panel, popover, dialog, or toast.
4. WHEN a glass surface is shown, the UI MUST NOT animate the glass surface's position or size continuously.
5. WHEN low performance mode is on, the shell MUST stop looping animations, remove backdrop blur, and drop hover glow shadows.
6. WHEN the OS requests reduced motion, the shell MUST remove transitions and animations.
7. WHEN a shell component needs a colour, the component MUST read a `--shell-*` token instead of a raw hex value.

## Rationale

The user chose a neon, gradient, glass developer theme on 12 November 2026 on the condition that it not overload the CPU. The previous theme animated `border-color` and `box-shadow` forever on every bordered element, which repaints continuously. Transform and opacity animations run on the compositor; backdrop blur is recomputed whenever the content under or inside the glass changes.

Surface: @web/src/index.css, @web/src/App.vue, @web/src/components/BrandLogo.vue. Design reference: @design-system/developer-assistant/shell-preview.html and @design-system/developer-assistant/logo.svg.

## Examples (Good/Bad)

Good:

```css
.app-nav-item::after { box-shadow: 0 0 18px rgb(var(--shell-grad-a-rgb) / .14); opacity: 0; transition: opacity 180ms; }
.app-nav-item:hover::after { opacity: 1; }
```

Bad:

```css
.card { animation: glow 4s infinite; }
@keyframes glow { 50% { box-shadow: 0 0 18px cyan; border-color: cyan; } }
```

## Enforcement

In review, search `@keyframes` in the frontend and reject keyframes that change properties other than `transform` and `opacity`. Check new `backdrop-filter` uses against clause 3. Verify `data-low-performance="true"` leaves no running infinite animation via `document.getAnimations()`.