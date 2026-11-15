<?php

namespace App\Support {
    function money_format(int $amount): string { return (string) $amount; }
}

namespace App\Traits {
    trait Logs {
        public function log(): void {}
    }
}

namespace App\Http\Controllers {
    class OrderController {
        use \App\Traits\Logs;
        public function store(): void {}
    }
    class ShowDashboard {
        public function __invoke(): void {}
    }
}

namespace {
    $handler = function (): void {};
    $arrow = fn (int $value): int => $value;
    function global_helper(): void {}
}
