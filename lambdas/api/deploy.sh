#!/usr/bin/env bash
#
# Deploy the UEBA serving layer: one IAM role, two Lambdas, a Function URL, and
# the S3 trigger that rebuilds the snapshot after each detector run.
#
#   lambdas/api/deploy.sh [--profile snowbit-research] [--region ap-south-1]
#
# Idempotent: re-running updates code and configuration in place and never
# creates a second copy of anything. It does not touch the detector, its role,
# or anything under `baseline/`.
set -euo pipefail

PROFILE="${PROFILE:-snowbit-research}"
REGION="${REGION:-ap-south-1}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The tenant topology comes from the registry rather than being restated here,
# so adding a customer is one edit in one file. `serving/tenants.py` imports no
# boto3 and reads no AWS, which is what makes this safe to call before the IAM
# policy this script is about to write even exists.
TENANTS_JSON="$(cd "$HERE" && python3 -c '
import json
from serving.tenants import all as everyone
print(json.dumps([{"id": t.id, "bucket": t.bucket, "prefix": t.state_prefix}
                  for t in everyone()]))')"
DEFAULT_TENANT="$(cd "$HERE" && python3 -c '
from serving.tenants import default
print(default().id)')"
BUCKETS="$(python3 -c '
import json, sys
seen = []
for t in json.loads(sys.argv[1]):
    if t["bucket"] not in seen:
        seen.append(t["bucket"])
print(" ".join(seen))' "$TENANTS_JSON")"

ROLE="ueba-ui-serving-role-ap2"
API_FN="ueba-ui-api-ap2"
SNAP_FN="ueba-ui-snapshot-ap2"
RUNTIME="python3.11"
ARCH="arm64"
AWS=(aws --profile "$PROFILE" --region "$REGION")
ACCOUNT="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- package ----
# Excludes the venv, the tests, and the two local-only modules that import
# `requests` (absent from the Lambda runtime): backfill.py and
# serving/coralogix.py. Nothing on the request or build path imports either.
say "Packaging"
PKG="$(mktemp -d)/serving.zip"
( cd "$HERE" && zip -qr -X "$PKG" . \
    -x '.venv/*' 'tests/*' '*__pycache__*' '*.pyc' 'backfill.py' \
       'serving/coralogix.py' 'deploy.sh' 'requirements.txt' '.api-key' '.DS_Store' )
echo "package: $(du -h "$PKG" | cut -f1)"

# ------------------------------------------------------------------- role ----
say "IAM role $ROLE"
if ! "${AWS[@]}" iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  "${AWS[@]}" iam create-role --role-name "$ROLE" \
    --description "Read baseline + read/write UI serving state for the UEBA console" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},
                    "Action":"sts:AssumeRole"}]}' >/dev/null
  "${AWS[@]}" iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "created; waiting for propagation"
  sleep 12
else
  echo "exists"
fi

# Least privilege, per tenant: the detector's own state is read-only to this
# role, and writes are confined to the `ui/` prefix the serving layer owns.
# One ListBucket statement per bucket, whose condition names every prefix this
# role may list in it; then a read and a write statement per tenant.
POLICY="$(python3 -c '
import json, sys

tenants = json.loads(sys.argv[1])
statements, by_bucket = [], {}
for t in tenants:
    by_bucket.setdefault(t["bucket"], []).append(t["prefix"])

for i, (bucket, prefixes) in enumerate(by_bucket.items()):
    statements.append({
        "Sid": f"ListBucketScoped{i}",
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": f"arn:aws:s3:::{bucket}",
        "Condition": {"StringLike": {"s3:prefix": [f"{p}/*" for p in prefixes]}},
    })
for t in tenants:
    sid = "".join(c for c in t["id"].title() if c.isalnum())
    bucket, prefix = t["bucket"], t["prefix"]
    statements.append({
        "Sid": f"ReadDetectorState{sid}",
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": f"arn:aws:s3:::{bucket}/{prefix}/baseline/*",
    })
    statements.append({
        "Sid": f"OwnUiPrefix{sid}",
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        "Resource": f"arn:aws:s3:::{bucket}/{prefix}/ui/*",
    })
print(json.dumps({"Version": "2012-10-17", "Statement": statements}))' "$TENANTS_JSON")"

if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "--- IAM policy (dry run) ---"; python3 -m json.tool <<<"$POLICY"
else
  "${AWS[@]}" iam put-role-policy --role-name "$ROLE" --policy-name ueba-ui-state \
    --policy-document "$POLICY"
fi
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"

# --------------------------------------------------------------- API key -----
# A shared secret on the Function URL. The UI already sends `X-Api-Key`; with
# none set, /health reports `api_key_required: false` rather than implying
# protection that is not there.
KEY_FILE="$HERE/.api-key"
if [[ -f "$KEY_FILE" ]]; then
  API_KEY="$(cat "$KEY_FILE")"
else
  # No pipeline here on purpose: `tr -dc ... </dev/urandom | head -c 40` takes
  # SIGPIPE when head closes, which under `set -o pipefail` aborts the deploy.
  API_KEY="$(openssl rand -hex 20)"
  printf '%s' "$API_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi

deploy_fn() {
  local name="$1" handler="$2" memory="$3" timeout="$4" tmp="$5" env="$6"
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "--- $name (dry run) ---"
    echo "  handler=$handler memory=$memory timeout=$timeout tmp=$tmp"
    echo "  env=$env"
    echo "  live env:"
    "${AWS[@]}" lambda get-function-configuration --function-name "$name" \
      --query 'Environment.Variables' --output json 2>/dev/null | sed 's/^/    /' \
      || echo "    (function does not exist yet)"
    return 0
  fi
  if "${AWS[@]}" lambda get-function-configuration --function-name "$name" >/dev/null 2>&1; then
    echo "updating code"
    "${AWS[@]}" lambda update-function-code --function-name "$name" \
      --zip-file "fileb://$PKG" --query 'LastUpdateStatus' --output text
    "${AWS[@]}" lambda wait function-updated --function-name "$name"
    "${AWS[@]}" lambda update-function-configuration --function-name "$name" \
      --handler "$handler" --memory-size "$memory" --timeout "$timeout" \
      --ephemeral-storage "Size=$tmp" --environment "$env" \
      --query 'LastUpdateStatus' --output text
  else
    echo "creating"
    "${AWS[@]}" lambda create-function --function-name "$name" \
      --runtime "$RUNTIME" --architectures "$ARCH" --role "$ROLE_ARN" \
      --handler "$handler" --memory-size "$memory" --timeout "$timeout" \
      --ephemeral-storage "Size=$tmp" --environment "$env" \
      --zip-file "fileb://$PKG" --query 'State' --output text
  fi
  "${AWS[@]}" lambda wait function-updated --function-name "$name"
}

say "Lambda $SNAP_FN (snapshot builder)"
# 6144 MB and 600 s. It parses a baseline of up to 150 MB *and* holds a whole
# 30-day window of findings in memory while normalising them, and the second of
# those is what sets the ceiling: a measured deel build over 21 days peaked at
# 2.34 GB, which left barely 600 MB against the old 3008 MB and would have
# OOM'd once the remaining days landed. Deel carries ~99k findings in 30 days to
# JioStar's ~29k, so sizing for JioStar alone is no longer sizing for the fleet.
#
# 600 s for the same reason: that build took 97 s of wall clock, and the timeout
# has to cover the slowest tenant, not the typical one.
#
# No STATE_BUCKET/STATE_PREFIX/TENANT_ID any more: the tenant is resolved per
# invocation from the S3 event or the payload. `update-function-configuration`
# replaces the whole Variables map, so the old three disappear on this deploy.
deploy_fn "$SNAP_FN" snapshot.handler 6144 600 1024 \
  "Variables={DEFAULT_TENANT_ID=$DEFAULT_TENANT,LOG_LEVEL=INFO}"

say "Lambda $API_FN (request path)"
# Small and quick: every response is a few cached object reads.
# Likewise: the tenant arrives on the request as `?tenant=`, and a request that
# names none is served DEFAULT_TENANT_ID — which is what every caller predating
# the parameter was already getting.
deploy_fn "$API_FN" lambda_handler.handler 512 30 512 \
  "Variables={DEFAULT_TENANT_ID=$DEFAULT_TENANT,LOG_LEVEL=INFO,UI_API_KEY=$API_KEY,ALLOWED_ORIGINS=http://localhost:5173\,http://127.0.0.1:5173}"

# --------------------------------------------------------- function URL ------
#
# AWS_IAM, not NONE. This account blocks unauthenticated Function URLs: with
# `AuthType: NONE` every request — key or no key — comes back as AWS's own
# `403 AccessDeniedException` before the function is ever invoked, and no
# resource policy changes that. Verified: the same URL under AWS_IAM with a
# SigV4-signed request returns 200.
#
# So a browser cannot call this URL directly. It needs a front door that signs
# with SigV4 (CloudFront + OAC), which is also what serving the UI out of a
# fully-private bucket requires. Until that exists, local_server.py is the way
# in and this URL is reachable only by a signed caller.
say "Function URL"
AUTH_TYPE="AWS_IAM"
CORS='AllowOrigins=["http://localhost:5173","http://127.0.0.1:5173"],AllowMethods=["GET"],AllowHeaders=["content-type","x-api-key"],MaxAge=300'
if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "--- function URL (dry run): left untouched ---"
else
  if ! "${AWS[@]}" lambda get-function-url-config --function-name "$API_FN" >/dev/null 2>&1; then
    "${AWS[@]}" lambda create-function-url-config --function-name "$API_FN" \
      --auth-type "$AUTH_TYPE" --cors "$CORS" --query 'FunctionUrl' --output text
  else
    "${AWS[@]}" lambda update-function-url-config --function-name "$API_FN" \
      --auth-type "$AUTH_TYPE" --cors "$CORS" --query 'FunctionUrl' --output text
  fi
  # Left over from an earlier NONE-auth attempt: its condition
  # (`lambda:FunctionUrlAuthType = NONE`) can never match an AWS_IAM URL, so it
  # grants nothing and only misleads anyone reading the policy.
  "${AWS[@]}" lambda remove-permission --function-name "$API_FN" \
    --statement-id FunctionUrlPublicInvoke >/dev/null 2>&1 || true
fi
API_URL="$("${AWS[@]}" lambda get-function-url-config --function-name "$API_FN" --query FunctionUrl --output text 2>/dev/null || echo "(not created yet)")"

# ---------------------------------------------------------- S3 trigger ------
# Fires when the detector re-uploads the baseline, so the snapshot follows every
# run with no schedule to drift out of step. The prefix+suffix pair matches the
# single key `.../baseline/baseline.json` and never `detector_state.json`.
say "S3 triggers on baseline.json"
for BUCKET in $BUCKETS; do
  # One statement id per bucket. A single fixed id meant the second bucket's
  # add-permission failed on the duplicate, the `|| echo` swallowed it, and the
  # bucket ended up notifying a function that would reject the invoke.
  SID="S3InvokeBaseline-$(tr -c 'A-Za-z0-9-' '-' <<<"$BUCKET" | cut -c1-80)"
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "  $BUCKET: would ensure invoke permission $SID"
  elif ! "${AWS[@]}" lambda add-permission --function-name "$SNAP_FN" \
      --statement-id "$SID" --action lambda:InvokeFunction \
      --principal s3.amazonaws.com --source-arn "arn:aws:s3:::$BUCKET" \
      --source-account "$ACCOUNT" >/dev/null 2>/tmp/ueba-addperm.err; then
    # Only an existing identical statement is acceptable; anything else is real.
    if grep -q ResourceConflictException /tmp/ueba-addperm.err; then
      echo "  $BUCKET: invoke permission already present"
    else
      cat /tmp/ueba-addperm.err >&2; exit 1
    fi
  fi

  EXISTING="$("${AWS[@]}" s3api get-bucket-notification-configuration --bucket "$BUCKET")"
  # Merge, do not replace. `put-bucket-notification-configuration` writes the
  # whole document, so anything this script does not own has to be carried
  # through verbatim — and an unconfigured bucket answers `{}`, which the old
  # `[[ -n ]] && ! grep` test read as a foreign configuration and refused on.
  MERGED="$(python3 -c '
import json, sys

live = json.loads(sys.argv[1] or "{}") or {}
bucket, prefix_json, fn_arn = sys.argv[2], sys.argv[3], sys.argv[4]
prefixes = json.loads(prefix_json)

owned = [{
    "Id": f"ueba-ui-snapshot-{tid}",
    "LambdaFunctionArn": fn_arn,
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {"Key": {"FilterRules": [
        {"Name": "prefix", "Value": f"{p}/baseline/"},
        {"Name": "suffix", "Value": "baseline.json"}]}},
} for tid, p in prefixes]
owned_ids = {c["Id"] for c in owned}
# The pre-multi-tenant rule this script used to write, superseded by the above.
owned_ids.add("ueba-ui-snapshot-on-baseline")

foreign = [c for c in live.get("LambdaFunctionConfigurations", [])
           if c.get("Id") not in owned_ids]
out = {k: v for k, v in live.items() if k != "LambdaFunctionConfigurations"}
out.pop("ResponseMetadata", None)
if foreign or owned:
    out["LambdaFunctionConfigurations"] = foreign + owned
print(json.dumps(out))' "$EXISTING" "$BUCKET" \
    "$(python3 -c '
import json, sys
print(json.dumps([[t["id"], t["prefix"]] for t in json.loads(sys.argv[1])
                  if t["bucket"] == sys.argv[2]]))' "$TENANTS_JSON" "$BUCKET")" \
    "arn:aws:lambda:$REGION:$ACCOUNT:function:$SNAP_FN")"

  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "--- notification for $BUCKET (dry run) ---"
    echo "  live:"; python3 -m json.tool <<<"${EXISTING:-{\}}" 2>/dev/null | sed 's/^/    /' || echo "    (none)"
    echo "  new:";  python3 -m json.tool <<<"$MERGED" | sed 's/^/    /'
  else
    "${AWS[@]}" s3api put-bucket-notification-configuration --bucket "$BUCKET" \
      --notification-configuration "$MERGED"
    echo "  $BUCKET: trigger configured"
  fi
done

# ------------------------------------------------------------- lifecycle -----
# The raw finding store grows by ~13 objects a day forever and the builder only
# ever reads the last 30 days. Expire at 45 to leave margin. Scoped to the
# serving layer's own prefix: detector state is untouched.
say "Lifecycle on each tenant's ui/anomalies/"
for BUCKET in $BUCKETS; do
  # `NoSuchLifecycleConfiguration`, not an empty document, is how a bucket with
  # no lifecycle answers — so the error is swallowed and treated as "none".
  LIVE_LC="$("${AWS[@]}" s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" 2>/dev/null || echo '{}')"

  # Same merge discipline as the notification above, and it matters more here:
  # this call replaces the bucket's entire lifecycle document, so the previous
  # single-rule version would have silently deleted any rule somebody else
  # added. Unowned rules are carried through untouched.
  LC="$(python3 -c '
import json, sys

live = json.loads(sys.argv[1] or "{}") or {}
prefixes = json.loads(sys.argv[2])

owned = [{
    "ID": f"ueba-ui-expire-raw-findings-{tid}",
    "Status": "Enabled",
    "Filter": {"Prefix": f"{p}/ui/anomalies/"},
    "Expiration": {"Days": 45},
} for tid, p in prefixes]
owned_ids = {r["ID"] for r in owned}
owned_ids.add("ueba-ui-expire-raw-findings")  # the pre-multi-tenant rule

foreign = [r for r in live.get("Rules", []) if r.get("ID") not in owned_ids]
# Rules only. `get-bucket-lifecycle-configuration` also returns
# TransitionDefaultMinimumObjectSize, but the matching `put` rejects it inside
# the document — it is a separate CLI parameter, passed below. Preserving it
# still matters (rewriting without it silently changes the bucket), so it is
# read out of the live document and handed over on its own flag.
print(json.dumps({"Rules": foreign + owned}))' "$LIVE_LC" \
    "$(python3 -c '
import json, sys
print(json.dumps([[t["id"], t["prefix"]] for t in json.loads(sys.argv[1])
                  if t["bucket"] == sys.argv[2]]))' "$TENANTS_JSON" "$BUCKET")")"

  # Carried across on its own flag, since `put` will not take it in the body.
  TDMOS="$(python3 -c '
import json, sys
live = json.loads(sys.argv[1] or "{}") or {}
print(live.get("TransitionDefaultMinimumObjectSize") or "")' "$LIVE_LC")"

  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "--- lifecycle for $BUCKET (dry run) ---"
    echo "  live:"; python3 -m json.tool <<<"${LIVE_LC:-{\}}" 2>/dev/null | sed 's/^/    /' || echo "    (none)"
    echo "  new:";  python3 -m json.tool <<<"$LC" | sed 's/^/    /'
    [[ -n "$TDMOS" ]] && echo "  transition-default-minimum-object-size: $TDMOS (preserved)"
  else
    if [[ -n "$TDMOS" ]]; then
      "${AWS[@]}" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
        --lifecycle-configuration "$LC" --transition-default-minimum-object-size "$TDMOS"
    else
      "${AWS[@]}" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
        --lifecycle-configuration "$LC"
    fi
    echo "  $BUCKET: lifecycle configured"
  fi
done

say "Done"
cat <<EOF

  API Function URL : $API_URL   (AuthType AWS_IAM — see the note above)
  X-Api-Key        : $API_KEY
                     (saved to lambdas/api/.api-key, mode 600 — keep it out of git)
                     Defence in depth only; the IAM signature is what gates access.

  Reach the UI now:

    lambdas/api/.venv/bin/python lambdas/api/local_server.py 8787
    cd ui && npm run dev

  Smoke-test the deployed API (needs a SigV4-signed request):

    creds=\$(aws configure export-credentials --profile $PROFILE --format process)
    curl -s --aws-sigv4 "aws:amz:$REGION:lambda" \\
      --user "\$(echo \$creds | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["AccessKeyId"]+":"+d["SecretAccessKey"])')" \\
      -H "x-amz-security-token: \$(echo \$creds | python3 -c 'import sys,json;print(json.load(sys.stdin)["SessionToken"])')" \\
      -H "X-Api-Key: \$(cat lambdas/api/.api-key)" ${API_URL%/}/api/health

  Build a snapshot now, rather than waiting for the detector's next run:

    aws lambda invoke --function-name $SNAP_FN --profile $PROFILE \\
      --region $REGION /dev/stdout                       # the default tenant

    aws lambda invoke --function-name $SNAP_FN --profile $PROFILE \\
      --region $REGION --payload '{"tenant":"<id>"}' --cli-binary-format raw-in-base64-out \\
      /dev/stdout                                        # one named tenant

  Tenants this deployment serves:

$(python3 -c '
import json, sys
for t in json.loads(sys.argv[1]):
    print("    %-10s s3://%s/%s" % (t["id"], t["bucket"], t["prefix"]))' "$TENANTS_JSON")

  Re-run with DRY_RUN=1 to print the IAM, notification and lifecycle documents
  it would write, alongside what is live, and change nothing.
EOF
