// Two threads per work item, both joined before the item counts. The heavy loop is the
// critical path; the light one burns visible CPU without mattering end to end. A plain
// profiler ranks both loops; causal profiling shows only the heavy line pays.
//
// The committed golden was captured by hand with 300 items:
//   clang++ -std=c++20 -g -O2 -fno-omit-frame-pointer -pthread critical_path.cpp -ldl
//   coz run --output profile.coz --- ./a.out
#if __has_include(<coz.h>)
#include <coz.h>
#else
#define COZ_PROGRESS_NAMED(name)
#endif
#include <cstdio>
#include <thread>

volatile unsigned long long sink = 0;

void heavy_work() {
    unsigned long long acc = 1;
    for (long i = 0; i < 500000000L; ++i) {
        acc = acc * 6364136223846793005ULL + 1442695040888963407ULL;
    }
    sink = acc;
}

void light_work() {
    unsigned long long acc = 1;
    for (long i = 0; i < 170000000L; ++i) {
        // BUG: hot enough to rank in any profile, yet off the critical path
        acc = acc * 2862933555777941757ULL + 3037000493ULL;
    }
    sink = acc;
}

int main() {
    for (int item = 0; item < 300; ++item) {
        std::thread heavy(heavy_work);
        std::thread light(light_work);
        heavy.join();
        light.join();
        COZ_PROGRESS_NAMED("item");
    }
    std::printf("done\n");
    return 0;
}
