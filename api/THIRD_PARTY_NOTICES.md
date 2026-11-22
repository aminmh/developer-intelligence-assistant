# Third-party notices

Components bundled into the Developer Assistant images that are distributed to others.
Decision: `.archcore/code-quality/bundled-security-rules-are-redistributable.adr.md`.
Regenerate the Psalm table from `Python-Service/php/psalm/composer.lock` whenever it changes.

## Psalm taint analysis (analyzer worker and API image, `/opt/psalm`)

Installed unmodified from Packagist at the versions below. Each package's full licence text ships in
`/opt/psalm/vendor/<package>/` inside the image.

| Package | Version | Licence | Source |
|---|---|---|---|
| amphp/amp | v3.1.3 | MIT | https://github.com/amphp/amp.git |
| amphp/byte-stream | v2.1.3 | MIT | https://github.com/amphp/byte-stream.git |
| amphp/cache | v2.0.1 | MIT | https://github.com/amphp/cache.git |
| amphp/dns | v2.4.2 | MIT | https://github.com/amphp/dns.git |
| amphp/parallel | v2.4.0 | MIT | https://github.com/amphp/parallel.git |
| amphp/parser | v1.1.1 | MIT | https://github.com/amphp/parser.git |
| amphp/pipeline | v1.2.7 | MIT | https://github.com/amphp/pipeline.git |
| amphp/process | v2.1.2 | MIT | https://github.com/amphp/process.git |
| amphp/serialization | v1.1.0 | MIT | https://github.com/amphp/serialization.git |
| amphp/socket | v2.4.2 | MIT | https://github.com/amphp/socket.git |
| amphp/sync | v2.3.1 | MIT | https://github.com/amphp/sync.git |
| composer/pcre | 3.4.0 | MIT | https://github.com/composer/pcre.git |
| composer/semver | 3.6.0 | MIT | https://github.com/composer/semver.git |
| composer/xdebug-handler | 3.0.5 | MIT | https://github.com/composer/xdebug-handler.git |
| danog/advanced-json-rpc | v3.2.3 | ISC | https://github.com/danog/php-advanced-json-rpc.git |
| daverandom/libdns | v2.1.0 | MIT | https://github.com/DaveRandom/LibDNS.git |
| dnoegel/php-xdg-base-dir | v0.1.1 | MIT | https://github.com/dnoegel/php-xdg-base-dir.git |
| doctrine/deprecations | 1.2.0 | MIT | https://github.com/doctrine/deprecations.git |
| felixfbecker/language-server-protocol | v1.5.3 | ISC | https://github.com/felixfbecker/php-language-server-protocol.git |
| fidry/cpu-core-counter | 1.4.1 | MIT | https://github.com/theofidry/cpu-core-counter.git |
| kelunik/certificate | v1.1.3 | MIT | https://github.com/kelunik/certificate.git |
| league/uri | 7.10.0 | MIT | https://github.com/thephpleague/uri.git |
| league/uri-interfaces | 7.10.0 | MIT | https://github.com/thephpleague/uri-interfaces.git |
| netresearch/jsonmapper | v5.0.1 | OSL-3.0 | https://github.com/cweiske/jsonmapper.git |
| nikic/php-parser | v5.9.0 | BSD-3-Clause | https://github.com/nikic/PHP-Parser.git |
| phpdocumentor/reflection-common | 2.2.1 | MIT | https://github.com/phpDocumentor/ReflectionCommon.git |
| phpdocumentor/reflection-docblock | 6.0.3 | MIT | https://github.com/phpDocumentor/ReflectionDocBlock.git |
| phpdocumentor/type-resolver | 2.1.0 | MIT | https://github.com/phpDocumentor/TypeResolver.git |
| phpstan/phpdoc-parser | 2.4.1 | MIT | https://github.com/phpstan/phpdoc-parser.git |
| psr/container | 2.0.2 | MIT | https://github.com/php-fig/container.git |
| psr/http-factory | 1.1.0 | MIT | https://github.com/php-fig/http-factory.git |
| psr/http-message | 2.0 | MIT | https://github.com/php-fig/http-message.git |
| psr/log | 3.0.2 | MIT | https://github.com/php-fig/log.git |
| revolt/event-loop | v1.0.9 | MIT | https://github.com/revoltphp/event-loop.git |
| sebastian/diff | 9.0.1 | BSD-3-Clause | https://github.com/sebastianbergmann/diff.git |
| spatie/array-to-xml | 3.4.4 | MIT | https://github.com/spatie/array-to-xml.git |
| symfony/console | v8.1.11 | MIT | https://github.com/symfony/console.git |
| symfony/deprecation-contracts | v3.8.1 | MIT | https://github.com/symfony/deprecation-contracts.git |
| symfony/filesystem | v8.1.11 | MIT | https://github.com/symfony/filesystem.git |
| symfony/polyfill-ctype | v1.37.0 | MIT | https://github.com/symfony/polyfill-ctype.git |
| symfony/polyfill-intl-grapheme | v1.43.3 | MIT | https://github.com/symfony/polyfill-intl-grapheme.git |
| symfony/polyfill-intl-normalizer | v1.43.0 | MIT | https://github.com/symfony/polyfill-intl-normalizer.git |
| symfony/polyfill-mbstring | v1.43.3 | MIT | https://github.com/symfony/polyfill-mbstring.git |
| symfony/polyfill-php84 | v1.43.0 | MIT | https://github.com/symfony/polyfill-php84.git |
| symfony/polyfill-php85 | v1.44.0 | MIT | https://github.com/symfony/polyfill-php85.git |
| symfony/service-contracts | v3.8.1 | MIT | https://github.com/symfony/service-contracts.git |
| symfony/string | v8.1.10 | MIT | https://github.com/symfony/string.git |
| vimeo/psalm | 6.20.5 | MIT | https://github.com/vimeo/psalm.git |
| webmozart/assert | 2.5.1 | MIT | https://github.com/webmozarts/assert.git |

`netresearch/jsonmapper` is licensed under the Open Software License 3.0 (OSL-3.0). It is shipped
unmodified; its source is available at the repository listed above, at the listed version. Any
modification must be published under OSL-3.0.

## In-house taint models and rules

- `Python-Service/app/agent/plugins/frameworks/*/psalm/taint.phpstub`: written for Developer Assistant from
  the public Laravel, Symfony and Doctrine APIs; no third-party stub content is copied.
- `Python-Service/app/code_quality/rules/builtin.yml`: written for Developer Assistant.

## Betterleaks history secret scan (analyzer worker and API image, `/usr/local/bin/betterleaks`)

Betterleaks 1.9.0 (MIT, Copyright (c) 2026 Zachary Rice), the official release binary from
https://github.com/betterleaks/betterleaks/releases/tag/v1.9.0, verified by SHA-256 and shipped
unmodified. Its licence text ships at `/usr/share/licenses/betterleaks/LICENSE`. Its detection rules are
the ones built into that release.

The binary statically links the Go modules listed in its `go.mod` at tag `v1.9.0`:

- MIT: Masterminds/semver, andybalholm/brotli, betterleaks/go-re2, charlievieth/fastwalk, dlclark/regexp2,
  expr-lang/expr, gitleaks/go-gitdiff, go-sprout/sprout, h2non/filetype, klauspost/pgzip, lucasjones/reggen,
  mattn/go-colorable, mattn/go-isatty, mholt/archives, mitchellh/copystructure, mitchellh/reflectwalk,
  pelletier/go-toml, pkoukk/tiktoken-go, rrethy/ahocorasick, rs/zerolog, shurcooL/githubv4,
  shurcooL/graphql, spf13/cast, stretchr/testify, wasilibs/wazero-helpers
- BSD-2-Clause / BSD-3-Clause / ISC / 0BSD: STARRY-S/zip, bodgit/plumbing, bodgit/sevenzip, bodgit/windows,
  dario.cat/mergo, davecgh/go-spew, dsnet/compress, fatih/semgroup, google/go-cmp, google/go-github,
  google/go-querystring, google/uuid, klauspost/compress (with Apache-2.0 and MIT parts),
  mikelolasagasti/xz, nwaples/rardecode, pierrec/lz4, pmezard/go-difflib, rogpeppe/go-internal,
  spf13/pflag, ulikunitz/xz, golang.org/x/{crypto,exp,sync,sys,text}, gopkg.in/check.v1
- Apache-2.0: go4.org, inconshreveable/mousetrap, minio/minlz, sorairolake/lzip-go, spf13/afero,
  spf13/cobra, tetratelabs/wazero; go.yaml.in/yaml/v3 and gopkg.in/yaml.v3 (MIT and Apache-2.0)
- MPL-2.0: hashicorp/go-version 1.7.0 and hashicorp/golang-lru/v2 2.0.7, unmodified; their source is at
  https://github.com/hashicorp/go-version and https://github.com/hashicorp/golang-lru at those versions.

Regenerate this list from the release's `go.mod` whenever `BETTERLEAKS_VERSION` changes.
