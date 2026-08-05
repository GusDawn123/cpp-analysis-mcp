// One thread updates the counter under a mutex, the other with no lock at
// all -- the classic half-guarded race. ThreadSanitizer reports which access
// held a mutex via "(mutexes: write M...)", which is exactly the diagnostic
// the tsan parser must extract into ThreadAccess.locks_held.

#include <mutex>
#include <thread>

namespace {

std::mutex guard;
int counter = 0;

void locked_bump() {
    for (int i = 0; i < 100000; ++i) {
        std::scoped_lock lock(guard);
        ++counter;
    }
}

void unlocked_bump() {
    for (int i = 0; i < 100000; ++i) {
        ++counter;  // BUG: writes without taking the mutex the other thread uses
    }
}

}  // namespace

int main() {
    std::thread locked(locked_bump);
    std::thread unlocked(unlocked_bump);
    locked.join();
    unlocked.join();
    return 0;
}
