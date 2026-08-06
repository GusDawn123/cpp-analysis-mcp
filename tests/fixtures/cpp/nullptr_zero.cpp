// A null pointer written the pre-C++11 way. clang-tidy's modernize-use-nullptr check
// exists precisely for this line; the fixture proves the check still fires and the
// parser still reads what it prints.
int main() {
    int* p = 0;  // BUG: a null pointer spelled 0 rather than nullptr
    return p == nullptr ? 0 : 1;
}
