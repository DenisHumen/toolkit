#!/usr/bin/env bash
#
# run.sh — the whole test suite. No framework, no dependencies.
#
# toolkit-hidden: yes
#
#   ./tests/run.sh              static checks + every fast test
#   ./tests/run.sh --full       also the slow ones (containers, live capture)
#   ./tests/run.sh --static     only shellcheck and the Python syntax checks
#   ./tests/run.sh test_harden  only the tests whose name matches
#
# The interactive tests drive the real programs on a pseudo-terminal, because
# raw-mode input and escape sequences are exactly where these tools break.
#
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO="$(dirname "$TESTS_DIR")"
cd "$REPO" || exit 1

FULL=0; STATIC_ONLY=0; FILTER=""
for arg in "$@"; do
    case "$arg" in
        --full)   FULL=1 ;;
        --static) STATIC_ONLY=1 ;;
        -h|--help) grep '^#' "$0" | grep -v '^#!' | grep -v '^# toolkit-' \
                       | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *)        FILTER="$arg" ;;
    esac
done

if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[90m'; Z=$'\033[0m'
else
    B=''; G=''; R=''; Y=''; D=''; Z=''
fi

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { printf '%s\n' "python3 is required to run the tests" >&2; exit 1; }

FAILED=(); PASSED=(); SKIPPED=()

heading() { printf '\n%s%s%s\n' "$B" "$1" "$Z"; }
note()    { printf '%s  %s%s\n' "$D" "$1" "$Z"; }

# --------------------------------------------------------------------------- #
# static checks
# --------------------------------------------------------------------------- #
heading "static checks"

if command -v shellcheck >/dev/null 2>&1; then
    sc_out=""
    while IFS= read -r f; do
        out="$(shellcheck -f gcc "$f" 2>&1)"
        [ -n "$out" ] && sc_out="$sc_out$out"$'\n'
    done < <(find . -name '*.sh' -not -path './.git/*' | sort)
    if [ -n "$sc_out" ]; then
        printf '  %sFAIL%s  shellcheck\n%s\n' "$R" "$Z" "$sc_out"
        FAILED+=("shellcheck")
    else
        printf '  %spass%s  shellcheck (every .sh)\n' "$G" "$Z"
        PASSED+=("shellcheck")
    fi
else
    printf '  %sskip%s  shellcheck %s(not installed)%s\n' "$Y" "$Z" "$D" "$Z"
    SKIPPED+=("shellcheck")
fi

sh_syntax_ok=1
while IFS= read -r f; do
    if ! bash -n "$f" 2>/dev/null; then
        printf '  %sFAIL%s  bash -n %s\n' "$R" "$Z" "$f"
        sh_syntax_ok=0
    fi
done < <(find . -name '*.sh' -not -path './.git/*' | sort)
if [ "$sh_syntax_ok" -eq 1 ]; then
    printf '  %spass%s  bash -n (every .sh)\n' "$G" "$Z"
    PASSED+=("bash -n")
else
    FAILED+=("bash -n")
fi

PY_FILES=()
while IFS= read -r f; do PY_FILES+=("$f"); done \
    < <(find . -name '*.py' -not -path './.git/*' | sort)

if "$PY" -c 'import pyflakes' >/dev/null 2>&1; then
    if pf_out="$("$PY" -m pyflakes "${PY_FILES[@]}" 2>&1)" && [ -z "$pf_out" ]; then
        printf '  %spass%s  pyflakes (every .py)\n' "$G" "$Z"
        PASSED+=("pyflakes")
    else
        printf '  %sFAIL%s  pyflakes\n%s\n' "$R" "$Z" "$pf_out"
        FAILED+=("pyflakes")
    fi
else
    py_syntax_ok=1
    while IFS= read -r f; do
        "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f" \
            >/dev/null 2>&1 || { printf '  %sFAIL%s  syntax %s\n' "$R" "$Z" "$f"; py_syntax_ok=0; }
    done < <(find . -name '*.py' -not -path './.git/*' | sort)
    if [ "$py_syntax_ok" -eq 1 ]; then
        printf '  %spass%s  python syntax %s(pyflakes not installed)%s\n' "$G" "$Z" "$D" "$Z"
        PASSED+=("python syntax")
    else
        FAILED+=("python syntax")
    fi
fi

# A script that is not executable in the index is one `./script.sh` away from a
# "Permission denied" on someone else's clone — chmod on a Windows checkout does
# not set it, so ask git rather than the filesystem.
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    not_exec="$(git ls-files -s -- '*.sh' | awk '$1 != "100755" {print $4}')"
    if [ -z "$not_exec" ]; then
        printf '  %spass%s  every .sh is executable in git\n' "$G" "$Z"
        PASSED+=("exec bits")
    else
        printf '  %sFAIL%s  not executable in git:\n%s\n' "$R" "$Z" "$not_exec"
        printf '  %sfix: git update-index --chmod=+x <file>%s\n' "$D" "$Z"
        FAILED+=("exec bits")
    fi
fi

# Every script must announce itself to the launcher.
missing_meta="$(grep -RL '^# toolkit-name:' --include='*.sh' \
    linux proxmox 2>/dev/null | grep -v '/tests/' || true)"
if [ -z "$missing_meta" ]; then
    printf '  %spass%s  every script carries toolkit metadata\n' "$G" "$Z"
    PASSED+=("metadata")
else
    printf '  %sFAIL%s  missing toolkit metadata:\n%s\n' "$R" "$Z" "$missing_meta"
    FAILED+=("metadata")
fi

if [ "$STATIC_ONLY" -eq 1 ]; then
    heading "summary"
    printf '  %s%d passed%s  %s%d failed%s  %d skipped\n' \
        "$G" "${#PASSED[@]}" "$Z" "$R" "${#FAILED[@]}" "$Z" "${#SKIPPED[@]}"
    [ "${#FAILED[@]}" -eq 0 ] || exit 1
    exit 0
fi

# --------------------------------------------------------------------------- #
# behaviour tests
# --------------------------------------------------------------------------- #
[ "$FULL" -eq 1 ] && export TOOLKIT_TEST_FULL=1
[ "$FULL" -eq 1 ] && note "full mode: containers and live captures are included"

for test_file in "$TESTS_DIR"/test_*.py; do
    name="$(basename "$test_file" .py)"
    if [ -n "$FILTER" ] && [ "${name#*"$FILTER"}" = "$name" ]; then
        continue
    fi
    if "$PY" "$test_file"; then
        PASSED+=("$name")
    else
        FAILED+=("$name")
    fi
done

heading "summary"
for name in "${PASSED[@]}"; do printf '  %s✔%s  %s\n' "$G" "$Z" "$name"; done
for name in "${SKIPPED[@]}"; do printf '  %s—%s  %s\n' "$Y" "$Z" "$name"; done
for name in "${FAILED[@]}"; do printf '  %s✖%s  %s\n' "$R" "$Z" "$name"; done
printf '\n  %s%d passed%s' "$G" "${#PASSED[@]}" "$Z"
[ "${#SKIPPED[@]}" -gt 0 ] && printf '  %s%d skipped%s' "$Y" "${#SKIPPED[@]}" "$Z"
[ "${#FAILED[@]}" -gt 0 ] && printf '  %s%d failed%s' "$R" "${#FAILED[@]}" "$Z"
printf '\n\n'
[ "${#FAILED[@]}" -eq 0 ]
