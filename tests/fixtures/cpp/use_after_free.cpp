// Reads a heap array after it has been deleted.
// AddressSanitizer reports "heap-use-after-free".

int main() {
    int* values = new int[4]();
    delete[] values;
    // Sink is volatile so -O1 keeps the load.
    volatile int sink = values[1];  // BUG: reads memory that was already freed
    (void)sink;
    return 0;
}
