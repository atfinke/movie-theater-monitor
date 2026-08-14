#!/usr/bin/env bash
set -euo pipefail

if (( $# != 3 )); then
  echo "Usage: $0 FUNCTION_NAME INCREMENTAL_RULE FULL_RESCAN_RULE" >&2
  exit 2
fi

function_name="$1"
incremental_rule="$2"
full_rescan_rule="$3"
aws_profile="${AWS_PROFILE:-sozzled}"
aws_region="${AWS_REGION:-us-west-2}"
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT

aws_cmd=(aws --profile "$aws_profile" --region "$aws_region")
configuration="$(${aws_cmd[@]} lambda get-function-configuration --function-name "$function_name")"
runtime="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["Runtime"])' <<<"$configuration")"
architecture="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["Architectures"][0])' <<<"$configuration")"

if [[ "$architecture" != "arm64" ]]; then
  echo "movie-theater-monitor requires an arm64 Lambda architecture." >&2
  exit 1
fi
platform="manylinux2014_aarch64"
case "$runtime" in
  python3.*) python_version="${runtime#python}" ;;
  *) echo "Unsupported Lambda runtime: $runtime" >&2; exit 1 ;;
esac

function_arn="$(${aws_cmd[@]} lambda get-function --function-name "$function_name" --query 'Configuration.FunctionArn' --output text)"
verify_rule_target() {
  local rule="$1"
  local expected_input="$2"
  local target_input

  target_input="$(${aws_cmd[@]} events list-targets-by-rule --rule "$rule" \
    --query "Targets[?Arn=='$function_arn'].Input" --output text)"
  if [[ "$target_input" != "$expected_input" ]]; then
    echo "Rule $rule must target $function_name with input $expected_input." >&2
    exit 1
  fi
}

verify_rule_target "$incremental_rule" '{}'
verify_rule_target "$full_rescan_rule" '{"full_rescan":true}'

mkdir -p "$build_dir/package"
python3 -m pip install \
  --disable-pip-version-check \
  --target "$build_dir/package" \
  --platform "$platform" \
  --implementation cp \
  --python-version "$python_version" \
  --only-binary=:all: \
  --requirement "$project_root/requirements-lambda.txt"
cp -R "$project_root/src/movie_theater_monitor" "$build_dir/package/"
cp "$project_root/lambda_function.py" "$build_dir/package/"
(cd "$build_dir/package" && zip -qr "$build_dir/function.zip" .)

${aws_cmd[@]} lambda update-function-code --function-name "$function_name" \
  --architectures arm64 --zip-file "fileb://$build_dir/function.zip" >/dev/null
${aws_cmd[@]} lambda wait function-updated --function-name "$function_name"
${aws_cmd[@]} lambda get-function-configuration --function-name "$function_name" --query 'LastUpdateStatus' --output text | grep -qx 'Successful'

verify_rule_target "$incremental_rule" '{}'
verify_rule_target "$full_rescan_rule" '{"full_rescan":true}'

echo "Deployed $function_name and verified its incremental and full-rescan rule targets."
