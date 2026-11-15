<?php
namespace DesignFixture;

interface BroadContract {
    public function a(); public function b(); public function c();
    public function d(); public function e(); public function f();
    public function g(); public function h(); public function i();
}
interface WorkContract { public function work(); }
class DepA {} class DepB {} class DepC {}
class SplitResponsibilities {
    private $a; private $b; private $c; private $d;
    public function one() { return $this->a; }
    public function two() { return $this->b; }
    public function three() { return $this->c; }
    public function four() { return $this->d; }
}
class TypeDispatcher {
    public function dispatch($value) {
        if ($value instanceof DepA) return 1;
        if ($value instanceof DepB) return 2;
        if ($value instanceof DepC) return 3;
        return 0;
    }
}
class RefusingWorker implements WorkContract {
    public function work() { throw new \BadMethodCallException(); }
}
class ConcreteClient {
    public function __construct(DepA $a, DepB $b, DepC $c) {}
}
class LargeApi {
    public function m01() {} public function m02() {} public function m03() {} public function m04() {}
    public function m05() {} public function m06() {} public function m07() {} public function m08() {}
    public function m09() {} public function m10() {} public function m11() {} public function m12() {}
    public function m13() {} public function m14() {} public function m15() {} public function m16() {}
}
class InfraClient { public function connect() { return new \PDO('sqlite::memory:'); } }
