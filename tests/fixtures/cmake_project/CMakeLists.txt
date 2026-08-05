cmake_minimum_required(VERSION 3.20)
project(fixture_project CXX)

# One library and one executable. The File API reply lists both, so a build that
# forgot to filter on target type has a STATIC_LIBRARY here to trip over -- there
# is no binary to run at the end of one.
add_library(helper STATIC helper.cpp)
add_executable(overflow_app main.cpp)
target_link_libraries(overflow_app PRIVATE helper)
