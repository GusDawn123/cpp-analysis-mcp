// One thread takes a-then-b, a later thread takes b-then-a.
// The two threads never overlap, so this program cannot actually hang, but
// ThreadSanitizer's lock graph still reports
// "WARNING: ThreadSanitizer: lock-order-inversion".

#include <mutex>
#include <thread>

namespace {

std::mutex a;
std::mutex b;

void lock_a_then_b() {
    std::scoped_lock outer(a);
    std::scoped_lock inner(b);
}

void lock_b_then_a() {
    std::scoped_lock outer(b);
    std::scoped_lock inner(a);  // BUG: reverses the a-then-b order taken above
}

}  // namespace

int main() {
    // Join before starting the second thread so the inversion stays harmless.
    std::thread first(lock_a_then_b);
    first.join();

    std::thread second(lock_b_then_a);
    second.join();
    return 0;
}
