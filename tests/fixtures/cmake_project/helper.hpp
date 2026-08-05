#pragma once

// Lives in its own translation unit so the caller cannot see the value and fold
// the out-of-bounds access away at compile time.
int allocation_size();
