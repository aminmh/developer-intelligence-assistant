FROM php:8.4-fpm

RUN apt-get update && apt-get install -y \
    git \
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

COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

WORKDIR /app

COPY . .

RUN composer install --no-dev --no-interaction --no-progress --optimize-autoloader

RUN mkdir -p storage/framework/cache storage/framework/sessions storage/framework/views storage/logs

RUN php artisan l5-swagger:generate

RUN php artisan config:cache && \
    php artisan route:cache && \
    php artisan view:cache

EXPOSE 9003

CMD ["php-fpm"]
