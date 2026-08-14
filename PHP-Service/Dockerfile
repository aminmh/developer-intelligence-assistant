FROM php:8.4-fpm

RUN apt-get update && apt-get install -y \
    git \
    tree \
    unzip \
    libsqlite3-dev \
    libzip-dev \
    libonig-dev \
    && rm -rf /var/lib/apt/lists/*

RUN docker-php-ext-install \
    pdo_sqlite \
    bcmath \
    mbstring \
    zip

# Default 128M is too small for extracting/forwarding a large RAG document's
# text (a plain-text upload's extracted text is roughly the same size as the
# original file, plus overhead from JSON-encoding it for the Node callback).
RUN echo "memory_limit = 512M" > /usr/local/etc/php/conf.d/memory-limit.ini

COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

WORKDIR /app

COPY . .

RUN composer install --no-dev --no-interaction --no-progress --optimize-autoloader

RUN mkdir -p storage/framework/cache storage/framework/sessions storage/framework/views storage/logs

RUN php artisan l5-swagger:generate

# No config:cache/route:cache here -- config:cache bakes env() values into a
# static file at BUILD time, which would silently ignore secrets docker-compose
# injects at container RUNTIME (UPLOAD_TOKEN_SECRET, RAG_CALLBACK_SECRET, etc).
RUN php artisan view:cache

EXPOSE 9003

# --no-reload is required, not just an optimization: Laravel's ServeCommand
# filters the spawned server process's environment down to a small hardcoded
# whitelist (APP_ENV, PATH, XDEBUG_*, ...) whenever a .env file exists, which
# it always does here since the whole directory is bind-mounted for dev reload
# -- silently dropping every docker-compose environment: var (UPLOAD_TOKEN_SECRET,
# RAG_CALLBACK_SECRET, NODE_RAG_CALLBACK_URL, AWS_*, etc). --no-reload disables
# that filtering entirely.
CMD ["php", "artisan", "serve", "--host=0.0.0.0", "--port=9003", "--no-reload"]
