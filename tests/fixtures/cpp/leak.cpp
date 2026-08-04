// Allocates and never frees.
// LeakSanitizer reports "detected memory leaks". LSan is Linux-only, so this
// fixture is skipped on macOS.

int main() {
    int* volatile values = new int[4]();  // BUG: allocated and never deleted
    values[0] = 1;
    // Drop the last pointer through a volatile store so LSan sees the block as
    // unreachable rather than still-referenced.
    values = nullptr;
    return 0;
}
