#pragma once

#include <mutex>

#include "xchainer/backend.h"

#define SKIP_UNLESS_DEVICE_AVAILABLE(backend, num)                             \
    do {                                                                       \
        if (xchainer::testing::SkipsUnlessDeviceAvailable((backend), (num))) { \
            return;                                                            \
        }                                                                      \
    } while (0)

namespace xchainer {
namespace testing {

int GetSkippedNativeTestCount();

int GetSkippedCudaTestCount();

int GetDeviceLimit(Backend& backend);

bool SkipsUnlessDeviceAvailable(Backend& backend, int num);

}  // namespace testing
}  // namespace xchainer
