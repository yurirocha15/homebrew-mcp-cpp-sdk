#!/usr/bin/env bash
set -euo pipefail

readonly version="1.7.12"
readonly archive="actionlint_${version}_linux_amd64.tar.gz"
readonly digest="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
readonly url="https://github.com/rhysd/actionlint/releases/download/v${version}/${archive}"
readonly work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

curl --fail --location --proto '=https' --retry 3 --show-error --silent --tlsv1.2 \
  --output "${work_dir}/${archive}" "${url}"
printf '%s  %s\n' "${digest}" "${work_dir}/${archive}" | sha256sum --check --status
tar -xzf "${work_dir}/${archive}" -C "${work_dir}" actionlint
"${work_dir}/actionlint" -no-color -ignore 'label ".*" is unknown' .github/workflows/*.yml
