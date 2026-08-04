// Control fixture: the same shared counter as data_race.cpp, correctly guarded.
// Must produce zero findings under thread, address, and undefined sanitizers.

#include <mutex>
#include <thread>

namespace {

int counter = 0;
std::mutex counter_mutex;

void bump() {
    for (int i = 0; i < 100000; ++i) {
        std::lock_guard<std::mutex> guard(counter_mutex);
        ++counter;
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
