#!/bin/bash
# Build the bates_pricer pybind11 extension.
# Output: bates_pricer.so in the pricer directory (importable from notebooks/).

set -e
cd "$(dirname "$0")"

PYTHON=python3
PYBIND_INC=$($PYTHON -c "import pybind11; print(pybind11.get_include())")
PY_INC=$($PYTHON -c "import sysconfig; print(sysconfig.get_path('include'))")
PY_EXT=$($PYTHON -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")

OUT="bates_pricer${PY_EXT}"

EXTRA_LDFLAGS=()
if [[ "$(uname -s)" == "Darwin" ]]; then
    EXTRA_LDFLAGS+=("-undefined" "dynamic_lookup")
else
    PY_LDFLAGS=$($PYTHON -c "import sysconfig; print(sysconfig.get_config_var('LDFLAGS') or '')")
    if [[ -n "${PY_LDFLAGS}" ]]; then
        # shellcheck disable=SC2206
        EXTRA_LDFLAGS+=(${PY_LDFLAGS})
    fi
fi

g++ -O3 -march=native -std=c++20 \
    -shared -fPIC \
    -I"${PYBIND_INC}" \
    -I"${PY_INC}" \
    -I"." \
    bindings.cpp \
    "${EXTRA_LDFLAGS[@]}" \
    -o "${OUT}"

echo "Built: $(pwd)/${OUT}"
