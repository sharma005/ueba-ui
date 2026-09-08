#!/usr/bin/env bash
#
# Remove everything cloudfront-deploy.sh created, and nothing else.
#
#   deploy/cloudfront-teardown.sh [--profile snowbit-research] [--yes]
#
# Never touches the API/snapshot Lambdas themselves, the data bucket
# (azuread-anomaly-iforest-state-*), or its S3 notification config.
# Safe to re-run after a partial failure — every step tolerates a missing resource.
set -euo pipefail

PROFILE="${PROFILE:-snowbit-research}"
REGION="${REGION:-ap-south-1}"
ASSUME_YES="no"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    --yes)     ASSUME_YES="yes"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

AWS=(aws --profile "$PROFILE" --region "$REGION")
CF=(aws --profile "$PROFILE" --region us-east-1)
ACCOUNT="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"

BUCKET="ueba-ui-demo-${ACCOUNT}-ap2"
API_FN="ueba-ui-api-ap2"
FUNC_NAME="ueba-demo-basic-auth"
OAC_S3="ueba-ui-demo-s3"
OAC_LAMBDA="ueba-ui-demo-lambda"
CACHE_POLICY="ueba-demo-api"
COMMENT="ueba-ui-demo"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

DIST_ID="$("${CF[@]}" cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='$COMMENT'].Id | [0]" --output text 2>/dev/null || echo None)"

cat <<EOF
About to delete, in account $ACCOUNT:
  distribution   ${DIST_ID}
  bucket         s3://$BUCKET  (and everything in it)
  function       $FUNC_NAME
  OACs           $OAC_S3, $OAC_LAMBDA
  cache policy   $CACHE_POLICY
  lambda perms   AllowCloudFrontInvokeUrl + AllowCloudFrontInvokeFunction on $API_FN
EOF
if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "type 'delete' to continue: " reply
  [[ "$reply" == "delete" ]] || { echo "aborted"; exit 1; }
fi

# ------------------------------------------------------- distribution ------
# A distribution cannot be deleted while enabled: disable, wait for the change to
# reach every edge, then delete with the ETag the disable returned.
if [[ "$DIST_ID" != "None" && -n "$DIST_ID" ]]; then
  say "disabling distribution $DIST_ID"
  ETAG="$("${CF[@]}" cloudfront get-distribution-config --id "$DIST_ID" --query ETag --output text)"
  "${CF[@]}" cloudfront get-distribution-config --id "$DIST_ID" \
    --query DistributionConfig --output json \
    | python3 -c 'import json,sys; c=json.load(sys.stdin); c["Enabled"]=False; print(json.dumps(c))' \
    > /tmp/ueba-dist-disabled.json
  "${CF[@]}" cloudfront update-distribution --id "$DIST_ID" \
    --distribution-config "file:///tmp/ueba-dist-disabled.json" --if-match "$ETAG" >/dev/null
  rm -f /tmp/ueba-dist-disabled.json

  say "waiting for it to finish deploying (5-15 min)"
  "${CF[@]}" cloudfront wait distribution-deployed --id "$DIST_ID"

  say "deleting distribution"
  ETAG="$("${CF[@]}" cloudfront get-distribution-config --id "$DIST_ID" --query ETag --output text)"
  "${CF[@]}" cloudfront delete-distribution --id "$DIST_ID" --if-match "$ETAG"
else
  say "no distribution with comment '$COMMENT' — skipping"
fi

# ----------------------------------------------------------- function ------
say "function"
if "${CF[@]}" cloudfront describe-function --name "$FUNC_NAME" >/dev/null 2>&1; then
  ETAG="$("${CF[@]}" cloudfront describe-function --name "$FUNC_NAME" --query ETag --output text)"
  "${CF[@]}" cloudfront delete-function --name "$FUNC_NAME" --if-match "$ETAG" && echo "deleted"
else
  echo "already gone"
fi

# --------------------------------------------------------------- OACs ------
say "origin access controls"
for name in "$OAC_S3" "$OAC_LAMBDA"; do
  id="$("${CF[@]}" cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$name'].Id | [0]" --output text 2>/dev/null || echo None)"
  if [[ "$id" != "None" && -n "$id" ]]; then
    etag="$("${CF[@]}" cloudfront get-origin-access-control --id "$id" --query ETag --output text)"
    "${CF[@]}" cloudfront delete-origin-access-control --id "$id" --if-match "$etag" \
      && echo "$name deleted"
  else
    echo "$name already gone"
  fi
done

# -------------------------------------------------------- cache policy -----
say "cache policy"
POLICY_ID="$("${CF[@]}" cloudfront list-cache-policies --type custom \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='$CACHE_POLICY'].CachePolicy.Id | [0]" \
  --output text 2>/dev/null || echo None)"
if [[ "$POLICY_ID" != "None" && -n "$POLICY_ID" ]]; then
  ETAG="$("${CF[@]}" cloudfront get-cache-policy --id "$POLICY_ID" --query ETag --output text)"
  "${CF[@]}" cloudfront delete-cache-policy --id "$POLICY_ID" --if-match "$ETAG" && echo "deleted"
else
  echo "already gone"
fi

# ------------------------------------------------------------- bucket ------
say "bucket"
if "${AWS[@]}" s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  "${AWS[@]}" s3 rm "s3://$BUCKET" --recursive --only-show-errors
  "${AWS[@]}" s3api delete-bucket --bucket "$BUCKET" && echo "deleted"
else
  echo "already gone"
fi

# --------------------------------------------------- lambda permission -----
say "lambda permission"
for sid in AllowCloudFrontInvokeUrl AllowCloudFrontInvokeFunction; do
  "${AWS[@]}" lambda remove-permission --function-name "$API_FN" \
    --statement-id "$sid" 2>/dev/null && echo "$sid removed" || echo "$sid already gone"
done

say "done — the API Lambda, snapshot Lambda and data bucket were not touched"
