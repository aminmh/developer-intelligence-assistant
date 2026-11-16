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
