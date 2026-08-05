// Writes a variable annotated guarded_by without holding the mutex that guards it.
// clang's -Wthread-safety reports "requires holding mutex 'counter_mutex' exclusively".
//
// The capability attributes are spelled out rather than written through the usual
// macros so the fixture needs no header. gcc has no equivalent analysis, so it
// compiles this silently -- that difference is the point of keeping it here.

struct __attribute__((capability("mutex"))) Mutex {
    void lock() __attribute__((acquire_capability())) {}
    void unlock() __attribute__((release_capability())) {}
};

namespace {

Mutex counter_mutex;
int counter __attribute__((guarded_by(counter_mutex))) = 0;

}  // namespace

int main() {
    counter = 1;  // BUG: writes a guarded variable with no lock held
    return 0;
}
