# PHP runtime images for the sandbox daemon (tier-1 installs + tier-2 probes).
#
# composer:2 tracks the newest PHP (8.5+), which real-world projects requiring
# ^8.2..^8.4 (+ bcmath/gd/intl/zip — armaiti case) cannot boot on. These images
# pair a pinned php-cli with the official composer binary + the extensions the
# probes need, matching composer.json require.php floors:
#
#   docker build --build-arg PHP_VERSION=8.3 -t php-composer:8.3 .
#
# Load into the nested-DinD sandbox daemon (mac Docker Desktop save/load via
# the runner container — a plain save|load pipe truncates big images):
#
#   docker save php-composer:8.3 -o data/php-composer-8.3.tar
#   docker cp data/php-composer-8.3.tar dev-assistant-sandbox-sandbox-runner-1:/tmp/
#   docker exec dev-assistant-sandbox-sandbox-runner-1 docker load -i /tmp/php-composer-8.3.tar
#
# The map lives in app/config/sandbox_runtimes.yml under runtimes.php.php_versions.
ARG PHP_VERSION=8.3
FROM composer:2 AS composer-src
FROM php:${PHP_VERSION}-cli

COPY --from=composer-src /usr/bin/composer /usr/local/bin/composer
ENV COMPOSER_ALLOW_SUPERUSER=1 \
    COMPOSER_NO_INTERACTION=1

# build deps for gd (freetype+jpeg), intl (icu), zip; git/unzip for composer
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git unzip \
      libicu-dev \
      libpng-dev \
      libjpeg62-turbo-dev \
      libfreetype6-dev \
      libzip-dev \
 && docker-php-ext-configure gd --with-freetype --with-jpeg \
 && docker-php-ext-install -j"$(nproc)" bcmath gd intl zip opcache \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
