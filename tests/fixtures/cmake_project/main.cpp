// Writes one element past the end of a heap array sized by the helper library.
// AddressSanitizer reports "heap-buffer-overflow".

#include "helper.hpp"

int main() {
    const int size = allocation_size();
    int* values = new int[size]();
    // Volatile so -O1 keeps the dead store instead of dropping it and eliding
    // the new/delete pair along with it.
    volatile int* slot = values;
    slot[size] = 42;  // BUG: index size is one past the end of a size-element array
    delete[] values;
    return 0;
}
