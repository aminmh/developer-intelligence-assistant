
---

## Configurable Font Selection Prompt

```
You are configuring the font preferences for a Developer Intelligence Assistant.

The application has TWO font domains:
1. **App UI** - Used for the interface, panels, menus, and general text.
2. **Code** - Used for code snippets, diffs, ASTs, JSON, and data structures.

### Font Choices Available:

**App UI:**
- [ ] Inter
- [ ] IBM Plex Sans

**Code:**
- [ ] Fira Code
- [ ] JetBrains Mono

### Task:

Please help configure the fonts for both domains based on the user's requirements.

1. Ask which UI font the user prefers (Inter or IBM Plex Sans).
2. Ask which Code font the user prefers (Fira Code or JetBrains Mono).
3. Once both choices are made, output the final configuration in the following JSON format:

```json
{
  "fonts": {
    "ui": {
      "family": "Inter" | "IBM Plex Sans"
    },
    "code": {
      "family": "Fira Code" | "JetBrains Mono"
    }
  }
}
```

4. Validate the selections against the allowed choices. If an invalid input is provided, remind the user of the available options and ask them to choose again.
5. Optionally provide a recommended pairing and a brief explanation of why that combo suits their described use case.

### Constraints:
- Only accept values from the provided font lists for each domain.
- If the user doesn't specify a preference, default to:
  - UI: **Inter** (lightweight, highly legible on screens)
  - Code: **JetBrains Mono** (excellent for long coding sessions, clear ligatures)
```

---

**How to use this in your assistant's settings panel:**

You can place this prompt in a system-level instruction or pass it as context when the user interacts with a font-configuration modal. The structured output (`JSON`) makes it easy to hook directly into your backend (PHP, Laravel, etc.) to persist the user's font preferences in your settings table.

```php
// Example persistence in Laravel
$user->update([
    'settings->font_ui'  => $request->input('fonts.ui.family'),
    'settings->font_code'=> $request->input('fonts.code.family'),
]);
```

---