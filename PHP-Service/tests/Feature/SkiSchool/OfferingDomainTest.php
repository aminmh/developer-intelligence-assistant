<?php

namespace Tests\Feature\SkiSchool;

use App\Models\SkiSchool\Age;
use App\Models\SkiSchool\Booking;
use App\Models\SkiSchool\Schedule;
use App\Models\SkiSchool\Session;
use App\Services\SkiSchool\BookingSchedulePlanningService;
use App\Models\SkiSchool\Course;
use App\Models\SkiSchool\Lesson;
use App\Models\SkiSchool\Level;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

class OfferingDomainTest extends TestCase
{
    use RefreshDatabase;

    private BookingSchedulePlanningService $schedulePlanner;

    protected function setUp(): void
    {
        parent::setUp();

        $this->schedulePlanner = new BookingSchedulePlanningService();
    }

    public function test_lesson_and_course_share_duration_rules_and_level_range_support(): void
    {
        $ageChild = Age::query()->create(['name' => 'Child', 'start' => 6, 'end' => 18]);
        $levelBeginner = Level::query()->create(['name' => 'Beginner', 'range_start' => 1, 'range_end' => 30]);
        $levelAdvanced = Level::query()->create(['name' => 'Advanced', 'range_start' => 20, 'range_end' => 40]);

        $lesson = new Lesson([
            'duration_minutes' => 30,
            'age_id' => $ageChild->id,
            'level_id' => $levelBeginner->id,
        ]);

        $course = new Course([
            'duration_minutes' => 60,
            'age_id' => $ageChild->id,
            'level_id' => $levelBeginner->id,
        ]);

        $this->assertSame(30, $lesson->duration_minutes);
        $this->assertSame(60, $course->duration_minutes);
        $this->assertTrue($lesson->isDurationSupported($lesson->duration_minutes));
        $this->assertTrue($course->isDurationSupported($course->duration_minutes));

        $this->assertTrue($lesson->supportsAgeRange(9));
        $this->assertFalse($lesson->supportsAgeRange(5));
        $this->assertTrue($course->supportsAgeRange(14));
        $this->assertFalse($course->supportsAgeRange(19));

        $this->assertSame($ageChild->id, $lesson->age_id);
        $this->assertSame($levelBeginner->id, $lesson->level_id);

        $this->assertTrue($lesson->supportsLevelRange(15));
        $this->assertTrue($lesson->supportsLevelRange($levelBeginner));
        $this->assertFalse($lesson->supportsLevelRange($levelAdvanced));
    }

    public function test_booking_model_is_polymorphic_and_has_booking_table_name(): void
    {
        $booking = new Booking();

        $this->assertSame(Booking::class, $booking->getMorphClass());
        $this->assertSame('bookings', $booking->getTable());
    }

    public function test_course_and_lesson_models_only_store_age_id_and_level_id_relations(): void
    {
        $course = new Course();
        $lesson = new Lesson();

        $this->assertFalse(isset($course->age));
        $this->assertFalse(isset($lesson->age));

        $courseFillable = $course->getFillable();
        $lessonFillable = $lesson->getFillable();

        $this->assertNotContains('age_start', $courseFillable);
        $this->assertNotContains('age_end', $courseFillable);
        $this->assertNotContains('level_start', $courseFillable);
        $this->assertNotContains('level_end', $courseFillable);
        $this->assertNotContains('difficulty', $courseFillable);
        $this->assertContains('age_id', $courseFillable);
        $this->assertContains('level_id', $courseFillable);

        $this->assertNotContains('age_start', $lessonFillable);
        $this->assertNotContains('age_end', $lessonFillable);
        $this->assertNotContains('level_start', $lessonFillable);
        $this->assertNotContains('level_end', $lessonFillable);
        $this->assertNotContains('difficulty', $lessonFillable);
        $this->assertContains('age_id', $lessonFillable);
        $this->assertContains('level_id', $lessonFillable);
    }

    public function test_booking_session_tracks_capacity_and_times(): void
    {
        $course = Course::query()->create(['title' => 'Morning Group', 'duration_minutes' => 30]);
        $startsAt = now()->startOfHour()->addMinutes(1);
        $schedule = $course->schedules()->create([
            'frequency' => Schedule::FREQUENCY_WEEKLY,
            'start_at' => $startsAt,
            'end_at' => $startsAt->copy()->addMonth(),
            'weekday' => $startsAt->dayOfWeek,
            'month_day' => null,
            'age_id' => null,
            'level_id' => null,
        ]);

        $session = $schedule->sessions()->create([
            'start_at' => $startsAt,
            'end_at' => $startsAt->copy()->addMinutes(30),
            'capacity' => 2,
        ]);

        $userOne = User::factory()->create();
        $userTwo = User::factory()->create();
        $userThree = User::factory()->create();

        $session->bookings()->createMany([
            ['user_id' => $userOne->id],
            ['user_id' => $userTwo->id],
        ]);

        $session->refresh();

        $this->assertSame(2, $session->bookedCount());
        $this->assertFalse($session->hasAvailableSlot());

        $this->assertEquals(2, $session->bookings()->count());
        $this->assertEquals(2, Booking::query()->where('session_id', $session->id)->count());
        $this->assertNotNull($session->bookings()->first()->bookable_type);
        $this->assertSame(Course::class, $session->bookings()->first()->bookable_type);
        $this->assertDatabaseHas('booking_sessions', [
            'id' => $session->id,
            'capacity' => 2,
        ]);
        $this->assertFalse($session->hasAvailableSlot());
        $this->assertSame($schedule->id, $session->schedule_id);

        $this->assertInstanceOf(Session::class, $session);
    }

    public function test_schedule_supports_weekly_and_monthly_for_offers_with_constraints(): void
    {
        $lesson = Lesson::query()->create(['title' => 'Evening Lesson', 'duration_minutes' => 30]);
        $age = Age::query()->create(['name' => 'Kids', 'start' => 8, 'end' => 14]);
        $level = Level::query()->create(['name' => 'Advanced', 'range_start' => 20, 'range_end' => 40]);

        $weekly = $lesson->schedules()->create([
            'frequency' => Schedule::FREQUENCY_WEEKLY,
            'start_at' => now(),
            'end_at' => now()->addWeek(),
            'weekday' => 3,
            'month_day' => null,
            'age_id' => $age->id,
            'level_id' => $level->id,
        ]);

        $monthly = $lesson->schedules()->create([
            'frequency' => Schedule::FREQUENCY_MONTHLY,
            'start_at' => now(),
            'end_at' => now()->addMonth(),
            'weekday' => null,
            'month_day' => 15,
            'age_id' => $age->id,
            'level_id' => $level->id,
        ]);

        $this->assertSame(Lesson::class, $weekly->bookable_type);
        $this->assertSame(Lesson::class, $monthly->bookable_type);
        $this->assertSame(3, $weekly->weekday);
        $this->assertSame(15, $monthly->month_day);
        $this->assertSame($age->id, $weekly->age_id);
        $this->assertSame($level->id, $monthly->level_id);
        $this->assertTrue(Schedule::isValidFrequency($weekly->frequency));
        $this->assertTrue(Schedule::isValidFrequency($monthly->frequency));

        $this->assertDatabaseHas('booking_schedules', [
            'id' => $weekly->id,
            'frequency' => Schedule::FREQUENCY_WEEKLY,
        ]);

        $this->assertDatabaseHas('booking_schedules', [
            'id' => $monthly->id,
            'frequency' => Schedule::FREQUENCY_MONTHLY,
        ]);
    }

    public function test_user_can_plan_weekly_schedule_with_odd_weekdays_and_time_window(): void
    {
        $course = Course::query()->create(['title' => 'Planning Course', 'duration_minutes' => 30]);
        $startsAt = now()->startOfWeek()->addDay();
        $endsAt = $startsAt->copy()->addWeeks(2);
        $ageOne = Age::query()->create(['name' => 'Junior', 'start' => 6, 'end' => 12]);
        $ageTwo = Age::query()->create(['name' => 'Teen', 'start' => 13, 'end' => 18]);
        $levelBeginner = Level::query()->create(['name' => 'Beginner', 'range_start' => 1, 'range_end' => 20]);
        $levelIntermediate = Level::query()->create(['name' => 'Intermediate', 'range_start' => 21, 'range_end' => 40]);

        $schedule = $this->schedulePlanner->planWeekly(
            $course,
            $startsAt,
            $endsAt,
            [1, 3, 5],
            11,
            14,
            4,
            30,
            [$ageOne->id, $ageTwo->id],
            [$levelBeginner->id, $levelIntermediate->id],
        );

        $sessions = $schedule->sessions()->orderBy('start_at')->get();
        $this->assertNotEmpty($sessions);
        $this->assertCount(6, $sessions);

        foreach ($sessions as $session) {
            $this->assertEquals(11, $session->start_at->hour);
            $this->assertEquals(0, $session->start_at->minute);
            $this->assertEquals(30, $session->end_at->minute);
            $this->assertTrue($session->end_at->hour <= 14);
            $this->assertContains($session->start_at->dayOfWeek, [1, 3, 5]);
            $this->assertEquals(4, $session->capacity);
            $this->assertEquals($schedule->id, $session->schedule_id);
        }

        $this->assertCount(2, $schedule->ages);
        $this->assertCount(2, $schedule->levels);

        $this->assertEqualsCanonicalizing(
            [$ageOne->id, $ageTwo->id],
            $schedule->ages()->pluck('ages.id')->all()
        );
        $this->assertEqualsCanonicalizing(
            [$levelBeginner->id, $levelIntermediate->id],
            $schedule->levels()->pluck('levels.id')->all()
        );

        $this->assertTrue($schedule->supportsAge(10));
        $this->assertTrue($schedule->supportsAge(16));
        $this->assertFalse($schedule->supportsAge(5));
        $this->assertTrue($schedule->supportsLevel($levelBeginner));
        $this->assertTrue($schedule->supportsLevel($levelIntermediate));
    }

    public function test_user_can_plan_monthly_schedule_with_month_days_and_time_window(): void
    {
        $lesson = Lesson::query()->create(['title' => 'Planning Lesson', 'duration_minutes' => 30]);
        $startsAt = now()->startOfMonth()->addDay();
        $endsAt = $startsAt->copy()->addMonths(2);
        $age = Age::query()->create(['name' => 'All', 'start' => 3, 'end' => 65]);
        $level = Level::query()->create(['name' => 'All', 'range_start' => 1, 'range_end' => 100]);

        $schedule = $this->schedulePlanner->planMonthly(
            $lesson,
            $startsAt,
            $endsAt,
            [1, 15, 29],
            11,
            14,
            3,
            30,
            [$age->id],
            [$level->id],
        );

        $sessions = $schedule->sessions()->orderBy('start_at')->get();
        $this->assertNotEmpty($sessions);

        foreach ($sessions as $session) {
            $this->assertEquals(11, $session->start_at->hour);
            $this->assertEquals(0, $session->start_at->minute);
            $this->assertEquals(30, $session->end_at->minute);
            $this->assertTrue($session->end_at->hour <= 14);
            $this->assertContains($session->start_at->day, [1, 15, 29]);
            $this->assertEquals(3, $session->capacity);
            $this->assertEquals($schedule->id, $session->schedule_id);
        }

        $this->assertEquals([$age->id], $schedule->ages()->pluck('ages.id')->all());
        $this->assertEquals([$level->id], $schedule->levels()->pluck('levels.id')->all());
    }
}
