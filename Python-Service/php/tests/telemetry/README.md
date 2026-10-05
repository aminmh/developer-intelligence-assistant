# Phase 0 symbol fixtures

Run from `api/Python-Service/php`:

```sh
php tests/telemetry/check_symbols.php
php tests/telemetry/check_exporter.php
php tests/telemetry/export_fixture.php
```

`symbol_ids.json` is the single shared golden set for PHP and Python. The first
script needs only PHP; `--json` emits the actual normalized results in fixture
order. The exporter scripts additionally need the existing Composer dependencies.
They exercise `CodeChunker` → `CodeChunk::toMetadata` → `CodeParseRenderer`.

The exporter includes actual methods, namespaced and global functions, closures,
arrow functions and an invokable controller. Route and table rows are explicit
framework fact fixtures normalized by the same library: Phase 0 does not inspect
Laravel/Symfony runtime registrations or SQL.

## Input contract

`App\Telemetry\SymbolId::normalize(array): string` accepts:

| `kind` | Required fields |
| --- | --- |
| `method` | `class`, `method` |
| `function` | `name` (namespace-qualified or global) |
| `closure` | `path`, `line` (positive integer, never boolean/string/float) |
| `controller` | `controller` (`Class@method`, `Class::method`, or invokable class) |
| `route` | `method` (one HTTP method), `template` |
| `table` | `connection`, `name` |

Strings are trimmed; PHP identifier case is preserved. Version 1 accepts ASCII
identifier segments `[A-Za-z_][A-Za-z0-9_]*`. Exactly one optional leading namespace
separator is stripped; repeated separators and malformed segments fail. Functions
always emit one leading separator. A controller class must include a namespace
separator, including an explicit leading separator for global classes; bare names
cannot distinguish service IDs from classes and are rejected. Resolve service IDs
upstream before normalizing. Invalid inputs throw `InvalidArgumentException`.

Closure paths use `/`, strip initial `./`, and reject absolute/drive/UNC paths,
empty segments, internal `.` or any `..` segment, colons and control characters.
Same-file/same-line closures have the same format; the Python snapshot boundary
rejects duplicate closure identities instead of choosing one silently.

Routes uppercase the single alphabetic HTTP method; multi-method registrations
must be expanded upstream. Templates start with `/`, preserve optional trailing
slashes, permit `{name}` and `{name?}` parameters, and reject whitespace, query
strings, fragments, backslashes, duplicate slashes and dot path segments. Tables
require an identifier for both connection and unqualified table name; schema
qualification needs an explicit future contract.

`fromChunk(array): ?string` handles `method`, `function`, `closure` and
`arrow_function` metadata only. Other declarations return `null`. `toMetadata`
adds `symbol_id` on success, or `symbol_identity_error` for an invalid supported
symbol without breaking legacy chunk export. Existing `chunk_id` and
`fq_symbol_name` remain retrieval metadata. Anonymous class methods have no
canonical runtime identity, so they remain indexed as AST-only chunks without
either a `symbol_id` or an identity error.

Trait methods retain the declaring trait identity (`App\Traits\Logs::log`). An
effective consuming-class identity is distinct (`App\Service::log`); the normalizer
supports explicit consumer facts but the AST exporter does not resolve trait
aliases/adaptations or invent consumer methods. Runtime trait correlation remains
an explicit mapping task, never a silent identity merge. Anonymous class methods
have no runtime symbol ID because their synthetic class name is not a stable
FQCN; source snapshots retain them under a path-and-line AST storage identity.
