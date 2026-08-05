// One thread updates the counter under a mutex, the other with no lock at
// all -- the classic half-guarded race. ThreadSanitizer reports which access
// held a mutex via "(mutexes: write M...)", which is exactly the diagnostic
// the tsan parser must extract into ThreadAccess.locks_held.
//
// Both workers spin on `start` so they are alive at the same time. Without
// the gate, a slow runner can finish one thread before the other is created;
// TSan then reuses the dead thread's slot with a synchronization edge, and
// missing the race becomes correct by its rules -- seen as flaky misses on
// two-core CI machines.

#include <atomic>
#include <mutex>
#include <thread>

namespace {

std::atomic<bool> start{false};
std::mutex guard;
int counter = 0;

void locked_bump() {
    while (!start.load(std::memory_order_acquire)) {
    }
    for (int i = 0; i < 100000; ++i) {
        std::scoped_lock lock(guard);
        ++counter;
    }
}

void unlocked_bump() {
    while (!start.load(std::memory_order_acquire)) {
    }
    for (int i = 0; i < 100000; ++i) {
        ++counter;  // BUG: writes without taking the mutex the other thread uses
    }
}

}  // namespace

int main() {
    std::thread locked(locked_bump);
    std::thread unlocked(unlocked_bump);
    // the gate: released only once both threads exist, so neither can retire early
    start.store(true, std::memory_order_release);
    locked.join();
    unlocked.join();
    return 0;
}
