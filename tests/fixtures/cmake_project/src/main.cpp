// Same data race as tests/fixtures/cpp/data_race.cpp, wrapped in a CMake
// project so the cmake build path has something to compile.

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
