#pragma once

#include <absl/types/optional.h>

#include "chainerx/array.h"
#include "chainerx/scalar.h"

namespace chainerx {

Array Sqrt(const Array& x);

Array Square(const Array& x);

Array Absolute(const Array& x);

Array Fabs(const Array& x);

Array Sign(const Array& x);

Array Maximum(const Array& x1, Scalar x2);
Array Maximum(Scalar x1, const Array& x2);
Array Maximum(const Array& x1, const Array& x2);

Array Minimum(const Array& x1, Scalar x2);
Array Minimum(Scalar x1, const Array& x2);
Array Minimum(const Array& x1, const Array& x2);

 Array Clip(const Array& x1, absl::optional<Scalar> a_min = absl::nullopt, absl::optional<Scalar> a_max = absl::nullopt);

}  // namespace chainerx
