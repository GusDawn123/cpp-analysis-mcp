// Writes one element past the end of a 4-element heap array.
// AddressSanitizer reports "heap-buffer-overflow".

int main() {
    // Index and store are both volatile: otherwise -O1 drops the dead store and
    // then elides the new/delete pair entirely.
    volatile int index = 4;
    int* values = new int[4]();
    volatile int* slot = values;
    slot[index] = 42;  // BUG: index 4 is out of bounds for a 4-element array
    delete[] values;
    return 0;
}
