// Adds one to INT_MAX, which is undefined behaviour.
// UndefinedBehaviorSanitizer reports
// "runtime error: signed integer overflow". UBSan recovers by default, so the
// process still exits 0 -- detection is by output, not by exit code.

#include <climits>

int main() {
    volatile int x = INT_MAX;
    x = x + 1;  // BUG: signed overflow past INT_MAX
    return 0;
}
