#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname -- "${SCRIPT_DIR}")"
# shellcheck source=../egpu-pci-lib.sh
source "${REPO_DIR}/egpu-pci-lib.sh"

failures=0
required_functions=(
    find_hp_dock_router_dir
    hp_dock_router_present
    hp_pci_subtree_present
    is_pci_descendant_of
    resolve_egpu_topology
)

for function in "${required_functions[@]}"; do
    if declare -F "${function}" >/dev/null; then
        printf 'PASS  shared PCI helper exports %s\n' "${function}"
    else
        printf 'FAIL  shared PCI helper does not export %s\n' "${function}" >&2
        failures=$((failures + 1))
    fi
done

printf '\nPCI library API tests complete: %d failure(s).\n' "${failures}"
(( failures == 0 ))
