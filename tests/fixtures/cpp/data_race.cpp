// Two threads increment a shared counter with no synchronization.
// ThreadSanitizer reports "WARNING: ThreadSanitizer: data race".

#include <thread>

namespace {

int counter = 0;

void bump() {
    for (int i = 0; i < 100000; ++i) {
        ++counter;  // BUG: unsynchronized read-modify-write from two threads
    }
}

}  // namespace

int main() {
    std::thread first(bump);
    std::thread second(bump);
    first.join();
    second.join();
    return 0;
}
