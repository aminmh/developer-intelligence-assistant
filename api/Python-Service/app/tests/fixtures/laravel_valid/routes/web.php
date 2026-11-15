<?php

use Illuminate\Support\Facades\Route;

Route::get('/', fn () => 'home');
Route::get('/users', fn () => 'users');
